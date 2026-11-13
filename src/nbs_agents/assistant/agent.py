"""
Read-only beamline assistant hosted by nbs-agents-runner.

The assistant watches every run on the document stream, whoever queued it.
For each run it writes a deterministic one-line ``run`` entry to its daily
agent-log record (``assistant-YYYYMMDD``) using the nbs-cli reducers. Triage
decides when a run, the end of a group of runs, an operator question or a
quiet hour is worth a short Claude call; that call (``claude-beamline
--headless --role assistant``) reads the beamline with read-only nbs-cli and
posts summaries, advice, alerts and answers back to the same record.

With ``[assistant.roles]`` the assistant is several roles: one chat role
(the manager, which writes that record and answers questions) and headless
roles that each take their own events, write their own record
(``assistant-ROLE-YYYYMMDD``) and keep a tile (``nbs_agents.tiles``). Every
event goes to exactly one role. The assistant also follows the open driver
sessions (``driver_active``) for triage and the snapshot.

The assistant never queues, removes, aborts or starts anything and never
messages driver agents. It is not opened with ``open_agent`` and never sits in
the queue.
"""

import functools
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
from ..cli.config import ASSISTANT_ROLE, ASSISTANT_ROLE_PREFIX, AssistantConfig, RoleConfig
from ..reduce.metrics import SummaryContext, summarize
from nbs_agents.constants import AGENT_UID_KEY, CLOSE_AGENT_PLAN, OPEN_AGENT_PLAN
from .launcher import ClaudeLauncher, LaunchedAgent
from .resources import SKILLS_DIR, WORKDIR_TEMPLATE
from .timing import served_model_file
from nbs_agents.agents.base import BaseAgent
from nbs_agents.agents.external import ExternalAgent

LOGGER = logging.getLogger(__name__)

SENTINEL_PLANS = (OPEN_AGENT_PLAN, CLOSE_AGENT_PLAN)
# Events that need the stronger model
URGENT_EVENTS = ("alert", "question", "recheck", "request", "reply")
# Calls whose events the record keeps, so a post can be re-checked later
CALL_EVENTS_KEPT = 30
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
        # agent_uids of the open driver sessions (the assistant keeps it current)
        self.drivers: set[str] = set()

    @property
    def driver_active(self) -> bool:
        """Whether a driver session is open."""
        return bool(self.drivers)

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
            ``alerts`` and ``flags`` (lists of short reasons),
            ``ended_group``: the runs of the group this run closed, or ``[]``,
            and ``driver_run``: whether an open driver session queued the run.
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
        driver_run = bool(facts.get("agent_uid")) and facts.get("agent_uid") in self.drivers
        return {"alerts": alerts, "flags": flags, "ended_group": ended, "driver_run": driver_run}

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


class RoleState:
    """
    One assistant role in the runner: its settings, the events waiting for
    it, its running call and today's record.

    Parameters
    ----------
    role : RoleConfig
        The role, with its defaults filled in.
    launcher : ClaudeLauncher or None
        Starts the role's calls (each role has its own working folder).
    uid : str
        Today's record: the main assistant record for the chat role.
    """

    def __init__(self, role: RoleConfig, launcher: Optional[ClaudeLauncher], uid: str) -> None:
        self.role = role
        self.launcher = launcher
        self.uid = uid
        self.pending: list[dict[str, Any]] = []
        self.call: Optional[dict[str, Any]] = None
        self.last_heartbeat = 0.0
        self.last_checkin_t = 0.0

    @property
    def name(self) -> str:
        return self.role.name

    @property
    def chat(self) -> bool:
        return self.role.chat

    @property
    def record_role(self) -> Optional[str]:
        """The ``role`` its record is named after; None for the chat role's main record."""
        return None if self.chat else self.role.name

    @property
    def cli_role(self) -> str:
        """``NBS_CLI_ROLE`` for its calls."""
        return ASSISTANT_ROLE if self.chat else f"{ASSISTANT_ROLE_PREFIX}{self.role.name}"

    @property
    def source(self) -> str:
        """``source`` of the entries the runner writes for it."""
        return ASSISTANT_NAME if self.chat else f"{ASSISTANT_NAME}:{self.role.name}"


class AssistantAgent(BaseAgent):
    """
    Passive, read-only assistant that triages runs and calls Claude to
    summarize and advise.

    The assistant is one or more roles (``[assistant.roles]``): every event
    goes to the one role whose ``triggers`` name it, each role batches its own
    events and runs one call at a time, on its own models, skill and working
    folder. The chat role also gets operator questions, re-checks and the
    replies to its ``role request``s; a headless role gets those requests.

    Parameters
    ----------
    config : AssistantConfig
        Assistant settings.
    beamline : nbs_agents.cli.core.Beamline
        Read access to status dicts, the beam map and the manager status.
    catalog : ZMQCatalog
        Document catalog (runs arrive through ``on_run_stop``).
    agent_log : AgentLog
        Shared agent-log store holding the daily records.
    launcher : ClaudeLauncher, optional
        Starts the chat role's calls, when ``launchers`` does not name it.
        Without one, runs are still logged but no call is made.
    signal, norm : str, optional
        Reducer detector and normalization keys.
    reference : str, optional
        Energy-reference detector key, for reducer plugins.
    clock : callable, optional
        Time source, for tests.
    launchers : dict, optional
        A launcher for each role, by name.
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
        launchers: Optional[dict[str, ClaudeLauncher]] = None,
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
        self.signal = signal
        self.norm = norm
        self.reference = reference
        self.clock = clock
        self.triage = Triage(config)
        launchers = dict(launchers or {})
        self.roles: dict[str, RoleState] = {}
        for role in config.resolved_roles().values():
            role_launcher = launchers.get(role.name, launcher if role.chat else None)
            state = RoleState(role, role_launcher, assistant_uid(clock(), None if role.chat else role.name))
            if role_launcher is not None:
                role_launcher.on_exit = functools.partial(self._on_call_exit, state)
            self.roles[role.name] = state
        self.chat = next(s for s in self.roles.values() if s.chat)
        # Event kind -> the role that takes it
        self.routes = {t: s.name for s in self.roles.values() for t in s.role.triggers}
        # Open driver sessions (ExternalAgent blocks): agent_uid -> what the snapshot shows
        self.drivers: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._started_t = clock()
        self._last_run_t: Optional[float] = None
        self._manager_state: Optional[str] = None
        self._xas_plans: Optional[dict] = None
        self._xas_plans_t = 0.0
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    @property
    def launcher(self) -> Optional[ClaudeLauncher]:
        """The chat role's launcher."""
        return self.chat.launcher

    @property
    def pending(self) -> list[dict[str, Any]]:
        """Every role's waiting events, chat role first."""
        return [e for s in self.roles.values() for e in s.pending]

    @property
    def call(self) -> Optional[dict[str, Any]]:
        """The chat role's running call."""
        return self.chat.call

    # ---------------------------------------------------------------- drivers

    @property
    def driver_active(self) -> bool:
        """Whether a driver session (an open ExternalAgent block) is running."""
        return bool(self.drivers)

    def driver_state(self) -> dict[str, Any]:
        """
        ``driver_active`` and the open driver sessions, each with its
        ``agent_uid``, ``record`` (agent-log key) and ``goal``.
        """
        return {"driver_active": self.driver_active, "drivers": list(self.drivers.values())}

    def _driver_changed(self) -> None:
        self.triage.drivers = set(self.drivers)
        try:
            self.agent_log.update(self.chat.uid, **self.driver_state())
        except Exception:
            LOGGER.exception("Could not record the driver sessions in the assistant record")

    def on_agent_opened(self, agent: Any) -> None:
        """Note a driver session the supervisor opened."""
        if not isinstance(agent, ExternalAgent):
            return
        with self._lock:
            self.drivers[agent.agent_uid] = {
                "agent_uid": agent.agent_uid,
                "record": self.agent_log.key_prefix + agent.agent_uid,
                "goal": (agent.parameters or {}).get("goal"),
                "since": self.clock(),
            }
            self._driver_changed()

    def on_agent_adopted(self, agent: Any, record: dict) -> None:
        """Note a driver session a previous runner left open."""
        self.on_agent_opened(agent)

    def on_agent_closed(self, agent: Any) -> None:
        """Forget a driver session that closed."""
        with self._lock:
            if self.drivers.pop(agent.agent_uid, None) is not None:
                self._driver_changed()

    # ------------------------------------------------------------------ record

    def _role_log(self, state: RoleState, kind: str, text: str, **refs: Any) -> None:
        if state.chat:
            self.log(kind, text, **refs)
            return
        try:
            self.agent_log.append(state.uid, kind, text, source=state.source, **refs)
        except Exception:
            LOGGER.exception("Failed to write the %s role's record", state.name)

    def _open_role(self, state: RoleState) -> None:
        role = state.role
        self.agent_log.ensure_assistant_record(self.clock(), role=state.record_role)
        fields: dict[str, Any] = {
            "heartbeat": self.clock(), "pending": len(state.pending),
            "models": {"routine": role.model or None, "alert": role.alert_model or None},
            "role_config": {
                "name": role.name, "mode": role.mode, "skill": role.skill,
                "triggers": role.triggers, "sinks": role.sinks,
            },
        }
        if state.chat:
            fields["roles"] = {
                s.name: {"mode": s.role.mode, "record": s.uid, "triggers": s.role.triggers, "sinks": s.role.sinks}
                for s in self.roles.values()
            }
            fields.update(self.driver_state())
        self.agent_log.set_status(state.uid, "watching", **fields)

    def open_log(self) -> None:
        """Create today's records if needed and mark every role as watching."""
        for state in self.roles.values():
            self._open_role(state)

    def _roll_day(self) -> None:
        uid = assistant_uid(self.clock())
        if uid == self.agent_uid:
            return
        for state in self.roles.values():
            try:
                self.agent_log.set_status(state.uid, "closed", close_reason="new day")
            except Exception:
                LOGGER.exception("Could not close yesterday's %s record", state.name)
            state.uid = assistant_uid(self.clock(), state.record_role)
        self.agent_uid = uid
        self.open_log()

    def _beat(self, force: bool = False) -> None:
        now = self.clock()
        for state in self.roles.values():
            if not force and now - state.last_heartbeat < HEARTBEAT_S:
                continue
            state.last_heartbeat = now
            status = "calling" if state.call else "watching"
            try:
                self.agent_log.set_status(state.uid, status, heartbeat=now, pending=len(state.pending))
            except Exception:
                LOGGER.exception("Failed to write the %s role's heartbeat", state.name)

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Prepare the workdirs, open today's records and start the timer thread."""
        for state in self.roles.values():
            try:
                prepare_workdir(state.role.workdir)
            except Exception:
                LOGGER.exception("Could not prepare the %s role's workdir %s", state.name, state.role.workdir)
        self.open_log()
        self._thread = threading.Thread(target=self._loop, name="assistant", daemon=True)
        self._thread.start()

    def stop(self, reason: str) -> None:
        """Stop the timer thread and any running call."""
        self._stop_event.set()
        for state in self.roles.values():
            if state.call is not None and state.launcher is not None:
                state.launcher.terminate(state.call["uid"])
            if not state.chat:
                try:
                    self.agent_log.set_status(state.uid, "stopped", close_reason=reason)
                except Exception:
                    LOGGER.exception("Could not mark the %s role stopped", state.name)
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

    def _new_posts(self, state: RoleState, call: dict[str, Any], kinds=ASSISTANT_POST_KINDS) -> list[dict[str, Any]]:
        entries = (self.agent_log.get(call["uid"]) or {}).get("log") or []
        return [e for e in entries[call.get("log_len", 0):] if e.get("kind") in kinds]

    def _check_answered(self, state: RoleState, call: dict[str, Any], log_path: str, kinds=("answer",),
                        what: str = "an answer to the operator's question") -> None:
        """Say so in the record when a call for an operator question (or re-check) posted no answer."""
        try:
            if self._new_posts(state, call, kinds):
                return
        except Exception:
            LOGGER.exception("Could not read the assistant record after call %s", call["n"])
            return
        text = f"call {call['n']} ended without posting {what}; see {log_path}"
        reply = last_reply(log_path)
        if reply:
            text += f"\n\nIts last reply was:\n\n{reply.strip()}"
        LOGGER.warning("Assistant %s call %s posted no answer; its log is %s", state.name, call["n"], log_path)
        self._role_log(state, "error", text)

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
                driver_run=result["driver_run"] or None,
                **refs,
            )
            self._add_event("run", run_line(facts), [facts["uid"]])
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

    def _add_event(self, kind: str, text: str, run_uids: Optional[list[str]] = None,
                   role: Optional[RoleState] = None, **extra: Any) -> None:
        """
        Queue an event for the role that takes it: ``role`` if given, else
        the one whose triggers name ``kind``. An event no role takes is dropped.
        """
        if role is None:
            name = self.routes.get(kind)
            if name is None:
                return
            role = self.roles[name]
        role.pending.append({"kind": kind, "text": text, "run_uids": run_uids or [], "t": self.clock(), **extra})

    # ------------------------------------------------------------------ timer

    def tick(self) -> None:
        """
        Periodic work: day rollover, heartbeat, operator questions, role
        requests, the end of a group when the queue goes idle, the hourly
        check-in and call timeouts.
        """
        with self._lock:
            self._roll_day()
            self._check_operator()
            self._check_requests()
            self._check_idle()
            self._check_quiet_hour()
            self._check_timeout()
            self._dispatch()
            self._beat()

    def _check_operator(self) -> None:
        try:
            messages = self.agent_log.take_operator_messages(self.chat.uid)
        except Exception:
            LOGGER.exception("Could not read operator messages for the assistant")
            return
        for message in messages:
            if message.get("kind") != "operator":
                continue
            if isinstance(message.get("recheck"), dict):
                self._add_recheck(message)
            else:
                self._add_event("question", f"operator asked: {message.get('text', '')}", role=self.chat)

    def _check_requests(self) -> None:
        for state in self.roles.values():
            if state.chat:
                continue
            try:
                requests = self.agent_log.take_requests(state.uid)
            except Exception:
                LOGGER.exception("Could not read requests for the %s role", state.name)
                continue
            for request in requests:
                self._add_event(
                    "request", f"{request.get('source') or 'the manager'} asked: {request.get('text', '')}",
                    list(request.get("run_uids") or []), role=state,
                    request={"t": request["t"], "reply_to": request.get("reply_to") or self.chat.uid},
                )

    def _add_recheck(self, message: dict[str, Any]) -> None:
        """Queue a blind re-check of the post an operator ``recheck`` entry names."""
        want = message["recheck"]
        record = self.agent_log.get(self.chat.uid) or {}
        post = next(
            (e for e in record.get("log") or [] if e.get("t") == want.get("t") and e.get("kind") in ASSISTANT_POST_KINDS),
            None,
        )
        if post is None:
            self.log("error", f"cannot re-check: no post at {want.get('t')} in {self.chat.uid}")
            return
        event = {
            "kind": "recheck", "text": message.get("text", ""), "run_uids": list(post.get("run_uids") or []),
            "t": self.clock(), "model": want.get("model") or None, "note": want.get("note"), "post": post,
        }
        self.chat.pending.append(event)

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

    def quiet_since(self, state: Optional[RoleState] = None) -> float:
        """
        When the hourly timer last restarted for a role (the one that takes
        check-ins by default): the assistant started, the role posted, got an
        operator message, or ran a check-in.

        Returns
        -------
        float
            Epoch seconds.
        """
        state = state or self.roles.get(self.routes.get("checkin", ""), self.chat)
        latest = max(self._started_t, state.last_checkin_t)
        record = self.agent_log.get(state.uid) or {}
        for entry in record.get("log") or []:
            if entry.get("kind") in ASSISTANT_POST_KINDS + ("operator",):
                latest = max(latest, entry.get("t") or 0)
        return latest

    def _check_quiet_hour(self) -> None:
        name = self.routes.get("checkin")
        if name is None or self._last_run_t is None:
            return
        state = self.roles[name]
        if state.call is not None or state.pending:
            return
        since = self.quiet_since(state)
        if self._last_run_t <= since or self.clock() - since < self.config.checkin_s:
            return
        state.last_checkin_t = self.clock()
        minutes = round(self.config.checkin_s / 60)
        self._add_event("checkin", f"hourly check-in: runs finished in the last {minutes} min with no post from you")

    def _check_timeout(self) -> None:
        for state in self.roles.values():
            call = state.call
            if call is None or state.launcher is None:
                continue
            if self.clock() - call["t"] > self.config.call_timeout_s and not call.get("timed_out"):
                call["timed_out"] = True
                LOGGER.warning("Assistant %s call %s timed out; stopping it", state.name, call["n"])
                state.launcher.terminate(call["uid"])

    # ------------------------------------------------------------------ calls

    def snapshot(self, hide=None, role: Optional[RoleState] = None) -> list[str]:
        """
        Prompt lines with what each call used to fetch first: the beamline
        state (with the beam map and status PVs), whether a driver is
        running, the newest entries of the role's record for today, and its
        notebook. Each part is cut at ``snapshot_chars``; a part that cannot
        be read says why.

        Parameters
        ----------
        hide : callable, optional
            Called with each record entry; true for entries to leave out,
            such as the post a re-check must not see.
        role : RoleState, optional
            The role the call is for; the chat role if omitted.

        Returns
        -------
        list of str
            Prompt lines; empty when ``snapshot_chars`` is 0.
        """
        role = role or self.chat
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
            # Nothing from Tiled, which can be slow: the record already has a line per run
            out = self.beamline.state(full=False, agent_uid=None, n_runs=0)
            out["beam"] = out.get("beam") or self.beamline.beam()
            return out

        def record():
            entries = (self.agent_log.get(role.uid) or {}).get("log", [])
            if hide is not None:
                entries = [e for e in entries if not hide(e)]
            return entries[-SNAPSHOT_LOG_ENTRIES:]

        def notebook():
            return read_notebook(role.role.workdir) or "(empty)"

        when = time.strftime("%H:%M:%S", time.localtime(self.clock()))
        lines = [
            "",
            f"Snapshot taken at {when}, when this call started. Use it instead of running",
            "`nbs-cli state`, `beam` or `log` or reading the notebook again; fetch only what",
            "is missing here, cut short, or may have changed since.",
        ]
        lines += part("Beamline state (nbs-cli state)", state)
        lines += part("Driver sessions", self.driver_state)
        lines += part(f"Today's record, last {SNAPSHOT_LOG_ENTRIES} entries (nbs-cli log)", record)
        lines += part("Your notebook", notebook)
        return lines

    def _intro(self, role: RoleState) -> list[str]:
        if role.chat:
            who = f"You are the read-only beamline assistant;\ntoday's record is {role.uid}."
        else:
            who = (f"You are the read-only beamline assistant's {role.name} role;\n"
                   f"your record for today is {role.uid}.")
        return [
            f"Follow the beamline assistant instructions in your system prompt. {who} You observe and advise only: never try to control",
            "the beamline (no queue changes, no device moves, no queue server, 0MQ or Redis access",
            "from python or anything else; python is for analysing data only).",
        ]

    def build_prompt(self, events: list[dict[str, Any]], role: Optional[RoleState] = None) -> str:
        """
        The prompt for one call: the role's record, the triggering events and
        a snapshot of the beamline, the record's recent entries and the notebook.

        Parameters
        ----------
        events : list of dict
            Pending events, oldest first.
        role : RoleState, optional
            The role making the call; the chat role if omitted.

        Returns
        -------
        str
            Prompt text.
        """
        role = role or self.chat
        if events and events[0]["kind"] == "recheck":
            return self.recheck_prompt(events[0])
        lines = self._intro(role) + ["These events triggered this call:"]
        for event in events:
            when = time.strftime("%H:%M:%S", time.localtime(event["t"]))
            line = f"- [{event['kind']}] {when} {event['text']}"
            if event["run_uids"]:
                line += f" (runs: {' '.join(event['run_uids'])})"
            lines.append(line)
        lines += self.snapshot(role=role)
        kinds = {e["kind"] for e in events}
        if "question" in kinds:
            lines.append("Answer each operator question with `nbs-cli post --kind answer --file notebook/post.md`.")
        if "reply" in kinds:
            lines.append("Pass on what the operator needs from each role's reply, in your own post.")
        if "request" in kinds:
            lines.append(
                "Answer each request with `nbs-cli post --kind answer --file notebook/post.md`;"
                " the runner passes your answer back to whoever asked."
            )
        if "tile" in role.role.sinks and not role.chat:
            lines.append(
                "Keep your tile current with `nbs-cli post --tile --severity ok|info|warn|alert --file notebook/tile.md`"
                " (one or two lines; alert only when someone must act)."
            )
        lines.append("Post only what the operator needs, then end your turn.")
        return "\n".join(lines)

    def recheck_prompt(self, event: dict[str, Any]) -> str:
        """
        The prompt for a blind re-check of one of the chat role's posts.

        The call gets the events behind the post (kept in the record's
        ``call_events``) and a snapshot without the posts of that call or
        earlier re-checks of it, so it forms its own view.

        Parameters
        ----------
        event : dict
            A ``recheck`` event, with the ``post`` to re-check.

        Returns
        -------
        str
            Prompt text.
        """
        post = event["post"]
        when = time.strftime("%H:%M:%S", time.localtime(post["t"]))
        record = self.agent_log.get(self.chat.uid) or {}
        behind = (record.get("call_events") or {}).get(str(post.get("call"))) or []
        lines = self._intro(self.chat) + [
            "",
            f"The operator asked for a blind re-check of the {post['kind']} the assistant posted at {when}.",
            "You do not see that post, on purpose: form your own view from the data. Do not look it up in the",
            "record (`nbs-cli log`) before you have posted yours.",
        ]
        if behind:
            lines.append("The events behind that post were:")
            for e in behind:
                line = f"- [{e.get('kind')}] {e.get('text')}"
                if e.get("run_uids"):
                    line += f" (runs: {' '.join(e['run_uids'])})"
                lines.append(line)
        elif post.get("run_uids"):
            lines.append(f"It was about runs {' '.join(post['run_uids'])}.")
        else:
            lines.append("Its events are no longer known: look at the runs and the state around that time.")
        note = event.get("note")
        if note:
            lines.append(f"The operator added: {note}")

        def hide(e):
            if e.get("kind") not in ASSISTANT_POST_KINDS:
                return False
            if post.get("call") is not None and e.get("call") == post.get("call"):
                return True
            return e.get("t") == post["t"] or e.get("recheck_of") == post["t"]

        lines += self.snapshot(hide=hide, role=self.chat)
        lines.append(
            f"Post your own view once with `nbs-cli post --kind {post['kind']} --file notebook/post.md`"
            " (as an alert if the operator must act), then end your turn."
        )
        return "\n".join(lines)

    def _dispatch(self) -> None:
        for state in self.roles.values():
            self._dispatch_role(state)

    def _dispatch_role(self, state: RoleState) -> None:
        if state.call is not None or not state.pending or not self._active:
            return
        if state.launcher is None:
            if any(e["kind"] in URGENT_EVENTS for e in state.pending):
                LOGGER.warning("Assistant %s has events but no launcher: %s", state.name, [e["text"] for e in state.pending])
            state.pending.clear()
            return
        # A re-check runs on its own, so its call sees nothing but the post's events
        recheck = next((e for e in state.pending if e["kind"] == "recheck"), None)
        if recheck is not None:
            events = [recheck]
            state.pending.remove(recheck)
        else:
            events, state.pending = state.pending, []
        urgent = any(e["kind"] in URGENT_EVENTS for e in events)
        model = state.role.alert_model if urgent else state.role.model
        if recheck is not None and recheck.get("model"):
            model = recheck["model"]
        prompt = self.build_prompt(events, role=state)
        record = self.agent_log.get(state.uid) or {}
        n = int(record.get("calls") or 0) + 1
        reasons = sorted({e["kind"] for e in events})
        args = ["--role", "assistant"]
        if model:
            args += ["--model", model]
        env = {
            "NBS_CLI_ROLE": state.cli_role, "NBS_AGENT_UID": "", "NBS_ASSISTANT_PROMPT": prompt,
            "NBS_MODEL": model or "", "NBS_ASSISTANT_CALL": str(n),
            "NBS_RECHECK_OF": repr(recheck["post"]["t"]) if recheck is not None else "",
            "NBS_ASSISTANT_SKILL": state.role.skill,
        }
        driver = f"assistant-call-{n}" if state.chat else f"assistant-{state.name}-call-{n}"
        try:
            launched = state.launcher.launch(state.uid, None, driver, extra_args=args, env=env)
        except Exception as exc:
            LOGGER.exception("Could not start an assistant %s call", state.name)
            self._role_log(state, "error", f"could not start a call: {exc}")
            return
        state.call = {
            "n": n, "uid": state.uid, "t": self.clock(), "model": model, "reasons": reasons,
            "log_len": len(record.get("log") or []),
            "requests": [e["request"] for e in events if e.get("request")],
        }
        run_uids = sorted({u for e in events for u in e["run_uids"]})
        kept = dict(list((record.get("call_events") or {}).items())[-(CALL_EVENTS_KEPT - 1):])
        if recheck is None:
            kept[str(n)] = [{k: e[k] for k in ("kind", "text", "run_uids") if e.get(k)} for e in events]
        try:
            self.agent_log.set_status(
                state.uid,
                "calling",
                calls=n,
                heartbeat=self.clock(),
                pending=len(state.pending),
                call_events=kept,
                last_call={
                    "n": n,
                    "t": state.call["t"],
                    "model": model,
                    "reasons": reasons,
                    "events": len(events),
                    "run_uids": run_uids,
                    "pid": launched.pid,
                    "log_path": launched.log_path,
                    **({"recheck_of": recheck["post"]["t"]} if recheck is not None else {}),
                },
            )
        except Exception:
            LOGGER.exception("Failed to record the assistant call")
        LOGGER.info("Assistant %s call %s (%s, %s): %s", state.name, n, model, launched.pid, reasons)

    def _forward_replies(self, state: RoleState, call: dict[str, Any], log_path: str) -> None:
        """Pass a headless role's answers to requests back to the record that asked, as ``reply`` entries."""
        try:
            posts = self._new_posts(state, call)
        except Exception:
            LOGGER.exception("Could not read the %s role's record after call %s", state.name, call["n"])
            posts = []
        text = "\n\n".join(p["text"] for p in posts if p.get("text"))
        if not text:
            text = f"the {state.name} role ended without answering; see {log_path}"
            reply = last_reply(log_path)
            if reply:
                text += f"\n\nIts last reply was:\n\n{reply.strip()}"
        run_uids = sorted({u for p in posts for u in p.get("run_uids") or []})
        for request in call["requests"]:
            reply_to = request.get("reply_to") or self.chat.uid
            try:
                self.agent_log.append(
                    reply_to, "reply", text, source=state.source, role=state.name,
                    request_t=request["t"], run_uids=run_uids or None,
                )
            except Exception:
                LOGGER.exception("Could not pass the %s role's reply to %s", state.name, reply_to)
                continue
            if reply_to == self.chat.uid:
                self._add_event("reply", f"the {state.name} role replied: {text}", run_uids, role=self.chat)

    def _save_notebook(self, state: RoleState) -> None:
        """Keep a headless role's notebook in its record, where ``nbs-cli role show`` reads it."""
        try:
            text = read_notebook(state.role.workdir)
            limit = self.config.snapshot_chars or 12000
            self.agent_log.update(state.uid, notebook=text[:limit] if text else None)
        except Exception:
            LOGGER.exception("Could not keep the %s role's notebook in its record", state.name)

    def _on_call_exit(self, state: RoleState, launched: LaunchedAgent, returncode: int) -> None:
        with self._lock:
            call, state.call = state.call, None
            if call is not None:
                duration = round(self.clock() - call["t"], 1)
                served = served_model_file(launched.log_path)
                try:
                    record = self.agent_log.get(call["uid"]) or {}
                    last = dict(record.get("last_call") or {})
                    last.update(returncode=returncode, duration_s=duration)
                    if last.get("n") == call["n"] and served:
                        last["served_model"] = served
                    self.agent_log.update(call["uid"], last_call=last)
                    if served:
                        # Only where it says more than the model asked for (a gateway alias, or none)
                        self.agent_log.annotate(
                            call["uid"],
                            lambda e: e.get("call") == call["n"] and e.get("model") != served,
                            served_model=served,
                        )
                except Exception:
                    LOGGER.exception("Failed to record the assistant call exit")
                if not state.chat:
                    self._save_notebook(state)
                if call.get("requests"):
                    self._forward_replies(state, call, launched.log_path)
                if returncode != 0:
                    why = "timed out" if call.get("timed_out") else f"exited with status {returncode}"
                    self._role_log(state, "error", f"call {call['n']} {why}; see {launched.log_path}")
                elif "question" in call["reasons"]:
                    self._check_answered(state, call, launched.log_path)
                elif "recheck" in call["reasons"]:
                    self._check_answered(state, call, launched.log_path, ASSISTANT_POST_KINDS, "its re-check")
            self._dispatch()
            self._beat(force=True)


def read_notebook(workdir: str) -> str:
    """
    A role's notebook: every ``notebook/*.md`` in its working folder.

    Parameters
    ----------
    workdir : str
        The role's working folder.

    Returns
    -------
    str
        The files, each under a ``### notebook/NAME`` heading; empty when there are none.
    """
    folder = Path(workdir).expanduser() / "notebook"
    notes = sorted(folder.glob("*.md")) if folder.is_dir() else []
    return "\n\n".join(f"### notebook/{p.name}\n{p.read_text().strip()}" for p in notes)
