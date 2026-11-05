"""
Where a Claude call's time went, from its stream-json log.

The runner keeps one log per Claude call (``claude-<agent>-<date>-<time>.log``
under the beamline's ``work/logs``). Assistant and tool-result messages carry
timestamps, so the wall time splits into startup (launch to the first model
output), model time (waiting on the API with no tool running) and tool time
(a tool call running), with each tool call's own duration.
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

LAUNCH_RE = re.compile(r"-(\d{8}-\d{6})\.log$")


def _when(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _launch_time(path: Path) -> Optional[datetime]:
    """The launch time in the log's name (local time), as an aware datetime."""
    match = LAUNCH_RE.search(path.name)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y%m%d-%H%M%S").astimezone()


def _tool_label(block: dict[str, Any]) -> str:
    name = block.get("name", "?")
    args = block.get("input") or {}
    if name == "Bash":
        return " ".join(str(args.get("command", "")).split())[:80]
    for key in ("file_path", "pattern", "skill", "path"):
        if key in args:
            return f"{name} {args[key]}"[:80]
    return name


def analyze(lines: Iterable[str], launched: Optional[datetime] = None) -> dict[str, Any]:
    """
    Split one Claude call's time from its stream-json lines.

    Parameters
    ----------
    lines : iterable of str
        The log's lines; lines that are not JSON are skipped.
    launched : datetime, optional
        When the call was started, for the startup time.

    Returns
    -------
    dict
        ``startup_s``, ``model_s``, ``tools_s`` (wall time with a tool
        running), ``tools`` (each call's ``label`` and ``seconds``, in order),
        ``api_retries``, and from the final result ``total_s``, ``api_s``,
        ``turns`` and ``cost_usd`` when present.
    """
    events = []  # (time, kind, ids): kind "issue" with tool ids, "result" with ids, "model"
    labels: dict[str, str] = {}
    retries = 0
    result: dict[str, Any] = {}
    for line in lines:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        kind = msg.get("type")
        if kind == "system" and msg.get("subtype") == "api_retry":
            retries += 1
        elif kind == "result":
            result = msg
        if "timestamp" not in msg:
            continue
        when = _when(msg["timestamp"])
        content = (msg.get("message") or {}).get("content") or []
        if not isinstance(content, list):
            content = []
        if kind == "assistant":
            ids = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    labels[block["id"]] = _tool_label(block)
                    ids.append(block["id"])
            events.append((when, "issue" if ids else "model", ids))
        elif kind == "user":
            ids = [
                b.get("tool_use_id")
                for b in content
                if isinstance(b, dict) and b.get("type") == "tool_result"
            ]
            events.append((when, "result", ids))

    events.sort(key=lambda e: e[0])
    out: dict[str, Any] = {"startup_s": None, "model_s": 0.0, "tools_s": 0.0, "tools": [], "api_retries": retries}
    if events and launched is not None:
        out["startup_s"] = max(0.0, (events[0][0] - launched).total_seconds())
    issued: dict[str, datetime] = {}
    previous = events[0][0] if events else None
    for when, kind, ids in events:
        gap = (when - previous).total_seconds()
        # Time ending in a tool result, or passing while tools run, is tool time
        if issued:
            out["tools_s"] += gap
        else:
            out["model_s"] += gap
        if kind == "issue":
            for tool_id in ids:
                issued[tool_id] = when
        elif kind == "result":
            for tool_id in ids:
                start = issued.pop(tool_id, None)
                if start is not None:
                    out["tools"].append(
                        {"label": labels.get(tool_id, "?"), "seconds": (when - start).total_seconds()}
                    )
        previous = when
    if result:
        if "duration_ms" in result:
            out["total_s"] = result["duration_ms"] / 1000
        if "duration_api_ms" in result:
            out["api_s"] = result["duration_api_ms"] / 1000
        if "num_turns" in result:
            out["turns"] = result["num_turns"]
        if "total_cost_usd" in result:
            out["cost_usd"] = result["total_cost_usd"]
    return out


def analyze_file(path: Path) -> dict[str, Any]:
    """``analyze`` one log file, taking the launch time from its name."""
    with open(path, errors="replace") as f:
        return analyze(f, launched=_launch_time(path))


def format_report(path: Path, timing: dict[str, Any], top: int = 10) -> str:
    """A short plain-text report of ``analyze`` output."""
    lines = [path.name]
    head = []
    if "total_s" in timing:
        head.append(f"{timing['total_s']:.1f} s total")
    if "turns" in timing:
        head.append(f"{timing['turns']} turns")
    if "api_s" in timing:
        head.append(f"API {timing['api_s']:.1f} s")
    if "cost_usd" in timing:
        head.append(f"${timing['cost_usd']:.2f}")
    if head:
        lines.append("  " + ", ".join(head))
    if timing["startup_s"] is not None:
        lines.append(f"  startup (launch to first model output): {timing['startup_s']:.1f} s")
    lines.append(f"  model (no tool running):               {timing['model_s']:.1f} s")
    lines.append(f"  tools (a tool running):                {timing['tools_s']:.1f} s")
    if timing["api_retries"]:
        lines.append(f"  API retries: {timing['api_retries']}")
    slow = sorted(timing["tools"], key=lambda t: -t["seconds"])[:top]
    if slow:
        lines.append("  slowest tool calls:")
        for tool in slow:
            lines.append(f"    {tool['seconds']:6.1f} s  {tool['label']}")
    return "\n".join(lines)


def latest_logs(log_dir: Path, n: int = 1) -> list[Path]:
    """The ``n`` newest Claude call logs in ``log_dir``, newest first."""
    logs = sorted(log_dir.glob("claude-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[:n]
