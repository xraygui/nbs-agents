"""
``nbs-agents``: set up and check an nbs-agents install.

    nbs-agents paths                 where every file lives
    nbs-agents init                  make $NBS_AGENTS_HOME and its starting files
    nbs-agents init --refresh-assistant
                                     also update the assistant's workdir from the template
    nbs-agents check                 check files, permissions, tools and services

See ``nbs_agents.paths`` for the layout.
"""

import argparse
import os
import shutil
import stat
import sys
import threading
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from .paths import HOME_ENV, paths

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_TEMPLATE = REPO_ROOT / "templates" / "nbs-cli.toml"
GATEWAY_KEYS = ("AIFAPIM_HOST", "AIFAPIM_API_KEY")
PROBE_TIMEOUT_S = 5.0

OK, WARN, FAIL = "ok", "warn", "FAIL"


def _write_new(path: Path, text: str, mode: int) -> bool:
    """Create ``path`` with ``text`` and ``mode`` unless it exists; True if made."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return True


def init(refresh_assistant: bool = False, out=print) -> int:
    """
    Make the folders and starting files of the current layout.

    Existing files are never overwritten, except the assistant's template
    files with ``refresh_assistant`` (old copies kept as ``*.bak``).

    Returns
    -------
    int
        Exit status.
    """
    from .agents.assistant import prepare_workdir
    from .auth import ensure_token

    p = paths()
    p.home.mkdir(parents=True, exist_ok=True)
    out(f"nbs-agents home: {p.home}")
    for name, folder in p.folders.items():
        folder.mkdir(mode=0o700 if name == "config" else 0o777, parents=True, exist_ok=True)
    os.chmod(p.token.parent, 0o700)

    if CONFIG_TEMPLATE.exists() and _write_new(p.config_file, CONFIG_TEMPLATE.read_text(), 0o600):
        out(f"wrote {p.config_file}: edit it for this beamline")
    ensure_token(str(p.token))
    prepare_workdir(str(p.assistant), refresh=refresh_assistant)
    out("done; run `nbs-agents check` next")
    return 0


# ------------------------------------------------------------------- check


def _private(path: Path, mode: int) -> tuple[str, str]:
    if not path.exists():
        return FAIL, "missing"
    info = path.stat()
    if info.st_uid != os.getuid():
        return FAIL, "not owned by you"
    have = stat.S_IMODE(info.st_mode)
    if have & 0o077:
        return FAIL, f"mode {have:o}; should be {mode:o}"
    return OK, f"mode {have:o}"


def _gateway(path: Path) -> tuple[str, str]:
    """Where ``claude-beamline --sandbox bwclaude`` will get the gateway host and key."""
    if all(os.environ.get(k) for k in GATEWAY_KEYS):
        return OK, "AIFAPIM_HOST and AIFAPIM_API_KEY set in the environment"
    if not path.exists():
        return WARN, (f"AIFAPIM_HOST/AIFAPIM_API_KEY not in the environment and no {path} "
                      "(needed only for --sandbox bwclaude; load them before starting the runner)")
    state, detail = _private(path, 0o600)
    if state != OK:
        return state, f"{path}: {detail}"
    values = {}
    for line in path.read_text().splitlines():
        name, sep, value = line.strip().removeprefix("export ").partition("=")
        if sep:
            values[name.strip()] = value.strip().strip("'\"")
    missing = [k for k in GATEWAY_KEYS if not (os.environ.get(k) or values.get(k))]
    if missing:
        return WARN, f"{', '.join(missing)} not set in the environment or {path} (needed only for --sandbox bwclaude)"
    return OK, f"from {path} ({detail})"


def _probe(func: Callable[[], str]) -> tuple[str, str]:
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


def _redis_ping(settings: dict) -> str:
    import redis

    client = redis.Redis(host=settings["host"], port=int(settings["port"]), db=int(settings.get("db", 0)),
                         socket_timeout=PROBE_TIMEOUT_S, socket_connect_timeout=PROBE_TIMEOUT_S)
    client.ping()
    return f"{settings['host']}:{settings['port']} db {settings.get('db', 0)}"


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


def _row(name: str, result: tuple[str, str]) -> tuple[str, str, str]:
    return result[0], name, result[1]


def check(out=print) -> int:
    """
    Report each file, tool and service nbs-agents needs.

    Returns
    -------
    int
        1 if anything failed, else 0.
    """
    from .cli.config import AssistantConfig, CliConfig
    from .launcher import ClaudeLauncher

    p = paths()
    rows: list[tuple[str, str, str]] = []
    rows.append((OK, "home", str(p.home)))

    config: Optional[CliConfig] = None
    if not p.config_file.exists() and not os.environ.get("NBS_CLI_CONFIG"):
        rows.append((FAIL, "config", f"{p.config_file} missing; run `nbs-agents init`"))
    try:
        config = CliConfig.load()
        assistant = AssistantConfig.load()
        rows.append((OK, "config", os.environ.get("NBS_CLI_CONFIG") or str(p.config_file)))
    except Exception as exc:
        rows.append((FAIL, "config", f"{type(exc).__name__}: {exc}"))
    state, detail = _private(p.token.parent, 0o700)
    rows.append((state, "private folder", f"{p.token.parent}, {detail}"))
    rows.append(_row("gateway", _gateway(Path(os.environ.get("NBS_GATEWAY_ENV") or p.gateway_env))))
    if p.token.exists():
        state, detail = _private(p.token, 0o600)
        rows.append((state, "web token", f"{p.token}, {detail}"))
    else:
        rows.append((WARN, "web token", f"{p.token} (made by nbs-agents-web or init)"))
    for name, folder in p.folders.items():
        if name != "config":
            rows.append((OK if folder.is_dir() else WARN, name, f"{folder}{'' if folder.is_dir() else ' (made on first use)'}"))
    if config is not None and config.kb_dir:
        rows.append((OK if Path(config.kb_dir).expanduser().is_dir() else WARN, "kb_dir", config.kb_dir))

    if config is not None:
        try:
            launcher = ClaudeLauncher.from_config(config)
            rows.append((OK, "claude_command", " ".join(launcher.resolve_command()) if launcher else "off"))
        except Exception as exc:
            rows.append((FAIL, "claude_command", str(exc)))
        bwclaude = os.environ.get("NBS_BWCLAUDE") or shutil.which("bwclaude")
        rows.append((OK if bwclaude else WARN, "bwclaude", bwclaude or "not found (needed for --sandbox bwclaude)"))
        if config.agent_log.serve:
            server = shutil.which(config.agent_log.redis_server.split()[0])
            rows.append((OK if server else FAIL, "redis-server", server or f"{config.agent_log.redis_server} not found"))
        if assistant.enabled:
            rows.append((OK, "assistant on", f"workdir {assistant.workdir}"))

        rows.append(_row("QueueServer", _probe(lambda: _queueserver(config.qs_addr))))
        rows.append(_row("beamline Redis", _probe(lambda: _redis_ping(config.redis_settings))))
        if config.agent_log.dedicated:
            state, detail = _probe(lambda: _redis_ping(config.agent_log_settings))
            if state == FAIL and config.agent_log.serve:
                state, detail = WARN, "not running (nbs-agents-runner starts it)"
            rows.append((state, "agent-log Redis", detail))
        rows.append(_row("Tiled", _probe(lambda: _tiled(config.tiled_uri))))
        if not (config.tiled_api_key or os.environ.get("TILED_API_KEY")):
            rows.append((WARN, "Tiled key", "no TILED_API_KEY; fine only if Tiled allows anonymous reads"))

    width = max(len(name) for _, name, _ in rows)
    for state, name, detail in rows:
        out(f"{state:>4}  {name:<{width}}  {detail}")
    return 1 if any(state == FAIL for state, _, _ in rows) else 0


def show_paths(out=print) -> int:
    """Print every file location of the current layout."""
    p = paths()
    out(f"home           {p.home}")
    for name in ("config_file", "gateway_env", "token", "agent_log_dir", "attachments", "kb_agent",
                 "sessions", "assistant", "logs"):
        out(f"{name:<14} {getattr(p, name)}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="nbs-agents", description=__doc__.strip().splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("paths", help="print where every file lives")
    init_parser = sub.add_parser("init", help=f"make ${HOME_ENV} and its starting files")
    init_parser.add_argument(
        "--refresh-assistant", action="store_true",
        help="overwrite the assistant's workdir files that differ from the template (keeps *.bak)",
    )
    sub.add_parser("check", help="check files, permissions, tools and services")
    args = parser.parse_args(argv)
    if args.command == "paths":
        return show_paths()
    if args.command == "init":
        return init(args.refresh_assistant)
    return check()


if __name__ == "__main__":
    sys.exit(main())
