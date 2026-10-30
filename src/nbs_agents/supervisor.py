import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from bluesky_queueserver_api.zmq import REManagerAPI

from .agentlog import AgentLog
from .agents.base import AgentRegistry, agent_name_from_run
from .agents.external import ExternalAgent
from .agents.repeat import RepeatAgent
from .catalog import ZMQCatalog, ZMQRun
from .constants import (
    AGENT_PARAMETERS_KEY,
    AGENT_SEQUENCE_KEY,
    AGENT_UID_KEY,
    CLOSE_AGENT_PLAN,
    OPEN_AGENT_PLAN,
)
from .queue import ensure_queue_stop_before_close, update_close_agent_uid

LOGGER = logging.getLogger(__name__)

# Packages add agents under this entry-point group: name = "module:AgentClass"
AGENTS_ENTRY_POINT = "nbs_agents.agents"


class AgentSupervisor:
    """
    Persistent supervisor for in-process queue agents.

    Parameters
    ----------
    manager : REManagerAPI
        QueueServer API client.
    catalog : ZMQCatalog
        Bluesky document catalog.
    registry : AgentRegistry, optional
        Agent registry. If omitted, a default registry with built-in agents is used.
    agent_log : AgentLog, optional
        Shared agent-log store that agents write their records to.
    observers : list, optional
        Plugins, such as the beamline assistant or the Claude launcher. Each
        may define any of ``on_run_stop(run)`` (every finished run),
        ``on_agent_opened(agent)`` (an agent became active) and
        ``on_agent_closed(agent)`` (an agent finished). They are never opened
        or queued themselves.
    """

    def __init__(
        self,
        manager: REManagerAPI,
        catalog: ZMQCatalog,
        registry: Optional[AgentRegistry] = None,
        agent_log: Optional[AgentLog] = None,
        observers: Optional[list[Any]] = None,
    ) -> None:
        self.manager = manager
        self.catalog = catalog
        self.registry = registry or default_registry()
        self.agent_log = agent_log
        self.active_agents: dict[str, Any] = {}
        self.observers = list(observers or [])
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent-qs")
        self.catalog.subscribe_stop(self._on_run_stop)

    def shutdown(self) -> None:
        """
        Stop background queue work and release the worker thread.
        """
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _on_run_stop(self, run: ZMQRun) -> None:
        """
        Route completed runs to lifecycle handlers and active agents.

        Parameters
        ----------
        run : ZMQRun
            Finished run from the catalog.
        """
        self._executor.submit(self._handle_run_stop, run)

    def _handle_run_stop(self, run: ZMQRun) -> None:
        """
        Process a completed run on the QueueServer worker thread.

        Parameters
        ----------
        run : ZMQRun
            Finished run from the catalog.
        """
        try:
            if run.plan_name == OPEN_AGENT_PLAN:
                self._activate_agent(run)
            elif run.plan_name == CLOSE_AGENT_PLAN:
                self._deactivate_agent_from_close(run)

            for agent in list(self.active_agents.values()):
                if agent.run_belongs_to_agent(run):
                    self._log_run(agent, run)
                agent.on_run_stop(run)
        except Exception:
            LOGGER.exception("Failed to handle run stop for run %s", run.uid)
        for observer in self.observers:
            if hasattr(observer, "on_run_stop"):
                try:
                    observer.on_run_stop(run)
                except Exception:
                    LOGGER.exception("Observer %s failed on run %s", type(observer).__name__, run.uid)

    def _notify(self, method: str, agent: Any) -> None:
        """Call ``method(agent)`` on every observer that has it; failures are logged."""
        for observer in self.observers:
            hook = getattr(observer, method, None)
            if hook is None:
                continue
            try:
                hook(agent)
            except Exception:
                LOGGER.exception("Observer %s failed in %s for %s", type(observer).__name__, method, agent.agent_uid)

    def _activate_agent(self, run: ZMQRun) -> None:
        """
        Instantiate an agent after an ``open_agent`` run completes.

        Parameters
        ----------
        run : ZMQRun
            Finished ``open_agent`` run.
        """
        agent_uid = run.start.get(AGENT_UID_KEY)
        agent_name = agent_name_from_run(run)
        parameters = run.start.get(AGENT_PARAMETERS_KEY, {})

        if not agent_uid or not agent_name:
            LOGGER.error("open_agent run %s missing agent metadata", run.uid)
            return

        if agent_uid in self.active_agents:
            LOGGER.warning("agent %s already active", agent_uid)
            return

        pair = ensure_queue_stop_before_close(self.manager, agent_uid=None)
        if pair is None:
            LOGGER.error("No close_agent sentinel found for open_agent run %s", run.uid)
            return

        try:
            update_close_agent_uid(self.manager, pair.close_agent_uid, agent_uid)
        except Exception:
            LOGGER.exception("Failed to update close_agent uid for agent %s", agent_uid)
            return

        pair = ensure_queue_stop_before_close(self.manager, agent_uid=agent_uid)
        if pair is None:
            LOGGER.error("close_agent sentinel missing after uid update for %s", agent_uid)
            return

        try:
            agent = self.registry.create(
                agent_name,
                agent_uid=agent_uid,
                parameters=parameters,
                manager=self.manager,
                catalog=self.catalog,
                open_run=run,
                on_finished=self._remove_agent,
                agent_log=self.agent_log,
            )
        except KeyError:
            LOGGER.exception("Unknown agent name %s", agent_name)
            return

        self.active_agents[agent_uid] = agent
        LOGGER.info("Activated agent %s (%s)", agent_name, agent_uid)
        agent.open_log()
        agent.start()
        if agent.active:
            self._notify("on_agent_opened", agent)

    @staticmethod
    def _log_run(agent: Any, run: ZMQRun) -> None:
        """
        Record a finished run in its agent's shared log.

        Parameters
        ----------
        agent : BaseAgent
            Agent that emitted the run.
        run : ZMQRun
            Finished run.
        """
        status = (run.stop or {}).get("exit_status", "unknown")
        text = f"scan {run.scan_id} {run.plan_name} {status}"
        sample = run.start.get("sample_name") or run.start.get("sample_id")
        if sample:
            text += f" on {sample}"
        agent.log(
            "run",
            text,
            run_uid=run.uid,
            scan_id=run.scan_id,
            plan=run.plan_name,
            exit_status=status,
            agent_sequence=run.start.get(AGENT_SEQUENCE_KEY),
        )

    def _deactivate_agent_from_close(self, run: ZMQRun) -> None:
        """
        Remove an agent when its ``close_agent`` sentinel executes.

        Parameters
        ----------
        run : ZMQRun
            Finished ``close_agent`` run.
        """
        agent_uid = run.start.get(AGENT_UID_KEY)
        if agent_uid and agent_uid in self.active_agents:
            self.active_agents[agent_uid].stop("close_agent executed")

    def _remove_agent(self, agent: Any) -> None:
        """
        Drop a finished agent from the active set.

        Parameters
        ----------
        agent : BaseAgent
            Agent that has shut down.
        """
        self.active_agents.pop(agent.agent_uid, None)
        self._notify("on_agent_closed", agent)


def default_registry() -> AgentRegistry:
    """
    Build the agent registry: the built-in agents plus every agent installed
    packages declare under the ``nbs_agents.agents`` entry-point group.

    Returns
    -------
    AgentRegistry
        Registry with built-in and installed agents registered.
    """
    from importlib.metadata import entry_points

    registry = AgentRegistry()
    registry.register("repeat", RepeatAgent)
    registry.register("external", ExternalAgent)
    for entry in entry_points(group=AGENTS_ENTRY_POINT):
        try:
            registry.register(entry.name, entry.load())
        except Exception:
            LOGGER.exception("Could not load agent %s from %s", entry.name, entry.value)
    return registry
