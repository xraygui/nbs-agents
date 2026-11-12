import logging
import os
import socket
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from bluesky_queueserver_api.zmq import REManagerAPI

from .agentlog import AgentLog, is_assistant_record
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
from .queue import claim_close_agent, describe_owner, ensure_queue_stop_before_close

LOGGER = logging.getLogger(__name__)

# Packages add agents under this entry-point group: name = "module:AgentClass"
AGENTS_ENTRY_POINT = "nbs_agents.agents"


# Session statuses that are still open, so a restarted runner adopts or closes them
OPEN_STATUSES = ("opening", "active", "finalizing")


def _close_uid(item: dict[str, Any]) -> Optional[str]:
    args = item.get("args") or []
    return args[0] if args else (item.get("kwargs") or {}).get(AGENT_UID_KEY)


def runner_identity() -> dict[str, Any]:
    """A new runner's owner stamp: a random ``id``, and ``host``, ``pid`` and ``started`` for people."""
    return {"id": uuid.uuid4().hex, "host": socket.gethostname(), "pid": os.getpid(), "started": time.time()}


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
    owner : dict, optional
        This runner's owner stamp (``runner_identity``). The supervisor claims
        each agent block for it on the block's ``close_agent`` item, and leaves
        a block another runner owns alone, so two runners never both drive it.
    """

    # Wait before reading a claimed close_agent back (see claim_close_agent)
    CLAIM_SETTLE_S = 0.5

    def __init__(
        self,
        manager: REManagerAPI,
        catalog: ZMQCatalog,
        registry: Optional[AgentRegistry] = None,
        agent_log: Optional[AgentLog] = None,
        observers: Optional[list[Any]] = None,
        owner: Optional[dict[str, Any]] = None,
    ) -> None:
        self.manager = manager
        self.owner = owner or runner_identity()
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
            holder = claim_close_agent(
                self.manager, pair.close_agent_uid, agent_uid, self.owner, settle_s=self.CLAIM_SETTLE_S
            )
        except Exception:
            LOGGER.exception("Failed to claim close_agent for agent %s", agent_uid)
            return
        if holder is not None and holder["id"] != self.owner["id"]:
            LOGGER.warning("agent %s belongs to the %s; not starting it here", agent_uid, describe_owner(holder))
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

        agent.owner = self.owner
        self.active_agents[agent_uid] = agent
        LOGGER.info("Activated agent %s (%s)", agent_name, agent_uid)
        agent.open_log()
        if self.agent_log is not None:
            try:
                self.agent_log.update(agent_uid, runner=self.owner)
            except Exception:
                LOGGER.exception("Could not record the runner for agent %s", agent_uid)
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

        A session this runner is not running (it was opened before a runner
        restart) is closed in the agent log directly, so it does not stay
        "active" forever.

        Parameters
        ----------
        run : ZMQRun
            Finished ``close_agent`` run.
        """
        agent_uid = run.start.get(AGENT_UID_KEY)
        if not agent_uid:
            return
        if agent_uid in self.active_agents:
            self.active_agents[agent_uid].stop("close_agent executed")
            return
        self._close_orphan(agent_uid, "close_agent executed (this runner did not open the session, e.g. after a restart)")

    def _close_orphan(self, agent_uid: str, reason: str) -> None:
        """Close a session no agent of this runner drives, in the agent log only."""
        if self.agent_log is None:
            return
        try:
            record = self.agent_log.get(agent_uid)
            if record is None or record.get("status") not in OPEN_STATUSES:
                return
            self.agent_log.append(agent_uid, "close", reason, source="runner")
            self.agent_log.set_status(agent_uid, "closed", close_reason=reason)
            LOGGER.info("Closed orphaned agent session %s: %s", agent_uid, reason)
        except Exception:
            LOGGER.exception("Could not close orphaned agent session %s", agent_uid)

    def adopt_open_sessions(self) -> list[str]:
        """
        Take over the sessions a previous runner left open.

        Only one runner serves a beamline (``nbs_agents.runner_lock``), so every
        open session in the agent log is this runner's to drive. A session whose
        ``close_agent`` is still queued is claimed for this runner and, if its
        agent can resume (``RESUMABLE``), started again, and observers'
        ``on_agent_adopted(agent, record)`` runs (the Claude launcher stops a
        leftover Claude and launches a new one). Any other open session is
        closed as orphaned.

        Returns
        -------
        list of str
            UIDs of the adopted sessions.
        """
        if self.agent_log is None:
            return []
        records = {
            uid: r
            for uid, r in self.agent_log.records().items()
            if r.get("status") in OPEN_STATUSES and not is_assistant_record(r, uid)
        }
        if not records:
            return []
        items = self.manager.queue_get().get("items", [])
        adopted = []
        for agent_uid, record in records.items():
            if agent_uid in self.active_agents:
                continue
            close = next(
                (i for i in items if i.get("name") == CLOSE_AGENT_PLAN and _close_uid(i) == agent_uid), None
            )
            if close is None:
                self._close_orphan(agent_uid, "the runner restarted and this session's block is no longer queued")
                continue
            agent_name = record.get("agent_name")
            try:
                cls = self.registry.get(agent_name)
            except KeyError:
                cls = None
            if cls is None or not getattr(cls, "RESUMABLE", False):
                self._close_orphan(agent_uid, f"the runner restarted and a {agent_name!r} agent cannot resume")
                continue
            try:
                claim_close_agent(self.manager, close["item_uid"], agent_uid, self.owner, settle_s=0, force=True)
                agent = self.registry.create(
                    agent_name,
                    agent_uid=agent_uid,
                    parameters=record.get("parameters") or {},
                    manager=self.manager,
                    catalog=self.catalog,
                    open_run=None,
                    on_finished=self._remove_agent,
                    agent_log=self.agent_log,
                )
            except Exception:
                LOGGER.exception("Could not adopt agent session %s", agent_uid)
                continue
            agent.owner = self.owner
            self.active_agents[agent_uid] = agent
            self.agent_log.update(agent_uid, runner=self.owner)
            agent.log("adopt", f"adopted by the {describe_owner(self.owner)} after a runner restart")
            agent.start()
            if not agent.active:
                continue
            adopted.append(agent_uid)
            for observer in self.observers:
                if hasattr(observer, "on_agent_adopted"):
                    try:
                        observer.on_agent_adopted(agent, record)
                    except Exception:
                        LOGGER.exception("Observer %s failed adopting %s", type(observer).__name__, agent_uid)
        return adopted

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
