"""
``nbs-agents``: set up and check an nbs-agents install.

    nbs-agents init --beamline NAME  make $NBS_AGENTS_HOME/NAME and its starting files
    nbs-agents check                 check files, permissions, tools and services
    nbs-agents paths                 where every file lives
    nbs-agents beamlines             the beamlines set up
    nbs-agents tiled-login           log in to Tiled now (the runner also asks when needed)

Each command takes ``--beamline NAME``; it may be left out when only one
beamline is set up (or ``$NBS_BEAMLINE`` names one).

See ``nbs_agents.paths`` for the layout. Packages built on nbs-agents (such as
sst-assistant) run these first and add their own steps: ``init(...,
extra_settings=..., extra_tables=...)`` adds their settings to a new config file, and
``check_rows()`` returns rows they extend before ``report()``.
"""

import argparse
import json
import os
import re
import shutil
import stat
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from .paths import HOME_ENV, PRIVATE, BeamlineError, add_beamline_argument, beamlines, home, paths, use_beamline

# Package data, so it is there however nbs-agents was installed
CONFIG_TEMPLATE = Path(__file__).parent / "templates" / "nbs-cli.toml"
PROBE_TIMEOUT_S = 5.0

OK, WARN, FAIL = "ok", "warn", "FAIL"

Row = tuple[str, str, str]


def write_new(path: Path, text: str, mode: int) -> bool:
    """Create ``path`` with ``text`` and ``mode`` unless it exists; True if made."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return True


def config_text(tiled_uri: Optional[str] = None, tiled_path: Optional[str] = None, extra_settings: str = "") -> str:
    """
    The starting ``nbs-cli.toml``, with this install's Tiled server and catalog filled in.

    Parameters
    ----------
    tiled_uri, tiled_path : str, optional
        Values for the template's commented ``tiled_uri`` and ``tiled_path`` lines.
    extra_settings : str, optional
        Lines added at the end of the ``[nbs_cli]`` table.
    """
    text = CONFIG_TEMPLATE.read_text()
    if extra_settings:
        # [nbs_cli] is the template's first table; the next one starts with "["
        end = text.index("\n[", text.index("\n[nbs_cli]\n") + 1) + 1
        text = text[:end] + extra_settings.rstrip("\n") + "\n\n" + text[end:]
    for name, value in (("tiled_uri", tiled_uri), ("tiled_path", tiled_path)):
        if value is not None:
            text = re.sub(rf"^# {name} = .*$", f"{name} = {json.dumps(value)}", text, count=1, flags=re.M)
    return text


def init(
    out=print,
    tiled_uri: Optional[str] = None,
    tiled_path: Optional[str] = None,
    extra_settings: str = "",
    extra_tables: str = "",
) -> int:
    """
    Make the folders and starting files of the current layout.

    Existing files are never overwritten.

    Parameters
    ----------
    out : callable, optional
        Where messages go.
    tiled_uri, tiled_path : str, optional
        This beamline's Tiled server and run catalog, written into a new
        ``nbs-cli.toml``.
    extra_settings : str, optional
        Lines another package adds to the ``[nbs_cli]`` table of a new ``nbs-cli.toml``.
    extra_tables : str, optional
        Tables another package appends to a new ``nbs-cli.toml``.

    Returns
    -------
    int
        Exit status.
    """
    p = paths()
    p.root.mkdir(parents=True, exist_ok=True)
    out(f"beamline {p.beamline}: {p.root}")
    for name, folder in p.folders.items():
        folder.mkdir(mode=0o700 if name in PRIVATE else 0o777, parents=True, exist_ok=True)
        if name in PRIVATE:
            os.chmod(folder, 0o700)
    text = config_text(tiled_uri, tiled_path, extra_settings) + (("\n" + extra_tables) if extra_tables else "")
    if write_new(p.config_file, text, 0o600):
        out(f"wrote {p.config_file}: edit it for this beamline")
    elif tiled_uri is not None or tiled_path is not None:
        out(f"{p.config_file} already exists; set tiled_uri / tiled_path there by hand")
    return 0


# ------------------------------------------------------------------- check


def private_status(path: Path, mode: int) -> tuple[str, str]:
    """``(state, detail)`` for a file or folder that only its owner may read."""
    if not path.exists():
        return FAIL, "missing"
    info = path.stat()
    if info.st_uid != os.getuid():
        return FAIL, "not owned by you"
    have = stat.S_IMODE(info.st_mode)
    if have & 0o077:
        return FAIL, f"mode {have:o}; should be {mode:o}"
    return OK, f"mode {have:o}"


def probe(func: Callable[[], str]) -> tuple[str, str]:
    """Run a service check in a thread, so a dead host cannot hang ``check``."""
    result: list = []

    def run():
        try:
            result.append((OK, func()))
        except Exception as exc:  # each probe reports its own failure
            result.append((FAIL, f"{type(exc).__name__}: {exc}"))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(PROBE_TIMEOUT_S)
    return result[0] if result else (FAIL, f"no answer in {PROBE_TIMEOUT_S:.0f} s")


def row(name: str, result: tuple[str, str]) -> Row:
    """A ``(state, name, detail)`` row from a ``(state, detail)`` result."""
    return result[0], name, result[1]


def _redis_ping(settings: dict) -> str:
    from .redisclient import connect_settings

    client = connect_settings(settings, socket_timeout=PROBE_TIMEOUT_S, socket_connect_timeout=PROBE_TIMEOUT_S)
    client.ping()
    tls = ", TLS" if settings.get("ssl") else ""
    return f"{settings['host']}:{settings['port']} db {settings.get('db', 0)}{tls}"


def _queueserver(addr: str) -> str:
    from bluesky_queueserver_api.zmq import REManagerAPI

    manager = REManagerAPI(zmq_control_addr=addr, timeout_recv=PROBE_TIMEOUT_S)
    try:
        status = manager.status()
    finally:
        manager.close()
    return f"{addr}, manager {status.get('manager_state')}"


def _tiled(uri: str) -> str:
    with urllib.request.urlopen(uri.rstrip("/") + "/healthz", timeout=PROBE_TIMEOUT_S) as response:
        return f"{uri} (HTTP {response.status})"


def _tiled_key_row(config) -> Row:
    """Where agents get Tiled access from."""
    if config.tiled_api_key or os.environ.get("TILED_API_KEY"):
        return OK, "Tiled key", "fixed key from tiled_api_key or TILED_API_KEY"
    path = Path(config.tiled_api_key_file).expanduser() if config.tiled_api_key_file else paths().tiled_agent_key
    try:
        expires = json.loads(path.read_text()).get("expires")
    except (OSError, ValueError):
        return WARN, "Tiled key", f"no agent key yet; nbs-agents-runner makes one after you log in ({path})"
    if isinstance(expires, (int, float)) and expires > time.time():
        return OK, "Tiled key", f"agent key valid for {(expires - time.time()) / 3600:.1f} h ({path})"
    return WARN, "Tiled key", f"agent key expired; is nbs-agents-runner running? ({path})"


def check_rows(config=None) -> list[Row]:
    """
    Check each file, tool and service nbs-agents needs.

    Parameters
    ----------
    config : AgentsConfig, optional
        Settings to check, such as another package's subclass; loaded if omitted.

    Returns
    -------
    list of tuple
        ``(state, name, detail)`` rows; ``state`` is ``OK``, ``WARN`` or ``FAIL``.
    """
    from .config import NO_AGENT_LOG, AgentsConfig

    p = paths()
    rows: list[Row] = [(OK, "beamline", f"{p.beamline} ({p.root})")]
    if not p.config_file.exists() and not os.environ.get("NBS_CLI_CONFIG"):
        rows.append((FAIL, "config", f"{p.config_file} missing; run `nbs-agents init`"))
    if config is None:
        try:
            config = AgentsConfig.load()
            rows.append((OK, "config", os.environ.get("NBS_CLI_CONFIG") or str(p.config_file)))
        except Exception as exc:
            rows.append((FAIL, "config", f"{type(exc).__name__}: {exc}"))
    for name in PRIVATE:
        folder = p.folders[name]
        if folder.exists() or name == "config":
            state, detail = private_status(folder, 0o700)
            rows.append((state, name, f"{folder}, {detail}"))
    for name, folder in p.folders.items():
        if name not in PRIVATE:
            rows.append((OK if folder.is_dir() else WARN, name, f"{folder}{'' if folder.is_dir() else ' (made on first use)'}"))
    if config is None:
        return rows

    if config.agent_log.serve:
        server = shutil.which(config.agent_log.redis_server.split()[0])
        rows.append((OK if server else FAIL, "redis-server", server or f"{config.agent_log.redis_server} not found"))
    rows.append(row("QueueServer", probe(lambda: _queueserver(config.qs_addr))))
    if config.agent_log.dedicated:
        state, detail = probe(lambda: _redis_ping(config.agent_log_settings))
        if state == FAIL and config.agent_log.serve:
            state, detail = WARN, "not running (nbs-agents-runner starts it)"
        rows.append((state, "agent-log Redis", detail))
    else:
        rows.append((FAIL, "agent-log Redis", NO_AGENT_LOG))
    rows.append(row("Tiled", probe(lambda: _tiled(config.tiled_uri))))
    if not config.tiled_path:
        rows.append((WARN, "tiled_path", "not set: runs are read from the server's root catalog"))
    rows.append(_tiled_key_row(config))
    return rows


def report(rows: list[Row], out=print) -> int:
    """
    Print check rows as a table.

    Returns
    -------
    int
        1 if any row failed, else 0.
    """
    width = max(len(name) for _, name, _ in rows)
    for state, name, detail in rows:
        out(f"{state:>4}  {name:<{width}}  {detail}")
    return 1 if any(state == FAIL for state, _, _ in rows) else 0


def check(out=print) -> int:
    """Report each file, tool and service nbs-agents needs; 1 if anything failed."""
    return report(check_rows(), out)


def tiled_login(out=print) -> int:
    """Log in to the configured Tiled server, reusing a cached login if it is still good."""
    from .config import AgentsConfig
    from .tiled_auth import login_context

    config = AgentsConfig.load()
    context = login_context(config.tiled_uri, interactive=True)
    who = context.whoami() if context.authenticated else None
    out(f"Tiled {config.tiled_uri}: " + (f"logged in as {who.get('identities', [{}])[0].get('id', '?')}" if who else "no login needed"))
    return 0


def show_paths(out=print) -> int:
    """Print every file location of the current layout."""
    p = paths()
    out(f"beamline         {p.beamline}")
    out(f"home             {p.home}")
    for name in ("root", "config_file", "gateway_env", "token", "agent_log_dir", "tiled_cache", "tiled_agent_key",
                 "attachments", "kb_agent", "sessions", "assistant", "logs"):
        out(f"{name:<16} {getattr(p, name)}")
    return 0


def show_beamlines(out=print) -> int:
    """Print the beamlines set up, marking the one used when none is named."""
    found = beamlines()
    if not found:
        out(f"no beamline set up in {home()}; run `init --beamline NAME`")
        return 1
    try:
        default = paths().beamline
    except BeamlineError:
        default = None
    for name in found:
        out(f"{'*' if name == default else ' '} {name}")
    return 0


def add_init_arguments(parser: argparse.ArgumentParser) -> None:
    """The ``init`` options, for packages that wrap it in their own command."""
    add_beamline_argument(parser)
    parser.add_argument("--tiled-uri", help="this beamline's Tiled server, e.g. https://tiled.example.org")
    parser.add_argument("--tiled-path", help="run catalog inside Tiled, e.g. BEAMLINE/raw")


def choose_beamline(args) -> Optional[int]:
    """
    Apply ``--beamline`` for a command; ``init`` may name a new one.

    Returns
    -------
    int or None
        An exit status to stop with (after printing why), or None to go on.
    """
    if args.command == "beamlines":
        return None
    try:
        use_beamline(getattr(args, "beamline", None), must_exist=args.command != "init")
    except BeamlineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="nbs-agents", description=__doc__.strip().splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    add_init_arguments(sub.add_parser("init", help=f"make ${HOME_ENV}/BEAMLINE and its starting files"))
    add_beamline_argument(sub.add_parser("check", help="check files, permissions, tools and services"))
    add_beamline_argument(sub.add_parser("paths", help="print where every file lives"))
    sub.add_parser("beamlines", help="list the beamlines set up")
    add_beamline_argument(sub.add_parser("tiled-login", help="log in to Tiled now"))
    args = parser.parse_args(argv)
    stop = choose_beamline(args)
    if stop is not None:
        return stop
    if args.command == "init":
        return init(tiled_uri=args.tiled_uri, tiled_path=args.tiled_path)
    if args.command == "paths":
        return show_paths()
    if args.command == "beamlines":
        return show_beamlines()
    if args.command == "tiled-login":
        return tiled_login()
    return check()


if __name__ == "__main__":
    sys.exit(main())
