"""
Core library behind nbs-cli: one beamline client with a state snapshot,
run summaries, a blocking wait, and verbs that only touch the caller's own
``open_agent`` / ``queue_stop`` / ``close_agent`` block.
"""

import json
import logging
import os
import pickle
import time
import tomllib
from datetime import datetime
from typing import Any, Optional

import numpy as np

from nbs_agents.agentlog import (
    ASSISTANT_POST_KINDS,
    AgentLog,
    assistant_uid,
    find_assistant_post,
    is_assistant_record,
    unread_operator_entries,
)
from ..assistant.attachments import AttachmentStore
from nbs_agents.constants import (
    AGENT_NAME_KEY,
    AGENT_OWNER_ENV,
    AGENT_SEQUENCE_KEY,
    AGENT_UID_KEY,
    CLOSE_AGENT_PLAN,
    OPEN_AGENT_PLAN,
    QUEUE_STOP_INSTRUCTION,
)
from ..queue import close_agent_owner, describe_owner
from ..beamline_info import DEVICES, METADATA, SAMPLES, BeamlineInfo, load_beamline_info
from .config import ASSISTANT_ROLE, AssistantConfig, CliConfig, RoleConfig
from ..tiles import TileError, TileStore, acknowledge
from ..threads import ThreadError, ThreadStore, open_thread
from ..directives import DIRECTIVE_STATUSES, DirectiveError, DirectiveStore, decide_directive, parse_expiry, \
    log_directive, revoke_directive
from ..reduce.metrics import SummaryContext, summarize

LOGGER = logging.getLogger(__name__)

AGENT_REASON_KEY = "agent_reason"
ACTIVE_STATUSES = ("opening", "active")
SENTINEL_PLANS = (OPEN_AGENT_PLAN, CLOSE_AGENT_PLAN)
SAMPLE_LAYOUT_ENTRY_POINT = "nbs_agents.sample_layout"
# Longest metadata value (as JSON) that state shows in full
METADATA_VALUE_CHARS = 300


class CliError(RuntimeError):
    """An error to report to the CLI user without a traceback."""


def writer_refs() -> dict[str, Any]:
    """
    What the runner told this Claude about itself, for the entries it writes.

    Returns
    -------
    dict
        ``model`` (``NBS_MODEL``), and for the assistant ``call``
        (``NBS_ASSISTANT_CALL``) and ``recheck_of`` (``NBS_RECHECK_OF``),
        each only when set.
    """
    out: dict[str, Any] = {}
    if os.environ.get("NBS_MODEL"):
        out["model"] = os.environ["NBS_MODEL"]
    for key, var in (("call", "NBS_ASSISTANT_CALL"), ("recheck_of", "NBS_RECHECK_OF")):
        try:
            out[key] = int(os.environ[var]) if key == "call" else float(os.environ[var])
        except (KeyError, ValueError):
            pass
    return out


def parse_since(text: str, now: float) -> float:
    """
    A start time from ``HH:MM`` or ``HH:MM:SS`` (today, local), epoch
    seconds, or an age such as ``30m``, ``2h`` or ``90s``.

    Raises
    ------
    CliError
        For anything else.
    """
    text = str(text).strip()
    units = {"s": 1, "m": 60, "h": 3600}
    if text[-1:] in units:
        try:
            return now - float(text[:-1]) * units[text[-1]]
        except ValueError:
            pass
    if text.count(":") in (1, 2):
        fmt = "%H:%M:%S" if text.count(":") == 2 else "%H:%M"
        try:
            clock = time.strptime(text, fmt)
        except ValueError:
            pass
        else:
            day = time.localtime(now)
            return time.mktime((day.tm_year, day.tm_mon, day.tm_mday, clock.tm_hour, clock.tm_min, clock.tm_sec, 0, 0, -1))
    try:
        return float(text)
    except ValueError:
        raise CliError(f"--since takes HH:MM, HH:MM:SS, epoch seconds or an age like 30m or 2h, not {text!r}") from None


def is_person(config: CliConfig) -> bool:
    """Whether this client is a person: not the assistant, and not run by the runner for a driver."""
    return not (config.is_assistant or os.environ.get("NBS_AGENT_UID") or os.environ.get("NBS_DRIVER"))


def login_name() -> str:
    """The person running this client."""
    import getpass

    return getpass.getuser()


def refuse_read_only(config: CliConfig, verb: str) -> None:
    """
    Refuse a verb that changes the queue or another agent's record.

    Parameters
    ----------
    config : CliConfig
        Client settings.
    verb : str
        The verb being refused, for the message.

    Raises
    ------
    CliError
        In the read-only assistant role.
    """
    if not config.drivers:
        raise CliError(
            f"{verb} is not allowed: Claude agents that drive the beamline are off here "
            "([nbs_cli] drivers = false)"
        )
    if config.read_only:
        raise CliError(
            f"{verb} is not allowed in the read-only assistant role; "
            "suggest the command to the operator in a post instead"
        )


def _check(response: dict[str, Any], what: str) -> dict[str, Any]:
    if not response.get("success", False):
        raise CliError(f"{what} failed: {response.get('msg', response)}")
    return response


def plain_reason(reason: Optional[str]) -> Optional[str]:
    """
    A beam map reason without the PV names and values the map IOC puts in it.

    The IOC writes ``"<node> blocked: <pv> = <value>"``, ``"<node> not
    selected: ..."``, ``"<node>: <pv> disconnected"`` or ``"beam off: ..."``;
    this keeps the part before the first ``": "``.
    """
    if not reason:
        return reason
    head, sep, rest = reason.partition(": ")
    if sep and rest.endswith(" disconnected"):
        return f"{head}: input disconnected"
    return head


def _iso(t: Optional[float]) -> Optional[str]:
    if t is None:
        return None
    return datetime.fromtimestamp(t).isoformat(timespec="seconds")


def _close_agent_uid(item: dict[str, Any]) -> Optional[str]:
    args = item.get("args") or []
    if args:
        return args[0]
    return (item.get("kwargs") or {}).get(AGENT_UID_KEY)


def compact_item(item: dict[str, Any], estimate: Optional[float] = None) -> dict[str, Any]:
    """
    Reduce a queue item to the fields a reader needs.

    Parameters
    ----------
    item : dict
        Queue item.
    estimate : float, optional
        Estimated duration in seconds.

    Returns
    -------
    dict
        Compact item.
    """
    out: dict[str, Any] = {"item_uid": item.get("item_uid"), "name": item.get("name")}
    if item.get("item_type") != "plan":
        out["item_type"] = item.get("item_type")
    if item.get("args"):
        out["args"] = item["args"]
    if item.get("kwargs"):
        out["kwargs"] = item["kwargs"]
    meta = item.get("meta") or {}
    if meta.get(AGENT_REASON_KEY):
        out["reason"] = meta[AGENT_REASON_KEY]
    if meta.get(AGENT_SEQUENCE_KEY) is not None:
        out["seq"] = meta[AGENT_SEQUENCE_KEY]
    if estimate is not None:
        out["est_s"] = round(estimate, 1)
    return out


def sample_layout(samples: dict[str, Any]) -> dict[str, Any]:
    """
    Where the samples sit on the holder, from the beamline package's
    ``nbs_agents.sample_layout`` entry point (sst-assistant's groups them by
    holder side, in order along the side).

    Parameters
    ----------
    samples : dict
        The loaded samples (``BeamlineInfo.samples``).

    Returns
    -------
    dict
        The layout.

    Raises
    ------
    CliError
        If no beamline package provides a layout.
    """
    from importlib.metadata import entry_points

    found = sorted(entry_points(group=SAMPLE_LAYOUT_ENTRY_POINT), key=lambda e: e.name)
    if not found:
        raise CliError("no sample layout for this beamline (no nbs_agents.sample_layout plugin installed)")
    return found[0].load()(samples)


def read_status_pv(pv: Any, timeout: float) -> dict[str, Any]:
    """
    Read one status PV for ``nbs-cli``.

    Parameters
    ----------
    pv : epics.PV
        The channel, created with ``form="ctrl"`` so it carries units and
        enum strings.
    timeout : float
        Seconds to wait for it to connect.

    Returns
    -------
    dict
        ``value``: a number for numeric scalars (with ``units`` when the PV
        has them), the state string for enums, text otherwise, or
        ``"not connected: <pv>"``.
    """
    if not pv.wait_for_connection(timeout=timeout):
        return {"value": f"not connected: {pv.pvname}"}
    value = pv.get(timeout=timeout)
    if value is None:
        return {"value": f"not connected: {pv.pvname}"}
    if "enum" not in str(pv.type) and isinstance(value, (int, float, np.integer, np.floating)):
        out = {"value": value.item() if isinstance(value, np.generic) else value}
        if pv.units:
            out["units"] = pv.units
        return out
    return {"value": pv.get(as_string=True, timeout=timeout)}

# What nbs-cli reports instead of a bare null when the beamline has no beam map
NO_BEAM_MAP = {
    "beam_map": False,
    "expected": None,
    "reason": "no beam map at this beamline, so beam status is unknown",
}


class Beamline:
    """
    Client for one beamline: QueueServer, Tiled, the document stream and the
    agent log, plus whatever the beamline's ``BeamlineInfo`` provider knows.

    Parameters
    ----------
    config : CliConfig, optional
        Connection settings. Loaded from the environment if omitted.
    """

    def __init__(self, config: Optional[CliConfig] = None, config_path: Optional[str] = None) -> None:
        self.config = config or CliConfig.load(config_path)
        self.config_path = config_path
        self._manager = None
        self._tiled = None
        self._info = None
        self._agent_log = None
        self._assistant = None

    # ------------------------------------------------------------------ clients

    @property
    def manager(self):
        """QueueServer API client."""
        if self._manager is None:
            from bluesky_queueserver_api.zmq import REManagerAPI

            manager = REManagerAPI(zmq_control_addr=self.config.qs_addr, timeout_recv=5)
            manager.user = self.config.qs_user
            # The assistant submits as a group with no plans; reads below name the group they need
            manager.user_group = (
                self.config.assistant_user_group if self.config.read_only else self.config.qs_user_group
            )
            self._manager = manager
        return self._manager

    @property
    def tiled(self):
        """Tiled catalog of runs."""
        if self._tiled is None:
            from nbs_agents.tiled_auth import TiledLoginRequired, connect

            try:
                self._tiled = connect(self.config)
            except TiledLoginRequired as exc:
                raise CliError(str(exc))
        return self._tiled

    @property
    def info(self) -> BeamlineInfo:
        """What the beamline package's provider knows (``nbs_agents.beamline_info``)."""
        if self._info is None:
            self._info = load_beamline_info(self)
        return self._info

    @property
    def assistant(self) -> AssistantConfig:
        """The ``[assistant]`` settings, for its roles."""
        if self._assistant is None:
            try:
                self._assistant = AssistantConfig.load(self.config_path)
            except ValueError as exc:
                raise CliError(f"bad [assistant] settings: {exc}") from exc
        return self._assistant

    @property
    def agent_log(self) -> AgentLog:
        """Agent-log records, in the ``[agent_log]`` Redis."""
        if self._agent_log is None:
            try:
                settings = self.config.agent_log_settings
            except ValueError as exc:
                raise CliError(str(exc)) from exc
            self._agent_log = AgentLog.from_settings(settings)
        return self._agent_log

    def close(self) -> None:
        """Close network clients."""
        if self._manager is not None:
            self._manager.close()
            self._manager = None
        if self._info is not None:
            self._info.close()
            self._info = None

    # ------------------------------------------------------------ beamline info

    def samples(self, full: bool = False) -> dict[str, Any]:
        """
        Samples loaded on the beamline.

        Parameters
        ----------
        full : bool, optional
            Return every field instead of name and description.

        Returns
        -------
        dict
            Samples keyed by sample id.
        """
        samples = self._samples()
        if full:
            return samples
        return {
            sid: {k: info.get(k) for k in ("name", "description") if info.get(k) not in (None, "")}
            for sid, info in samples.items()
        }

    def _samples(self) -> dict[str, Any]:
        samples = self.info.samples()
        if samples is None:
            raise CliError("samples are not available at this beamline")
        return samples

    def metadata(self, keys: Optional[list[str]] = None) -> dict[str, Any]:
        """
        The metadata the beamline will stamp on its next run (read only).

        Parameters
        ----------
        keys : list of str, optional
            Only these keys.

        Returns
        -------
        dict
            Key to value.
        """
        md = self.info.metadata(keys)
        if md is None:
            raise CliError("run metadata is not available at this beamline")
        return md

    def metadata_summary(self, limit: int = METADATA_VALUE_CHARS) -> dict[str, Any]:
        """
        ``metadata`` for ``state``: each value cut to ``limit`` characters of
        JSON, with a note to fetch it whole.
        """
        out = {}
        for key, value in sorted(self.metadata().items()):
            text = json.dumps(value, default=str)
            out[key] = value if len(text) <= limit else f"({len(text)} chars; nbs-cli metadata {key})"
        return out

    def sample_layout(self) -> dict[str, Any]:
        """
        Where the samples sit on the holder, side by side. See ``sample_layout``.

        Returns
        -------
        dict
            Layout of the loaded samples.
        """
        return sample_layout(self._samples())

    def plans_allowed(self) -> dict[str, Any]:
        """
        Plans this user group may submit, with parameter schemas.

        Returns
        -------
        dict
            Plan descriptions keyed by plan name.
        """
        response = self.manager.plans_allowed(user_group=self.config.qs_user_group)
        return _check(response, "plans_allowed")["plans_allowed"]

    def plan_info(self, name: str) -> dict[str, Any]:
        """
        Docstring and parameters for one plan.

        Parameters
        ----------
        name : str
            Plan name.

        Returns
        -------
        dict
            Plan description, with what the beamline adds (such as a time rule).
        """
        plans = self.plans_allowed()
        if name not in plans:
            raise CliError(f"plan {name!r} is not allowed or does not exist")
        plan = plans[name]
        params = []
        for p in plan.get("parameters", []):
            entry = {"name": p.get("name"), "kind": p.get("kind", {}).get("name")}
            for key in ("default", "description"):
                if p.get(key) not in (None, ""):
                    entry[key] = p[key]
            annotation = p.get("annotation", {}).get("type")
            if annotation:
                entry["type"] = annotation
            params.append(entry)
        out = {"name": name, "description": plan.get("description", ""), "parameters": params}
        out.update(self.info.plan_extras(name))
        return out

    def plans_overview(self) -> dict[str, Any]:
        """
        Allowed plan names and the XAS plan table.

        Returns
        -------
        dict
            ``allowed`` names and ``xas`` edges.
        """
        allowed = sorted(self.plans_allowed())
        if self.config.allowed_plans is not None:
            allowed = [p for p in allowed if p in self.config.allowed_plans]
        xas = {
            k: {kk: v.get(kk) for kk in ("name", "edge", "element") if v.get(kk)}
            for k, v in self.info.xas_plans().items()
        }
        return {"allowed": allowed, "xas": xas}

    def devices(
        self, movable_only: bool = False, node: Optional[str] = None, role: Optional[str] = None
    ) -> dict[str, Any]:
        """
        Devices this user group may pass to plans, such as motors for ``mv``.

        Parameters
        ----------
        movable_only : bool, optional
            Only list devices that can be moved.
        node : str, optional
            Only list devices on this beam map node.
        role : str, optional
            Only list devices with this role.

        Returns
        -------
        dict
            ``{name: {"class", "movable", "readable", "node", "desc"}}`` for
            each top-level device. ``node`` is the beam map node the device
            sits on, or None.
        """
        response = _check(
            self.manager.devices_allowed(user_group=self.config.qs_user_group), "devices_allowed"
        )
        info_by_name = self.info.device_info()
        node_by_name = self._map_device_nodes()
        out = {}
        for name, info in sorted(response.get("devices_allowed", {}).items()):
            movable = bool(info.get("is_movable"))
            if movable_only and not movable:
                continue
            static = info_by_name.get(name) or {}
            dev_node = static.get("node") or node_by_name.get(name)
            if node is not None and dev_node != node:
                continue
            if role is not None and role not in static.get("roles", []):
                continue
            out[name] = {
                "class": info.get("classname"),
                "movable": movable,
                "readable": bool(info.get("is_readable")),
                "node": dev_node,
                "desc": static.get("desc"),
            }
        return out

    def _map_device_nodes(self) -> dict[str, str]:
        """Device name to node, from the ``devices`` lists in the beam map."""
        nodes = (self.info.beam_map() or {}).get("nodes") or {}
        return {dev: name for name, entry in nodes.items() for dev in entry.get("devices", [])}

    def device(self, names: list[str]) -> dict[str, Any]:
        """
        Static description of devices: class, beam map node, groups, roles,
        description, axes and so on. No live values.

        From the beamline's ``BeamlineInfo`` provider.

        Parameters
        ----------
        names : list of str
            Device names.

        Returns
        -------
        dict
            ``{name: info}``; an unknown name maps to ``{"error": "unknown device"}``.
        """
        if DEVICES not in self.info.capabilities:
            raise CliError("device information is not available at this beamline")
        info = self.info.device_info(list(names))
        return {name: info.get(name) or {"error": "unknown device"} for name in names}

    # -------------------------------------------------------------------- queue

    def queue(self) -> dict[str, Any]:
        """
        Current queue and running item.

        Returns
        -------
        dict
            QueueServer ``queue_get`` response.
        """
        return _check(self.manager.queue_get(), "queue_get")

    def estimate(self, item: dict[str, Any], estimator: Any = None) -> Optional[float]:
        """
        Estimate a queue item's duration in seconds.

        Parameters
        ----------
        item : dict
            Queue item.
        estimator : callable, optional
            The provider's ``estimator()``, fetched once for many items.

        Returns
        -------
        float or None
            Estimate, if the beamline has one for the plan.
        """
        if estimator is None:
            estimator = self.info.estimator()
        return estimator(item) if estimator else None

    # ----------------------------------------------------------------- sessions

    def find_session(self, agent_uid: Optional[str] = None) -> Optional[dict[str, Any]]:
        """
        Find the caller's agent session record.

        Parameters
        ----------
        agent_uid : str, optional
            Explicit session UID. Otherwise the newest open session for
            ``config.agent_name`` is used.

        Returns
        -------
        dict or None
            Agent-log record. Assistant records are never a driver's session.
        """
        if agent_uid:
            record = self.agent_log.get(agent_uid)
            if record is None or (is_assistant_record(record, agent_uid) and not self.config.read_only):
                raise CliError(f"no agent session {agent_uid}")
            return record
        candidates = [
            r
            for uid, r in self.agent_log.records().items()
            if r.get("agent_name") == self.config.agent_name
            and r.get("status") in ACTIVE_STATUSES
            and not is_assistant_record(r, uid)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r.get("opened", 0))

    @staticmethod
    def split_queue(items: list[dict[str, Any]], agent_uid: Optional[str]) -> dict[str, Any]:
        """
        Split queue items into the agent's block and everything else.

        Parameters
        ----------
        items : list of dict
            Queue items.
        agent_uid : str or None
            Session UID.

        Returns
        -------
        dict
            ``block`` items, ``close_index``, ``queue_stop`` item,
            and ``before``/``after`` counts of other items.
        """
        close_index = None
        if agent_uid:
            for i, item in enumerate(items):
                if item.get("name") == CLOSE_AGENT_PLAN and _close_agent_uid(item) == agent_uid:
                    close_index = i
                    break
        out: dict[str, Any] = {"close_index": close_index, "block": [], "queue_stop": None, "owner": None}
        if close_index is None:
            out["before"], out["after"] = len(items), 0
            return out
        out["owner"] = close_agent_owner(items[close_index])
        stop_index = None
        if close_index > 0:
            prev = items[close_index - 1]
            if prev.get("item_type") == "instruction" and prev.get("name") == QUEUE_STOP_INSTRUCTION:
                stop_index = close_index - 1
                out["queue_stop"] = prev
        block_indices = {
            i
            for i, item in enumerate(items[:close_index])
            if (item.get("meta") or {}).get(AGENT_UID_KEY) == agent_uid
        }
        out["block"] = [items[i] for i in sorted(block_indices)]
        others_before = [
            i for i in range(close_index) if i not in block_indices and i != stop_index
        ]
        out["before"] = len(others_before)
        out["after"] = len(items) - close_index - 1
        out["block_at_head"] = not others_before
        return out

    @staticmethod
    def check_owner(split: dict[str, Any], agent_uid: str) -> None:
        """
        Refuse an agent launched by one runner changing a block another runner owns.

        A Claude that nbs-agents-runner launches carries its runner's id in
        ``$NBS_AGENT_OWNER``; the runner that claimed the block stamped its own
        on the block's ``close_agent``. Without either, nothing is checked.

        Parameters
        ----------
        split : dict
            ``split_queue`` of the queue for ``agent_uid``.
        agent_uid : str
            Session UID.

        Raises
        ------
        CliError
            If both are set and differ.
        """
        token = os.environ.get(AGENT_OWNER_ENV)
        owner = split.get("owner")
        if token and owner and owner["id"] != token:
            raise CliError(
                f"session {agent_uid}'s block belongs to the {describe_owner(owner)}; this agent was launched "
                "by another runner and must not change it. Stop here: two runners are serving this beamline"
            )

    def _session_view(self, record: dict[str, Any], items: list, estimator: Any, running: dict) -> tuple:
        uid = record["agent_uid"]
        split = self.split_queue(items, uid)
        block = [compact_item(i, self.estimate(i, estimator)) for i in split["block"]]
        queued_s = sum(b.get("est_s") or 0 for b in block)
        if running and (running.get("meta") or {}).get(AGENT_UID_KEY) == uid:
            queued_s += self.estimate(running, estimator) or 0
        view = {
            "agent_uid": uid,
            "agent_name": record.get("agent_name"),
            "goal": record.get("goal"),
            "status": record.get("status"),
            "opened": _iso(record.get("opened")),
            "block": block,
            "stop_in_place": split["queue_stop"] is not None,
            "close_in_queue": split["close_index"] is not None,
            "owner": split["owner"],
        }
        budget = dict(record.get("budget") or {})
        if record.get("opened"):
            budget["used_s"] = round(time.time() - record["opened"])
            budget["queued_s"] = round(queued_s)
            if budget.get("time_s"):
                budget["left_s"] = round(budget["time_s"] - budget["used_s"] - queued_s)
        view["budget"] = budget
        view["log_tail"] = [
            {k: v for k, v in e.items() if k != "t"} | {"time": _iso(e.get("t"))}
            for e in (record.get("log") or [])[-5:]
        ]
        return view, split

    # --------------------------------------------------------------------- runs

    def _summary_context(self) -> SummaryContext:
        return SummaryContext(self.config.signal, self.config.norm, self.info.xas_plans(),
                              reference=self.config.reference)

    def get_run(self, ref: Any):
        """
        Look up a run by UID, UID prefix, scan id, or negative index.

        Parameters
        ----------
        ref : str or int
            ``"-1"`` is the latest run; digits are a scan id; anything else a UID.

        Returns
        -------
        BlueskyRun
            Tiled run.
        """
        from ..reduce.runs import find_run

        try:
            return find_run(self.tiled, ref)
        except LookupError as exc:
            raise CliError(str(exc)) from None

    def run_data(self, run) -> dict[str, np.ndarray]:
        """
        Primary-stream arrays of a run.

        Parameters
        ----------
        run : BlueskyRun
            Tiled run.

        Returns
        -------
        dict
            Arrays keyed by data key.
        """
        if "primary" not in run:
            return {}
        ds = run["primary"].read()
        return {k: np.asarray(ds[k].values) for k in ds.data_vars}

    def run_baseline(self, run, keys=None) -> dict[str, list]:
        """
        Start and end baseline readings of a run.

        Parameters
        ----------
        run : BlueskyRun
            Tiled run.
        keys : sequence of str, optional
            Baseline data keys; missing ones are skipped. Defaults to the
            config's ``baseline_keys``.

        Returns
        -------
        dict
            ``{key: [start, end]}`` to 4 significant figures.
        """
        if keys is None:
            keys = self.config.baseline_keys
        out = {}
        try:
            if "baseline" not in run:
                return out
            stream = run["baseline"]
            for key in keys:
                if key in stream:
                    values = np.asarray(stream[key].read(), dtype=float).ravel()
                    if values.size:
                        out[key] = [float(f"{values[0]:.4g}"), float(f"{values[-1]:.4g}")]
        except Exception:  # baseline extras must never break a summary
            LOGGER.debug("baseline read failed", exc_info=True)
        return out

    def run_summary(self, run, ctx: Optional[SummaryContext] = None, data: bool = True) -> dict[str, Any]:
        """
        Compact description of a run.

        Parameters
        ----------
        run : BlueskyRun
            Tiled run.
        ctx : SummaryContext, optional
            Reducer settings.
        data : bool, optional
            Whether to read data and add a reducer summary.

        Returns
        -------
        dict
            Run description.
        """
        start = dict(run.metadata["start"])
        stop = run.metadata.get("stop") or {}
        out = {
            "uid": start["uid"],
            "scan_id": start.get("scan_id"),
            "plan": start.get("plan_name"),
            "time": _iso(start.get("time")),
            "status": stop.get("exit_status", "running"),
        }
        if stop.get("time"):
            out["duration_s"] = round(stop["time"] - start["time"], 1)
        if stop.get("reason"):
            out["stop_reason"] = stop["reason"]
        sample = start.get("sample_id") or start.get("sample_name")
        if sample:
            out["sample"] = sample
        for key in ("edge", "element"):
            if start.get(key):
                out[key] = start[key]
        for key in (AGENT_UID_KEY, AGENT_REASON_KEY, AGENT_SEQUENCE_KEY):
            if start.get(key) is not None:
                out[key] = start[key]
        if data and start.get("plan_name") not in SENTINEL_PLANS:
            out.update(self.run_baseline(run))
            try:
                out["summary"] = summarize(start, self.run_data(run), ctx or self._summary_context())
            except Exception as exc:  # summaries must never break state
                out["summary"] = {"error": f"{type(exc).__name__}: {exc}"}
        return out

    def recent_runs(self, n: int = 5, agent_uid: Optional[str] = None, data: bool = True) -> list[dict]:
        """
        Summaries of the newest runs, newest first.

        Parameters
        ----------
        n : int, optional
            Number of runs.
        agent_uid : str, optional
            Only runs from this agent session.
        data : bool, optional
            Whether to add reducer summaries.

        Returns
        -------
        list of dict
            Run descriptions.
        """
        from tiled.queries import Key

        catalog = self.tiled
        if agent_uid:
            catalog = catalog.search(Key(AGENT_UID_KEY) == agent_uid)
        runs = list(catalog.values()[-1 : -n - 1 : -1]) if n > 0 else []
        ctx = self._summary_context()
        return [self.run_summary(r, ctx, data=data) for r in runs]

    @property
    def attachments(self) -> AttachmentStore:
        """Store for figures that messages link as ``att:ID``."""
        return AttachmentStore(self.config.attachments_dir or None)

    def attach(self, path: str, alt: str = "", run_uid: Optional[str] = None) -> dict[str, Any]:
        """
        Store a figure for messages to show.

        Parameters
        ----------
        path : str
            Image file (PNG, JPEG, GIF, WebP or SVG).
        alt : str, optional
            One-line description; shown under the figure and in plain views.
        run_uid : str, optional
            Run the figure is about.

        Returns
        -------
        dict
            Attachment metadata, plus ``markdown``: the ``![alt](att:ID)``
            link to put in a note, question, summary or post.
        """
        if not os.path.isfile(path):
            raise CliError(f"no such file {path!r}")
        alt = " ".join((alt or "").split()).replace("]", ")").replace("[", "(")
        try:
            info = self.attachments.put(path, alt=alt, run_uid=run_uid)
        except ValueError as exc:
            raise CliError(str(exc))
        info["markdown"] = f"![{alt}](att:{info['id']})"
        return info

    def attach_run_plot(self, run, alt: str = "") -> dict[str, Any]:
        """
        Plot a run (as ``plot_run``) and store the figure.

        Parameters
        ----------
        run : BlueskyRun
            Tiled run.
        alt : str, optional
            Description; defaults to the scan id, plan and sample.

        Returns
        -------
        dict
            As ``attach``.
        """
        import tempfile

        start = run.metadata["start"]
        if not alt:
            alt = f"scan {start.get('scan_id')} {start.get('plan_name')}"
            if start.get("sample_name") or start.get("sample_id"):
                alt += f" {start.get('sample_name') or start.get('sample_id')}"
        with tempfile.TemporaryDirectory() as tmp:
            path = self.plot_run(run, os.path.join(tmp, "plot.png"))
            return self.attach(path, alt=alt, run_uid=start.get("uid"))

    def plot_run(self, run, path: str) -> str:
        """
        Save a PNG plot of a run's signal against its scan axis.

        Parameters
        ----------
        run : BlueskyRun
            Tiled run.
        path : str
            Output file.

        Returns
        -------
        str
            The output path.
        """
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        from ..reduce.metrics import dimension_keys

        start = run.metadata["start"]
        data = self.run_data(run)
        dims = [d for d in dimension_keys(start) if d in data]
        x = data[dims[0]] if dims else np.arange(max(len(v) for v in data.values()))
        fig, axes = plt.subplots(2, 1, figsize=(7, 6), sharex=True)
        sig, norm = self.config.signal, self.config.norm
        for key in start.get("detectors", []):
            if key in data and np.asarray(data[key]).ndim == 1:
                axes[0].plot(x, data[key], label=key)
        axes[0].legend()
        axes[0].set_ylabel("raw")
        if sig in data and norm in data:
            axes[1].plot(x, np.asarray(data[sig]) / np.asarray(data[norm]), "k.-")
            axes[1].set_ylabel(f"{sig}/{norm}")
        axes[1].set_xlabel(dims[0] if dims else "point")
        title = f"scan {start.get('scan_id')} {start.get('plan_name')}"
        if start.get("sample_name") or start.get("sample_id"):
            title += f" {start.get('sample_name') or start.get('sample_id')}"
        fig.suptitle(title)
        fig.tight_layout()
        fig.savefig(path, dpi=100)
        plt.close(fig)
        return path

    # -------------------------------------------------------------------- state

    def status_pv_names(self) -> dict[str, dict[str, str]]:
        """
        The status PVs the beam map file names in its ``[status]`` table.

        Returns
        -------
        dict
            Name to ``{"pv": ..., "desc": ...}``; an entry given as a bare PV
            name gets an empty ``desc``. Empty without a beam map file.

        Raises
        ------
        CliError
            If the beam map file cannot be read.
        """
        if not self.config.beam_map_file:
            return {}
        try:
            with open(os.path.expanduser(self.config.beam_map_file), "rb") as f:
                specs = tomllib.load(f).get("status", {})
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise CliError(f"cannot read beam_map_file {self.config.beam_map_file}: {exc}") from exc
        out = {}
        for name, spec in specs.items():
            if isinstance(spec, str):
                spec = {"pv": spec}
            if isinstance(spec, dict) and isinstance(spec.get("pv"), str):
                out[name] = {"pv": spec["pv"], "desc": str(spec.get("desc", ""))}
        return out

    def status_pvs(self) -> dict[str, Any]:
        """
        Read the beam map file's status PVs directly over Channel Access.

        Returns
        -------
        dict
            Name to ``{"value": ..., "desc": ...}`` (``desc`` only when the map
            gives one); enum PVs read as their state strings, and a PV that
            does not answer as ``"not connected: <pv>"``. Empty when the map
            file names none.
        """
        specs = self.status_pv_names()
        if not specs:
            return {}
        if self.config.epics_ca_addr_list:
            os.environ["EPICS_CA_ADDR_LIST"] = self.config.epics_ca_addr_list
            os.environ["EPICS_CA_AUTO_ADDR_LIST"] = "NO"
        import epics

        # Create every channel first so they connect in parallel
        pvs = {
            name: epics.get_pv(spec["pv"], form="ctrl", connect=False)
            for name, spec in specs.items()
        }
        out = {}
        for name, pv in pvs.items():
            out[name] = read_status_pv(pv, self.config.status_pv_timeout_s)
            if specs[name]["desc"]:
                out[name]["desc"] = specs[name]["desc"]
        return out

    def beam(self, full: bool = False) -> Optional[dict[str, Any]]:
        """
        Whether beam is expected at this beamline's endstation, from the
        beamline's beam map (``BeamlineInfo.beam_map``).

        Parameters
        ----------
        full : bool, optional
            Also include the beamline map: every node's upstream, children
            and status.

        Returns
        -------
        dict or None
            ``endstation``, ``expected``, ``reason`` and ``updated`` (plus
            ``nodes`` with ``full``), or None if the beamline has no beam map.
        """
        data = self.info.beam_map()
        if not data:
            return None
        out = {
            "endstation": data.get("endstation"),
            "expected": data.get("beam_expected"),
            "reason": plain_reason(data.get("reason")),
            "updated": data.get("updated"),
        }
        if full:
            out["nodes"] = {
                name: {**entry, "reason": plain_reason(entry.get("reason"))}
                for name, entry in (data.get("nodes") or {}).items()
            }
        return out

    def diagnose(self) -> dict[str, Any]:
        """
        Walk the beam map from the endstation up to the source, with each
        node's status and the devices on it, to find why there is no beam.

        Reports only the map status and static device descriptions; it reads
        no device values.

        Returns
        -------
        dict
            ``endstation``, ``beam_expected``, ``reason``, ``blocking`` (the
            blocking nodes on the path, source first) and ``path``: one entry
            per node from the endstation upstream, each with ``node``,
            ``desc``, ``blocking``, ``path_clear``, ``beam_expected``,
            ``reason`` and ``devices`` (``name``, ``desc``, ``roles``).
        """
        data = self.info.beam_map()
        if not data:
            raise CliError("beam diagnosis is not available at this beamline (it has no beam map)")
        nodes = data.get("nodes") or {}
        endstation = data.get("endstation")
        if endstation not in nodes:
            raise CliError("the beam map has no endstation")
        info_by_name = self.info.device_info()
        by_node: dict[str, list] = {}
        for dev, info in info_by_name.items():
            if isinstance(info, dict) and info.get("node") and not info.get("alias_of"):
                by_node.setdefault(info["node"], []).append(dev)
        path = []
        name, seen = endstation, set()
        while name is not None and name in nodes and name not in seen:
            seen.add(name)
            entry = nodes[name]
            devices = []
            for dev in by_node.get(name) or entry.get("devices", []):
                info = info_by_name.get(dev) or {}
                if info.get("alias_of"):
                    continue
                devices.append({"name": dev, "desc": info.get("desc"), "roles": info.get("roles", [])})
            path.append({
                "node": name,
                "desc": entry.get("desc", ""),
                "blocking": entry.get("blocking"),
                "path_clear": entry.get("path_clear"),
                "beam_expected": entry.get("beam_expected"),
                "reason": plain_reason(entry.get("reason")),
                "devices": devices,
            })
            name = entry.get("upstream")
        return {
            "endstation": endstation,
            "beam_expected": data.get("beam_expected"),
            "reason": plain_reason(data.get("reason")),
            "updated": data.get("updated"),
            "blocking": [p["node"] for p in reversed(path) if p["blocking"]],
            "path": path,
        }

    def capabilities(self) -> list[str]:
        """
        What this beamline can answer beyond the queue, runs and agent log:
        the provider's capabilities, plus ``status_pvs`` with a beam map file.
        """
        out = set(self.info.capabilities)
        if self.config.beam_map_file:
            out.add("status_pvs")
        return sorted(out)

    def state(self, full: bool = False, agent_uid: Optional[str] = None, n_runs: int = 5) -> dict[str, Any]:
        """
        One JSON snapshot of the beamline.

        Parameters
        ----------
        full : bool, optional
            Include full queue items, samples and the whole agent log.
        agent_uid : str, optional
            Session to describe; defaults to the newest open session.
        n_runs : int, optional
            Number of recent runs. 0 reads nothing from Tiled: no recent
            runs, and no elapsed time for the running item.

        Returns
        -------
        dict
            State document.
        """
        errors: dict[str, str] = {}
        status = self.manager.status()
        queue = self.queue()
        items = queue.get("items", [])
        running = queue.get("running_item") or {}
        estimator = self.info.estimator()

        out: dict[str, Any] = {"time": _iso(time.time()), "capabilities": self.capabilities()}
        out["manager"] = {
            "state": status.get("manager_state"),
            "re_state": status.get("re_state"),
            "environment_open": status.get("worker_environment_exists"),
            "queue_stop_pending": status.get("queue_stop_pending"),
            "items_in_queue": status.get("items_in_queue"),
        }

        if running:
            run_view = compact_item(running, self.estimate(running, estimator))
            if n_runs > 0:
                try:
                    latest = self.tiled.values()[-1:-2:-1]
                    latest = next(iter(latest), None)
                    if latest is not None and not latest.metadata.get("stop"):
                        started = latest.metadata["start"]["time"]
                        run_view["elapsed_s"] = round(time.time() - started)
                        if run_view.get("est_s") is not None:
                            run_view["est_remaining_s"] = max(0, round(run_view["est_s"] - run_view["elapsed_s"]))
                except Exception as exc:
                    errors["running"] = str(exc)
            out["running"] = run_view
        else:
            out["running"] = None

        record = None
        try:
            record = self.find_session(agent_uid)
        except Exception as exc:
            errors["agent"] = str(exc)
        uid = record["agent_uid"] if record else None
        if record:
            view, split = self._session_view(record, items, estimator, running)
            if full:
                view["log"] = record.get("log", [])
                view.pop("log_tail")
            out["agent"] = view
            if not self.config.read_only:
                out["operator"] = self.operator_inbox(uid)
        else:
            split = self.split_queue(items, None)
            out["agent"] = None
        out["queue_other"] = {"before": split["before"], "after": split["after"]}
        if full:
            out["queue"] = [compact_item(i, self.estimate(i, estimator)) for i in items]

        try:
            out["beam"] = self.beam() or dict(NO_BEAM_MAP)
        except Exception as exc:
            errors["beam"] = str(exc)
        try:
            status = self.status_pvs()
            if status:
                out["status"] = status
        except Exception as exc:
            errors["status"] = str(exc)
        if METADATA in self.info.capabilities:
            try:
                out["metadata"] = self.metadata_summary()
            except Exception as exc:
                errors["metadata"] = str(exc)
        if SAMPLES in self.info.capabilities:
            try:
                out["samples"] = self.samples(full=full)
            except Exception as exc:
                errors["samples"] = str(exc)
        try:
            out["plans"] = self.plans_overview()
        except Exception as exc:
            errors["plans"] = str(exc)
        if n_runs > 0:
            try:
                out["recent_runs"] = self.recent_runs(n_runs, agent_uid=None)
            except Exception as exc:
                errors["recent_runs"] = str(exc)
        if errors:
            out["errors"] = errors
        return out

    # --------------------------------------------------------------------- wait

    def wait(self, timeout: float = 600, until: str = "run", agent_uid: Optional[str] = None) -> dict[str, Any]:
        """
        Block until a run finishes or the manager state changes.

        Parameters
        ----------
        timeout : float, optional
            Maximum seconds to wait.
        until : {"run", "idle", "change"}, optional
            ``run`` returns after the next non-sentinel run stops (or when the
            queue goes idle); ``idle`` when the manager is idle; ``change`` on
            either a run stop or any manager-state change. Every mode also
            returns with reason ``beam_lost`` when the beam map stops
            expecting beam at the endstation.
        agent_uid : str, optional
            Session to report on.

        Returns
        -------
        dict
            ``reason``, the before/after manager summary, and finished runs.
        """
        if until not in ("run", "idle", "change"):
            raise CliError(f"unknown --until {until!r}")
        stream = DocumentStream(self.config.zmq_addr)
        try:

            def snapshot():
                st = self.manager.status()
                return {
                    "state": st.get("manager_state"),
                    "items_in_queue": st.get("items_in_queue"),
                    "running_item_uid": st.get("running_item_uid"),
                    "plan_queue_uid": st.get("plan_queue_uid"),
                }

            try:
                session = self.find_session(agent_uid)
            except CliError:
                session = None
            session_uid = session["agent_uid"] if session else None

            def operator_waiting():
                if session_uid is None or self.config.read_only:
                    return False
                return bool(unread_operator_entries(self.agent_log.get(session_uid) or {}))

            def beam_expected():
                try:
                    return self.beam().get("expected")
                except Exception:
                    return None

            before = snapshot()
            beam_before = beam_expected()
            t0 = time.time()
            deadline = t0 + timeout
            stopped: list[str] = []
            reason = "timeout"
            # Give the subscription a moment, then check whether there is anything to wait for.
            stream.poll(0.3)
            if operator_waiting():
                reason = "operator_message"
                deadline = t0
            elif until in ("run", "idle") and before["state"] == "idle":
                reason = "idle"
                deadline = t0  # nothing running
            last_poll = 0.0
            current = before
            while time.time() < deadline:
                for name, doc in stream.poll(0.5):
                    if name == "stop" and stream.plan_name(doc) not in SENTINEL_PLANS:
                        stopped.append(doc["run_start"])
                if stopped and until in ("run", "change"):
                    reason = "run_stopped"
                    break
                if time.time() - last_poll > 1.0:
                    last_poll = time.time()
                    if operator_waiting():
                        reason = "operator_message"
                        break
                    if beam_before is not False and beam_expected() is False:
                        reason = "beam_lost"
                        break
                    current = snapshot()
                    if until == "change" and current != before:
                        reason = "state_changed"
                        break
                    if until in ("run", "idle") and current["state"] == "idle":
                        # Let a stop document that raced the status poll arrive.
                        for name, doc in stream.poll(1.0):
                            if name == "stop" and stream.plan_name(doc) not in SENTINEL_PLANS:
                                stopped.append(doc["run_start"])
                        reason = "run_stopped" if (stopped and until == "run") else "idle"
                        break
            after = snapshot()
        finally:
            stream.close()

        out: dict[str, Any] = {
            "reason": reason,
            "waited_s": round(time.time() - t0, 1),
            "manager": {"before": before["state"], "after": after["state"]},
            "items_in_queue": {"before": before["items_in_queue"], "after": after["items_in_queue"]},
        }
        runs = []
        ctx = self._summary_context() if stopped else None
        for uid in stopped:
            runs.append(self._summary_when_ready(uid, ctx))
        out["finished_runs"] = runs
        if not runs:
            # A run may have finished just before the wait started; show the latest one.
            try:
                latest = next(iter(self.tiled.values()[-1:-2:-1]), None)
                if latest is not None:
                    out["latest_run"] = self.run_summary(latest)
            except Exception as exc:
                out["latest_run"] = {"error": str(exc)}
        try:
            record = self.find_session(agent_uid)
        except CliError:
            record = None
        if record:
            queue = self.queue()
            split = self.split_queue(queue.get("items", []), record["agent_uid"])
            running = queue.get("running_item") or {}
            out["agent"] = {
                "status": record.get("status"),
                "running": (
                    compact_item(running)
                    if (running.get("meta") or {}).get(AGENT_UID_KEY) == record["agent_uid"]
                    else None
                ),
                "block_pending": len(split["block"]),
                "stop_in_place": split["queue_stop"] is not None,
            }
            if not self.config.read_only:
                out["operator"] = self.operator_inbox(record["agent_uid"])
        return out

    def _summary_when_ready(self, uid: str, ctx: SummaryContext, timeout: float = 10) -> dict[str, Any]:
        deadline = time.time() + timeout
        while True:
            try:
                run = self.tiled[uid]
                if run.metadata.get("stop") or time.time() > deadline:
                    return self.run_summary(run, ctx)
            except KeyError:
                if time.time() > deadline:
                    return {"uid": uid, "error": "run not found in Tiled"}
            time.sleep(0.5)

    # -------------------------------------------------------------------- verbs

    def is_driver(self, record: dict[str, Any]) -> bool:
        """
        Whether this client drives the session.

        A session opened with a driver token belongs to the client that has
        the same token; a session opened without one belongs to any client.
        The read-only assistant never drives a session.
        """
        if self.config.read_only:
            return False
        driver = record.get("driver")
        return not driver or driver == self.config.driver

    def operator_inbox(self, agent_uid: str) -> Optional[dict[str, Any]]:
        """
        Collect operator messages the agent has not seen.

        The session's driver marks them seen. Any other client (an observer
        running ``state``) only peeks, so it cannot swallow a message meant for
        the agent.

        Parameters
        ----------
        agent_uid : str
            Session UID.

        Returns
        -------
        dict or None
            ``messages``, ``stop_requested`` and an ``instruction``, or
            ``None`` when there is nothing new and no stop.
        """
        record = self.agent_log.get(agent_uid) or {}
        driver = self.is_driver(record)
        if driver:
            messages = self.agent_log.take_operator_messages(agent_uid)
            record = self.agent_log.get(agent_uid) or {}
        else:
            messages = unread_operator_entries(record)
        stop = record.get("stop_requested")
        if not messages and not stop:
            return None
        out: dict[str, Any] = {
            "messages": [
                {"time": _iso(m.get("t")), "kind": m.get("kind"), "text": m.get("text"), "source": m.get("source")}
                for m in messages
            ],
            "stop_requested": bool(stop),
        }
        if not driver:
            out["peek"] = True
            out["instruction"] = "Read-only view: another client drives this session; these stay unread for it."
            return out
        if stop:
            out["instruction"] = (
                "The operator asked this session to stop: add is refused. Wrap up now with "
                "`nbs-cli finalize --summary ...` (add --drop-pending to skip queued scans). If you do not, "
                "the runner winds the block up itself within a few minutes and drops the queued scans."
            )
        else:
            out["instruction"] = "Messages from the operator at the beamline. Read and follow them before continuing."
        return out

    def _require_session(self, agent_uid: Optional[str]) -> dict[str, Any]:
        record = self.find_session(agent_uid)
        if record is None:
            raise CliError(
                f"no open {self.config.agent_name!r} session; run `nbs-cli open --goal ...` first"
            )
        if record.get("status") != "active":
            raise CliError(f"session {record['agent_uid']} is {record.get('status')}, not active")
        return record

    def _maybe_start(self, agent_uid: str) -> bool:
        """Start the queue if it is idle and the head belongs to this session."""
        refuse_read_only(self.config, "start the queue")
        status = self.manager.status()
        if status.get("manager_state") != "idle" or not status.get("worker_environment_exists"):
            return False
        items = self.queue().get("items", [])
        if not items:
            return False
        head = items[0]
        ours = (head.get("meta") or {}).get(AGENT_UID_KEY) == agent_uid or (
            head.get("name") == CLOSE_AGENT_PLAN and _close_agent_uid(head) == agent_uid
        )
        if not ours:
            return False
        _check(self.manager.queue_start(), "queue_start")
        return True

    def open_session(
        self,
        goal: str,
        budget_s: Optional[float] = None,
        start: bool = True,
        wait_s: float = 60,
    ) -> dict[str, Any]:
        """
        Queue an ``open_agent`` / ``queue_stop`` / ``close_agent`` block.

        Parameters
        ----------
        goal : str
            What the session is for.
        budget_s : float, optional
            Time budget in seconds.
        start : bool, optional
            Start the queue if the block is at its head.
        wait_s : float, optional
            Seconds to wait for the supervisor to assign the session UID.

        Returns
        -------
        dict
            Session UID (if known), and whether the queue was started.
        """
        refuse_read_only(self.config, "open")
        from nbs_agents.queue import submit_agent_block

        if not goal or not goal.strip():
            raise CliError("--goal is required")
        existing = self.find_session()
        if existing is not None:
            raise CliError(
                f"session {existing['agent_uid']} is already {existing.get('status')}; finalize it first"
            )
        if not self.agent_log.runner_alive():
            raise CliError(
                "nbs-agents-runner is not running (no heartbeat in Redis), so no session can open; "
                "ask the operator to start it (`pixi run agent-runner` in nbs-agents)"
            )
        params: dict[str, Any] = {"goal": goal, "driver": "nbs-cli"}
        if budget_s:
            params["budget"] = {"time_s": float(budget_s)}
        try:
            submitted = submit_agent_block(self.manager, self.config.agent_name, params, start=start)
        except RuntimeError as exc:
            raise CliError(str(exc)) from exc
        added = submitted["items"]
        close_item_uid = added[-1]["item_uid"]
        open_item_uid = added[0]["item_uid"]
        started = submitted["started"]

        out: dict[str, Any] = {"queued": True, "started": started, "agent_uid": None}
        if not started:
            out["waiting_behind"] = submitted["position"]
            out["note"] = "open_agent is queued; the session becomes active when it runs"
            return out

        deadline = time.time() + wait_s
        failure = "open_agent ran but no supervisor assigned a session UID; is nbs-agents-runner running?"
        while time.time() < deadline:
            items = self.queue().get("items", [])
            close = next((i for i in items if i.get("item_uid") == close_item_uid), None)
            uid = _close_agent_uid(close) if close else None
            if not uid and self._open_failed(open_item_uid):
                failure = "open_agent failed and the queue stopped; see the queue server history."
                break
            if uid:
                record = self.agent_log.get(uid)
                if record is None or record.get("status") != "active":
                    record = self.agent_log.open(
                        uid, self.config.agent_name, goal=goal, budget=params.get("budget"), status="active"
                    )
                if self.config.driver:
                    record = self.agent_log.set_status(uid, record.get("status", "active"), driver=self.config.driver)
                out["agent_uid"] = uid
                out["status"] = record.get("status")
                return out
            time.sleep(0.5)
        removed = self._remove_items([it["item_uid"] for it in added])
        raise CliError(f"{failure} Removed the {removed} queued session items, so it is safe to retry.")

    def _open_failed(self, open_item_uid: str) -> bool:
        """Whether the newest history entry is this open_agent item, failed."""
        try:
            history = self.manager.history_get().get("items", [])
        except Exception:
            return False
        if not history:
            return False
        last = history[-1]
        status = (last.get("result") or {}).get("exit_status")
        return last.get("item_uid") == open_item_uid and status not in (None, "completed")

    def _remove_items(self, item_uids: list[str]) -> int:
        """Remove those of ``item_uids`` still in the queue; return how many were removed."""
        queued = {i.get("item_uid") for i in self.queue().get("items", [])}
        removed = 0
        for item_uid in item_uids:
            if item_uid in queued and self.manager.item_remove(uid=item_uid).get("success"):
                removed += 1
        return removed

    def add(
        self,
        plan: str,
        args: list[Any],
        kwargs: dict[str, Any],
        reason: str,
        *,
        dry_run: bool = False,
        start: bool = True,
        agent_uid: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Insert a plan before this session's ``queue_stop``.

        Parameters
        ----------
        plan : str
            Plan name.
        args : list
            Positional plan arguments.
        kwargs : dict
            Keyword plan arguments.
        reason : str
            Why the plan is being added. Stored in the item metadata and log.
        dry_run : bool, optional
            Validate and estimate only.
        start : bool, optional
            Start the queue if it is idle at this session's stop.
        agent_uid : str, optional
            Explicit session UID.

        Returns
        -------
        dict
            The item, its estimate, the budget and whether the queue started.
        """
        refuse_read_only(self.config, "add")
        from bluesky_queueserver_api import BPlan

        from nbs_agents.queue import insert_before_queue_stop

        if not reason or not reason.strip():
            raise CliError("--reason is required")
        record = self._require_session(agent_uid)
        uid = record["agent_uid"]
        stop = record.get("stop_requested")
        if stop:
            raise CliError(
                f"operator requested stop ({stop.get('text')}); finalize with `nbs-cli finalize --summary ...`"
            )

        if self.config.allowed_plans is not None and plan not in self.config.allowed_plans:
            raise CliError(f"plan {plan!r} is not in the nbs-cli allowlist")
        allowed = self.plans_allowed()
        if plan not in allowed:
            raise CliError(f"plan {plan!r} is not allowed for group {self.config.qs_user_group!r}")
        params = allowed[plan].get("parameters", [])
        names = {p["name"] for p in params}
        has_var_kw = any(p.get("kind", {}).get("name") == "VAR_KEYWORD" for p in params)
        unknown = [k for k in kwargs if k not in names]
        if unknown and not has_var_kw:
            raise CliError(f"unknown parameters for {plan}: {unknown}; allowed: {sorted(names)}")

        estimator = self.info.estimator()
        item_dict = {"item_type": "plan", "name": plan, "args": list(args), "kwargs": dict(kwargs)}
        est = self.estimate(item_dict, estimator)

        items = self.queue().get("items", [])
        split = self.split_queue(items, uid)
        if split["close_index"] is None:
            raise CliError(f"close_agent for session {uid} is not in the queue")
        self.check_owner(split, uid)
        queued_s = sum(self.estimate(i, estimator) or 0 for i in split["block"])
        budget = dict(record.get("budget") or {})
        used_s = time.time() - record.get("opened", time.time())
        if budget.get("time_s"):
            needed = used_s + queued_s + (est or 0)
            budget.update(used_s=round(used_s), queued_s=round(queued_s))
            if needed > budget["time_s"]:
                raise CliError(
                    f"over budget: used {used_s:.0f}s + queued {queued_s:.0f}s + this {est or 0:.0f}s "
                    f"> {budget['time_s']:.0f}s"
                )
            budget["left_after_s"] = round(budget["time_s"] - needed)

        sequence = 1 + sum(1 for e in record.get("log", []) if e.get("kind") == "add")
        out: dict[str, Any] = {
            "item": compact_item(item_dict | {"meta": {AGENT_REASON_KEY: reason, AGENT_SEQUENCE_KEY: sequence}}, est),
            "budget": budget,
        }
        warning = self.info.add_warning(plan, dict(kwargs), names, split["block"])
        if warning:
            out["warning"] = warning
        if dry_run:
            out["dry_run"] = True
            return out

        item = BPlan(plan, *args, **kwargs)
        item.meta = {
            AGENT_UID_KEY: uid,
            AGENT_NAME_KEY: record.get("agent_name", self.config.agent_name),
            AGENT_SEQUENCE_KEY: sequence,
            AGENT_REASON_KEY: reason,
        }
        response = insert_before_queue_stop(self.manager, item, uid)
        item_uid = response["item"]["item_uid"]
        out["item"]["item_uid"] = item_uid
        self.agent_log.append(uid, "add", reason, item_uid=item_uid, plan=plan, source="nbs-cli")
        out["started"] = self._maybe_start(uid) if start else False
        inbox = self.operator_inbox(uid)
        if inbox:
            out["operator"] = inbox
        return out

    def remove(self, item_uid: str, reason: str, agent_uid: Optional[str] = None) -> dict[str, Any]:
        """
        Remove a pending item from this session's block.

        Parameters
        ----------
        item_uid : str
            Queue item UID (a unique prefix is accepted).
        reason : str
            Why it is being removed.
        agent_uid : str, optional
            Explicit session UID.

        Returns
        -------
        dict
            The removed item.
        """
        refuse_read_only(self.config, "remove")
        if not reason or not reason.strip():
            raise CliError("--reason is required")
        record = self._require_session(agent_uid)
        uid = record["agent_uid"]
        items = self.queue().get("items", [])
        split = self.split_queue(items, uid)
        self.check_owner(split, uid)
        block = split["block"]
        matches = [i for i in block if i.get("item_uid", "").startswith(item_uid)]
        if not matches:
            raise CliError(f"no pending item {item_uid!r} in this session's block")
        if len(matches) > 1:
            raise CliError(f"item prefix {item_uid!r} is ambiguous")
        target = matches[0]
        _check(self.manager.item_remove(uid=target["item_uid"]), "item_remove")
        self.agent_log.append(
            uid, "remove", reason, item_uid=target["item_uid"], plan=target.get("name"), source="nbs-cli"
        )
        return {"removed": compact_item(target)}

    def abort(
        self,
        reason: str,
        *,
        resume: bool = False,
        agent_uid: Optional[str] = None,
        timeout: float = 30,
    ) -> dict[str, Any]:
        """
        Abort this session's running plan.

        The queue server puts an aborted item back at the head of the queue;
        that copy is removed so it does not rerun when the queue restarts.

        Parameters
        ----------
        reason : str
            Why the plan is being aborted.
        resume : bool, optional
            Restart the queue afterwards if this session has items pending.
        agent_uid : str, optional
            Explicit session UID.
        timeout : float, optional
            Seconds to wait for the RunEngine to pause and then go idle.

        Returns
        -------
        dict
            The aborted item, whether its re-queued copy was removed, and
            whether the queue was restarted.
        """
        refuse_read_only(self.config, "abort")
        if not reason or not reason.strip():
            raise CliError("--reason is required")
        record = self._require_session(agent_uid)
        uid = record["agent_uid"]
        queue = self.queue()
        running = queue.get("running_item") or {}
        if not running:
            raise CliError("nothing is running")
        if (running.get("meta") or {}).get(AGENT_UID_KEY) != uid:
            raise CliError("the running plan is not part of this session; ask the operator")
        self.check_owner(self.split_queue(queue.get("items", []), uid), uid)

        def wait_for(check):
            deadline = time.time() + timeout
            while time.time() < deadline:
                status = self.manager.status()
                if check(status):
                    return status
                time.sleep(0.5)
            raise CliError("timed out waiting for the RunEngine; check the queue server")

        if self.manager.status().get("re_state") != "paused":
            _check(self.manager.re_pause(option="immediate"), "re_pause")
            wait_for(lambda st: st.get("re_state") == "paused")
        _check(self.manager.re_abort(), "re_abort")
        wait_for(lambda st: st.get("manager_state") == "idle")

        def same_plan(item):
            meta = item.get("meta") or {}
            return (
                item.get("name") == running.get("name")
                and (item.get("args") or []) == (running.get("args") or [])
                and (item.get("kwargs") or {}) == (running.get("kwargs") or {})
                and meta.get(AGENT_UID_KEY) == uid
                and meta.get("agent_sequence") == (running.get("meta") or {}).get("agent_sequence")
            )

        block = self.split_queue(self.queue().get("items", []), uid)["block"]
        copy = next((i for i in block if same_plan(i)), None)
        if copy is not None:
            _check(self.manager.item_remove(uid=copy["item_uid"]), "item_remove")
        self.agent_log.append(
            uid, "abort", reason, item_uid=running.get("item_uid"), plan=running.get("name"), source="nbs-cli"
        )
        started = self._maybe_start(uid) if resume else False
        return {
            "aborted": compact_item(running),
            "requeued_copy_removed": copy is not None,
            "started": started,
        }

    def visible_records(self) -> dict[str, dict[str, Any]]:
        """
        Agent-log records this client may read.

        Returns
        -------
        dict
            Every record for the assistant; for a driver, every record except
            the assistant's, so its advice and the operator's questions to it
            never reach driving agents.
        """
        records = self.agent_log.records()
        if self.config.read_only:
            return records
        return {uid: r for uid, r in records.items() if not is_assistant_record(r, uid)}

    def ask_assistant(
        self,
        text: str,
        source: str = "nbs-cli",
        thread: Optional[str] = None,
        entry: Optional[str] = None,
        tile: Optional[str] = None,
        by: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Ask the assistant (its chat role, the manager) a question.

        Each new question opens its own thread: the runner answers it, and
        each follow-up (``thread``), in a Claude session of its own. Only the
        question shows in today's main record, as the thread's one line.
        With ``[assistant] threads = false`` the question goes into the main
        record and is answered there, as the GUI's Assistant tab did before.

        Parameters
        ----------
        text : str
            The question.
        source : str, optional
            Where it came from.
        thread : str, optional
            A follow-up in this thread.
        entry : str, optional
            Ask about one of today's assistant posts: ``HH:MM``, ``HH:MM:SS``,
            epoch seconds or ``"last"``.
        tile : str, optional
            Ask about this role's tile.
        by : str, optional
            Who asks; the login name if omitted.

        Returns
        -------
        dict
            ``agent_uid`` (the main record), ``thread`` (its id) and the
            appended ``entry`` (the main record's line) or ``message`` (a follow-up).
        """
        refuse_read_only(self.config, "ask-assistant")
        if not text.strip():
            raise CliError("empty question")
        if sum(x is not None for x in (thread, entry, tile)) > 1:
            raise CliError("a question is a follow-up (--thread), about an entry (--entry) or about a tile (--tile)")
        by = by or login_name()
        uid = self.agent_log.ensure_assistant_record()
        if thread:
            try:
                message = ThreadStore.for_agent_log(self.agent_log).ask(thread, text, by, source=source)
            except ThreadError as exc:
                raise CliError(str(exc)) from exc
            return {"agent_uid": uid, "thread": thread, "message": message}
        if not self.assistant.threads:
            if entry or tile:
                raise CliError("--entry and --tile open a thread, and [assistant] threads is off")
            return {"agent_uid": uid, "entry": self.agent_log.message(uid, text.strip(), source=source)}
        seed = None
        if entry:
            try:
                post = find_assistant_post(self.agent_log.get(uid) or {}, entry)
            except ValueError as exc:
                raise CliError(str(exc)) from exc
            seed = {"kind": "entry", "record": uid, "entry": post}
        elif tile:
            current = TileStore.for_agent_log(self.agent_log).get(tile)
            if current is None:
                raise CliError(f"no tile for role {tile!r}")
            seed = {"kind": "tile", "role": tile, "tile": current}
        out = open_thread(self.agent_log, text, by, source=source, seed=seed)
        return {"agent_uid": uid, "thread": out["thread"]["id"], "entry": out["entry"]}

    def _refuse_unless_person_or_assistant(self, verb: str) -> None:
        """Assistant records stay out of drivers' reach (the firewall); people and the assistant read them."""
        if not self.config.is_assistant and not is_person(self.config):
            raise CliError(f"{verb} is for people and the assistant, not for driving agents")

    def thread_list(self, since: Optional[str] = None) -> list[dict[str, Any]]:
        """
        The assistant's threads, newest first, without their messages.

        Parameters
        ----------
        since : str, optional
            Only threads active since then (as ``role show --since``).
        """
        self._refuse_unless_person_or_assistant("thread list")
        start = parse_since(since, time.time()) if since else None
        return ThreadStore.for_agent_log(self.agent_log).listing(start)

    def thread_show(self, thread_id: str) -> dict[str, Any]:
        """One thread with its messages."""
        self._refuse_unless_person_or_assistant("thread show")
        thread = ThreadStore.for_agent_log(self.agent_log).get(thread_id)
        if thread is None:
            raise CliError(f"no thread {thread_id}")
        return thread

    def directive_propose(self, role: str, text: str, expires: str = "beamtime") -> dict[str, Any]:
        """
        Propose a directive for an assistant role (the chat role only). It
        waits for a person to confirm it.

        Parameters
        ----------
        role : str
            The role it is for.
        text : str
            What the role should do or stop doing.
        expires : str, optional
            ``beamtime``, a duration (``12h``, ``3d``) or a date (``YYYY-MM-DD``).

        Returns
        -------
        dict
            The ``directive`` and a ``note`` on what happens next.
        """
        self._refuse_unless_chat_role("directive propose")
        self._named_role(role)
        now = time.time()
        proposal = None
        if expires.strip() == "beamtime":
            try:
                proposal = (self.info.metadata(["proposal"]) or {}).get("proposal")
            except Exception:
                proposal = None
        try:
            when = parse_expiry(expires, now, proposal, self.assistant.beamtime_s)
            thread = os.environ.get("NBS_ASSISTANT_THREAD") or None
            directive = DirectiveStore.for_agent_log(self.agent_log).propose(
                role, text, when, by=ASSISTANT_ROLE, thread=thread, t=now,
            )
        except DirectiveError as exc:
            raise CliError(str(exc)) from exc
        log_directive(
            self.agent_log, directive,
            f"proposed directive {directive['id']} for {role} (until {when['text']}): {directive['text']}",
            source=ASSISTANT_ROLE, thread=thread, **writer_refs(),
        )
        if thread:
            try:
                ThreadStore.for_agent_log(self.agent_log).add(
                    thread, "note", f"proposed directive {directive['id']} for the {role} role: {directive['text']}",
                    directive=directive["id"], source=ASSISTANT_ROLE,
                )
            except ThreadError:
                pass
        return {"directive": directive,
                "note": "a person must confirm it (web UI, or nbs-cli directive confirm ID); tell them in your answer"}

    def directive_list(self, role: Optional[str] = None, all_: bool = False) -> list[dict[str, Any]]:
        """Directives waiting for a person and active ones (every one with ``all_``), oldest first."""
        self._refuse_unless_person_or_assistant("directive list")
        statuses = DIRECTIVE_STATUSES if all_ else ("proposed", "active")
        return DirectiveStore.for_agent_log(self.agent_log).listing(role, statuses)

    def directive_decide(self, directive_id: str, action: str, reason: str = "", by: Optional[str] = None) -> dict[str, Any]:
        """
        A person confirms, rejects or revokes a directive; it is logged.

        Parameters
        ----------
        directive_id : str
            The directive.
        action : {"confirm", "reject", "revoke"}
            What to do.
        reason : str, optional
            Why (for a reject or revoke).
        by : str, optional
            Who; the login name if omitted.
        """
        if not is_person(self.config):
            raise CliError("only a person confirms, rejects or revokes a directive")
        by = by or login_name()
        chat = self.assistant.chat_role().name
        try:
            if action == "revoke":
                return revoke_directive(self.agent_log, directive_id, by, source="nbs-cli", reason=reason,
                                        chat_role=chat)
            return decide_directive(self.agent_log, directive_id, action == "confirm", by, source="nbs-cli",
                                    reason=reason, chat_role=chat)
        except DirectiveError as exc:
            raise CliError(str(exc)) from exc

    def recheck(self, about: str, model: Optional[str] = None, text: str = "", source: str = "nbs-cli") -> dict[str, Any]:
        """
        Ask the assistant for a blind second look at one of today's posts.

        The runner starts a fresh call (on ``model``, else the assistant's
        ``alert_model``) that sees the events behind the post but not the post.

        Parameters
        ----------
        about : str
            The post: ``HH:MM``, ``HH:MM:SS``, epoch seconds or ``"last"``.
        model : str, optional
            Model for the re-check.
        text : str, optional
            What to look at in particular.
        source : str, optional
            Where the request came from.

        Returns
        -------
        dict
            ``agent_uid`` and the appended entry.
        """
        refuse_read_only(self.config, "ask-assistant")
        uid = self.agent_log.ensure_assistant_record()
        try:
            entry = self.agent_log.request_recheck(uid, about, model=model, text=text, source=source)
        except ValueError as exc:
            raise CliError(str(exc)) from exc
        return {"agent_uid": uid, "entry": entry}

    def _own_role(self) -> RoleConfig:
        """
        The assistant role this client posts for: the headless role
        ``NBS_CLI_ROLE=assistant:NAME`` names, else the chat role.
        """
        roles = self.assistant.resolved_roles()
        name = self.config.assistant_role
        if name is None:
            return next(r for r in roles.values() if r.chat)
        role = roles.get(name)
        if role is None or role.chat:
            raise CliError(f"{name!r} is not a headless role in [assistant.roles] (roles: {sorted(roles)})")
        return role

    def _role_record(self, role: RoleConfig) -> str:
        return self.agent_log.ensure_assistant_record(role=None if role.chat else role.name)

    def post(
        self,
        kind: Optional[str],
        text: str,
        runs: Optional[list[str]] = None,
        tile: bool = False,
        severity: Optional[str] = None,
        figure: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Post to the assistant role's own record for today, or to its tile.

        A headless role (``NBS_CLI_ROLE=assistant:NAME``) writes its own
        record and tile; the chat role writes today's main assistant record.
        Each may post only where its ``sinks`` allow.

        Parameters
        ----------
        kind : {"summary", "advice", "alert", "answer"} or None
            Entry kind; None with ``tile``.
        text : str
            What to tell the operator.
        runs : list of str, optional
            UIDs of the runs the post is about.
        tile : bool, optional
            Post to the role's tile instead (logged as a ``tile`` entry).
        severity : {"ok", "info", "warn", "alert"}, optional
            The tile's severity; needed with ``tile``. An alert stays on the
            tile until a person acknowledges it.
        figure : str, optional
            The tile's figure, as ``att:ID`` from ``nbs-cli attach``.

        Returns
        -------
        dict
            The log entry, with ``agent_uid`` (and ``tile`` for a tile post).
        """
        if not self.config.read_only:
            raise CliError("post is only for the assistant role (NBS_CLI_ROLE=assistant or --role assistant)")
        if not text or not text.strip():
            raise CliError("post text is required")
        role = self._own_role()
        refs = writer_refs()
        source = ASSISTANT_ROLE if role.chat else f"{ASSISTANT_ROLE}:{role.name}"
        if tile:
            if "tile" not in role.sinks:
                raise CliError(f"the {role.name} role has no tile (its sinks are {role.sinks})")
            try:
                written = TileStore.for_agent_log(self.agent_log).post(
                    role.name, severity or "", text, run_uids=runs, figure=figure, model=refs.get("model"),
                )
            except TileError as exc:
                raise CliError(str(exc)) from exc
            uid = self._role_record(role)
            entry = self.agent_log.append(
                uid, "tile", text.strip(), source=source, severity=severity,
                run_uids=list(runs or []) or None, figure=figure or None, **refs,
            )
            return {"agent_uid": uid, **entry, "tile": written}
        if "record" not in role.sinks:
            raise CliError(f"the {role.name} role posts only to its tile (its sinks are {role.sinks}); use --tile")
        if kind not in ASSISTANT_POST_KINDS:
            raise CliError(f"unknown post kind {kind!r}; use one of {list(ASSISTANT_POST_KINDS)}")
        uid = self._role_record(role)
        entry = self.agent_log.append(
            uid, kind, text.strip(), source=source, run_uids=list(runs or []) or None, **refs
        )
        return {"agent_uid": uid, **entry}

    def ack(self, role: str, by: Optional[str] = None) -> dict[str, Any]:
        """
        Acknowledge an assistant role's alert, as a person; it is logged in
        the role's record.

        Parameters
        ----------
        role : str
            The role whose tile holds the alert.
        by : str, optional
            Who acknowledges; the login name if omitted.

        Returns
        -------
        dict
            ``agent_uid`` (where it was logged), ``tile`` and ``entry``.
        """
        if not is_person(self.config):
            raise CliError("only a person acknowledges an alert; an agent can mention it in a post instead")
        roles = self.assistant.resolved_roles()
        if role not in roles and TileStore.for_agent_log(self.agent_log).get(role) is None:
            raise CliError(f"no role {role!r} (roles: {sorted(roles)})")
        by = by or login_name()
        try:
            return acknowledge(self.agent_log, role, by, source="nbs-cli", chat=role in roles and roles[role].chat)
        except TileError as exc:
            raise CliError(str(exc)) from exc

    def _refuse_unless_chat(self, verb: str) -> None:
        self._refuse_unless_chat_role(f"role {verb}")

    def _refuse_unless_chat_role(self, verb: str) -> None:
        if not self.config.read_only or self.config.assistant_role is not None:
            raise CliError(
                f"{verb} is for the assistant's chat role (the manager: NBS_CLI_ROLE=assistant or --role assistant)"
            )

    def _named_role(self, name: str) -> RoleConfig:
        roles = self.assistant.resolved_roles()
        if name not in roles:
            raise CliError(f"no role {name!r} (roles: {sorted(roles)})")
        return roles[name]

    def role_list(self) -> dict[str, Any]:
        """
        Every assistant role with its settings, tile and status.

        Returns
        -------
        dict
            ``{name: {mode, skill, models, triggers, sinks, record, status, tile}}``.
        """
        self._refuse_unless_chat("list")
        tiles = TileStore.for_agent_log(self.agent_log)
        out = {}
        for role in self.assistant.resolved_roles().values():
            uid = assistant_uid(role=None if role.chat else role.name)
            record = self.agent_log.get(uid) or {}
            out[role.name] = {
                "mode": role.mode, "skill": role.skill, "models": [role.model, role.alert_model],
                "triggers": role.triggers, "sinks": role.sinks, "record": uid,
                "status": record.get("status"), "tile": tiles.get(role.name),
            }
        return out

    def role_show(self, name: str, since: Optional[str] = None, tail: int = 20) -> dict[str, Any]:
        """
        A role's tile, its record for today and its notebook (as of its last call).

        Parameters
        ----------
        name : str
            The role.
        since : str, optional
            Only entries from then on: ``HH:MM``, ``HH:MM:SS``, epoch seconds,
            or an age such as ``30m`` or ``2h``.
        tail : int, optional
            Most entries; 0 for all.

        Returns
        -------
        dict
            ``role``, its settings, ``record``, ``status``, ``last_call``,
            ``tile``, ``entries`` and ``notebook``.
        """
        self._refuse_unless_chat("show")
        role = self._named_role(name)
        uid = assistant_uid(role=None if role.chat else role.name)
        record = self.agent_log.get(uid) or {}
        entries = record.get("log") or []
        if since:
            start = parse_since(since, time.time())
            entries = [e for e in entries if (e.get("t") or 0) >= start]
        if tail:
            entries = entries[-tail:]
        return {
            "role": role.name, "mode": role.mode, "skill": role.skill, "models": [role.model, role.alert_model],
            "triggers": role.triggers, "sinks": role.sinks, "record": uid, "status": record.get("status"),
            "last_call": record.get("last_call"), "tile": TileStore.for_agent_log(self.agent_log).get(role.name),
            "entries": entries,
            "notebook": "(your own notebook folder)" if role.chat else record.get("notebook"),
        }

    def role_request(self, name: str, text: str, runs: Optional[list[str]] = None) -> dict[str, Any]:
        """
        Ask a headless role to do something with its own tools and prompt.

        The runner gives the request to the role in its next call and passes
        the role's answer back to today's main assistant record as a
        ``reply`` entry, which starts a call of the chat role.

        Parameters
        ----------
        name : str
            A headless role.
        text : str
            The request.
        runs : list of str, optional
            Run uids it is about.

        Returns
        -------
        dict
            ``agent_uid`` (the role's record) and the ``request`` entry.
        """
        self._refuse_unless_chat("request")
        role = self._named_role(name)
        if role.chat:
            raise CliError(f"{name} is the chat role itself; requests go to headless roles")
        if not text or not text.strip():
            raise CliError("request text is required")
        uid = self._role_record(role)
        thread = os.environ.get("NBS_ASSISTANT_THREAD") or None
        entry = self.agent_log.append(
            uid, "request", text.strip(), source=ASSISTANT_ROLE, reply_to=assistant_uid(), thread=thread,
            run_uids=list(runs or []) or None, **writer_refs(),
        )
        where = "into this thread" if thread else "as a reply entry"
        return {"agent_uid": uid, "entry": entry, "note": f"the {name} role's answer will come back {where}"}

    def note(self, text: str, agent_uid: Optional[str] = None) -> dict[str, Any]:
        """
        Append a note to this session's log.

        Parameters
        ----------
        text : str
            Note text.
        agent_uid : str, optional
            Explicit session UID.

        Returns
        -------
        dict
            The log entry.
        """
        refuse_read_only(self.config, "note")
        if not text or not text.strip():
            raise CliError("note text is required")
        record = self.find_session(agent_uid)
        if record is None:
            raise CliError("no open session to note on")
        return self.agent_log.append(record["agent_uid"], "note", text, source="nbs-cli", **writer_refs())

    def ask(self, text: str, agent_uid: Optional[str] = None) -> dict[str, Any]:
        """
        Ask the operator a question; the GUI shows the session as awaiting input.

        Parameters
        ----------
        text : str
            The question.
        agent_uid : str, optional
            Explicit session UID.

        Returns
        -------
        dict
            The log entry.
        """
        refuse_read_only(self.config, "ask")
        if not text or not text.strip():
            raise CliError("question text is required")
        record = self.find_session(agent_uid)
        if record is None:
            raise CliError("no open session to ask from")
        return self.agent_log.ask(record["agent_uid"], text, source="nbs-cli", **writer_refs())

    def finalize(self, summary: str, drop_pending: bool = False, agent_uid: Optional[str] = None) -> dict[str, Any]:
        """
        End the session: log a summary and let the queue run to ``close_agent``.

        Removes this session's ``queue_stop`` so the remaining block (if any)
        runs straight into ``close_agent``, then starts the queue if it is
        idle at this session's block.

        Parameters
        ----------
        summary : str
            Final summary for the log.
        drop_pending : bool, optional
            Remove pending block items first.
        agent_uid : str, optional
            Explicit session UID.

        Returns
        -------
        dict
            What was removed and whether the queue started.
        """
        refuse_read_only(self.config, "finalize")
        if not summary or not summary.strip():
            raise CliError("--summary is required")
        record = self._require_session(agent_uid)
        uid = record["agent_uid"]
        items = self.queue().get("items", [])
        split = self.split_queue(items, uid)
        self.check_owner(split, uid)
        dropped = []
        if drop_pending:
            for item in split["block"]:
                _check(self.manager.item_remove(uid=item["item_uid"]), "item_remove")
                dropped.append(item["item_uid"])
        self.agent_log.append(uid, "finalize", summary, source="nbs-cli", **writer_refs())
        self.agent_log.set_status(uid, "finalizing")
        if split["queue_stop"] is not None:
            _check(self.manager.item_remove(uid=split["queue_stop"]["item_uid"]), "item_remove")
        started = self._maybe_start(uid)
        return {"agent_uid": uid, "dropped": dropped, "started": started, "status": "finalizing"}


class DocumentStream:
    """
    Blocking reader for the Bluesky 0MQ document stream.

    Parameters
    ----------
    address : str
        ``host:port`` of the 0MQ proxy's outbound side.
    """

    def __init__(self, address: str) -> None:
        import zmq

        self._zmq = zmq
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.SUB)
        if "://" not in address:
            address = f"tcp://{address}"
        self.socket.connect(address)
        self.socket.setsockopt_string(zmq.SUBSCRIBE, "")
        self._plan_names: dict[str, Optional[str]] = {}

    def plan_name(self, stop_doc: dict[str, Any]) -> Optional[str]:
        """Plan name for a stop document's run, if its start was seen."""
        return self._plan_names.get(stop_doc.get("run_start"))

    def poll(self, timeout: float) -> list[tuple[str, dict[str, Any]]]:
        """
        Read documents for up to ``timeout`` seconds.

        Parameters
        ----------
        timeout : float
            Seconds to wait for the first document.

        Returns
        -------
        list of (str, dict)
            ``(name, doc)`` pairs for start and stop documents.
        """
        out = []
        wait_ms = int(timeout * 1000)
        while self.socket.poll(wait_ms):
            message = self.socket.recv()
            wait_ms = 0
            try:
                _, name, payload = message.split(b" ", 2)
                name = name.decode()
            except ValueError:
                continue
            if name not in ("start", "stop"):
                continue
            try:
                doc = pickle.loads(payload)
            except Exception:
                continue
            if name == "start":
                self._plan_names[doc["uid"]] = doc.get("plan_name")
            out.append((name, doc))
        return out

    def close(self) -> None:
        """Close the socket."""
        self.socket.close(linger=0)
        self.context.term()
