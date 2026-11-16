"""
Deterministic work the runner does for headless assistant roles.

A role with ``worker = "plotter"`` gets no model call for its ``run`` and
``group_end`` events. The ``Plotter`` reduces each run with ``nbs-reduce
summary``, plots it with ``nbs-reduce plot`` (the normalized overlay of the
recent runs on the same plan, sample and edge, and the raw signal and
normalization), stores the figures (``att:ID``), keeps the role's tile and
record, and passes the reducer's surprise flags on as ``flag`` events (to
the integrity role, if one takes them). It calls the role's model only when
the keys come back ambiguous or the chosen signal is flat; that call picks
the keys, and its choice is kept per plan and set of data keys, so the next
such run is plotted without a call.

A role with ``worker = "health"`` has a ``Health`` watcher: it reads the
beam map and the status PVs every ``health_poll_s``, keeps the tile from
them, and raises a ``beam`` event (a model call, on the routine model) only
when the state changes for the worse, to explain it. The beam-loss alert
that triage raised for a run moves here.

Both run on their own threads, outside the assistant's lock, and take it
only to queue events.
"""

import hashlib
import json
import logging
import queue
import shlex
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Optional

from nbs_agents.tiles import TileStore

from .attachments import AttachmentStore

LOGGER = logging.getLogger(__name__)

AMBIGUOUS_STATUS = 3
# Signal-check problems that mean the chosen signal shows nothing
WEAK_PROBLEMS = ("zero", "flat", "no_structure", "all_non_finite")
PLOTS_DIR = "plots"
KEYS_CACHE_FILE = "keys-cache.json"
KEYS_CHOICE_DIR = "keys-choice"
# Characters of nbs-reduce output put into a keys call's prompt
REDUCE_OUTPUT_CHARS = 6000
RETRY_S = 5.0


def run_reduce(command: str, args: list[str], cwd: str, timeout: float) -> tuple[int, dict[str, Any]]:
    """
    Run one nbs-reduce command and read its JSON.

    Parameters
    ----------
    command : str
        The nbs-reduce command (split like a shell would).
    args : list of str
        Verb and arguments.
    cwd : str
        Working folder (plots are written inside it).
    timeout : float
        Seconds before it is stopped.

    Returns
    -------
    tuple of (int, dict)
        Exit status and the JSON it printed; ``{"error": ...}`` when it
        printed none.
    """
    cmd = shlex.split(command) + ["--indent", "0"] + args
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 1, {"error": f"{' '.join(cmd)} timed out after {timeout:.0f} s"}
    except OSError as exc:
        return 1, {"error": f"cannot run {cmd[0]}: {exc}"}
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        tail = (proc.stderr or proc.stdout or "").strip()[-500:]
        out = {"error": f"{cmd[0]} exited {proc.returncode} without JSON: {tail}"}
    return proc.returncode, out if isinstance(out, dict) else {"result": out}


def key_args(choice: Optional[dict[str, Any]]) -> list[str]:
    """``--signal``/``--norm`` arguments for a keys choice (none for None)."""
    if not choice:
        return []
    args = []
    for signal in choice.get("signal") or []:
        args += ["--signal", str(signal)]
    norm = choice.get("norm")
    args += ["--norm", str(norm) if norm else "none"]
    return args


def cache_key(plan: Any, data_keys: Any) -> str:
    """The plotter's key-choice cache key: the plan and a hash of the run's data keys."""
    digest = hashlib.sha1(",".join(sorted(str(k) for k in data_keys or [])).encode()).hexdigest()[:12]
    return f"{plan}-{digest}"


def weak_signals(summary: dict[str, Any]) -> list[str]:
    """The signals of an ``nbs-reduce summary`` whose signal check found nothing to see."""
    out = []
    for name, entry in (summary.get("signals") or {}).items():
        problems = set((entry.get("check") or {}).get("problems") or [])
        if problems & set(WEAK_PROBLEMS):
            out.append(f"{name} ({', '.join(sorted(problems & set(WEAK_PROBLEMS)))})")
    return out


def summary_line(job: dict[str, Any], summary: dict[str, Any]) -> str:
    """One line of what was reduced: the run, the keys and why, and each signal's edge jump and J/N."""
    keys = summary.get("keys") or {}
    norm = keys.get("norm")
    what = ", ".join(f"{s}/{norm}" if norm else str(s) for s in keys.get("signal") or [])
    head = f"scan {job.get('scan_id')} {job.get('plan')}"
    if job.get("sample"):
        head += f" on {job['sample']}"
    parts = [what] if what else []
    why = (keys.get("reasons") or {}).get("signal")
    for name, entry in (summary.get("signals") or {}).items():
        bits = []
        if entry.get("edge_jump") is not None:
            bits.append(f"jump {entry['edge_jump']:.3g}")
        if entry.get("jump_to_noise") is not None:
            bits.append(f"J/N {entry['jump_to_noise']:.3g}")
        if bits:
            parts.append(f"{name} " + ", ".join(bits))
    text = head + (": " + "; ".join(parts) if parts else "")
    if why:
        text += f" (keys: {why})"
    return text


class Plotter:
    """
    The plotter worker for one role.

    Parameters
    ----------
    agent : AssistantAgent
        The assistant (its lock, events, agent log and settings).
    state : RoleState
        The plotter role.
    attachments : AttachmentStore, optional
        Where figures go; the default store if omitted.
    reduce : callable, optional
        ``reduce(args, cwd) -> (status, dict)``, for tests; runs
        ``[assistant] reduce_command`` if omitted.
    sleep : callable, optional
        Waits between tries of a run Tiled does not have yet, for tests.
    """

    def __init__(self, agent: Any, state: Any, attachments: Optional[AttachmentStore] = None,
                 reduce: Optional[Callable] = None, sleep: Callable = time.sleep) -> None:
        self.agent = agent
        self.state = state
        self.attachments = attachments or AttachmentStore()
        config = agent.config
        self.reduce = reduce or (lambda args, cwd: run_reduce(config.reduce_command, args, cwd, config.reduce_timeout_s))
        self.sleep = sleep
        self.jobs: "queue.Queue[Optional[dict[str, Any]]]" = queue.Queue()
        # Recent plotted runs: (plan, sample, edge) -> uids, newest last
        self.recent: dict[tuple, deque] = {}
        self._thread: Optional[threading.Thread] = None

    @property
    def workdir(self) -> Path:
        return Path(self.state.role.workdir).expanduser()

    # ----------------------------------------------------------------- queue

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name=f"assistant-{self.state.name}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.jobs.put(None)

    def submit(self, event: dict[str, Any]) -> None:
        """Queue a ``run`` or ``group_end`` event (with ``run`` facts or ``runs``) for the worker."""
        self.jobs.put(event)

    def _loop(self) -> None:
        while True:
            job = self.jobs.get()
            if job is None:
                return
            try:
                self.handle(job)
            except Exception:
                LOGGER.exception("Plotter %s failed on %s", self.state.name, job.get("text"))

    def handle(self, event: dict[str, Any]) -> None:
        """Do one event (on the worker thread, or directly in tests)."""
        if event["kind"] == "run":
            self.plot_run(event["run"])
        elif event["kind"] == "group_end":
            self.plot_group(event.get("runs") or [])

    # ----------------------------------------------------------------- cache

    def _cache(self) -> dict[str, Any]:
        try:
            return json.loads((self.workdir / KEYS_CACHE_FILE).read_text())
        except (OSError, ValueError):
            return {}

    def choice(self, key: str) -> Optional[dict[str, Any]]:
        """The keys a model chose before for this plan and set of data keys, or None."""
        return self._cache().get(key)

    def remember(self, key: str, choice: dict[str, Any]) -> None:
        """Keep a keys choice for the next run with the same plan and data keys."""
        cache = self._cache()
        cache[key] = {"signal": list(choice.get("signal") or []), "norm": choice.get("norm") or None,
                      "why": choice.get("why"), "t": time.time()}
        self.workdir.mkdir(parents=True, exist_ok=True)
        (self.workdir / KEYS_CACHE_FILE).write_text(json.dumps(cache, indent=1))

    def choice_file(self, key: str) -> str:
        """Where a keys call writes its choice, relative to the working folder."""
        return f"{KEYS_CHOICE_DIR}/{key}.json"

    def collect_choices(self, events: list[dict[str, Any]]) -> None:
        """After a keys call: keep the choices it wrote, and drop the files."""
        for event in events:
            key = event.get("cache_key")
            if event.get("kind") != "keys" or not key:
                continue
            path = self.workdir / self.choice_file(key)
            try:
                choice = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(choice, dict) and choice.get("signal"):
                signal = choice["signal"]
                choice["signal"] = [signal] if isinstance(signal, str) else signal
                self.remember(key, choice)
                LOGGER.info("Plotter %s keeps keys %s for %s", self.state.name, choice, key)
            try:
                path.unlink()
            except OSError:
                pass

    # ----------------------------------------------------------------- work

    def _reduce(self, args: list[str], retry: bool = False) -> tuple[int, dict[str, Any]]:
        """Run nbs-reduce; with ``retry``, keep trying for ``reduce_wait_s`` while it fails (Tiled lag)."""
        self.workdir.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + (self.agent.config.reduce_wait_s if retry else 0)
        while True:
            status, out = self.reduce(args, str(self.workdir))
            if status in (0, AMBIGUOUS_STATUS) or time.monotonic() >= deadline:
                return status, out
            self.sleep(RETRY_S)

    def plot_run(self, run: dict[str, Any]) -> None:
        """
        Reduce and plot one finished run, or ask the model for keys.

        Parameters
        ----------
        run : dict
            Run facts (``run_facts``) with ``data_keys``.
        """
        if not run.get("points"):
            return
        uid = run["uid"]
        key = cache_key(run.get("plan"), run.get("data_keys"))
        choice = self.choice(key)
        status, summary = self._reduce(["summary", uid] + key_args(choice), retry=True)
        if status == AMBIGUOUS_STATUS:
            self.ask_for_keys(run, key, f"nbs-reduce could not settle the keys: {summary.get('error')}", summary)
            return
        if status != 0:
            self.report_error(run, f"nbs-reduce summary failed: {summary.get('error')}")
            return
        weak = weak_signals(summary)
        if weak and not choice:
            self.ask_for_keys(run, key, "the signal nbs-reduce chose shows nothing: " + ", ".join(weak), summary)
            return
        keys = summary.get("keys") or {}
        chosen = key_args({"signal": keys.get("signal"), "norm": keys.get("norm")})
        group = (run.get("plan"), run.get("sample"), run.get("edge"))
        recent = self.recent.setdefault(group, deque(maxlen=max(1, self.agent.config.plot_runs)))
        if uid not in recent:
            recent.append(uid)
        name = f"scan{run.get('scan_id') or uid[:8]}"
        figures = []
        for kind, uids, what in (("normalized", list(recent), "normalized overlay"), ("raw", [uid], "raw signal and norm")):
            out_file = f"{PLOTS_DIR}/{name}-{kind}.png"
            status, plotted = self._reduce(["plot"] + uids + ["--kind", kind, "-o", out_file] + chosen)
            if status != 0:
                LOGGER.warning("Plotter %s: %s plot of %s failed: %s", self.state.name, kind, uid, plotted.get("error"))
                continue
            try:
                att = self.attachments.put(
                    self.workdir / out_file, alt=f"{summary_line(run, {})}: {what}", run_uid=uid,
                    caption=plotted.get("caption"), role=self.state.name,
                )
            except (OSError, ValueError) as exc:
                LOGGER.warning("Plotter %s could not store %s: %s", self.state.name, out_file, exc)
                continue
            figures.append((what, att["id"]))
        flags = summary.get("flags") or []
        line = summary_line(run, summary)
        severity = "info" if flags or weak else "ok"
        tile = line + (f"; flags: {', '.join(flags)}" if flags else "") + (f"; weak: {', '.join(weak)}" if weak else "")
        text = tile + "".join(f"\n\n![{what}](att:{att})" for what, att in figures)
        self.post(severity, tile, text, [uid], figures[0][1] if figures else None)
        if flags:
            self.agent.worker_events(
                [("flag", f"{line}: reducer flag {flag}", [uid]) for flag in flags]
            )

    def plot_group(self, runs: list[dict[str, Any]]) -> None:
        """Overlay a finished group's runs (normalized), with the keys of its last run."""
        runs = [r for r in runs if r.get("points")][-max(1, self.agent.config.plot_runs):]
        if len(runs) < 2:
            return
        last = runs[-1]
        choice = self.choice(cache_key(last.get("plan"), last.get("data_keys")))
        name = f"group-scan{runs[0].get('scan_id')}-{runs[-1].get('scan_id')}"
        out_file = f"{PLOTS_DIR}/{name}.png"
        uids = [r["uid"] for r in runs]
        status, plotted = self._reduce(["plot"] + uids + ["--kind", "normalized", "-o", out_file] + key_args(choice))
        from .agent import describe_group

        what = describe_group(runs)
        if status != 0:
            LOGGER.warning("Plotter %s: group plot failed: %s", self.state.name, plotted.get("error"))
            return
        try:
            att = self.attachments.put(self.workdir / out_file, alt=f"{what}: normalized overlay",
                                       run_uids=uids, caption=plotted.get("caption"), role=self.state.name)
        except (OSError, ValueError) as exc:
            LOGGER.warning("Plotter %s could not store %s: %s", self.state.name, out_file, exc)
            return
        tile = f"group finished: {what}, normalized overlay"
        self.post("ok", tile, f"{tile}\n\n![{what}](att:{att['id']})", uids, att["id"])

    def ask_for_keys(self, run: dict[str, Any], key: str, why: str, output: dict[str, Any]) -> None:
        """Queue a ``keys`` call for the role's model, with nbs-reduce's output."""
        text = json.dumps(output, default=str, separators=(",", ":"))
        if len(text) > REDUCE_OUTPUT_CHARS:
            text = text[:REDUCE_OUTPUT_CHARS] + " …(cut)"
        event = (
            f"scan {run.get('scan_id')} {run.get('plan')} (run {run['uid']}): {why}.\n"
            f"nbs-reduce said: {text}"
        )
        self.post("info", f"scan {run.get('scan_id')}: choosing keys ({why})", None, [run["uid"]], None)
        self.agent.worker_events([("keys", event, [run["uid"]])], role=self.state,
                                 cache_key=key, choice_file=self.choice_file(key))

    def report_error(self, run: dict[str, Any], why: str) -> None:
        LOGGER.warning("Plotter %s: scan %s: %s", self.state.name, run.get("scan_id"), why)
        self.post("warn", f"scan {run.get('scan_id')}: could not plot ({why})", None, [run["uid"]], None)

    def post(self, severity: str, tile: str, text: Optional[str], run_uids: list[str], figure: Optional[str]) -> None:
        """Write the tile, and the record entry when ``text`` is given (both as the role)."""
        state = self.state
        try:
            TileStore.for_agent_log(self.agent.agent_log).post(
                state.name, severity, tile, run_uids=run_uids, figure=f"att:{figure}" if figure else None,
            )
            self.agent.agent_log.append(
                state.uid, "tile", tile, source=state.source, severity=severity, run_uids=run_uids or None,
                figure=f"att:{figure}" if figure else None, worker=True,
            )
            if text and "record" in state.role.sinks:
                self.agent.agent_log.append(state.uid, "summary", text, source=state.source,
                                            run_uids=run_uids or None, worker=True)
        except Exception:
            LOGGER.exception("Plotter %s could not post", state.name)


def status_signature(beam: Optional[dict[str, Any]], status: dict[str, Any]) -> dict[str, Any]:
    """
    What the health worker compares between reads: whether beam is expected
    and why, and each status PV that reads as a state (a string, such as a
    shutter's ``Open``), not as a number that changes all the time.
    """
    states = {
        name: entry.get("value") for name, entry in sorted((status or {}).items())
        if isinstance(entry, dict) and isinstance(entry.get("value"), str)
    }
    beam = beam or {}
    return {"expected": beam.get("expected"), "reason": beam.get("reason"),
            "endstation": beam.get("endstation"), "states": states}


def health_tile(sig: dict[str, Any], previous: Optional[dict[str, Any]], running: bool) -> tuple[str, str, list[str]]:
    """
    The health tile for a state.

    Parameters
    ----------
    sig : dict
        ``status_signature`` now.
    previous : dict or None
        The one before, or None on the first read.
    running : bool
        Whether the queue is running a plan.

    Returns
    -------
    tuple of (str, str, list of str)
        Severity, message, and the changes since ``previous``.
    """
    where = sig.get("endstation") or "the endstation"
    changes = []
    if previous is not None:
        if previous.get("expected") != sig.get("expected"):
            changes.append(f"beam expected {previous.get('expected')} -> {sig.get('expected')}")
        before, now = previous.get("states") or {}, sig.get("states") or {}
        for name in sorted(set(before) | set(now)):
            if before.get(name) != now.get(name):
                changes.append(f"{name}: {before.get(name)} -> {now.get(name)}")
    if sig.get("expected") is False:
        severity = "alert" if running else "warn"
        message = f"No beam expected at {where}: {sig.get('reason') or 'no reason given'}"
        if running:
            message += " (a plan is running)"
    elif sig.get("expected") is None:
        severity, message = "info", "No beam map: beam at the endstation is unknown"
    else:
        severity, message = "ok", f"Beam expected at {where}"
    disconnected = [n for n, v in (sig.get("states") or {}).items() if v.startswith("not connected")]
    if disconnected and severity == "ok":
        severity = "info"
    if disconnected:
        message += f"; not connected: {', '.join(disconnected)}"
    if changes:
        message += "; changed: " + "; ".join(changes)
    return severity, message, changes


SEVERITY_RANK = {"ok": 0, "info": 1, "warn": 2, "alert": 3}


class Health:
    """
    The health worker for one role: polls the beam map and status PVs.

    Parameters
    ----------
    agent : AssistantAgent
        The assistant (its beamline, lock, events and agent log).
    state : RoleState
        The health role.
    """

    def __init__(self, agent: Any, state: Any) -> None:
        self.agent = agent
        self.state = state
        self.last: Optional[dict[str, Any]] = None
        self.last_severity = "ok"
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name=f"assistant-{self.state.name}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while True:
            try:
                self.check()
            except Exception:
                LOGGER.exception("Health %s check failed", self.state.name)
            if self._stop.wait(self.agent.config.health_poll_s):
                return

    def read(self) -> tuple[dict[str, Any], bool]:
        """The state now, and whether a plan is running."""
        beamline = self.agent.beamline
        try:
            beam = beamline.beam()
        except Exception as exc:
            beam = {"expected": None, "reason": f"beam map unreadable: {type(exc).__name__}: {exc}"}
        try:
            status = beamline.status_pvs()
        except Exception as exc:
            LOGGER.warning("Health %s: status PVs unreadable: %s", self.state.name, exc)
            status = {}
        try:
            running = (beamline.manager.status() or {}).get("manager_state") not in (None, "idle")
        except Exception:
            running = False
        return status_signature(beam, status), running

    def check(self) -> Optional[dict[str, Any]]:
        """
        Read the state; on a change, write the tile, and queue a ``beam``
        event when it got worse.

        Returns
        -------
        dict or None
            ``severity``, ``message`` and ``changes`` when the tile was written.
        """
        sig, running = self.read()
        if sig == self.last:
            return None
        previous, self.last = self.last, sig
        severity, message, changes = health_tile(sig, previous, running)
        state = self.state
        try:
            TileStore.for_agent_log(self.agent.agent_log).post(state.name, severity, message)
            self.agent.agent_log.append(state.uid, "tile", message, source=state.source, severity=severity, worker=True)
        except Exception:
            LOGGER.exception("Health %s could not post its tile", state.name)
        worse = SEVERITY_RANK[severity] > SEVERITY_RANK[self.last_severity] or (
            severity != "ok" and previous is not None and changes)
        if worse:
            self.agent.worker_events([("beam", message, [])], role=state)
        self.last_severity = severity
        return {"severity": severity, "message": message, "changes": changes}

