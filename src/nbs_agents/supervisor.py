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
from .launcher import ClaudeLauncher, LaunchedAgent
from .queue import ensure_queue_stop_before_close, update_close_agent_uid

LOGGER = logging.getLogger(__name__)

CLAUDE_AGENT = "claude"


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
    launcher : ClaudeLauncher, optional
        Starts a Claude child when a ``"claude"`` session opens with
        ``launch: true`` and stops it when the session closes. Without one,
        such sessions log an error and wait for a Claude started by hand.
    """

    def __init__(
        self,
        manager: REManagerAPI,
        catalog: ZMQCatalog,
        registry: Optional[AgentRegistry] = None,
        agent_log: Optional[AgentLog] = None,
        launcher: Optional[ClaudeLauncher] = None,
    ) -> None:
        self.manager = manager
        self.catalog = catalog
        self.registry = registry or default_registry()
        self.agent_log = agent_log
        self.launcher = launcher
        if launcher is not None:
            launcher.on_exit = self._on_launched_exit
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
                if agent.run_belongs_to_agent(run):
                    self._log_run(agent, run)
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
                agent_log=self.agent_log,
            )
        except KeyError:
            LOGGER.exception("Unknown agent name %s", agent_name)
            return

        self.active_agents[agent_uid] = agent
        LOGGER.info("Activated agent %s (%s)", agent_name, agent_uid)
        agent.open_log()
        agent.start()
        if agent_name == CLAUDE_AGENT and (parameters or {}).get("launch") and agent.active:
            self._launch_claude(agent)

    def _launch_claude(self, agent: Any) -> None:
        """
        Start a Claude child to drive a newly opened session.

        Failures are written to the session log, never raised, so the runner
        keeps going.

        Parameters
        ----------
        agent : BaseAgent
            The active ``"claude"`` agent.
        """
        if self.launcher is None:
            agent.log("error", "launch requested, but this runner has no claude_command configured")
            return
        driver = f"runner-launch-{agent.agent_uid[:8]}"
        try:
            # Record the driver first, so the child marks operator messages read from its first call
            if self.agent_log is not None:
                self.agent_log.update(agent.agent_uid, driver=driver)
            launched = self.launcher.launch(agent.agent_uid, (agent.parameters or {}).get("goal"), driver)
        except Exception as exc:
            LOGGER.exception("Could not launch Claude for %s", agent.agent_uid)
            agent.log("error", f"could not launch Claude: {exc}")
            return
        launch = {
            "pid": launched.pid,
            "command": launched.command,
            "workdir": launched.workdir,
            "log_path": launched.log_path,
        }
        if self.agent_log is not None:
            try:
                self.agent_log.update(agent.agent_uid, launch=launch)
            except Exception:
                LOGGER.exception("Failed to record launch for %s", agent.agent_uid)
        agent.log("launch", f"started Claude (pid {launched.pid})", pid=launched.pid, log_path=launched.log_path)

    def _on_launched_exit(self, launched: LaunchedAgent, returncode: int) -> None:
        """
        Log a launched Claude's exit in its session.

        Parameters
        ----------
        launched : LaunchedAgent
            The child that exited.
        returncode : int
            Its exit status; negative for a signal.
        """
        text = f"Claude (pid {launched.pid}) exited with status {returncode}"
        agent = self.active_agents.get(launched.agent_uid)
        kind = "error" if agent is not None and agent.active and returncode != 0 else "launch"
        if agent is not None:
            agent.log(kind, text, pid=launched.pid, exit_status=returncode, log_path=launched.log_path)
        elif self.agent_log is not None:
            try:
                self.agent_log.append(
                    launched.agent_uid, kind, text, source=CLAUDE_AGENT,
                    pid=launched.pid, exit_status=returncode, log_path=launched.log_path,
                )
            except Exception:
                LOGGER.exception("Failed to log Claude exit for %s", launched.agent_uid)

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
        if self.launcher is not None:
            try:
                self.launcher.terminate(agent.agent_uid)
            except Exception:
                LOGGER.exception("Failed to stop Claude for %s", agent.agent_uid)


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
    registry.register("external", ExternalAgent)
    registry.register(CLAUDE_AGENT, ExternalAgent)
    return registry
