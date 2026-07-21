import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from bluesky_queueserver_api.zmq import REManagerAPI

from .agents.base import AgentRegistry, agent_name_from_run
from .agents.repeat import RepeatAgent
from .catalog import ZMQCatalog, ZMQRun
from .constants import (
    AGENT_PARAMETERS_KEY,
    AGENT_UID_KEY,
    CLOSE_AGENT_PLAN,
    OPEN_AGENT_PLAN,
)
from .queue import ensure_queue_stop_before_close, update_close_agent_uid

LOGGER = logging.getLogger(__name__)


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
    """

    def __init__(
        self,
        manager: REManagerAPI,
        catalog: ZMQCatalog,
        registry: Optional[AgentRegistry] = None,
    ) -> None:
        self.manager = manager
        self.catalog = catalog
        self.registry = registry or default_registry()
        self.active_agents: dict[str, Any] = {}
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
                agent.on_run_stop(run)
        except Exception:
            LOGGER.exception("Failed to handle run stop for run %s", run.uid)

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
            )
        except KeyError:
            LOGGER.exception("Unknown agent name %s", agent_name)
            return

        self.active_agents[agent_uid] = agent
        LOGGER.info("Activated agent %s (%s)", agent_name, agent_uid)
        agent.start()

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


def default_registry() -> AgentRegistry:
    """
    Build the default agent registry for the prototype runner.

    Returns
    -------
    AgentRegistry
        Registry with built-in agents registered.
    """
    registry = AgentRegistry()
    registry.register("repeat", RepeatAgent)
    return registry
