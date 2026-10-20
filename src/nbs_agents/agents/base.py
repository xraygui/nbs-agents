import logging
from typing import Any, Callable, Optional, Type

from ..agentlog import AgentLog
from ..catalog import ZMQCatalog, ZMQRun
from ..constants import AGENT_NAME_KEY, AGENT_UID_KEY, OPEN_AGENT_PLAN
from ..queue import (
    ensure_queue_stop_before_close,
    insert_before_queue_stop,
    release_queue_toward_close,
    tag_agent_metadata,
)

LOGGER = logging.getLogger(__name__)


class BaseAgent:
    """
    Base class for in-process queue agents.

    Parameters
    ----------
    agent_uid : str
        Shared UID for this agent session.
    agent_name : str
        Registered agent name.
    parameters : dict
        Parameters from the ``open_agent`` plan.
    manager : object
        QueueServer API client.
    catalog : ZMQCatalog
        Document catalog for run inspection.
    open_run : ZMQRun
        Finished ``open_agent`` run that activated this agent.
    on_finished : callable, optional
        Callback invoked when the agent shuts down.
    agent_log : AgentLog, optional
        Shared agent-log store. Without one, ``log`` only writes to Python logging.
    """

    def __init__(
        self,
        agent_uid: str,
        agent_name: str,
        parameters: dict[str, Any],
        manager: Any,
        catalog: ZMQCatalog,
        open_run: ZMQRun,
        on_finished: Optional[Callable[["BaseAgent"], None]] = None,
        agent_log: Optional[AgentLog] = None,
    ) -> None:
        self.agent_uid = agent_uid
        self.agent_name = agent_name
        self.parameters = parameters
        self.manager = manager
        self.catalog = catalog
        self.open_run = open_run
        self.on_finished = on_finished
        self.agent_log = agent_log
        self._sequence = 0
        self._active = True

    @property
    def active(self) -> bool:
        """
        Whether the agent is still active.

        Returns
        -------
        bool
            True if the agent has not shut down.
        """
        return self._active

    def start(self) -> None:
        """
        Begin agent execution.
        """

    def on_run_stop(self, run: ZMQRun) -> None:
        """
        Handle a completed Bluesky run.

        Parameters
        ----------
        run : ZMQRun
            Finished run from the catalog.
        """

    def stop(self, reason: str) -> None:
        """
        Shut down the agent.

        Parameters
        ----------
        reason : str
            Reason for shutdown.
        """
        if not self._active:
            return
        self._active = False
        LOGGER.info("Stopping agent %s (%s): %s", self.agent_name, self.agent_uid, reason)
        self.log("close", reason)
        self.set_status("closed", close_reason=reason)
        if self.on_finished is not None:
            self.on_finished(self)

    def log(self, kind: str, text: str, **refs: Any) -> None:
        """
        Append an entry to this agent's shared log.

        Parameters
        ----------
        kind : str
            Entry kind, for example ``"note"``, ``"add"`` or ``"run"``.
        text : str
            Human-readable text.
        **refs
            Optional references such as ``item_uid`` or ``run_uid``.
        """
        LOGGER.info("[%s %s] %s: %s", self.agent_name, self.agent_uid[:8], kind, text)
        if self.agent_log is None:
            return
        try:
            self.agent_log.append(self.agent_uid, kind, text, source=self.agent_name, **refs)
        except Exception:
            LOGGER.exception("Failed to write agent log for %s", self.agent_uid)

    def set_status(self, status: str, **fields: Any) -> None:
        """
        Set this agent's status in the shared log.

        Parameters
        ----------
        status : str
            New status, for example ``"active"`` or ``"closed"``.
        **fields
            Additional top-level record fields.
        """
        if self.agent_log is None:
            return
        try:
            self.agent_log.set_status(self.agent_uid, status, **fields)
        except Exception:
            LOGGER.exception("Failed to set agent status for %s", self.agent_uid)

    def open_log(self) -> None:
        """
        Create this agent's shared-log record from its ``open_agent`` parameters.
        """
        if self.agent_log is None:
            return
        params = self.parameters or {}
        try:
            self.agent_log.open(
                self.agent_uid,
                self.agent_name,
                goal=params.get("goal"),
                budget=params.get("budget"),
                status="active",
                parameters=params,
                open_run_uid=self.open_run.uid if self.open_run is not None else None,
            )
        except Exception:
            LOGGER.exception("Failed to open agent log for %s", self.agent_uid)
            return
        self.log("open", params.get("goal") or f"{self.agent_name} session opened")

    def ensure_sentinel_pair(self):
        """
        Ensure the queue contains a ``queue_stop`` item before ``close_agent``.

        Returns
        -------
        AgentSentinelPair or None
            Sentinel pair if ``close_agent`` exists.
        """
        return ensure_queue_stop_before_close(self.manager, agent_uid=self.agent_uid)

    def insert_plan(self, item: Any) -> None:
        """
        Insert a tagged plan before this agent's ``queue_stop`` sentinel.

        Parameters
        ----------
        item : BItem
            Queue item to insert.
        """
        if not self.active:
            raise RuntimeError(f"agent {self.agent_uid} is not active")

        self._sequence += 1
        tagged = tag_agent_metadata(
            item,
            self.agent_uid,
            self.agent_name,
            self._sequence,
        )
        response = insert_before_queue_stop(self.manager, tagged, self.agent_uid)
        self.log(
            "add",
            f"{getattr(item, 'name', 'item')} (sequence {self._sequence})",
            item_uid=(response.get("item") or {}).get("item_uid"),
            plan=getattr(item, "name", None),
        )

    def finalize(self) -> None:
        """
        Release the queue so ``close_agent`` can execute.
        """
        release_queue_toward_close(self.manager)
        self.stop("completed")

    def run_belongs_to_agent(self, run: ZMQRun) -> bool:
        """
        Check whether a run was emitted by this agent session.

        Parameters
        ----------
        run : ZMQRun
            Finished run to inspect.

        Returns
        -------
        bool
            True if the run metadata matches this agent UID.
        """
        if run.plan_name == OPEN_AGENT_PLAN:
            return False
        run_md = run.start
        return run_md.get(AGENT_UID_KEY) == self.agent_uid


class AgentRegistry:
    """
    Registry mapping agent names to agent classes.
    """

    def __init__(self) -> None:
        self._agents: dict[str, Type[BaseAgent]] = {}

    def register(self, name: str, agent_cls: Type[BaseAgent]) -> None:
        """
        Register an agent class.

        Parameters
        ----------
        name : str
            Agent name used in ``open_agent``.
        agent_cls : type
            Agent class to instantiate.
        """
        self._agents[name] = agent_cls

    def get(self, name: str) -> Type[BaseAgent]:
        """
        Look up a registered agent class.

        Parameters
        ----------
        name : str
            Agent name.

        Returns
        -------
        type
            Registered agent class.

        Raises
        ------
        KeyError
            If the agent name is unknown.
        """
        return self._agents[name]

    def create(
        self,
        name: str,
        agent_uid: str,
        parameters: dict[str, Any],
        manager: Any,
        catalog: ZMQCatalog,
        open_run: ZMQRun,
        on_finished: Optional[Callable[[BaseAgent], None]] = None,
        agent_log: Optional[AgentLog] = None,
    ) -> BaseAgent:
        """
        Instantiate a registered agent.

        Parameters
        ----------
        name : str
            Agent name.
        agent_uid : str
            Shared agent UID.
        parameters : dict
            Agent parameters.
        manager : object
            QueueServer API client.
        catalog : ZMQCatalog
            Document catalog.
        open_run : ZMQRun
            Activating ``open_agent`` run.
        on_finished : callable, optional
            Shutdown callback.
        agent_log : AgentLog, optional
            Shared agent-log store.

        Returns
        -------
        BaseAgent
            Instantiated agent.
        """
        agent_cls = self.get(name)
        return agent_cls(
            agent_uid=agent_uid,
            agent_name=name,
            parameters=parameters,
            manager=manager,
            catalog=catalog,
            open_run=open_run,
            on_finished=on_finished,
            agent_log=agent_log,
        )


def agent_name_from_run(run: ZMQRun) -> Optional[str]:
    """
    Extract an agent name from an ``open_agent`` run.

    Parameters
    ----------
    run : ZMQRun
        Finished open run.

    Returns
    -------
    str or None
        Agent name if present.
    """
    return run.start.get(AGENT_NAME_KEY)
