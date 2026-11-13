"""
``nbs-cli``: drive a beamline from a terminal or an agent.

Every command prints one JSON document on stdout. Errors print
``{"error": ...}`` and exit with status 1.

``--role assistant`` (or ``NBS_CLI_ROLE=assistant``) makes the client
read-only: only the read verbs, ``post``, ``role``, ``thread`` and
``directive propose`` work.
``NBS_CLI_ROLE=assistant:NAME`` (set by the runner, never by ``--role``) is
the assistant's headless role NAME: read-only too, and ``post`` writes only
that role's record and tile.
"""

import argparse
import ast
import copy
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import numpy as np

from nbs_agents.agentlog import ASSISTANT_POST_KINDS, assistant_uid
from nbs_agents.tiles import SEVERITIES
from .config import ASSISTANT_ROLE, DEFAULT_CATALOG, ROLES, CliConfig
from .core import NO_BEAM_MAP, Beamline, CliError, refuse_read_only
from .kb import KBError, KnowledgeBase
from .refs import (
    NOT_PUBLIC, ReferenceLibrary, RefError, catalogs, open_catalog, refused, resolve_refs, run_location,
    take_snapshot,
)
from nbs_agents.paths import BeamlineError, add_beamline_argument, use_beamline


def parse_value(text: str) -> Any:
    """
    Parse a command-line value as JSON, then a Python literal, then a string.

    Parameters
    ----------
    text : str
        Raw value.

    Returns
    -------
    object
        Parsed value.
    """
    for parser in (json.loads, ast.literal_eval):
        try:
            return parser(text)
        except (ValueError, SyntaxError):
            continue
    return text


def parse_plan_params(tokens: list[str]) -> tuple[list[Any], dict[str, Any]]:
    """
    Split ``value`` and ``key=value`` tokens into args and kwargs.

    Parameters
    ----------
    tokens : list of str
        Tokens after the plan name.

    Returns
    -------
    tuple of (list, dict)
        Positional and keyword arguments.
    """
    args, kwargs = [], {}
    for token in tokens:
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", token, re.S)
        if match:
            kwargs[match.group(1)] = parse_value(match.group(2))
        elif kwargs:
            raise CliError(f"positional value {token!r} after keyword arguments")
        else:
            args.append(parse_value(token))
    return args, kwargs


def parse_duration(text: str) -> float:
    """
    Parse ``90``, ``90s``, ``30m`` or ``2h`` into seconds.

    Parameters
    ----------
    text : str
        Duration.

    Returns
    -------
    float
        Seconds.
    """
    match = re.fullmatch(r"\s*([0-9.]+)\s*([smh]?)\s*", text)
    if not match:
        raise CliError(f"bad duration {text!r}; use e.g. 90s, 30m, 2h")
    value = float(match.group(1))
    return value * {"": 1, "s": 1, "m": 60, "h": 3600}[match.group(2)]


def _json_default(obj: Any) -> Any:
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, np.generic):
        return obj.item()
    return str(obj)


def cwd_path(name: str, flag: str = "--save") -> Path:
    """
    Resolve a file name given to ``flag``, refusing anything outside the current folder.

    Parameters
    ----------
    name : str
        Relative file name.
    flag : str
        Option the name came from, for the error message.

    Returns
    -------
    Path
        Absolute path inside the current folder.

    Raises
    ------
    CliError
        If the name is absolute or resolves outside the current folder.
    """
    cwd = Path.cwd().resolve()
    if Path(name).is_absolute():
        raise CliError(f"{flag} needs a relative path inside {cwd}")
    path = (cwd / name).resolve()
    if path == cwd or cwd not in path.parents:
        raise CliError(f"{flag} needs a relative path inside {cwd}")
    return path


CATALOG_HELP = "Tiled catalog to read: default (this beamline's), or a name from `ref catalogs`"
# Global only: `devices --role` filters devices
ROLE_HELP = "driver (default) or assistant (read-only; default $NBS_CLI_ROLE)"


def build_parser() -> argparse.ArgumentParser:
    """
    Build the argument parser.

    Returns
    -------
    argparse.ArgumentParser
        Parser for all subcommands.
    """
    parser = argparse.ArgumentParser(
        prog="nbs-cli",
        description="Inspect and drive an NBS beamline. Every command prints JSON.",
    )
    parser.add_argument("--config", help="TOML settings file (default $NBS_CLI_CONFIG or the beamline's config/nbs-cli.toml)")
    add_beamline_argument(parser)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--agent-uid",
        default=argparse.SUPPRESS,
        help="agent session to act on (default: $NBS_AGENT_UID, else the newest open session)",
    )
    common.add_argument("--indent", type=int, default=argparse.SUPPRESS, help="JSON indent; 0 for one line")
    parser.add_argument("--agent-uid", help="agent session to act on (default: $NBS_AGENT_UID, else the newest open session)")
    parser.add_argument("--indent", type=int, default=1, help="JSON indent; 0 for one line")
    parser.add_argument("--role", dest="cli_role", choices=ROLES, help=ROLE_HELP)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("state", parents=[common], help="one JSON snapshot of the beamline")
    p.add_argument("--full", action="store_true", help="full queue, samples and agent log")
    p.add_argument("--runs", type=int, default=5, help="number of recent runs; 0 reads nothing from Tiled")

    p = sub.add_parser("beam", parents=[common], help="is beam expected at the endstation, and why not")
    p.add_argument("--map", action="store_true", help="include the beamline map with every node's status")

    p = sub.add_parser("plans", parents=[common], help="allowed plans, or one plan's parameters")
    p.add_argument("name", nargs="?")

    p = sub.add_parser("metadata", parents=[common], help="the metadata the next run will carry (read only)")
    p.add_argument("keys", nargs="*", help="only these keys")

    p = sub.add_parser("samples", parents=[common], help="loaded samples, or where they sit on the holder")
    p.add_argument("--full", action="store_true", help="every field, including position")
    p.add_argument("--layout", action="store_true", help="where samples sit on the holder, as the beamline package lays them out")

    p = sub.add_parser("devices", parents=[common], help="devices (motors, detectors) plans may use")
    p.add_argument("--movable", action="store_true", help="only devices that can be moved")
    p.add_argument("--node", help="only devices on this beam map node")
    p.add_argument("--role", help="only devices with this role")

    p = sub.add_parser("device", parents=[common], help="what devices are: node, roles, description, axes")
    p.add_argument("names", nargs="+", metavar="NAME")

    p = sub.add_parser("diagnose", parents=[common], help="walk the beam map from the endstation to the source")

    p = sub.add_parser("run", parents=[common], help="details of a run (uid, uid prefix, scan id, or -1 for latest)")
    p.add_argument("ref", nargs="?", default="-1")
    p.add_argument("--data", action="store_true", help="include primary-stream arrays")
    p.add_argument("--plot", metavar="PNG", help="save a plot to this file")
    p.add_argument(
        "--save", metavar="FILE", help="write the run with its data as JSON to FILE (relative, inside the current folder)"
    )
    p.add_argument("--attach", action="store_true", help="plot the run into the figure store; prints the att: link")
    p.add_argument("--alt", default="", help="figure description for --attach (default: scan id, plan, sample)")
    p.add_argument("--catalog", default=DEFAULT_CATALOG, help=CATALOG_HELP)

    p = sub.add_parser("runs", parents=[common], help="summaries of recent runs")
    p.add_argument("-n", type=int, default=10)
    p.add_argument("--mine", action="store_true", help="only runs from the current session")
    p.add_argument("--no-summary", action="store_true", help="skip reading data")
    p.add_argument("--catalog", default=DEFAULT_CATALOG, help=CATALOG_HELP)

    p = sub.add_parser("wait", parents=[common], help="block until a run finishes or the queue changes")
    p.add_argument("--timeout", type=float, default=600)
    p.add_argument("--until", choices=("run", "idle", "change"), default="run")

    p = sub.add_parser("open", parents=[common], help="open an agent session block in the queue")
    p.add_argument("--goal", required=True)
    p.add_argument("--budget", help="time budget, e.g. 2h or 45m")
    p.add_argument("--no-start", action="store_true", help="do not start the queue")

    p = sub.add_parser("add", parents=[common], help="add a plan before this session's queue_stop")
    p.add_argument("plan")
    p.add_argument("params", nargs="*", help="positional values and key=value pairs (JSON or Python literals)")
    p.add_argument("--reason", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-start", action="store_true", help="do not start the queue")

    p = sub.add_parser("remove", parents=[common], help="remove a pending item from this session's block")
    p.add_argument("item_uid")
    p.add_argument("--reason", required=True)

    p = sub.add_parser("abort", parents=[common], help="abort this session's running plan")
    p.add_argument("--reason", required=True)
    p.add_argument("--resume", action="store_true", help="restart the queue for this session's pending items")

    p = sub.add_parser("note", parents=[common], help="append a note to the agent log")
    p.add_argument("text")

    p = sub.add_parser("attach", parents=[common], help="store a figure; prints the ![alt](att:ID) link for a message")
    p.add_argument("path", help="PNG, JPEG, GIF, WebP or SVG file")
    p.add_argument("--alt", required=True, help="one-line description of the figure")
    p.add_argument("--run", dest="run_uid", help="uid of the run the figure is about")

    p = sub.add_parser("ask", parents=[common], help="ask the operator a question and show the session as awaiting input")
    p.add_argument("text")

    p = sub.add_parser("finalize", parents=[common], help="end the session and let the queue reach close_agent")
    summary = p.add_mutually_exclusive_group(required=True)
    summary.add_argument("--summary", help="final summary text")
    summary.add_argument(
        "--summary-file", metavar="FILE", help="read the summary from FILE (relative, inside the current folder)"
    )
    p.add_argument("--drop-pending", action="store_true", help="remove pending block items first")

    p = sub.add_parser("tell", parents=[common], help="operator: send a message to a running agent")
    p.add_argument("text")

    p = sub.add_parser("ask-assistant", parents=[common],
                       help="operator: ask the beamline assistant a question (each question opens its own thread)")
    p.add_argument("text", nargs="?", default="", help="the question; with --about, what to look at in particular")
    p.add_argument("--about", metavar="POST",
                   help="re-check one of today's assistant posts with a fresh call that does not see it: "
                   "its time (HH:MM, HH:MM:SS or the epoch seconds `log` shows) or 'last'")
    p.add_argument("--model", help="with --about: the model for the re-check (default: the assistant's alert_model)")
    about = p.add_mutually_exclusive_group()
    about.add_argument("--thread", metavar="ID", help="a follow-up in this thread (`thread list`)")
    about.add_argument("--entry", metavar="POST", help="ask about one of today's assistant posts (as --about)")
    about.add_argument("--tile", metavar="ROLE", help="ask about this role's tile")

    p = sub.add_parser("thread", parents=[common], help="operator and assistant: the assistant's question threads")
    thread = p.add_subparsers(dest="thread_command", required=True)
    q = thread.add_parser("list", parents=[common], help="threads, newest first")
    q.add_argument("--since", help="active since then: HH:MM, HH:MM:SS, epoch seconds, or an age like 2h")
    q = thread.add_parser("show", parents=[common], help="one thread with its messages")
    q.add_argument("id")

    p = sub.add_parser("directive", parents=[common],
                       help="standing instructions to an assistant role: the manager proposes, a person confirms")
    directive = p.add_subparsers(dest="directive_command", required=True)
    q = directive.add_parser("propose", parents=[common], help="assistant chat role: propose a directive for a role")
    q.add_argument("role")
    text = q.add_mutually_exclusive_group(required=True)
    text.add_argument("text", nargs="?")
    text.add_argument("--file", metavar="FILE", help="read the text from FILE (relative, inside the current folder)")
    q.add_argument("--expires", default="beamtime",
                   help="beamtime (default: until the proposal changes), a duration (12h, 3d) or a date (YYYY-MM-DD)")
    q = directive.add_parser("list", parents=[common], help="proposed and active directives")
    q.add_argument("--role", dest="directive_role", help="only this role's")
    q.add_argument("--all", action="store_true", help="also rejected, expired and revoked ones")
    for action, what in (("confirm", "confirm a proposed directive"), ("reject", "reject a proposed directive"),
                         ("revoke", "end an active directive now")):
        q = directive.add_parser(action, parents=[common], help=f"operator: {what} (logged)")
        q.add_argument("id")
        if action != "confirm":
            q.add_argument("--reason", default="")
    p = sub.add_parser("stop", parents=[common], help="operator: ask a running agent to wrap up")
    p.add_argument("--reason", default="")

    p = sub.add_parser("log", parents=[common], help="agent-log records")
    p.add_argument("--all", action="store_true", help="every agent session, not just the current one")
    p.add_argument("--tail", type=int, default=20, help="entries per session")

    p = sub.add_parser("post", parents=[common], help="assistant: post to the role's record for today, or its tile")
    where = p.add_mutually_exclusive_group(required=True)
    where.add_argument("--kind", choices=ASSISTANT_POST_KINDS, help="post an entry of this kind to the record")
    where.add_argument("--tile", action="store_true", help="post to the role's tile instead (needs --severity)")
    p.add_argument("--severity", choices=SEVERITIES,
                   help="with --tile; an alert stays on the tile until a person acknowledges it")
    p.add_argument("--figure", metavar="ATT", help="with --tile: a figure, as att:ID from `nbs-cli attach`")
    text = p.add_mutually_exclusive_group(required=True)
    text.add_argument("text", nargs="?")
    text.add_argument(
        "--file", metavar="FILE",
        help="read the text from FILE (relative, inside the current folder), so Markdown, "
        "$math$ and backticks need no shell quoting",
    )
    p.add_argument("--runs", nargs="+", default=[], metavar="UID", help="run uids the post is about")

    p = sub.add_parser("ack", parents=[common], help="operator: acknowledge an assistant role's alert (logged)")
    p.add_argument("role")

    p = sub.add_parser("role", parents=[common], help="assistant chat role: read the other roles, or ask one")
    role = p.add_subparsers(dest="role_command", required=True)
    role.add_parser("list", parents=[common], help="every role with its settings, tile and status")
    q = role.add_parser("show", parents=[common], help="a role's tile, today's record and notebook")
    q.add_argument("name")
    q.add_argument("--since", help="entries from then on: HH:MM, HH:MM:SS, epoch seconds, or an age like 30m or 2h")
    q.add_argument("--tail", type=int, default=20, help="most entries (0 for all)")
    q = role.add_parser("request", parents=[common],
                        help="ask a headless role to do something; its answer comes back as a reply entry")
    q.add_argument("name")
    q.add_argument("text")
    q.add_argument("--runs", nargs="+", default=[], metavar="UID", help="run uids the request is about")

    p = sub.add_parser("kb", parents=[common], help="beamline knowledge base: search, read, add and append notes")
    kb = p.add_subparsers(dest="kb_command", required=True)
    filters = argparse.ArgumentParser(add_help=False)
    filters.add_argument("--device", help="only notes tagged with this device")
    filters.add_argument("--node", help="only notes tagged with this beam-map node")
    filters.add_argument("--topic", help="only notes with this topic")
    filters.add_argument("--by", choices=("human", "agent"), help="only notes written by humans or by agents")
    q = kb.add_parser("search", parents=[common, filters], help="notes matching the words, those with all of them first")
    q.add_argument("words", nargs="*", help="words to look for")
    q.add_argument("-n", type=int, default=10, help="most hits (default 10)")
    q = kb.add_parser("list", parents=[common, filters], help="every note passing the filters")
    q.add_argument("--drafts", action="store_true", help="drafts waiting for staff review instead")
    q = kb.add_parser("index", parents=[common, filters],
                      help="every title, and every topic, device, node and tag with the notes using it")
    q.add_argument("--only", nargs="+", choices=INDEX_PARTS, metavar="PART",
                   help=f"just these parts: {', '.join(INDEX_PARTS)}")
    q.add_argument("--names", action="store_true",
                   help="just the values (titles, or topic/device/node/tag names), without note IDs")
    q = kb.add_parser("show", parents=[common], help="a note's full text and appendix")
    q.add_argument("id")
    content = argparse.ArgumentParser(add_help=False)
    content.add_argument("--text", help="Markdown text")
    content.add_argument("--file", help="read the text from this file (relative, in the current folder)")
    tags = argparse.ArgumentParser(add_help=False)
    tags.add_argument("--topic", help="e.g. hardware, problem, calibration, quirk")
    for key in ("devices", "nodes", "tags"):
        tags.add_argument(f"--{key}", help=f"comma-separated {key}")
    q = kb.add_parser("add", parents=[common, content, tags], help="write a new agent note")
    q.add_argument("--title", required=True)
    q.add_argument("--draft", action="store_true",
                   help="a draft for staff to review in nbs-agents-web; not searchable until approved")
    q = kb.add_parser("edit", parents=[common, content, tags], help="change an agent note (human notes are append-only)")
    q.add_argument("id")
    q.add_argument("--title")
    q = kb.add_parser("append", parents=[common, content], help="append an attributed section to any note")
    q.add_argument("id")

    p = sub.add_parser("ref", parents=[common],
                       help="engineering reference scans: past runs that show part of the beamline working")
    ref = p.add_subparsers(dest="ref_command", required=True)
    rfilters = argparse.ArgumentParser(add_help=False)
    rfilters.add_argument("--device", help="only references tagged with this device")
    rfilters.add_argument("--node", help="only references tagged with this beam-map node")
    rfilters.add_argument("--tag", help="only references with this tag")
    rfilters.add_argument("--all", action="store_true", help="superseded and retired references too")
    q = ref.add_parser("list", parents=[common, rfilters], help="current references passing the filters")
    q.add_argument("--drafts", action="store_true", help="drafts waiting for staff review instead")
    q = ref.add_parser("search", parents=[common, rfilters], help="references matching the words, best first")
    q.add_argument("words", nargs="*", help="words to look for")
    q.add_argument("-n", type=int, default=10, help="most hits (default 10)")
    q = ref.add_parser("show", parents=[common], help="a reference, its snapshot, and whether its scan still resolves")
    q.add_argument("id")
    ref.add_parser("verify", parents=[common], help="check that every reference's scan still resolves in Tiled")
    q = ref.add_parser("create", parents=[common, content],
                       help="draft a reference from a run, for staff to review (agents only write drafts)")
    q.add_argument("run", help="uid, uid prefix, scan id, or -1 for the latest run")
    q.add_argument("--title", required=True)
    q.add_argument("--catalog", default=DEFAULT_CATALOG, help=CATALOG_HELP)
    for key in ("devices", "nodes", "tags", "aliases"):
        q.add_argument(f"--{key}", help=f"comma-separated {key}" + (
            " (default: the run's motors and detectors)" if key == "devices" else ""))
    q.add_argument("--request", default="", help="ID of the staff request this draft answers")
    q = ref.add_parser("edit", parents=[common, content],
                       help="change a draft you wrote (never an approved reference, never its snapshot)")
    q.add_argument("id")
    q.add_argument("--title")
    for key in ("devices", "nodes", "tags", "aliases"):
        q.add_argument(f"--{key}", help=f"comma-separated {key}")
    ref.add_parser("catalogs", parents=[common], help="Tiled catalogs runs can be read from, with their URI and path")
    return parser


INDEX_PARTS = ("titles", "topics", "devices", "nodes", "tags")


def trim_index(index: dict[str, Any], only: list[str] | None, names: bool) -> dict[str, Any]:
    """
    Narrow ``kb index`` output.

    Parameters
    ----------
    index : dict
        ``KnowledgeBase.index`` result.
    only : list of str or None
        Parts to keep (``INDEX_PARTS``); all when None. ``titles`` is the note list.
    names : bool
        Shorten each part: titles become ``{id: title}``, and topics, devices,
        nodes and tags plain lists of values.

    Returns
    -------
    dict
        The narrowed index; ``counts`` is kept only when no parts are chosen.
    """
    out = {"titles": index["notes"], **{k: index[k] for k in INDEX_PARTS[1:]}}
    if only:
        out = {k: out[k] for k in INDEX_PARTS if k in only}
    else:
        out["counts"] = index["counts"]
    if names:
        for key in INDEX_PARTS:
            if key in out:
                out[key] = {n["id"]: n["title"] for n in out[key]} if key == "titles" else list(out[key])
    return out


def kb_text(args: argparse.Namespace, required: bool = True, what: str = "note text") -> str | None:
    """The ``--text`` or ``--file`` content of a ``kb`` or ``ref`` write."""
    if args.text is not None and args.file:
        raise CliError("give --text or --file, not both")
    if args.file:
        path = cwd_path(args.file, "--file")
        try:
            return path.read_text()
        except OSError as exc:
            raise CliError(f"cannot read --file {args.file}: {exc.strerror}") from exc
    if args.text is None and required:
        raise CliError(f"give the {what} with --text or --file")
    return args.text


def kb_author(config: CliConfig, uid: str | None) -> str:
    """How a ``kb`` write is attributed: the role, plus the session for a driver."""
    if config.read_only:
        return config.role if config.assistant_role else ASSISTANT_ROLE
    return f"{config.role} {uid[:8]}" if uid else config.role


def split_list(text: str | None) -> list[str] | None:
    return None if text is None else [v.strip() for v in text.split(",") if v.strip()]


def run_kb(kb: KnowledgeBase, config: CliConfig, args: argparse.Namespace) -> Any:
    """
    Execute a ``kb`` subcommand. It needs no beamline connection.

    Parameters
    ----------
    kb : KnowledgeBase
        Notes folders.
    config : CliConfig
        Settings, for the writer's role.
    args : argparse.Namespace
        Parsed arguments.

    Returns
    -------
    object
        JSON-serializable result.
    """
    cmd = args.kb_command
    uid = args.agent_uid or os.environ.get("NBS_AGENT_UID") or None
    if kb.human_dir is not None and not kb.human_dir.is_dir():
        # Otherwise every search looks like an empty knowledge base
        print(f"nbs-cli: warning: kb_dir {kb.human_dir} is not a folder here; only agent notes are searched",
              file=sys.stderr)
    try:
        if cmd in ("search", "list", "index"):
            filters = {"device": args.device, "node": args.node, "topic": args.topic, "written_by": args.by}
            if cmd == "list":
                return [n.summary() for n in kb.select(kb.drafts() if args.drafts else None, **filters)]
            if cmd == "index":
                return trim_index(kb.index(**filters), args.only, args.names)
            return kb.search(" ".join(args.words), n=args.n, **filters)
        if cmd == "show":
            return kb.get(args.id).full()
        author = kb_author(config, uid)
        if cmd == "append":
            return kb.append(args.id, kb_text(args), author)
        lists = {key: split_list(getattr(args, key)) for key in ("devices", "nodes", "tags")}
        if cmd == "add":
            return kb.add(args.title, kb_text(args), author, topic=args.topic or "", draft=args.draft, **lists)
        if cmd == "edit":
            return kb.edit(args.id, author, body=kb_text(args, required=False), title=args.title,
                           topic=args.topic, **lists)
    except KBError as exc:
        raise CliError(str(exc)) from exc
    raise CliError(f"unknown kb command {cmd}")


def add_note_refs(kb: KnowledgeBase, cmd: str, result: Any, refs: ReferenceLibrary | None = None) -> Any:
    """Attach the IDs of tagged notes (``notes``) and current reference scans
    (``references``) to ``device`` and ``diagnose`` output."""
    sources = [("notes", kb)] + ([("references", refs)] if refs is not None else [])
    for key, source in sources:
        try:
            if cmd == "device" and isinstance(result, dict):
                for name, info in result.items():
                    found = source.refs(device=name)
                    if found and isinstance(info, dict):
                        info[key] = found
            elif cmd == "diagnose" and isinstance(result, dict):
                for entry in result.get("path", []):
                    found = source.refs(node=entry.get("node"))
                    if found:
                        entry[key] = found
                    for dev in entry.get("devices", []):
                        found = source.refs(device=dev.get("name"))
                        if found:
                            dev[key] = found
        except (KBError, RefError, OSError):
            pass  # a broken note or reference must not hide the beamline view
    return result


def run_in_catalog(bl: Beamline, config: CliConfig, name: str) -> tuple[Beamline, dict[str, str]]:
    """
    A beamline client reading runs from the named catalog.

    Returns
    -------
    tuple of (Beamline, dict)
        ``bl`` itself for ``default``, otherwise a copy whose ``tiled`` is the
        other catalog; and the catalog's ``uri`` and ``path``.
    """
    found = catalogs(config)
    if name not in found:
        raise CliError(f"no catalog {name!r}; have {', '.join(found)} ([references.catalogs] in nbs-cli.toml)")
    if name == DEFAULT_CATALOG:
        return bl, found[name]
    # Share the beamline info, so closing bl closes it once
    bl.info  # noqa: B018
    view = copy.copy(bl)
    try:
        view._tiled = open_catalog(config, found[name]["uri"], found[name]["path"])
    except RefError as exc:
        raise CliError(f"catalog {name}: {exc}") from exc
    return view, found[name]


def get_run(bl: Beamline, ref: str, catalog: str):
    """``bl.get_run``, saying for another catalog that a missing run may just not be public."""
    try:
        return bl.get_run(ref)
    except CliError as exc:
        if catalog == DEFAULT_CATALOG:
            raise
        raise CliError(f"{exc} in catalog {catalog}. {NOT_PUBLIC}") from exc
    except Exception as exc:
        if catalog != DEFAULT_CATALOG and refused(exc):
            raise CliError(f"Tiled refused to read {ref} in catalog {catalog}. {NOT_PUBLIC}") from exc
        raise


def run_ref(lib: ReferenceLibrary, config: CliConfig, args: argparse.Namespace, beamline) -> Any:
    """
    Execute a ``ref`` subcommand. Only ``create``, ``show`` and ``verify`` read Tiled.

    Parameters
    ----------
    lib : ReferenceLibrary
        Reference folders.
    config : CliConfig
        Settings: the writer's role, and the catalogs.
    args : argparse.Namespace
        Parsed arguments.
    beamline : callable
        Returns the ``Beamline`` client, made on first use.

    Returns
    -------
    object
        JSON-serializable result.
    """
    cmd = args.ref_command
    if lib.human_dir is not None and not lib.human_dir.is_dir():
        print(f"nbs-cli: warning: ref_dir {lib.human_dir} is not a folder here; only drafts are listed",
              file=sys.stderr)
    try:
        if cmd in ("list", "search"):
            filters = {"device": args.device, "node": args.node, "tag": args.tag, "all_statuses": args.all}
            if cmd == "list":
                return [e.summary() for e in lib.select(lib.drafts() if args.drafts else None, **filters)]
            return lib.search(" ".join(args.words), n=args.n, **filters)
        if cmd == "catalogs":
            return catalogs(config)
        if cmd == "show":
            entry = lib.get(args.id)
            out = entry.full()
            check = resolve_refs(config, [entry])[0]
            check.pop("id")
            out["scan"] = check
            return out
        if cmd == "verify":
            entries = lib.entries() + lib.drafts()
            checked = resolve_refs(config, entries)
            titles = {e.id: e.title for e in entries}
            bad = [dict(c, title=titles[c["id"]]) for c in checked if not c.get("resolves")]
            return {"checked": len(checked), "resolve": len(checked) - len(bad), "problems": bad}
        if cmd == "create":
            uid = args.agent_uid or os.environ.get("NBS_AGENT_UID") or None
            text = kb_text(args, what="description")
            view, where = run_in_catalog(beamline(), config, args.catalog)
            run = get_run(view, args.run, args.catalog)
            location = run_location(args.catalog, where, run)
            snapshot = take_snapshot(view, run)
            lists = {key: split_list(getattr(args, key)) for key in ("devices", "nodes", "tags", "aliases")}
            if not lists["devices"]:
                start = snapshot["start"]
                lists["devices"] = list(dict.fromkeys((start.get("motors") or []) + (start.get("detectors") or [])))
            return lib.create(args.title, text, kb_author(config, uid), location, snapshot,
                              request=args.request, **lists)
        if cmd == "edit":
            uid = args.agent_uid or os.environ.get("NBS_AGENT_UID") or None
            lists = {key: split_list(getattr(args, key)) for key in ("devices", "nodes", "tags", "aliases")}
            return lib.edit_draft(args.id, kb_author(config, uid), title=args.title,
                                  body=kb_text(args, required=False, what="description"), **lists)
    except (KBError, RefError) as exc:
        raise CliError(str(exc)) from exc
    raise CliError(f"unknown ref command {cmd}")


def run_command(bl: Beamline, args: argparse.Namespace) -> Any:
    """
    Execute a parsed command.

    Parameters
    ----------
    bl : Beamline
        Beamline client.
    args : argparse.Namespace
        Parsed arguments.

    Returns
    -------
    object
        JSON-serializable result.
    """
    cmd = args.command
    # A Claude launched by nbs-agents-runner gets its session through NBS_AGENT_UID
    uid = args.agent_uid or os.environ.get("NBS_AGENT_UID") or None
    if cmd == "state":
        return bl.state(full=args.full, agent_uid=uid, n_runs=args.runs)
    if cmd == "beam":
        out = bl.beam(full=args.map) or dict(NO_BEAM_MAP)
        try:
            status = bl.status_pvs()
        except Exception as exc:  # the beam map still answers without EPICS
            status = {"error": f"{type(exc).__name__}: {exc}"}
        if status:
            out["status"] = status
        return out
    if cmd == "plans":
        return bl.plan_info(args.name) if args.name else bl.plans_overview()
    if cmd == "metadata":
        return bl.metadata(args.keys or None)
    if cmd == "samples":
        return bl.sample_layout() if args.layout else bl.samples(full=args.full)
    if cmd == "devices":
        return bl.devices(movable_only=args.movable, node=args.node, role=args.role)
    if cmd == "device":
        return bl.device(args.names)
    if cmd == "diagnose":
        return bl.diagnose()
    if cmd in ("run", "runs"):
        bl, _ = run_in_catalog(bl, bl.config, args.catalog)
    if cmd == "run":
        run = get_run(bl, args.ref, args.catalog)
        out = bl.run_summary(run)
        if args.data:
            out["data"] = bl.run_data(run)
        if args.plot:
            out["plot"] = bl.plot_run(run, args.plot)
        if args.attach:
            out["attachment"] = bl.attach_run_plot(run, alt=args.alt)
        if args.save:
            path = cwd_path(args.save)
            path.parent.mkdir(parents=True, exist_ok=True)
            saved = dict(out)
            saved.setdefault("data", bl.run_data(run))
            path.write_text(json.dumps(saved, indent=1, default=_json_default))
            out.pop("data", None)
            out["saved"] = str(path)
        return out
    if cmd == "runs":
        agent = None
        if args.mine:
            record = bl.find_session(uid)
            if record is None:
                raise CliError("no open session")
            agent = record["agent_uid"]
        return bl.recent_runs(args.n, agent_uid=agent, data=not args.no_summary)
    if cmd == "wait":
        return bl.wait(timeout=args.timeout, until=args.until, agent_uid=uid)
    if cmd == "open":
        if uid:
            raise CliError(
                f"session {uid} is already open for this agent (NBS_AGENT_UID or --agent-uid); "
                "use it instead of opening another"
            )
        budget = parse_duration(args.budget) if args.budget else None
        return bl.open_session(args.goal, budget_s=budget, start=not args.no_start)
    if cmd == "add":
        plan_args, plan_kwargs = parse_plan_params(args.params)
        return bl.add(
            args.plan,
            plan_args,
            plan_kwargs,
            args.reason,
            dry_run=args.dry_run,
            start=not args.no_start,
            agent_uid=uid,
        )
    if cmd == "remove":
        return bl.remove(args.item_uid, args.reason, agent_uid=uid)
    if cmd == "abort":
        return bl.abort(args.reason, resume=args.resume, agent_uid=uid)
    if cmd == "note":
        return bl.note(args.text, agent_uid=uid)
    if cmd == "attach":
        return bl.attach(args.path, alt=args.alt, run_uid=args.run_uid)
    if cmd == "ask":
        return bl.ask(args.text, agent_uid=uid)
    if cmd == "finalize":
        summary = args.summary
        if args.summary_file:
            path = cwd_path(args.summary_file, "--summary-file")
            try:
                summary = path.read_text()
            except OSError as exc:
                raise CliError(f"cannot read --summary-file {args.summary_file}: {exc.strerror}") from exc
        return bl.finalize(summary, drop_pending=args.drop_pending, agent_uid=uid)
    if cmd == "ask-assistant":
        if args.about:
            if args.thread or args.entry or args.tile:
                raise CliError("--about is a re-check; --thread, --entry and --tile are for questions")
            return bl.recheck(args.about, model=args.model, text=args.text)
        if args.model:
            raise CliError("--model goes with --about (a re-check); a question uses the assistant's alert_model")
        return bl.ask_assistant(args.text, thread=args.thread, entry=args.entry, tile=args.tile)
    if cmd == "thread":
        if args.thread_command == "list":
            return bl.thread_list(since=args.since)
        return bl.thread_show(args.id)
    if cmd == "directive":
        if args.directive_command == "propose":
            text = args.text
            if args.file:
                path = cwd_path(args.file, "--file")
                try:
                    text = path.read_text()
                except OSError as exc:
                    raise CliError(f"cannot read --file {args.file}: {exc.strerror}") from exc
            return bl.directive_propose(args.role, text, expires=args.expires)
        if args.directive_command == "list":
            return bl.directive_list(role=args.directive_role, all_=args.all)
        return bl.directive_decide(args.id, args.directive_command, reason=getattr(args, "reason", ""))
    if cmd in ("tell", "stop"):
        refuse_read_only(bl.config, cmd)
        record = bl.find_session(uid)
        if record is None:
            raise CliError("no open session")
        if cmd == "tell":
            return bl.agent_log.message(record["agent_uid"], args.text)
        return bl.agent_log.request_stop(record["agent_uid"], args.reason)
    if cmd == "post":
        text = args.text
        if args.file:
            path = cwd_path(args.file, "--file")
            try:
                text = path.read_text()
            except OSError as exc:
                raise CliError(f"cannot read --file {args.file}: {exc.strerror}") from exc
        if args.tile and not args.severity:
            raise CliError("--tile needs --severity (ok, info, warn or alert)")
        if not args.tile and (args.severity or args.figure):
            raise CliError("--severity and --figure go with --tile")
        return bl.post(args.kind, text, runs=args.runs, tile=args.tile, severity=args.severity, figure=args.figure)
    if cmd == "ack":
        return bl.ack(args.role)
    if cmd == "role":
        if args.role_command == "list":
            return bl.role_list()
        if args.role_command == "show":
            return bl.role_show(args.name, since=args.since, tail=args.tail)
        return bl.role_request(args.name, args.text, runs=args.runs)
    if cmd == "log":
        if args.all:
            records = bl.visible_records()
        elif bl.config.read_only and not uid:
            own = assistant_uid(role=bl.config.assistant_role)
            record = bl.agent_log.get(own)
            records = {own: record} if record else {}
        else:
            record = bl.find_session(uid)
            records = {record["agent_uid"]: record} if record else {}
        for record in records.values():
            record["log"] = record.get("log", [])[-args.tail:] if args.tail else record.get("log", [])
        return records
    raise CliError(f"unknown command {cmd}")


SANDBOX_HINT = (
    "This nbs-cli call ran inside Claude Code's Bash sandbox, which has no network (even localhost is "
    "the sandbox's own), so the beamline Redis, the queue server, Tiled and the agent log are all out of "
    "reach; the service itself may be fine. Only a bare `nbs-cli ...` command line is let out: no pipes, "
    "redirects, `$(...)` or backticks, not even inside quotes. Put post text in a file and use --file."
)


def _is_connection_error(exc: BaseException) -> bool:
    names = {cls.__name__ for cls in type(exc).__mro__}
    return isinstance(exc, OSError) or bool(names & {"ConnectionError", "TimeoutError", "ProxyError", "ZMQError"})


def error_text(exc: BaseException, config: Any = None) -> str:
    """
    The ``error`` text for an unexpected exception.

    Connection errors name the service they were for, and say so when the
    call ran inside Claude Code's sandbox (``SANDBOX_RUNTIME`` is set there).

    Parameters
    ----------
    exc : BaseException
        What went wrong.
    config : CliConfig, optional
        Settings, to name the service from its address.

    Returns
    -------
    str
        One line for ``{"error": ...}``.
    """
    text = f"{type(exc).__name__}: {exc}"
    if not _is_connection_error(exc):
        return text
    if config is not None:
        services = []
        try:
            if config.agent_log.dedicated:
                services.append(("the agent log, served by the runner", config.agent_log_settings))
        except Exception:
            pass
        for name, settings in services:
            address = f"{settings.get('host')}:{settings.get('port')}"
            if address in text:
                text += f" ({address} is {name})"
                break
    if os.environ.get("SANDBOX_RUNTIME"):
        text += ". " + SANDBOX_HINT
    return text


def main(argv: list[str] | None = None) -> int:
    """
    Entry point for ``nbs-cli``.

    Parameters
    ----------
    argv : list of str, optional
        Arguments; defaults to ``sys.argv[1:]``.

    Returns
    -------
    int
        Exit status.
    """
    args = build_parser().parse_args(argv)
    indent = args.indent or None
    bl = None
    config = None
    try:
        try:
            use_beamline(args.beamline)
        except BeamlineError as exc:
            if not (args.config or os.environ.get("NBS_CLI_CONFIG")):
                raise CliError(str(exc))
        config = CliConfig.load(args.config)
        if args.cli_role:
            if config.read_only and args.cli_role != ASSISTANT_ROLE:
                raise CliError("this client is set to the read-only assistant role; --role cannot lift it")
            if config.assistant_role and args.cli_role != config.role:
                raise CliError(f"this client is the assistant's {config.assistant_role} role; --role cannot change it")
            config.role = args.cli_role
        kb = KnowledgeBase(config.kb_dir, config.kb_agent_dir, config.kb_review_dir)
        refs = ReferenceLibrary(config.ref_dir, config.ref_agent_dir, config.ref_review_dir)
        if args.command == "kb":
            result = run_kb(kb, config, args)
        elif args.command == "ref":
            def beamline() -> Beamline:
                nonlocal bl
                if bl is None:
                    bl = Beamline(config, config_path=args.config)
                return bl
            result = run_ref(refs, config, args, beamline)
        else:
            bl = Beamline(config, config_path=args.config)
            result = add_note_refs(kb, args.command, run_command(bl, args), refs)
        if result is None:
            # Success never prints a bare null, which reads like a silent failure
            result = {"result": None, "note": f"{args.command} succeeded with nothing to report"}
        status = 0
    except CliError as exc:
        result, status = {"error": str(exc)}, 1
    except Exception as exc:  # report connection problems as JSON too
        result, status = {"error": error_text(exc, config)}, 1
    finally:
        if bl is not None:
            bl.close()
    json.dump(result, sys.stdout, indent=indent, default=_json_default)
    sys.stdout.write("\n")
    return status


if __name__ == "__main__":
    sys.exit(main())
