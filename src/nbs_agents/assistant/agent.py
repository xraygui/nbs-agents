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

While a driver runs, the chat role is its manager: instead of summarizing
each group of the driver's runs, it writes a digest of everything the driver
did since the last one (``digest`` events: a driver group end at most every
``digest_min_s``, hourly, and when the session ends). Each question a person
asks opens a thread (``nbs_agents.threads``), a Claude session that every
follow-up resumes and that closes with a summary in ``notebook/threads.md``.
Directives a person confirmed (``nbs_agents.directives``) go into their
role's prompt until they expire.

A headless role may have a ``worker`` (``nbs_agents.assistant.workers``):
the plotter reduces and plots each run without a model call, the health
watcher keeps its tile from the beam map and status PVs, and the integrity
role gets a differential-diagnosis prompt without the driver's view.

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
import uuid
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
from ..cli.core import compact_item
from ..directives import DirectiveStore, directives_text, expired, log_directive
from ..threads import ThreadStore, seed_text, unseen
from ..reduce.metrics import SummaryContext, summarize
from nbs_agents.constants import AGENT_UID_KEY, CLOSE_AGENT_PLAN, OPEN_AGENT_PLAN
from .launcher import ClaudeLauncher, LaunchedAgent
from .resources import SKILLS_DIR, WORKDIR_TEMPLATE
from .timing import served_model_file
from .workers import Health, Plotter, series_key
from nbs_agents.agents.base import BaseAgent
from nbs_agents.agents.external import ExternalAgent

LOGGER = logging.getLogger(__name__)

SENTINEL_PLANS = (OPEN_AGENT_PLAN, CLOSE_AGENT_PLAN)
# Events that need the stronger model
URGENT_EVENTS = ("alert", "question", "recheck", "request", "reply")
# Sections of notebook/threads.md kept (the newest)
THREAD_SUMMARIES_KEPT = 40
# Run fields shown on a driver record entry in a digest
DIGEST_REFS = ("plan", "scan_id", "exit_status", "run_uid", "item_uid")
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
            and ``driver_run``: whether an open driver session queued the run
            (also set in ``facts``).
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

        driver_run = bool(facts.get("agent_uid")) and facts.get("agent_uid") in self.drivers
        facts["driver_run"] = driver_run
        ended: list[dict[str, Any]] = []
        if self.group and self.group_key(self.group[-1]) != self.group_key(facts):
            ended, self.group = self.group, []
        self.group.append(facts)
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


def entry_line(entry: dict[str, Any]) -> str:
    """
    One driver record entry for a digest: ``HH:MM:SS [kind] text (refs)``,
    the text whole, its later lines indented.
    """
    when = time.strftime("%H:%M:%S", time.localtime(entry.get("t") or 0))
    refs = ", ".join(f"{k}={entry[k]}" for k in DIGEST_REFS if entry.get(k) is not None)
    text = (entry.get("text") or "").strip().replace("\n", "\n    ")
    return f"{when} [{entry.get('kind')}] {text}" + (f" ({refs})" if refs else "")


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
    result = final_result(log_path)
    return (result or {}).get("result") or None


def final_result(log_path: str) -> Optional[dict[str, Any]]:
    """
    The ``result`` message that ends a call's stream-json log (with
    ``is_error`` set when the call failed, for example a session that could
    not be resumed), or None when the log is missing or has none.
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
            return message
    return None


def prepare_workdir(workdir: str, refresh: bool = False, cli_role: str = ASSISTANT_ROLE) -> Path:
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
    cli_role : str, optional
        ``NBS_CLI_ROLE`` that ``.claude/settings.json`` gives the folder's
        commands: ``assistant:NAME`` for a headless role, whose settings
        would otherwise make its nbs-cli post as the chat role.

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
    set_settings_role(path / ".claude" / "settings.json", cli_role)
    # The skill now comes in the system prompt (claude-beamline), so drop the
    # link older versions made; it would offer the same text again as a skill.
    # Never remove a real folder.
    skill_link = path / ".claude" / "skills" / SKILL_DIR.name
    if skill_link.is_symlink():
        skill_link.unlink()
    return path


def set_settings_role(settings: Path, cli_role: str) -> None:
    """Change ``env.NBS_CLI_ROLE`` in a Claude settings file that sets another value."""
    try:
        data = json.loads(settings.read_text())
    except (OSError, ValueError):
        return
    env = data.get("env")
    if not isinstance(env, dict) or env.get("NBS_CLI_ROLE") in (None, cli_role):
        return
    env["NBS_CLI_ROLE"] = cli_role
    settings.write_text(json.dumps(data, indent=2) + "\n")
    LOGGER.info("Set NBS_CLI_ROLE=%s in %s", cli_role, settings)


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
        # Digests: per driver session, the newest record entry the last digest
        # covered; sessions that ended since; group ends waiting for digest_min_s
        self.digest_since: dict[str, float] = {}
        self.ended_drivers: list[dict[str, Any]] = []
        self._digest_wait: list[dict[str, Any]] = []
        self._last_digest_t = clock()
        # Threads: running calls by thread id
        self.threads = ThreadStore.for_agent_log(agent_log)
        self.thread_calls: dict[str, dict[str, Any]] = {}
        self._pruned_t = 0.0
        # Directives, and the proposal they may expire with
        self.directives = DirectiveStore.for_agent_log(agent_log)
        self._proposal: Any = None
        self._proposal_t = 0.0
        # Workers of headless roles, by role name
        self.plotters = {s.name: Plotter(self, s) for s in self.roles.values() if s.role.worker == "plotter"}
        self.health = {s.name: Health(self, s) for s in self.roles.values() if s.role.worker == "health"}

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
            if not self.drivers:
                # The first digest waits digest_min_s from here, not from the runner's start
                self._last_digest_t = self.clock()
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
        """Forget a driver session that closed; the next digest covers its end."""
        with self._lock:
            driver = self.drivers.pop(agent.agent_uid, None)
            if driver is None:
                return
            self._driver_changed()
            if self.digests:
                self.ended_drivers.append(dict(driver, ended=self.clock()))
                self._queue_digest(f"driver session {agent.agent_uid[:8]} ended (goal: {driver.get('goal')})")
                self._dispatch()

    # ---------------------------------------------------------------- digests

    @property
    def digests(self) -> bool:
        """Whether a role takes ``digest`` events; if none does, driver runs get group summaries as before."""
        return "digest" in self.routes

    def _digest_mark(self, agent_uid: str) -> float:
        """The newest entry of a driver's record that the last digest covered (0 for none)."""
        if agent_uid not in self.digest_since:
            saved = (self.agent_log.get(self.chat.uid) or {}).get("digest_since") or {}
            self.digest_since[agent_uid] = float(saved.get(agent_uid) or 0)
        return self.digest_since[agent_uid]

    def _queue_digest(self, text: str, run_uids: Optional[list[str]] = None) -> None:
        """Queue a digest event now, with any group ends that waited for one."""
        waited = self._digest_wait
        self._digest_wait = []
        uids = sorted({u for w in waited for u in w["run_uids"]} | set(run_uids or []))
        lines = [w["text"] for w in waited] + [text]
        self._add_event("digest", "; ".join(lines), uids)
        self._last_digest_t = self.clock()

    def _group_ended(self, runs: list[dict[str, Any]], why: str) -> None:
        """
        A group of runs finished: a digest (at most every ``digest_min_s``)
        when a driver queued them all, else a ``group_end`` event as always.
        """
        if not (self.digests and self.driver_active and all(r.get("driver_run") for r in runs)):
            self._group_event(runs, why)
            return
        text = f"a driver's group finished ({why}): {describe_group(runs)}"
        uids = [r["uid"] for r in runs]
        if self.clock() - self._last_digest_t >= self.config.digest_min_s:
            self._queue_digest(text, uids)
        else:
            self._digest_wait.append({"text": text, "run_uids": uids})

    def _check_digest(self) -> None:
        """Queue a digest when group ends have waited ``digest_min_s``, or hourly while a driver runs."""
        if not self.digests:
            return
        since = self.clock() - self._last_digest_t
        if self._digest_wait and since >= self.config.digest_min_s:
            self._queue_digest("digest of the driver groups that finished since the last one")
        elif self.driver_active and since >= self.config.checkin_s:
            state = self.roles[self.routes["digest"]]
            if state.call is not None or any(e["kind"] == "digest" for e in state.pending):
                return
            new = any(self._driver_entries(uid) for uid in self.drivers)
            if new:
                minutes = round(self.config.checkin_s / 60)
                self._queue_digest(f"hourly digest: the driver did more in the last {minutes} min")
            else:
                self._last_digest_t = self.clock()

    def _driver_entries(self, agent_uid: str) -> list[dict[str, Any]]:
        """A driver's record entries newer than the last digest."""
        since = self._digest_mark(agent_uid)
        entries = (self.agent_log.get(agent_uid) or {}).get("log") or []
        return [e for e in entries if (e.get("t") or 0) > since]

    def _driver_queue(self, agent_uid: str) -> list[dict[str, Any]]:
        """The running and queued items a driver session added, with their reasons."""
        queue = self.beamline.queue() or {}
        out = []
        running = queue.get("running_item") or {}
        if (running.get("meta") or {}).get(AGENT_UID_KEY) == agent_uid:
            out.append(dict(compact_item(running), running=True))
        for item in queue.get("items") or []:
            if (item.get("meta") or {}).get(AGENT_UID_KEY) == agent_uid:
                out.append(compact_item(item))
        return out

    def digest_material(self, role: RoleState) -> dict[str, Any]:
        """
        What a digest call reads: every driver record entry since the last
        digest, for the open sessions and those that ended since, and each
        open session's queued items with their reasons. The entries are also
        written whole to ``digest/driver-UID8.md`` in the role's working
        folder, since the prompt holds at most ``digest_chars`` of them.

        Parameters
        ----------
        role : RoleState
            The role making the digest.

        Returns
        -------
        dict
            ``lines`` (prompt lines), ``marks`` (``{agent_uid: t}`` to keep
            once the digest is posted) and ``ended`` (agent_uids of the ended
            sessions it covers).
        """
        sessions = [(d, True) for d in self.drivers.values()] + [(d, False) for d in self.ended_drivers]
        budget = max(1000, self.config.digest_chars // max(1, len(sessions)))
        folder = Path(role.role.workdir).expanduser() / "digest"
        lines = ["", "## Driver sessions since the last digest"]
        marks: dict[str, float] = {}
        for driver, open_ in sessions:
            uid = driver["agent_uid"]
            since = self._digest_mark(uid)
            try:
                entries = self._driver_entries(uid)
            except Exception as exc:
                entries = []
                LOGGER.exception("Could not read the driver record %s", uid)
                lines += ["", f"(record {uid} unavailable: {type(exc).__name__}: {exc})"]
            marks[uid] = max([since] + [e.get("t") or 0 for e in entries])
            text = "\n".join(entry_line(e) for e in entries) or "(nothing new)"
            path = folder / f"driver-{uid[:8]}.md"
            try:
                folder.mkdir(parents=True, exist_ok=True)
                path.write_text(f"# Driver session {uid}: record since the last digest\n\n{text}\n")
                where = f"all of it in digest/{path.name}"
            except OSError as exc:
                where = f"could not write digest/{path.name}: {exc}"
            start = time.strftime("%H:%M:%S", time.localtime(since)) if since else "the session opened"
            lines += [
                "",
                f"### Session {uid[:8]} ({'open' if open_ else 'ended'}), goal: {driver.get('goal') or 'none given'}",
                f"Its record ({uid}) since {start}: {len(entries)} entries, {where}.",
            ]
            if len(text) > budget:
                text = text[:budget] + f" …(cut; Read digest/{path.name} for the rest)"
            lines.append(text)
            if open_:
                try:
                    queued = self._driver_queue(uid)
                    lines += ["Running and queued for it (with the reasons it gave):"]
                    lines += [json.dumps(q, default=str, separators=(",", ":")) for q in queued] or ["(nothing)"]
                except Exception as exc:
                    lines.append(f"(queue unavailable: {type(exc).__name__}: {exc})")
        if not sessions:
            lines.append("(no driver session is open or ended since the last digest)")
        return {"lines": lines, "marks": marks, "ended": [d["agent_uid"] for d in self.ended_drivers]}

    def _digest_done(self, state: RoleState, call: dict[str, Any]) -> None:
        """A digest call ended well: move the marks on and tag its posts as the digest."""
        digest = call.get("digest") or {}
        self.digest_since.update(digest.get("marks") or {})
        ended = set(digest.get("ended") or [])
        self.ended_drivers = [d for d in self.ended_drivers if d["agent_uid"] not in ended]
        for uid in ended:
            self.digest_since.pop(uid, None)
        try:
            self.agent_log.update(
                self.chat.uid, digest_since={u: t for u, t in self.digest_since.items() if u in self.drivers},
                last_digest={"t": call["t"], "n": call["n"], "role": state.name},
            )
            self.agent_log.annotate(
                call["uid"], lambda e: e.get("call") == call["n"] and e.get("kind") in ASSISTANT_POST_KINDS,
                digest=True,
            )
        except Exception:
            LOGGER.exception("Could not record the digest")

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
                prepare_workdir(state.role.workdir, cli_role=state.cli_role)
            except Exception:
                LOGGER.exception("Could not prepare the %s role's workdir %s", state.name, state.role.workdir)
        self.open_log()
        # A thread call a previous runner left running is gone; its question is answered again
        try:
            for thread in self.threads.open_threads():
                if thread.get("calling"):
                    self.threads.update(thread["id"], calling=False, seen=thread.get("call_seen_before", 0))
        except Exception:
            LOGGER.exception("Could not reset the threads' running calls")
        self._thread = threading.Thread(target=self._loop, name="assistant", daemon=True)
        self._thread.start()
        for worker in list(self.plotters.values()) + list(self.health.values()):
            worker.start()

    def stop(self, reason: str) -> None:
        """Stop the timer thread and any running call."""
        self._stop_event.set()
        for worker in list(self.plotters.values()) + list(self.health.values()):
            worker.stop()
        for thread_id in list(self.thread_calls):
            if self.chat.launcher is not None:
                self.chat.launcher.terminate(thread_id)
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
        facts["data_keys"] = sorted(run.keys)
        facts["series"] = series_key(run.start)
        if facts.get("points") is None:
            facts["points"] = max((len(data[k]) for k in data), default=0)
        # With a health role, beam loss is its alert, not a run's
        beam = None if "beam" in self.routes else self._beam_expected()
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
            self._add_event("run", run_line(facts), [facts["uid"]], run=facts)
            if result["ended_group"]:
                self._group_ended(result["ended_group"], "the next run changed plan, sample or edge")
            for alert in result["alerts"]:
                self._add_event("alert", f"{run_line(facts)}: {alert}", [facts["uid"]])
            for flag in result["flags"]:
                self._add_event("flag", f"{run_line(facts)}: {flag}", [facts["uid"]])
            self._dispatch()

    def _group_event(self, runs: list[dict[str, Any]], why: str) -> None:
        text = f"group finished ({why}): {describe_group(runs)}"
        self._add_event("group_end", text, [r["uid"] for r in runs], runs=runs)

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
        event = {"kind": kind, "text": text, "run_uids": run_uids or [], "t": self.clock(), **extra}
        plotter = self.plotters.get(role.name)
        if plotter is not None and kind == "run":
            plotter.submit(event)
            return
        event.pop("run", None)
        event.pop("runs", None)
        role.pending.append(event)

    def worker_events(self, events: list[tuple], role: Optional[RoleState] = None, **extra: Any) -> None:
        """
        Queue events from a worker thread: ``(kind, text, run_uids)`` each,
        for ``role`` or the role that takes the kind, and start the calls.
        """
        with self._lock:
            for kind, text, run_uids in events:
                self._add_event(kind, text, run_uids, role=role, **extra)
            self._dispatch()

    # ------------------------------------------------------------------ timer

    def tick(self) -> None:
        """
        Periodic work: day rollover, heartbeat, operator questions, role
        requests, the end of a group when the queue goes idle, digests, the
        hourly check-in, directive expiry, thread calls and call timeouts.
        """
        with self._lock:
            self._roll_day()
            self._check_operator()
            self._check_requests()
            self._check_idle()
            self._check_digest()
            self._check_quiet_hour()
            self._check_directives()
            self._check_timeout()
            self._dispatch()
            self._check_threads()
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
            elif message.get("thread"):
                continue  # opened by nbs-cli or the web UI; the thread's own call answers it
            elif self.config.threads:
                self._thread_from_message(message)
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
                    request={"t": request["t"], "reply_to": request.get("reply_to") or self.chat.uid,
                             "thread": request.get("thread")},
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
                self._group_ended(runs, "the queue went idle")

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
        if name is None or self._last_run_t is None or (self.digests and self.driver_active):
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
        for thread_id, call in self.thread_calls.items():
            if self.clock() - call["t"] > self.config.call_timeout_s and not call.get("timed_out"):
                call["timed_out"] = True
                LOGGER.warning("Assistant thread %s call timed out; stopping it", thread_id)
                self.chat.launcher.terminate(thread_id)

    def _current_proposal(self) -> Any:
        """The beamline's proposal metadata, read at most every few minutes; None if unknown."""
        now = self.clock()
        if now - self._proposal_t > STATUS_REFRESH_S:
            self._proposal_t = now
            try:
                self._proposal = (self.beamline.info.metadata(["proposal"]) or {}).get("proposal")
            except Exception:
                self._proposal = None
        return self._proposal

    def _check_directives(self) -> None:
        """End the active directives whose time ran out or whose beamtime ended."""
        try:
            active = self.directives.listing(statuses=("active",))
        except Exception:
            LOGGER.exception("Could not read the directives")
            return
        if not active:
            return
        proposal = self._current_proposal()
        for directive in active:
            why = expired(directive, self.clock(), proposal)
            if why is None:
                continue
            try:
                directive = self.directives.end(directive["id"], "expired", why, t=self.clock())
                log_directive(self.agent_log, directive, f"directive {directive['id']} for {directive['role']} "
                              f"expired: {why}. It was: {directive['text']}", source="runner", role_record=True,
                              chat_role=self.chat.name)
            except Exception:
                LOGGER.exception("Could not end directive %s", directive["id"])

    def directive_lines(self, role: RoleState) -> list[str]:
        """
        Prompt lines with the role's active directives; also writes them to
        ``directives.md`` in its working folder (outside the notebook, which
        the role may edit).
        """
        try:
            active = self.directives.active(role.name)
        except Exception:
            LOGGER.exception("Could not read the %s role's directives", role.name)
            return []
        text = directives_text(active)
        try:
            path = Path(role.role.workdir).expanduser()
            path.mkdir(parents=True, exist_ok=True)
            (path / "directives.md").write_text(text + "\n")
        except OSError:
            LOGGER.exception("Could not write the %s role's directives.md", role.name)
        if not active:
            return []
        return ["", "## Directives (a person confirmed these; follow them until they expire)", text]

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
        if role.role.worker != "integrity":
            # The integrity role forms its view without the driver's
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

    def build_prompt(self, events: list[dict[str, Any]], role: Optional[RoleState] = None,
                     digest: Optional[dict[str, Any]] = None) -> str:
        """
        The prompt for one call: the role's record, the triggering events and
        a snapshot of the beamline, the record's recent entries and the notebook.

        Parameters
        ----------
        events : list of dict
            Pending events, oldest first.
        role : RoleState, optional
            The role making the call; the chat role if omitted.
        digest : dict, optional
            ``digest_material`` for a call with ``digest`` events; read here if omitted.

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
        lines += self.directive_lines(role)
        kinds = {e["kind"] for e in events}
        if "digest" in kinds:
            lines += (digest or self.digest_material(role))["lines"]
            lines += [
                "",
                "This call writes the digest. Read the driver records above in full (Read the digest/ file where"
                " one is cut) and post one `summary` for a person who was away: what the driver did and found"
                " since the last digest, what it is doing now and has queued, and anything they need to decide."
                " Do not retell it step by step, and do not judge its science again unless you see a problem"
                " it missed; then say so plainly.",
            ]
        if "question" in kinds:
            lines.append("Answer each operator question with `nbs-cli post --kind answer --file notebook/post.md`.")
        if "reply" in kinds:
            lines.append("Pass on what the operator needs from each role's reply, in your own post.")
        if "request" in kinds:
            lines.append(
                "Answer each request with `nbs-cli post --kind answer --file notebook/post.md`;"
                " the runner passes your answer back to whoever asked."
            )
        lines += self.worker_lines(role, events)
        if "tile" in role.role.sinks and not role.chat:
            lines.append(
                "Keep your tile current with `nbs-cli post --tile --severity ok|info|warn|alert --file notebook/tile.md`"
                " (one or two lines; alert only when someone must act)."
            )
        lines.append("Post only what the operator needs, then end your turn.")
        return "\n".join(lines)

    def worker_lines(self, role: RoleState, events: list[dict[str, Any]]) -> list[str]:
        """What a worker role's call must do, for the events it got."""
        worker = role.role.worker
        kinds = {e["kind"] for e in events}
        lines: list[str] = []
        if worker == "plotter" and "keys" in kinds:
            lines += ["", "The runner plots every run itself with nbs-reduce; it called you because it could not "
                      "settle the keys for these runs. For each:",
                      "1. Run `nbs-reduce keys RUN` and choose the signal(s) and normalization from what each key"
                      " is and its signal check. Never pick a key with no signal; if no key shows the edge, say so.",
                      "2. Plot with `nbs-reduce plot RUN --signal K --norm K --kind normalized -o plots/NAME.png`,"
                      " store it with `nbs-cli attach plots/NAME.png --alt \"...\" --run RUN`, and post your tile with"
                      " `nbs-cli post --tile --severity info --figure att:ID --file notebook/tile.md`: the keys and why.",
                      "3. Write your choice as JSON, `{\"signal\": [\"K\"], \"norm\": \"K\" or null, \"why\": \"...\"}`,"
                      " to the file named for that run below. The runner then uses those keys for the next runs of the"
                      " same plan and detectors without calling you."]
            lines += [f"- run {e['run_uids'][0]}: write `{e['choice_file']}`" for e in events
                      if e["kind"] == "keys" and e.get("choice_file") and e["run_uids"]]
        if worker == "integrity" and kinds & {"alert", "flag"}:
            lines += ["", "You check the data's integrity. For each alert or flag:",
                      "- Give a differential diagnosis: at least three causes that could explain it (for example"
                      " sample charging or damage, a detector or amplifier problem, beam or i0 trouble, the wrong"
                      " keys, a real feature of the sample), and what would tell them apart.",
                      "- Check every cause you name that a read verb can check (nbs-reduce summary, keys and compare"
                      " against earlier runs or references; nbs-cli run, beam, device) before you post.",
                      "- Form your view from the data. Do not read the driver's record or reasoning (`nbs-cli log`"
                      " of a driver session) before you post; the manager weighs both views.",
                      "- Post the diagnosis to your record (`nbs-cli post --kind advice`, or `--kind alert` when"
                      " someone must act) with `--runs` for the runs, and keep your tile to one line."]
        if worker == "health" and "beam" in kinds:
            lines += ["", "The runner has already written your tile from the beam map and status PVs. Explain the"
                      " change in one or two lines: what it means for running and queued scans, the likely cause"
                      " (nbs-cli beam --full shows the map), and whether a person must act. Post that to your tile"
                      " with the same severity, or alert only if a person must act now."]
        return lines

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
        digest = self.digest_material(state) if any(e["kind"] == "digest" for e in events) else None
        prompt = self.build_prompt(events, role=state, digest=digest)
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
            "digest": digest and {"marks": digest["marks"], "ended": digest["ended"]},
            "keys": [e for e in events if e["kind"] == "keys"],
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
            if request.get("thread"):
                try:
                    self.threads.add(request["thread"], "reply", text, role=state.name, source=state.source,
                                     request_t=request["t"], run_uids=run_uids or None, t=self.clock())
                    continue
                except Exception:
                    LOGGER.exception("Could not pass the %s role's reply to thread %s; it goes to the main record",
                                     state.name, request["thread"])
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
        if getattr(launched, "agent_uid", None) in self.thread_calls:
            self._on_thread_exit(launched, returncode)
            return
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
                if call.get("keys") and state.name in self.plotters:
                    self.plotters[state.name].collect_choices(call["keys"])
                if call.get("requests"):
                    self._forward_replies(state, call, launched.log_path)
                if returncode != 0:
                    why = "timed out" if call.get("timed_out") else f"exited with status {returncode}"
                    self._role_log(state, "error", f"call {call['n']} {why}; see {launched.log_path}")
                elif "question" in call["reasons"]:
                    self._check_answered(state, call, launched.log_path)
                elif "recheck" in call["reasons"]:
                    self._check_answered(state, call, launched.log_path, ASSISTANT_POST_KINDS, "its re-check")
                if returncode == 0 and "digest" in call["reasons"]:
                    self._digest_done(state, call)
            self._dispatch()
            if call is not None and any(r.get("thread") for r in call.get("requests") or []):
                self._check_threads()
            self._beat(force=True)

    # ---------------------------------------------------------------- threads

    def _thread_from_message(self, message: dict[str, Any]) -> None:
        """Open a thread for a question written straight into the main record (as older UIs do)."""
        by = message.get("by") or message.get("source") or "operator"
        try:
            thread = self.threads.create(
                message.get("text", ""), by, source=message.get("source") or "operator",
                marker={"record": self.chat.uid, "t": message["t"]}, t=self.clock(),
            )
            self.agent_log.annotate(self.chat.uid, lambda e: e.get("t") == message["t"] and e.get("kind") == "operator",
                                    thread=thread["id"])
        except Exception:
            LOGGER.exception("Could not open a thread for an operator question; answering it in the record")
            self._add_event("question", f"operator asked: {message.get('text', '')}", role=self.chat)

    def _check_threads(self) -> None:
        """Answer new questions and replies in threads, and close the quiet ones."""
        if not self._active or self.chat.launcher is None:
            return
        try:
            threads = self.threads.open_threads()
        except Exception:
            LOGGER.exception("Could not read the threads")
            return
        now = self.clock()
        for thread in threads:
            if thread["id"] in self.thread_calls or len(self.thread_calls) >= max(1, self.config.thread_calls):
                continue
            if unseen(thread):
                self._thread_call(thread)
            elif now - (thread.get("last_t") or 0) > self.config.thread_idle_s:
                self._thread_call(thread, closing=True)
        if now - self._pruned_t > STATUS_REFRESH_S:
            self._pruned_t = now
            try:
                self.threads.prune(now - self.config.thread_keep_days * 86400)
            except Exception:
                LOGGER.exception("Could not prune old threads")

    def thread_prompt(self, thread: dict[str, Any], resume: bool) -> str:
        """
        The prompt for a thread call: what is new in the thread since the
        manager last saw it. A first call (or one that cannot resume the
        thread's session) also gets the seed, the earlier messages, the
        snapshot and how to answer.

        Parameters
        ----------
        thread : dict
            The thread.
        resume : bool
            Whether the call resumes the thread's Claude session.

        Returns
        -------
        str
            Prompt text.
        """
        messages = thread.get("messages") or []
        seen = thread.get("seen") or 0

        def listed(ms):
            out = []
            for m in ms:
                when = time.strftime("%H:%M:%S", time.localtime(m.get("t") or 0))
                who = m.get("by") or (f"the {m['role']} role" if m.get("role") else m.get("source") or "")
                verb = {"question": "asked", "reply": "replied", "answer": "answered"}.get(m.get("kind"), "noted")
                out.append(f"- {when} {who or m.get('kind')} {verb}: {m.get('text', '')}")
            return out

        answer = (f"Your final message is your answer: the runner shows it to the person in thread {thread['id']}."
                  " Do not use `nbs-cli post` for it.")
        if resume:
            return "\n".join([f"New in thread {thread['id']} since your last answer:"] + listed(messages[seen:])
                             + ["", answer])
        lines = self._intro(self.chat) + [
            "",
            f"A person asked you a question. This conversation is thread {thread['id']}: you answer it here,"
            " and their follow-ups resume this conversation.",
            seed_text(thread.get("seed") or {}),
        ]
        if seen:
            lines += ["", "Earlier in this thread:"] + listed(messages[:seen])
        lines += ["", "New:" if seen else "The question:"] + listed(messages[seen:])
        lines += self.snapshot(role=self.chat)
        lines += self.directive_lines(self.chat)
        folder = f"notebook/threads/{thread['id']}"
        lines += [
            "",
            answer,
            "- To see what another assistant role saw or did, read it first with `nbs-cli role show NAME`"
            " (its tile, record and notebook). Use `nbs-cli role request NAME \"...\"` only when you need that"
            " role's own tools (a re-plot with other keys, a re-check with its rules). Its answer comes back into"
            " this thread and resumes you.",
            "- If the person tells you something a role should keep doing or stop doing (\"U6 is known to charge,"
            " stop alerting on it\"), propose it: write it to"
            f" `{folder}/directive.md`, then `nbs-cli directive propose ROLE --file {folder}/directive.md"
            " --expires beamtime` (or a duration such as 12h, or a date). Only a person can confirm it; say in"
            " your answer that you proposed it and what it would change.",
            f"- Keep scratch files for this thread under `{folder}/`.",
        ]
        return "\n".join(lines)

    def thread_close_prompt(self, thread: dict[str, Any]) -> str:
        """The prompt that asks a quiet thread's session for its closing summary."""
        minutes = round(self.config.thread_idle_s / 60)
        return (
            f"Thread {thread['id']} has been quiet for {minutes} min and is closing. Reply with a summary of two to"
            " four lines: what was asked, what you found, any directive proposed, and anything left open. Your"
            " final message is the summary; it goes into notebook/threads.md, which later calls read. Run no"
            " commands."
        )

    def _thread_call(self, thread: dict[str, Any], closing: bool = False, fresh: bool = False) -> None:
        """
        Start a call for a thread: an answer to what is new in it, or its
        closing summary. It resumes the thread's Claude session unless the
        session does not exist yet or ``fresh`` asks for a new one.
        """
        state = self.chat
        thread_id = thread["id"]
        resume = bool(thread.get("started")) and not fresh
        if closing and not resume:
            self._close_thread(thread, None)
            return
        if fresh:
            thread = self.threads.update(thread_id, session_id=str(uuid.uuid4()), started=False)
        model = state.role.model if closing else state.role.alert_model
        prompt = self.thread_close_prompt(thread) if closing else self.thread_prompt(thread, resume)
        args = ["--role", "assistant"]
        if model:
            args += ["--model", model]
        args += ["--resume", thread["session_id"]] if resume else ["--session-id", thread["session_id"]]
        env = {
            "NBS_CLI_ROLE": state.cli_role, "NBS_AGENT_UID": "", "NBS_ASSISTANT_PROMPT": prompt,
            "NBS_MODEL": model or "", "NBS_ASSISTANT_CALL": "", "NBS_RECHECK_OF": "",
            "NBS_ASSISTANT_SKILL": state.role.skill, "NBS_ASSISTANT_THREAD": thread_id,
        }
        seen_before = thread.get("seen") or 0
        try:
            (Path(state.role.workdir).expanduser() / "notebook" / "threads" / thread_id).mkdir(parents=True, exist_ok=True)
            launched = state.launcher.launch(thread_id, None, f"assistant-thread-{thread_id}", extra_args=args, env=env)
        except Exception as exc:
            LOGGER.exception("Could not start a call for thread %s", thread_id)
            self.threads.add(thread_id, "error", f"could not start a call: {exc}", t=self.clock())
            self.threads.mark_seen(thread_id, len(thread.get("messages") or []))
            return
        self.thread_calls[thread_id] = {
            "t": self.clock(), "model": model, "closing": closing, "resume": resume, "fresh": fresh,
            "seen_before": seen_before, "pid": launched.pid, "log_path": launched.log_path,
        }
        self.threads.mark_seen(thread_id, len(thread.get("messages") or []), calling=True, started=True,
                               call_seen_before=seen_before, model=model)
        LOGGER.info("Assistant thread %s call (%s, %s, %s)", thread_id, model,
                    "closing" if closing else "resume" if resume else "new session", launched.pid)

    def _on_thread_exit(self, launched: LaunchedAgent, returncode: int) -> None:
        with self._lock:
            thread_id = launched.agent_uid
            call = self.thread_calls.pop(thread_id)
            result = final_result(launched.log_path) or {}
            failed = bool(result.get("is_error"))
            reply = None if failed else (result.get("result") or "").strip() or None
            served = served_model_file(launched.log_path)
            try:
                thread = self.threads.update(thread_id, calling=False)
                if call["closing"]:
                    self._close_thread(thread, reply if returncode == 0 else None)
                elif reply:
                    self.threads.add(thread_id, "answer", reply, model=call["model"], served_model=served,
                                     t=self.clock())
                elif call["resume"]:
                    # The session could not be resumed (or the call failed): start a new
                    # one that is shown the whole thread
                    LOGGER.warning("Thread %s call exited %s without an answer; retrying in a new session",
                                   thread_id, returncode)
                    thread = self.threads.mark_seen(thread_id, call["seen_before"])
                    self._thread_call(thread, fresh=True)
                else:
                    why = "timed out" if call.get("timed_out") else f"exited with status {returncode}"
                    if failed and result.get("result"):
                        why += f": {result['result']}"
                    self.threads.add(thread_id, "error", f"the assistant could not answer (call {why}; see"
                                     f" {launched.log_path})", t=self.clock())
            except Exception:
                LOGGER.exception("Could not record the end of thread %s's call", thread_id)
            self._check_threads()

    def _close_thread(self, thread: dict[str, Any], summary: Optional[str]) -> None:
        """Close a thread: its summary goes into the chat role's notebook/threads.md and onto its marker."""
        messages = thread.get("messages") or []
        question = messages[0]["text"] if messages else ""
        if not summary:
            answers = sum(1 for m in messages if m.get("kind") == "answer")
            summary = f"Asked: {question[:300]} ({answers} answer(s); closed without a summary)"
        summary = summary.strip()
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(thread.get("created_t") or self.clock()))
        heading = f"## {when} thread {thread['id']} ({thread.get('created_by')}): {question.splitlines()[0][:120] if question else ''}"
        path = Path(self.chat.role.workdir).expanduser() / "notebook" / "threads.md"
        try:
            old = path.read_text() if path.exists() else ""
            sections = [s for s in old.split("\n## ") if s.strip() and not s.startswith("# ")]
            sections = [s if s.startswith("## ") else "## " + s for s in sections][-(THREAD_SUMMARIES_KEPT - 1):]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# Closed threads (newest last)\n\n" + "\n\n".join(
                [s.strip() for s in sections] + [f"{heading}\n{summary}"]) + "\n")
        except OSError:
            LOGGER.exception("Could not write %s", path)
        try:
            self.threads.update(thread["id"], status="closed", summary=summary, closed_t=self.clock(), calling=False)
            marker = thread.get("marker") or {}
            if marker.get("record"):
                self.agent_log.annotate(marker["record"], lambda e: e.get("t") == marker.get("t") and e.get("thread"),
                                        thread_status="closed", thread_summary=summary)
        except Exception:
            LOGGER.exception("Could not close thread %s", thread["id"])


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
