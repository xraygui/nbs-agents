"""
Set up and check a Claude agents and beamline assistant install.

    PROG init --beamline NAME [--tiled-uri URI --tiled-path PATH] [--refresh-assistant]
    PROG check [--beamline NAME]
    PROG paths [--beamline NAME]
    PROG beamlines
    PROG tiled-login [--beamline NAME]
    PROG runner [--beamline NAME] [--assistant-only]
                nbs-agents-runner; --assistant-only turns drivers off
    PROG timing [--beamline NAME] [LOG ...] [-n N]

``init`` and ``check`` run nbs-agents' own first (``nbs_agents.admin``), then
add the assistant's steps: Claude and assistant settings in the config, the
web login token, the assistant's working folder; and checks of the gateway
key and bwclaude (when Claude runs in it), ``claude_command`` and the
knowledge base. A beamline package (such as sst-assistant) wraps ``main`` with
its own program name and preset settings.
"""

import argparse
import os
import shutil
from pathlib import Path
from typing import Optional

from .. import admin as core
from ..admin import FAIL, OK, WARN, Row, private_status, report, row
from ..paths import add_beamline_argument, paths

GATEWAY_KEYS = ("AIFAPIM_HOST", "AIFAPIM_API_KEY")
BWCLAUDE = "bwclaude"

# Added to the [nbs_cli] table of a new config
SETTINGS = """\
# --- Claude agents
# drivers = true                        # false: assistant only, no agent drives the beamline
# claude_command = "claude-beamline"    # e.g. "claude-beamline --model claude-opus-5-5"
# claude_sandbox = "none"               # or "bwclaude" (NSLS-II API gateway)
# allowed_plans = ["nbs_count", "move_sample"]   # client-side allowlist; unset = server decides
# kb_dir = ""                           # human knowledge-base notes, usually knowledge/ in the profile
# kb_review_dir = ""                    # nbs-agents-web's KB review record (default data/kb-review)
# signal = ""                           # run summaries: detector, normalization, reference
# norm = ""
# reference = ""
# baseline_keys = []
"""

# Appended to a new config
TABLES = """\
[assistant]
# Read-only beamline assistant hosted by nbs-agents-runner.
enabled = false
# model = "sonnet"
# alert_model = "opus"
"""


def sandbox(config=None) -> str:
    """The wrapper Claude runs in: ``NBS_CLAUDE_SANDBOX``, else ``[nbs_cli] claude_sandbox``."""
    if config is None:
        from ..cli.config import CliConfig

        config = CliConfig.load()
    return (config.claude_sandbox or "none").strip() or "none"


def sandboxed(config=None) -> bool:
    """Whether Claude runs in bwclaude."""
    return sandbox(config) == BWCLAUDE


def init(out=print, tiled_uri=None, tiled_path=None, refresh_assistant=False,
         settings: str = SETTINGS, tables: str = TABLES, prog: str = "nbs-agents") -> int:
    """
    nbs-agents' init, then the assistant's files.

    Parameters
    ----------
    out : callable, optional
        Where messages go.
    tiled_uri, tiled_path : str, optional
        This beamline's Tiled server and run catalog.
    refresh_assistant : bool, optional
        Overwrite the assistant's workdir files that differ from the template
        (old copies kept as ``*.bak``).
    settings, tables : str, optional
        Text added to the ``[nbs_cli]`` table and appended to a new config.
    prog : str, optional
        The program to name in the closing message.
    """
    from .agent import prepare_workdir
    from .auth import ensure_token

    status = core.init(out, tiled_uri=tiled_uri, tiled_path=tiled_path, extra_settings=settings, extra_tables=tables)
    p = paths()
    ensure_token(str(p.token))
    prepare_workdir(str(p.assistant), refresh=refresh_assistant)
    out(f"done; run `{prog} check` next")
    return status


def _gateway(path: Path, required: bool) -> tuple[str, str]:
    """Where ``claude-beamline --sandbox bwclaude`` will get the gateway host and key."""
    if all(os.environ.get(k) for k in GATEWAY_KEYS):
        return OK, "AIFAPIM_HOST and AIFAPIM_API_KEY set in the environment"
    missing_state = FAIL if required else WARN
    if not path.exists():
        return missing_state, (f"AIFAPIM_HOST/AIFAPIM_API_KEY not in the environment and no {path} "
                               "(bwclaude needs them; load them before starting the runner)")
    state, detail = private_status(path, 0o600)
    if state != OK:
        return state, f"{path}: {detail}"
    values = {}
    for line in path.read_text().splitlines():
        name, sep, value = line.strip().removeprefix("export ").partition("=")
        if sep:
            values[name.strip()] = value.strip().strip("'\"")
    missing = [k for k in GATEWAY_KEYS if not (os.environ.get(k) or values.get(k))]
    if missing:
        return missing_state, f"{', '.join(missing)} not set in the environment or {path} (bwclaude needs them)"
    return OK, f"from {path} ({detail})"


def check_rows(config_name: str = "assistant config", extra_rows=None) -> list[Row]:
    """
    nbs-agents' checks, then the assistant's.

    Parameters
    ----------
    config_name : str, optional
        Row name for a config that fails to load.
    extra_rows : callable, optional
        ``extra_rows(config)`` returning a beamline package's own rows, added last.
    """
    from ..cli.config import AssistantConfig, CliConfig
    from .launcher import ClaudeLauncher

    p = paths()
    try:
        config = CliConfig.load()
        assistant = AssistantConfig.load()
    except Exception as exc:
        return core.check_rows() + [(FAIL, config_name, f"{type(exc).__name__}: {exc}")]
    rows = core.check_rows(config)

    in_bwclaude = sandboxed(config)
    rows.append(row("gateway", _gateway(Path(os.environ.get("NBS_GATEWAY_ENV") or p.gateway_env), in_bwclaude)))
    if p.token.exists():
        state, detail = private_status(p.token, 0o600)
        rows.append((state, "web token", f"{p.token}, {detail}"))
    else:
        rows.append((WARN, "web token", f"{p.token} (made by nbs-agents-web or init)"))
    rows.append((OK, "drivers", "on: Claude agents may drive the beamline" if config.drivers
                 else "off: assistant only ([nbs_cli] drivers = false)"))
    try:
        launcher = ClaudeLauncher.from_config(config)
        rows.append((OK, "claude_command", " ".join(launcher.resolve_command()) if launcher else "off"))
    except Exception as exc:
        rows.append((FAIL, "claude_command", str(exc)))
    bwclaude = os.environ.get("NBS_BWCLAUDE") or shutil.which(BWCLAUDE)
    if in_bwclaude:
        rows.append((OK if bwclaude else FAIL, "bwclaude", bwclaude or "not found; put it on PATH or set NBS_BWCLAUDE"))
    else:
        rows.append((WARN, "bwclaude", f"off (claude_sandbox = {sandbox(config)!r}): Claude runs without a sandbox"))
    if config.kb_dir:
        rows.append((OK if Path(config.kb_dir).expanduser().is_dir() else WARN, "kb_dir", config.kb_dir))
    if assistant.enabled:
        workdir = Path(assistant.workdir).expanduser()
        rows.append((OK if workdir.is_dir() else WARN, "assistant on", f"workdir {workdir}"))
    if extra_rows is not None:
        rows.extend(extra_rows(config))
    return rows


def check(out=print, config_name: str = "assistant config", extra_rows=None) -> int:
    """Report nbs-agents' and the assistant's checks; 1 if anything failed."""
    return report(check_rows(config_name, extra_rows), out)


def add_assistant_only_argument(parser: argparse.ArgumentParser) -> None:
    """Add ``--assistant-only`` (see ``assistant_only``)."""
    parser.add_argument(
        "--assistant-only", action="store_true",
        help="run the assistant but no Claude agent that drives the beamline (sets NBS_DRIVERS=false)",
    )


def assistant_only() -> None:
    """
    Turn drivers off for this process and everything it starts.

    Sets ``NBS_DRIVERS=false``, which overrides ``[nbs_cli] drivers`` here, in
    the runner's plugin, and in the nbs-cli of any agent it starts (claude-beamline
    passes ``NBS_*`` variables into bwclaude).
    """
    os.environ["NBS_DRIVERS"] = "false"


def run_runner(beamline=None, only_assistant=False) -> int:
    """Start ``nbs-agents-runner`` (with the Claude plugin), optionally assistant-only."""
    from .. import runner

    if only_assistant:
        assistant_only()
    return runner.main(["--beamline", beamline] if beamline else [])


def show_timing(logs: list[str], n: int) -> int:
    """Print where each Claude call's time went, for ``logs`` or the ``n`` newest."""
    from . import timing

    found = [Path(p) for p in logs] or timing.latest_logs(paths().logs, n)
    if not found:
        print(f"No Claude call logs in {paths().logs}")
        return 1
    for path in found:
        print(timing.format_report(path, timing.analyze_file(path)))
    return 0


def main(argv=None, prog: str = "nbs-agents-assistant", description: Optional[str] = None,
         settings: str = SETTINGS, tables: str = TABLES, config_name: str = "assistant config",
         extra_rows=None) -> int:
    """
    The admin command line.

    Parameters
    ----------
    argv : list of str, optional
        Arguments; default ``sys.argv[1:]``.
    prog, description : str, optional
        Program name and one-line description for ``--help``.
    settings, tables : str, optional
        What ``init`` adds to a new config (see ``init``).
    config_name : str, optional
        Row name ``check`` uses for a config that fails to load.
    extra_rows : callable, optional
        A beamline package's own ``check`` rows (see ``check_rows``).
    """
    parser = argparse.ArgumentParser(prog=prog, description=description or __doc__.strip().splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    init_parser = sub.add_parser("init", help="set up nbs-agents and the assistant")
    core.add_init_arguments(init_parser)
    init_parser.add_argument(
        "--refresh-assistant", action="store_true",
        help="overwrite the assistant's workdir files that differ from the template (keeps *.bak)",
    )
    add_beamline_argument(sub.add_parser("check", help="check files, permissions, tools and services"))
    add_beamline_argument(sub.add_parser("paths", help="print where every file lives"))
    sub.add_parser("beamlines", help="list the beamlines set up")
    add_beamline_argument(sub.add_parser("tiled-login", help="log in to Tiled now"))
    runner_parser = sub.add_parser("runner", help="start nbs-agents-runner with the assistant")
    add_beamline_argument(runner_parser)
    add_assistant_only_argument(runner_parser)
    timing_parser = sub.add_parser("timing", help="show where Claude calls spent their time")
    add_beamline_argument(timing_parser)
    timing_parser.add_argument("logs", nargs="*", help="call logs (default: the newest in work/logs)")
    timing_parser.add_argument("-n", type=int, default=1, help="how many of the newest logs (default 1)")
    args = parser.parse_args(argv)
    if args.command == "runner":
        return run_runner(args.beamline, args.assistant_only)
    stop = core.choose_beamline(args)
    if stop is not None:
        return stop
    if args.command == "beamlines":
        return core.show_beamlines()
    if args.command == "init":
        return init(tiled_uri=args.tiled_uri, tiled_path=args.tiled_path, refresh_assistant=args.refresh_assistant,
                    settings=settings, tables=tables, prog=prog)
    if args.command == "paths":
        return core.show_paths()
    if args.command == "tiled-login":
        return core.tiled_login()
    if args.command == "timing":
        return show_timing(args.logs, args.n)
    return check(config_name=config_name, extra_rows=extra_rows)
