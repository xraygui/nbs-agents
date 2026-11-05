"""
Start and stop a Claude Code agent for an ``open_agent("claude", ...)`` session.

nbs-agents-runner uses this when an operator opens a Claude session from
nbs-gui with ``launch: true``. The child runs ``claude_command --headless``
in its own process group with ``NBS_AGENT_UID``, ``NBS_AGENT_GOAL`` and
``NBS_DRIVER`` set, so it drives the session that is already open instead of
opening another, and ``NBS_ENV_DIR`` naming this python environment.
"""

import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .resources import SCRIPTS_DIR

LOGGER = logging.getLogger(__name__)


@dataclass
class LaunchedAgent:
    """
    A running Claude child process.

    Attributes
    ----------
    agent_uid : str
        Session UID the child drives.
    process : subprocess.Popen
        The child.
    command : list of str
        Command line it was started with.
    workdir : str
        Its working folder.
    log_path : str
        File holding its stdout and stderr.
    """

    agent_uid: str
    process: subprocess.Popen
    command: list[str]
    workdir: str
    log_path: str

    @property
    def pid(self) -> int:
        return self.process.pid


def claude_launch_parameters(goal: str, budget: Optional[str] = None) -> dict:
    """
    ``open_agent`` parameters for a Claude session the runner should launch.

    Parameters
    ----------
    goal : str
        What the session is for. Required.
    budget : str, optional
        Time budget such as ``"90m"`` or ``"2h"``; empty for none.

    Returns
    -------
    dict
        ``goal``, ``launch: True`` and, with a budget, ``budget: {"time_s": ...}``
        (the shape ``nbs-cli open`` writes).

    Raises
    ------
    ValueError
        If the goal is empty or the budget cannot be parsed.
    """
    from ..cli.config import CliConfig
    from ..cli.main import parse_duration

    try:
        drivers = CliConfig.load().drivers
    except Exception:  # no config here (e.g. nbs-gui's account): the runner still refuses
        drivers = True
    if not drivers:
        raise ValueError("Claude agents are off on this beamline ([nbs_cli] drivers = false)")

    goal = (goal or "").strip()
    if not goal:
        raise ValueError("a goal is required")
    params: dict = {"goal": goal, "launch": True}
    if budget and budget.strip():
        try:
            params["budget"] = {"time_s": parse_duration(budget)}
        except Exception as exc:
            raise ValueError(str(exc)) from exc
    return params


class ClaudeLauncher:
    """
    Launch one Claude child per agent session and stop it when the session ends.

    Parameters
    ----------
    command : str
        Command to run, split with ``shlex``. A bare name not on ``PATH`` is
        looked up in the scripts nbs-agents ships (``resources.SCRIPTS_DIR``).
    workdir : str
        Working folder; ``{uid}`` and ``{uid8}`` are replaced by the session UID
        and its first eight characters. Created if missing.
    log_dir : str
        Folder for per-session log files. Created if missing.
    stop_timeout_s : float, optional
        Seconds between SIGTERM and SIGKILL in ``terminate``.
    on_exit : callable, optional
        Called as ``on_exit(launched, returncode)`` from a watcher thread when a
        child exits, whether on its own or after ``terminate``.
    """

    def __init__(
        self,
        command: str,
        workdir: str,
        log_dir: str,
        stop_timeout_s: float = 10.0,
        on_exit: Optional[Callable[[LaunchedAgent, int], None]] = None,
    ) -> None:
        self.command = command
        self.workdir = workdir
        self.log_dir = log_dir
        self.stop_timeout_s = stop_timeout_s
        self.on_exit = on_exit
        self.children: dict[str, LaunchedAgent] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, config) -> Optional["ClaudeLauncher"]:
        """
        Build a launcher from ``CliConfig``.

        Parameters
        ----------
        config : CliConfig
            Settings with ``claude_command``, ``claude_workdir``,
            ``claude_log_dir`` and ``claude_stop_timeout_s``.

        Returns
        -------
        ClaudeLauncher or None
            ``None`` when ``claude_command`` is empty, which turns launching off.
        """
        if not (config.claude_command or "").strip():
            return None
        return cls(
            config.claude_command,
            config.claude_workdir,
            config.claude_log_dir,
            stop_timeout_s=config.claude_stop_timeout_s,
        )

    def resolve_command(self) -> list[str]:
        """
        Split the command and find its executable.

        Returns
        -------
        list of str
            Command line, without ``--headless``.

        Raises
        ------
        FileNotFoundError
            If the executable is neither on ``PATH`` nor in ``scripts``.
        """
        argv = shlex.split(self.command)
        if not argv:
            raise FileNotFoundError("claude_command is empty")
        exe = argv[0]
        if os.sep not in exe and shutil.which(exe) is None:
            bundled = SCRIPTS_DIR / exe
            if not bundled.is_file():
                raise FileNotFoundError(f"{exe!r} is not on PATH or in {SCRIPTS_DIR}")
            argv[0] = str(bundled)
        return argv

    def launch(
        self,
        agent_uid: str,
        goal: Optional[str],
        driver: str,
        extra_args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
    ) -> LaunchedAgent:
        """
        Start a headless Claude for a session.

        Parameters
        ----------
        agent_uid : str
            Session UID, passed as ``NBS_AGENT_UID``.
        goal : str or None
            Session goal, passed as ``NBS_AGENT_GOAL``.
        driver : str
            Driver token recorded on the session, passed as ``NBS_DRIVER``.
        extra_args : list of str, optional
            Arguments after ``--headless``, such as ``["--role", "assistant"]``.
        env : dict, optional
            Environment variables set last, overriding the ones above.

        Returns
        -------
        LaunchedAgent
            The started child.

        Raises
        ------
        RuntimeError
            If a child for this session is still running.
        OSError
            If the command or folders cannot be used.
        """
        with self._lock:
            existing = self.children.get(agent_uid)
            if existing is not None and existing.process.poll() is None:
                raise RuntimeError(f"Claude for {agent_uid} is already running (pid {existing.pid})")
            argv = self.resolve_command()
            argv[1:1] = ["--headless", *(extra_args or [])]
            fill = {"uid": agent_uid, "uid8": agent_uid[:8]}
            workdir = Path(self.workdir.format(**fill)).expanduser()
            workdir.mkdir(parents=True, exist_ok=True)
            log_dir = Path(self.log_dir.format(**fill)).expanduser()
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"claude-{agent_uid[:8]}-{time.strftime('%Y%m%d-%H%M%S')}.log"
            env_overrides, env = env, dict(os.environ)
            env["NBS_AGENT_UID"] = agent_uid
            env["NBS_AGENT_GOAL"] = goal or ""
            env["NBS_DRIVER"] = driver
            # The python environment claude-beamline and sandbox-python use
            env.setdefault("NBS_ENV_DIR", sys.prefix)
            env.update(env_overrides or {})
            with open(log_path, "ab") as log_file:
                process = subprocess.Popen(
                    argv,
                    cwd=str(workdir),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            launched = LaunchedAgent(agent_uid, process, argv, str(workdir), str(log_path))
            self.children[agent_uid] = launched
        threading.Thread(
            target=self._watch, args=(launched,), name=f"claude-{agent_uid[:8]}", daemon=True
        ).start()
        LOGGER.info("Launched Claude for %s: pid %s, log %s", agent_uid, process.pid, log_path)
        return launched

    def _watch(self, launched: LaunchedAgent) -> None:
        """Reap a child and report its exit."""
        returncode = launched.process.wait()
        with self._lock:
            if self.children.get(launched.agent_uid) is launched:
                del self.children[launched.agent_uid]
        LOGGER.info("Claude for %s (pid %s) exited with %s", launched.agent_uid, launched.pid, returncode)
        if self.on_exit is not None:
            try:
                self.on_exit(launched, returncode)
            except Exception:
                LOGGER.exception("on_exit failed for %s", launched.agent_uid)

    def terminate(self, agent_uid: str) -> bool:
        """
        Stop a session's child: SIGTERM its process group, SIGKILL after a timeout.

        Returns at once; a timer sends the SIGKILL if needed.

        Parameters
        ----------
        agent_uid : str
            Session UID.

        Returns
        -------
        bool
            True if a running child was signalled.
        """
        with self._lock:
            launched = self.children.get(agent_uid)
        if launched is None or launched.process.poll() is not None:
            return False
        LOGGER.info("Stopping Claude for %s (pid %s)", agent_uid, launched.pid)
        _signal_group(launched.process, signal.SIGTERM)

        def kill_if_alive():
            if launched.process.poll() is None:
                LOGGER.warning("Claude for %s ignored SIGTERM; killing it", agent_uid)
                _signal_group(launched.process, signal.SIGKILL)

        timer = threading.Timer(self.stop_timeout_s, kill_if_alive)
        timer.daemon = True
        timer.start()
        return True


def _signal_group(process: subprocess.Popen, sig: int) -> None:
    """Signal a child's process group, or just the child if that fails."""
    try:
        os.killpg(os.getpgid(process.pid), sig)
    except (ProcessLookupError, PermissionError):
        try:
            process.send_signal(sig)
        except ProcessLookupError:
            pass


CLAUDE_AGENT = "claude"


class ClaudeLaunchHook:
    """
    Supervisor observer that starts a Claude child for each ``"claude"``
    session opened with ``launch: true``, and stops it when the session closes.

    Failures are written to the session log, never raised, so the runner
    keeps going.

    Parameters
    ----------
    launcher : ClaudeLauncher or None
        Starts the children. Without one, launch requests log an error and the
        session waits for a Claude started by hand.
    agent_log : AgentLog, optional
        Where launches and exits are recorded.
    """

    def __init__(self, launcher: Optional[ClaudeLauncher], agent_log=None) -> None:
        self.launcher = launcher
        self.agent_log = agent_log
        self.agents: dict[str, object] = {}
        if launcher is not None:
            launcher.on_exit = self._on_exit

    def on_agent_opened(self, agent) -> None:
        """Launch Claude if ``agent`` is a ``"claude"`` session asking for it."""
        if agent.agent_name != CLAUDE_AGENT or not (agent.parameters or {}).get("launch"):
            return
        if self.launcher is None:
            agent.log("error", "launch requested, but this runner has no claude_command configured")
            return
        self.agents[agent.agent_uid] = agent
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

    def on_agent_closed(self, agent) -> None:
        """Stop the session's Claude, if this hook started one."""
        self.agents.pop(agent.agent_uid, None)
        if self.launcher is not None:
            try:
                self.launcher.terminate(agent.agent_uid)
            except Exception:
                LOGGER.exception("Failed to stop Claude for %s", agent.agent_uid)

    def _on_exit(self, launched: LaunchedAgent, returncode: int) -> None:
        """Log a launched Claude's exit in its session."""
        text = f"Claude (pid {launched.pid}) exited with status {returncode}"
        agent = self.agents.get(launched.agent_uid)
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

    def shutdown(self) -> None:
        """Stop every Claude this hook started (the runner is stopping)."""
        if self.launcher is not None:
            for uid in list(self.launcher.children):
                self.launcher.terminate(uid)
