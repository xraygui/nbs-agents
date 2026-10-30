"""
Where nbs-agents keeps its files.

Everything lives under one folder, ``$NBS_AGENTS_HOME`` (default
``~/.nbs-agents``), with one subfolder per beamline, so a deployment is one
folder to make, back up or move::

    <beamline>/
        config/   nbs-cli.toml, gateway.env (gateway key), token (web login)
        state/    agent-log/ (the agent log's Redis snapshot), tiled-agent/
                  (the agents' read-only Tiled key)
        data/     attachments/ (figures), kb/ (agents' knowledge-base notes)
        work/     sessions/<uid8>/ (driver workdirs), assistant/, logs/
    tiled/        your Tiled login, shared by every beamline

The beamline is the one named by ``--beamline`` (which sets
``$NBS_BEAMLINE``) or ``$NBS_BEAMLINE``; if neither is given and only one
beamline is set up, that one. ``nbs-agents init --beamline NAME`` makes it.
Settings and variables that name a path (``NBS_CLI_CONFIG``,
``attachments_dir``, ...) still win.
"""

import argparse
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

HOME_ENV = "NBS_AGENTS_HOME"
BEAMLINE_ENV = "NBS_BEAMLINE"
DEFAULT_HOME = "~/.nbs-agents"
# Folders that hold secrets
PRIVATE = ("config", "tiled_cache", "tiled_agent")
CONFIG_NAME = "nbs-cli.toml"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class BeamlineError(RuntimeError):
    """No beamline is chosen and there is not exactly one to default to."""


@dataclass(frozen=True)
class Paths:
    """
    The nbs-agents file layout for one beamline.

    Attributes
    ----------
    home : Path
        Root folder (``$NBS_AGENTS_HOME``).
    beamline : str
        The beamline these paths are for.
    root : Path
        The beamline's folder, ``home / beamline``.
    config_file, gateway_env, token : Path
        nbs-cli settings, the gateway key file and the web login token.
    agent_log_dir : Path
        Snapshot folder of the agent log's own Redis.
    tiled_cache : Path
        Tiled's login token cache (``TILED_CACHE_DIR`` for nbs-agents), shared
        by every beamline.
    tiled_agent_key : Path
        The read-only, expiring Tiled API key the runner makes for agents. It
        is alone in its folder, which sandboxed agents get mounted read-only.
    attachments, kb_agent : Path
        Figures linked as ``att:ID``, and agents' knowledge-base notes.
    sessions, assistant, logs : Path
        Driver workdirs (one ``<uid8>`` folder per session), the assistant's
        workdir, and the logs of every Claude the runner starts.
    """

    home: Path
    beamline: str
    root: Path
    config_file: Path
    gateway_env: Path
    token: Path
    agent_log_dir: Path
    tiled_cache: Path
    tiled_agent_key: Path
    attachments: Path
    kb_agent: Path
    sessions: Path
    assistant: Path
    logs: Path

    @property
    def folders(self) -> dict[str, Path]:
        """Folders ``nbs-agents init`` makes; see ``PRIVATE`` for the mode 700 ones."""
        return {
            "config": self.config_file.parent,
            "agent_log_dir": self.agent_log_dir,
            "tiled_cache": self.tiled_cache,
            "tiled_agent": self.tiled_agent_key.parent,
            "attachments": self.attachments,
            "kb_agent": self.kb_agent,
            "sessions": self.sessions,
            "assistant": self.assistant,
            "logs": self.logs,
        }


def home() -> Path:
    """``$NBS_AGENTS_HOME``, or ``~/.nbs-agents``."""
    return Path(os.environ.get(HOME_ENV) or DEFAULT_HOME).expanduser()


def beamlines() -> list[str]:
    """Beamlines set up under the home folder: those with a ``config/nbs-cli.toml``."""
    root = home()
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir() if (d / "config" / CONFIG_NAME).is_file())


def check_name(name: str) -> str:
    """A beamline name, refused if it could escape the home folder."""
    if not NAME_RE.match(name or "") or name == "tiled":
        raise BeamlineError(f"bad beamline name {name!r}; use letters, digits, '.', '_' or '-'")
    return name


def current_beamline(name: Optional[str] = None) -> str:
    """
    The beamline in use.

    Parameters
    ----------
    name : str, optional
        Explicit choice; otherwise ``$NBS_BEAMLINE``, otherwise the only one set up.

    Raises
    ------
    BeamlineError
        If none is chosen and there is not exactly one set up.
    """
    chosen = name or os.environ.get(BEAMLINE_ENV)
    if chosen:
        return check_name(chosen)
    found = beamlines()
    if len(found) == 1:
        return found[0]
    if not found:
        raise BeamlineError(f"no beamline set up in {home()}; run `init --beamline NAME` first")
    raise BeamlineError(
        f"several beamlines in {home()} ({', '.join(found)}); pick one with --beamline or ${BEAMLINE_ENV}"
    )


def paths(beamline: Optional[str] = None) -> Paths:
    """
    The layout for a beamline.

    Parameters
    ----------
    beamline : str, optional
        See ``current_beamline``.

    Raises
    ------
    BeamlineError
        If no beamline can be chosen.
    """
    top = home()
    name = current_beamline(beamline)
    root = top / name
    config = root / "config"
    return Paths(
        home=top,
        beamline=name,
        root=root,
        config_file=config / CONFIG_NAME,
        gateway_env=config / "gateway.env",
        token=config / "token",
        agent_log_dir=root / "state" / "agent-log",
        tiled_cache=top / "tiled",
        tiled_agent_key=root / "state" / "tiled-agent" / "api-key.json",
        attachments=root / "data" / "attachments",
        kb_agent=root / "data" / "kb",
        sessions=root / "work" / "sessions",
        assistant=root / "work" / "assistant",
        logs=root / "work" / "logs",
    )


def add_beamline_argument(parser: argparse.ArgumentParser) -> None:
    """Add ``--beamline NAME`` (see ``use_beamline``)."""
    parser.add_argument(
        "--beamline",
        help=f"beamline folder under ${HOME_ENV} (default ${BEAMLINE_ENV}, or the only one set up)",
    )


def use_beamline(name: Optional[str] = None, must_exist: bool = True) -> str:
    """
    Choose the beamline for this process and the programs it starts.

    Sets ``$NBS_BEAMLINE``, so launched agents and their nbs-cli use the same one.

    Parameters
    ----------
    name : str, optional
        From ``--beamline``; see ``current_beamline`` when omitted.
    must_exist : bool, optional
        Refuse a beamline that is not set up yet (``init`` passes False).

    Raises
    ------
    BeamlineError
        If no beamline can be chosen, or it is not set up and ``must_exist``.
    """
    chosen = current_beamline(name)
    if must_exist and chosen not in beamlines():
        found = beamlines()
        raise BeamlineError(
            f"beamline {chosen!r} is not set up in {home()}"
            + (f" (have: {', '.join(found)})" if found else "")
            + f"; run `init --beamline {chosen}` first"
        )
    os.environ[BEAMLINE_ENV] = chosen
    return chosen
