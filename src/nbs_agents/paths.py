"""
Where nbs-agents keeps its files.

Everything lives under one folder, ``$NBS_AGENTS_HOME`` (default
``~/.nbs-agents``), so a deployment is one folder to make, back up or move::

    config/   nbs-cli.toml, gateway.env (gateway key), token (web login)
    state/    agent-log/ (the agent log's Redis snapshot)
    data/     attachments/ (figures), kb/ (agents' knowledge-base notes)
    work/     sessions/<uid8>/ (driver workdirs), assistant/, logs/

``nbs-agents init`` makes it. Settings and variables that name a path
(``NBS_CLI_CONFIG``, ``attachments_dir``, ...) still win.

Installs from before this layout kept files in ``~/.config``,
``~/.local/share``, ``~/.local/state`` and ``~/beamline-agents``. While
``$NBS_AGENTS_HOME`` is unset, the home folder does not exist and
``~/.config/nbs-cli.toml`` does, those old places are used instead, all
together, so an existing install keeps working until it moves.
"""

import os
from dataclasses import dataclass
from pathlib import Path

HOME_ENV = "NBS_AGENTS_HOME"
DEFAULT_HOME = "~/.nbs-agents"
LEGACY_CONFIG = "~/.config/nbs-cli.toml"


@dataclass(frozen=True)
class Paths:
    """
    The nbs-agents file layout.

    Attributes
    ----------
    home : Path
        Root folder (``$NBS_AGENTS_HOME``); for a legacy install, ``~``.
    legacy : bool
        Whether this is a pre-``NBS_AGENTS_HOME`` install.
    config_file, gateway_env, token : Path
        nbs-cli settings, the gateway key file and the web login token.
    agent_log_dir : Path
        Snapshot folder of the agent log's own Redis.
    attachments, kb_agent : Path
        Figures linked as ``att:ID``, and agents' knowledge-base notes.
    sessions, assistant, logs : Path
        Driver workdirs (one ``<uid8>`` folder per session), the assistant's
        workdir, and the logs of every Claude the runner starts.
    """

    home: Path
    legacy: bool
    config_file: Path
    gateway_env: Path
    token: Path
    agent_log_dir: Path
    attachments: Path
    kb_agent: Path
    sessions: Path
    assistant: Path
    logs: Path

    @property
    def folders(self) -> dict[str, Path]:
        """Folders ``nbs-agents init`` makes, with private ones first."""
        return {
            "config": self.token.parent,
            "agent_log_dir": self.agent_log_dir,
            "attachments": self.attachments,
            "kb_agent": self.kb_agent,
            "sessions": self.sessions,
            "assistant": self.assistant,
            "logs": self.logs,
        }


def _legacy() -> Paths:
    user = Path("~").expanduser()
    return Paths(
        home=user,
        legacy=True,
        config_file=user / ".config" / "nbs-cli.toml",
        gateway_env=user / ".config" / "nbs-agents" / "gateway.env",
        token=user / ".config" / "nbs-agents" / "token",
        agent_log_dir=user / ".local" / "state" / "nbs-agents" / "agent-log",
        attachments=user / ".local" / "share" / "nbs-agents" / "attachments",
        kb_agent=user / ".local" / "share" / "nbs-agents" / "kb",
        sessions=user / "beamline-agents",
        assistant=user / "beamline-agents" / "assistant",
        logs=user / "beamline-agents" / "logs",
    )


def paths() -> Paths:
    """
    The layout in use now.

    Returns
    -------
    Paths
        Under ``$NBS_AGENTS_HOME`` (or ``~/.nbs-agents``), or the legacy
        places for an install made before that existed.
    """
    explicit = os.environ.get(HOME_ENV)
    home = Path(explicit or DEFAULT_HOME).expanduser()
    if not explicit and not home.exists() and Path(LEGACY_CONFIG).expanduser().exists():
        return _legacy()
    config = home / "config"
    return Paths(
        home=home,
        legacy=False,
        config_file=config / "nbs-cli.toml",
        gateway_env=config / "gateway.env",
        token=config / "token",
        agent_log_dir=home / "state" / "agent-log",
        attachments=home / "data" / "attachments",
        kb_agent=home / "data" / "kb",
        sessions=home / "work" / "sessions",
        assistant=home / "work" / "assistant",
        logs=home / "work" / "logs",
    )
