"""
Read-only beamline assistant hosted by nbs-agents-runner.

The assistant watches every run on the document stream, whoever queued it.
For each run it writes a deterministic one-line ``run`` entry to its daily
agent-log record (``assistant-YYYYMMDD``) using the nbs-cli reducers. Triage
decides when a run, the end of a group of runs, an operator question or a
quiet hour is worth a short Claude call; that call (``claude-beamline
--headless --role assistant``) reads the beamline with read-only nbs-cli and
posts summaries, advice, alerts and answers back to the same record.

The assistant never queues, removes, aborts or starts anything and never
messages driver agents. It is not opened with ``open_agent`` and never sits in
the queue.
"""

import json
import logging
import os
import shutil
import statistics
import threading
import time
from pathlib import Path
from typing import Any, Optional

from nbs_agents.agentlog import (
    ASSISTANT_KIND,
    ASSISTANT_NAME,
    ASSISTANT_POST_KINDS,
    AgentLog,
    assistant_uid,
)
from nbs_agents.catalog import ZMQCatalog, ZMQRun
from ..cli.config import AssistantConfig
from ..cli.summaries import SummaryContext, summarize
from nbs_agents.constants import AGENT_UID_KEY, CLOSE_AGENT_PLAN, OPEN_AGENT_PLAN
from .launcher import ClaudeLauncher, LaunchedAgent
from .resources import SKILLS_DIR, WORKDIR_TEMPLATE
from nbs_agents.agents.base import BaseAgent

LOGGER = logging.getLogger(__name__)

SENTINEL_PLANS = (OPEN_AGENT_PLAN, CLOSE_AGENT_PLAN)
# Events that need the stronger model
URGENT_EVENTS = ("alert", "question")
HEARTBEAT_S = 30.0
STATUS_REFRESH_S = 600.0
SKILL_DIR = SKILLS_DIR / "beamline-assistant"


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.3g}"
    return str(value)


def run_facts(run: ZMQRun, summary: dict[str, Any], norm: str = "") -> dict[str, Any]:
    """
    The fields triage and the run line need from a finished run.

    Parameters
    ----------
    run : ZMQRun
        Finished run with buffered primary data.
    summary : dict
        Reducer summary from ``summarize``.
    norm : str, optional
        Normalization detector, for the i0 level.

    Returns
    -------
    dict
        ``uid``, ``scan_id``, ``plan``, ``sample``, ``edge``, ``status``,
        ``reason``, ``agent_uid``, and from the summary ``kind``, ``points``,
        ``edge_jump``, ``jump_to_noise``, ``i0`` and ``non_finite``.
    """
    start, stop = run.start, run.stop or {}
    detectors = summary.get("detectors") or {}
    non_finite = {k: v["non_finite"] for k, v in detectors.items() if v.get("non_finite")}
    i0 = (detectors.get(norm) or {}).get("mean")
    return {
        "uid": run.uid,
        "scan_id": run.scan_id,
        "plan": run.plan_name,
        "sample": start.get("sample_id") or start.get("sample_name"),
        "edge": start.get("edge") or summary.get("edge"),
        "status": stop.get("exit_status", "unknown"),
        "reason": stop.get("reason") or None,
        "agent_uid": start.get(AGENT_UID_KEY),
        "kind": summary.get("kind"),
        "points": summary.get("points"),
        "edge_jump": summary.get("edge_jump"),
        "jump_to_noise": summary.get("jump_to_noise"),
        "i0": i0,
        "non_finite": non_finite,
        "t": stop.get("time") or time.time(),
    }


def run_line(facts: dict[str, Any]) -> str:
    """
    One deterministic line describing a run.

    Parameters
    ----------
    facts : dict
        Output of ``run_facts``.

    Returns
    -------
    str
        For example ``"scan 41 c_xas success on s1, C edge, 120 pts, jump 0.85, J/N 46, i0 1.2e+05"``.
    """
    text = f"scan {facts.get('scan_id')} {facts.get('plan')} {facts.get('status')}"
    if facts.get("sample"):
        text += f" on {facts['sample']}"
    parts = []
    if facts.get("edge"):
        parts.append(f"{facts['edge']} edge")
    if facts.get("points") is not None:
        parts.append(f"{facts['points']} pts")
    if facts.get("edge_jump") is not None:
        parts.append(f"jump {_fmt(facts['edge_jump'])}")
    if facts.get("jump_to_noise") is not None:
        parts.append(f"J/N {_fmt(facts['jump_to_noise'])}")
    if facts.get("i0") is not None:
        parts.append(f"i0 {_fmt(facts['i0'])}")
    if facts.get("non_finite"):
        parts.append("non-finite " + ", ".join(f"{k}={v}" for k, v in facts["non_finite"].items()))
    if parts:
        text += ", " + ", ".join(parts)
    return text


# Entries of today's record that each call's prompt shows
SNAPSHOT_LOG_ENTRIES = 20


class Triage:
    """
    Decide which runs and groups are worth a Claude call.

    Pure bookkeeping on run facts, so it can be tested without a beamline.

    Parameters
    ----------
    config : AssistantConfig
        Thresholds and grouping.
    """

    def __init__(self, config: AssistantConfig) -> None:
        self.config = config
        self.group: list[dict[str, Any]] = []
        self._i0_history: list[float] = []
        self._last_jump: dict[tuple, float] = {}
        self._beam_alerted = False

    def group_key(self, facts: dict[str, Any]) -> tuple:
        return tuple(facts.get(name) for name in self.config.group_by)

    def observe(self, facts: dict[str, Any], beam_expected: Optional[bool] = None) -> dict[str, Any]:
        """
        Check one finished run.

        Parameters
        ----------
        facts : dict
            Output of ``run_facts``.
        beam_expected : bool or None, optional
            The beam map's ``expected`` when the run stopped.

        Returns
        -------
        dict
            ``alerts`` and ``flags`` (lists of short reasons), and
            ``ended_group``: the runs of the group this run closed, or ``[]``.
        """
        cfg = self.config
        alerts, flags = [], []
        status = facts.get("status")
        if status != "success":
            reason = f": {facts['reason']}" if facts.get("reason") else ""
            alerts.append(f"run {status}{reason}")
        if facts.get("non_finite"):
            alerts.append("non-finite points in " + ", ".join(sorted(facts["non_finite"])))
        if beam_expected is False:
            if not self._beam_alerted:
                alerts.append("beam not expected at the endstation (beam map)")
            self._beam_alerted = True
        elif beam_expected is True:
            self._beam_alerted = False

        jtn = facts.get("jump_to_noise")
        if facts.get("kind") == "xas" and jtn is not None and jtn < cfg.min_jump_to_noise:
            flags.append(f"low jump to noise {_fmt(jtn)} < {_fmt(cfg.min_jump_to_noise)}")

        i0 = facts.get("i0")
        if i0 is not None and status == "success":
            if len(self._i0_history) >= 2:
                baseline = statistics.median(self._i0_history)
                if baseline > 0 and i0 < cfg.i0_drop_fraction * baseline:
                    flags.append(f"i0 {_fmt(i0)} well below recent baseline {_fmt(baseline)}")
            self._i0_history = (self._i0_history + [float(i0)])[-max(1, cfg.i0_baseline_runs):]

        jump = facts.get("edge_jump")
        if jump is not None and facts.get("sample") and facts.get("edge") and status == "success":
            key = (facts.get("plan"), facts["sample"], facts["edge"])
            previous = self._last_jump.get(key)
            if previous:
                change = abs(jump - previous) / abs(previous)
                if change > cfg.change_fraction:
                    flags.append(
                        f"edge jump {_fmt(jump)} changed {change:.0%} from the last run on "
                        f"{facts['sample']} ({_fmt(previous)})"
                    )
            self._last_jump[key] = float(jump)

        ended: list[dict[str, Any]] = []
        if self.group and self.group_key(self.group[-1]) != self.group_key(facts):
            ended, self.group = self.group, []
        self.group.append(facts)
        return {"alerts": alerts, "flags": flags, "ended_group": ended}

    def end_group(self) -> list[dict[str, Any]]:
        """
        Close the current group, for example when the queue goes idle.

        Returns
        -------
        list of dict
            Its runs, or ``[]`` if there was none.
        """
        ended, self.group = self.group, []
        return ended


def describe_group(runs: list[dict[str, Any]]) -> str:
    """One line naming a group of runs."""
    first = runs[0]
    what = " ".join(str(first.get(k)) for k in ("plan", "sample", "edge") if first.get(k))
    scans = [r.get("scan_id") for r in runs]
    span = f"scan {scans[0]}" if len(scans) == 1 else f"scans {scans[0]}-{scans[-1]}"
    return f"{len(runs)} run(s) of {what} ({span})"


def last_reply(log_path: str) -> Optional[str]:
    """
    Claude's final reply in a call's stream-json log.

    Parameters
    ----------
    log_path : str
        The call's log file.

    Returns
    -------
    str or None
        The ``result`` text, or None when the log is missing or has none.
    """
    try:
        lines = Path(log_path).read_text(errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get("type") == "result":
            return message.get("result") or None
    return None


def prepare_workdir(workdir: str, refresh: bool = False) -> Path:
    """
    Create the assistant's working folder from the template, keeping any
    existing files (the notebook is its memory between calls).

    Parameters
    ----------
    workdir : str
        Folder to prepare.
    refresh : bool, optional
        Also overwrite files that differ from the template, such as an old
        ``.claude/settings.json``, keeping each old copy as ``*.bak``. The
        notebook is never overwritten.

    Returns
    -------
    Path
        The folder.
    """
    path = Path(workdir).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    if WORKDIR_TEMPLATE.is_dir():
        for src in WORKDIR_TEMPLATE.rglob("*"):
            rel = src.relative_to(WORKDIR_TEMPLATE)
            dest = path / rel
            if src.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            elif not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
            elif refresh and rel.parts[0] != "notebook" and dest.read_bytes() != src.read_bytes():
                shutil.copy2(dest, dest.with_name(dest.name + ".bak"))
                shutil.copy2(src, dest)
                LOGGER.info("Refreshed %s from the template (old copy in %s.bak)", dest, dest.name)
    else:
        LOGGER.warning("Assistant workdir template %s is missing", WORKDIR_TEMPLATE)
    (path / "notebook").mkdir(exist_ok=True)
    # The skill now comes in the system prompt (claude-beamline), so drop the
    # link older versions made; it would offer the same text again as a skill.
    # Never remove a real folder.
    skill_link = path / ".claude" / "skills" / SKILL_DIR.name
    if skill_link.is_symlink():
        skill_link.unlink()
    return path


class AssistantAgent(BaseAgent):
    """
    Passive, read-only assistant that triages runs and calls Claude to
    summarize and advise.

    Parameters
    ----------
    config : AssistantConfig
        Assistant settings.
    beamline : nbs_agents.cli.core.Beamline
        Read access to status dicts, the beam map and the manager status.
    catalog : ZMQCatalog
        Document catalog (runs arrive through ``on_run_stop``).
    agent_log : AgentLog
        Shared agent-log store holding the daily record.
    launcher : ClaudeLauncher, optional
        Starts each call. Without one, runs are still logged but no call is made.
    signal, norm : str, optional
        Reducer detector and normalization keys.
    reference : str, optional
        Energy-reference detector key, for reducer plugins.
    clock : callable, optional
        Time source, for tests.
    """

    def __init__(
        self,
        config: AssistantConfig,
        beamline: Any,
        catalog: Optional[ZMQCatalog],
        agent_log: AgentLog,
        launcher: Optional[ClaudeLauncher] = None,
        signal: str = "",
        norm: str = "",
        reference: str = "",
        clock=time.time,
    ) -> None:
        super().__init__(
            agent_uid=assistant_uid(clock()),
            agent_name=ASSISTANT_NAME,
            parameters={},
            manager=None,
            catalog=catalog,
            open_run=None,
            agent_log=agent_log,
        )
        self.config = config
        self.beamline = beamline
        self.launcher = launcher
        if launcher is not None:
            launcher.on_exit = self._on_call_exit
        self.signal = signal
        self.norm = norm
        self.reference = reference
        self.clock = clock
        self.triage = Triage(config)
        self.pending: list[dict[str, Any]] = []
        self.call: Optional[dict[str, Any]] = None
        self._lock = threading.RLock()
        self._started_t = clock()
        self._last_run_t: Optional[float] = None
        self._last_checkin_t = 0.0
        self._last_heartbeat = 0.0
        self._manager_state: Optional[str] = None
        self._xas_plans: Optional[dict] = None
        self._xas_plans_t = 0.0
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------ record

    def open_log(self) -> None:
        """Create today's record if needed and mark the assistant as watching."""
        self.agent_log.ensure_assistant_record(self.clock())
        self.set_status("watching", heartbeat=self.clock(), pending=len(self.pending))

    def _roll_day(self) -> None:
        uid = assistant_uid(self.clock())
        if uid == self.agent_uid:
            return
        self.set_status("closed", close_reason="new day")
        self.agent_uid = uid
        self.open_log()

    def _beat(self, force: bool = False) -> None:
        now = self.clock()
        if not force and now - self._last_heartbeat < HEARTBEAT_S:
            return
        self._last_heartbeat = now
        status = "calling" if self.call else "watching"
        try:
            self.agent_log.set_status(self.agent_uid, status, heartbeat=now, pending=len(self.pending))
        except Exception:
            LOGGER.exception("Failed to write the assistant heartbeat")

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Prepare the workdir, open today's record and start the timer thread."""
        try:
            prepare_workdir(self.config.workdir)
        except Exception:
            LOGGER.exception("Could not prepare the assistant workdir %s", self.config.workdir)
        self.open_log()
        self._thread = threading.Thread(target=self._loop, name="assistant", daemon=True)
        self._thread.start()

    def stop(self, reason: str) -> None:
        """Stop the timer thread and any running call."""
        self._stop_event.set()
        if self.call is not None and self.launcher is not None:
            self.launcher.terminate(self.call["uid"])
        self.set_status("stopped", close_reason=reason)
        self._active = False

    def shutdown(self) -> None:
        """Stop because nbs-agents-runner is stopping."""
        self.stop("runner stopped")

    def _loop(self) -> None:
        while not self._stop_event.wait(self.config.poll_s):
            try:
                self.tick()
            except Exception:
                LOGGER.exception("Assistant tick failed")

    def _check_answered(self, call: dict[str, Any], log_path: str) -> None:
        """Say so in the record when a call for an operator question posted no answer."""
        try:
            entries = (self.agent_log.get(call["uid"]) or {}).get("log") or []
        except Exception:
            LOGGER.exception("Could not read the assistant record after call %s", call["n"])
            return
        if any(e.get("kind") == "answer" for e in entries[call.get("log_len", 0):]):
            return
        text = f"call {call['n']} ended without posting an answer to the operator's question; see {log_path}"
        reply = last_reply(log_path)
        if reply:
            text += f"\n\nIts last reply was:\n\n{reply.strip()}"
        LOGGER.warning("Assistant call %s posted no answer; its log is %s", call["n"], log_path)
        self.log("error", text)

    # ------------------------------------------------------------------ runs

    def _summary_context(self) -> SummaryContext:
        now = self.clock()
        if self._xas_plans is None or now - self._xas_plans_t > STATUS_REFRESH_S:
            try:
                self._xas_plans = self.beamline.info.xas_plans()
            except Exception:
                LOGGER.exception("Could not read the XAS plans")
                self._xas_plans = self._xas_plans or {}
            self._xas_plans_t = now
        return SummaryContext(self.signal, self.norm, self._xas_plans, reference=self.reference)

    def _beam_expected(self) -> Optional[bool]:
        try:
            beam = self.beamline.beam()
        except Exception:
            return None
        return None if beam is None else beam.get("expected")

    def run_belongs_to_agent(self, run: ZMQRun) -> bool:
        """The assistant owns no runs."""
        return False

    def on_run_stop(self, run: ZMQRun) -> None:
        """
        Log every finished run and queue a call when triage asks for one.

        Parameters
        ----------
        run : ZMQRun
            Finished run from any session or user.
        """
        if run.plan_name in SENTINEL_PLANS:
            return
        data = {key: run.data(key) for key in run.keys}
        try:
            summary = summarize(run.start, data, self._summary_context())
        except Exception as exc:
            summary = {"error": f"{type(exc).__name__}: {exc}"}
        facts = run_facts(run, summary, self.norm)
        beam = self._beam_expected()
        with self._lock:
            self._roll_day()
            result = self.triage.observe(facts, beam)
            self._last_run_t = self.clock()
            refs = {
                k: facts.get(k)
                for k in ("scan_id", "plan", "sample", "edge")
                if facts.get(k) is not None
            }
            self.log(
                "run",
                run_line(facts),
                run_uid=facts["uid"],
                exit_status=facts["status"],
                alerts=result["alerts"] or None,
                flags=result["flags"] or None,
                **refs,
            )
            if result["ended_group"]:
                self._group_event(result["ended_group"], "the next run changed plan, sample or edge")
            for alert in result["alerts"]:
                self._add_event("alert", f"{run_line(facts)}: {alert}", [facts["uid"]])
            for flag in result["flags"]:
                self._add_event("flag", f"{run_line(facts)}: {flag}", [facts["uid"]])
            self._dispatch()

    def _group_event(self, runs: list[dict[str, Any]], why: str) -> None:
        text = f"group finished ({why}): {describe_group(runs)}"
        self._add_event("group_end", text, [r["uid"] for r in runs])

    def _add_event(self, kind: str, text: str, run_uids: Optional[list[str]] = None) -> None:
        self.pending.append({"kind": kind, "text": text, "run_uids": run_uids or [], "t": self.clock()})

    # ------------------------------------------------------------------ timer

    def tick(self) -> None:
        """
        Periodic work: day rollover, heartbeat, operator questions, the end of
        a group when the queue goes idle, the hourly check-in and call timeouts.
        """
        with self._lock:
            self._roll_day()
            self._check_operator()
            self._check_idle()
            self._check_quiet_hour()
            self._check_timeout()
            self._dispatch()
            self._beat()

    def _check_operator(self) -> None:
        try:
            messages = self.agent_log.take_operator_messages(self.agent_uid)
        except Exception:
            LOGGER.exception("Could not read operator messages for the assistant")
            return
        for message in messages:
            if message.get("kind") == "operator":
                self._add_event("question", f"operator asked: {message.get('text', '')}")

    def _check_idle(self) -> None:
        try:
            state = (self.beamline.manager.status() or {}).get("manager_state")
        except Exception:
            return
        previous, self._manager_state = self._manager_state, state
        if state == "idle" and previous not in (None, "idle"):
            runs = self.triage.end_group()
            if runs:
                self._group_event(runs, "the queue went idle")

    def quiet_since(self) -> float:
        """
        When the hourly timer last restarted: the assistant started, posted,
        got an operator message, or ran a check-in.

        Returns
        -------
        float
            Epoch seconds.
        """
        latest = max(self._started_t, self._last_checkin_t)
        record = self.agent_log.get(self.agent_uid) or {}
        for entry in record.get("log") or []:
            if entry.get("kind") in ASSISTANT_POST_KINDS + ("operator",):
                latest = max(latest, entry.get("t") or 0)
        return latest

    def _check_quiet_hour(self) -> None:
        if self.call is not None or self.pending or self._last_run_t is None:
            return
        since = self.quiet_since()
        if self._last_run_t <= since or self.clock() - since < self.config.checkin_s:
            return
        self._last_checkin_t = self.clock()
        minutes = round(self.config.checkin_s / 60)
        self._add_event("checkin", f"hourly check-in: runs finished in the last {minutes} min with no post from you")

    def _check_timeout(self) -> None:
        call = self.call
        if call is None or self.launcher is None:
            return
        if self.clock() - call["t"] > self.config.call_timeout_s and not call.get("timed_out"):
            call["timed_out"] = True
            LOGGER.warning("Assistant call %s timed out; stopping it", call["n"])
            self.launcher.terminate(call["uid"])

    # ------------------------------------------------------------------ calls

    def snapshot(self) -> list[str]:
        """
        Prompt lines with what each call used to fetch first: the beamline
        state (with the beam map and status PVs), the newest entries of
        today's record, and the notebook. Each part is cut at
        ``snapshot_chars``; a part that cannot be read says why.

        Returns
        -------
        list of str
            Prompt lines; empty when ``snapshot_chars`` is 0.
        """
        limit = self.config.snapshot_chars
        if limit <= 0:
            return []

        def part(title: str, read) -> list[str]:
            try:
                value = read()
                text = value if isinstance(value, str) else json.dumps(value, default=str, separators=(",", ":"))
            except Exception as exc:
                text = f"(unavailable: {type(exc).__name__}: {exc})"
            if len(text) > limit:
                text = text[:limit] + " …(cut; fetch it for the rest)"
            return ["", f"## {title}", text]

        def state():
            out = self.beamline.state(full=False, agent_uid=None)
            out["beam"] = out.get("beam") or self.beamline.beam()
            return out

        def record():
            entries = (self.agent_log.get(self.agent_uid) or {}).get("log", [])
            return entries[-SNAPSHOT_LOG_ENTRIES:]

        def notebook():
            folder = Path(self.config.workdir).expanduser() / "notebook"
            notes = sorted(folder.glob("*.md")) if folder.is_dir() else []
            return "\n\n".join(f"### notebook/{p.name}\n{p.read_text().strip()}" for p in notes) or "(empty)"

        when = time.strftime("%H:%M:%S", time.localtime(self.clock()))
        lines = [
            "",
            f"Snapshot taken at {when}, when this call started. Use it instead of running",
            "`nbs-cli state`, `beam` or `log` or reading the notebook again; fetch only what",
            "is missing here, cut short, or may have changed since.",
        ]
        lines += part("Beamline state (nbs-cli state)", state)
        lines += part(f"Today's record, last {SNAPSHOT_LOG_ENTRIES} entries (nbs-cli log)", record)
        lines += part("Your notebook", notebook)
        return lines

    def build_prompt(self, events: list[dict[str, Any]]) -> str:
        """
        The prompt for one call: today's record, the triggering events and a
        snapshot of the beamline, the record's recent entries and the notebook.

        Parameters
        ----------
        events : list of dict
            Pending events, oldest first.

        Returns
        -------
        str
            Prompt text.
        """
        lines = [
            "Follow the beamline assistant instructions in your system prompt. You are the read-only beamline assistant;",
            f"today's record is {self.agent_uid}. You observe and advise only: never try to control",
            "the beamline (no queue changes, no device moves, no queue server, 0MQ or Redis access",
            "from python or anything else; python is for analysing data only).",
            "These events triggered this call:",
        ]
        for event in events:
            when = time.strftime("%H:%M:%S", time.localtime(event["t"]))
            line = f"- [{event['kind']}] {when} {event['text']}"
            if event["run_uids"]:
                line += f" (runs: {' '.join(event['run_uids'])})"
            lines.append(line)
        lines += self.snapshot()
        kinds = {e["kind"] for e in events}
        if "question" in kinds:
            lines.append("Answer each operator question with `nbs-cli post --kind answer --file notebook/post.md`.")
        lines.append("Post only what the operator needs, then end your turn.")
        return "\n".join(lines)

    def _dispatch(self) -> None:
        if self.call is not None or not self.pending or not self._active:
            return
        if self.launcher is None:
            if any(e["kind"] in URGENT_EVENTS for e in self.pending):
                LOGGER.warning("Assistant has events but no launcher: %s", [e["text"] for e in self.pending])
            self.pending.clear()
            return
        events, self.pending = self.pending, []
        urgent = any(e["kind"] in URGENT_EVENTS for e in events)
        model = self.config.alert_model if urgent else self.config.model
        prompt = self.build_prompt(events)
        record = self.agent_log.get(self.agent_uid) or {}
        n = int(record.get("calls") or 0) + 1
        reasons = sorted({e["kind"] for e in events})
        args = ["--role", "assistant"]
        if model:
            args += ["--model", model]
        try:
            launched = self.launcher.launch(
                self.agent_uid,
                None,
                f"assistant-call-{n}",
                extra_args=args,
                env={"NBS_CLI_ROLE": "assistant", "NBS_AGENT_UID": "", "NBS_ASSISTANT_PROMPT": prompt},
            )
        except Exception as exc:
            LOGGER.exception("Could not start an assistant call")
            self.log("error", f"could not start a call: {exc}")
            return
        self.call = {
            "n": n, "uid": self.agent_uid, "t": self.clock(), "model": model, "reasons": reasons,
            "log_len": len(record.get("log") or []),
        }
        run_uids = sorted({u for e in events for u in e["run_uids"]})
        try:
            self.agent_log.set_status(
                self.agent_uid,
                "calling",
                calls=n,
                heartbeat=self.clock(),
                pending=0,
                last_call={
                    "n": n,
                    "t": self.call["t"],
                    "model": model,
                    "reasons": reasons,
                    "events": len(events),
                    "run_uids": run_uids,
                    "pid": launched.pid,
                    "log_path": launched.log_path,
                },
            )
        except Exception:
            LOGGER.exception("Failed to record the assistant call")
        LOGGER.info("Assistant call %s (%s, %s): %s", n, model, launched.pid, reasons)

    def _on_call_exit(self, launched: LaunchedAgent, returncode: int) -> None:
        with self._lock:
            call, self.call = self.call, None
            if call is not None:
                duration = round(self.clock() - call["t"], 1)
                try:
                    record = self.agent_log.get(call["uid"]) or {}
                    last = dict(record.get("last_call") or {})
                    last.update(returncode=returncode, duration_s=duration)
                    self.agent_log.update(call["uid"], last_call=last)
                except Exception:
                    LOGGER.exception("Failed to record the assistant call exit")
                if returncode != 0:
                    why = "timed out" if call.get("timed_out") else f"exited with status {returncode}"
                    self.log("error", f"call {call['n']} {why}; see {launched.log_path}")
                elif "question" in call["reasons"]:
                    self._check_answered(call, launched.log_path)
            self._dispatch()
            self._beat(force=True)
