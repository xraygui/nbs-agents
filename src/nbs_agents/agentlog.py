"""
General agent-log record shared by every nbs-agents agent.

Each agent session is one JSON value in Redis under
``{prefix}{topic}{agent_uid}``, the same layout ``RedisJSONDict`` and
``QtRedisJSONDict.from_settings(settings, topic="AGENT")`` use, so a GUI can
watch it through keyspace notifications.

Record schema::

    {
        "agent_uid": str,
        "agent_name": str,
        "goal": str or None,
        "status": str,          # e.g. "opening", "active", "finalizing", "closed", "failed"
        "budget": dict or None, # e.g. {"time_s": 7200}
        "opened": float,        # epoch seconds
        "updated": float,
        "log": [ {"t": float, "kind": str, "text": str, ...refs}, ... ],
    }

``kind`` is free-form but the shared vocabulary is ``open``, ``note``,
``question``, ``add``, ``remove``, ``run``, ``status``, ``finalize``, ``close``,
``launch`` (the runner started or reaped a Claude) and ``error``. Optional
refs include ``item_uid``, ``run_uid``, ``plan`` and ``source`` (who wrote it).

Entries a Claude writes carry ``model``: the model the runner asked for
(``NBS_MODEL`` in nbs-cli's environment). Once the call ends the runner adds
``served_model`` where the model that answered differs, as with a gateway
alias such as ``claude-auto``. A launched driver's record has the same two
fields at the top level.

Operators steer a running agent through the same record: ``operator`` entries
are messages to the agent, and a ``stop`` entry plus a top-level
``stop_requested`` field ask it to wrap up. ``operator_read`` holds the time of
the newest operator entry the agent has seen.

An agent that needs an answer writes a ``question`` entry with ``ask``, which
also sets a top-level ``awaiting_input`` field (``{"t", "text", "source"}``).
The next operator, ``finalize`` or ``close`` entry clears it, so a GUI can show
that the agent is waiting until someone replies.

The read-only beamline assistant keeps one record per day under
``assistant-YYYYMMDD`` with a top-level ``kind`` of ``"assistant"``. Its
entries are deterministic ``run`` lines and Claude's ``summary``, ``advice``,
``alert`` and ``answer`` posts; ``operator`` entries are questions to it.
Posts also carry ``call`` (the number of the call that wrote them). An
``operator`` entry with ``recheck`` (``{"t", "model", "note"}``) asks for a blind
second look at the post at time ``t``, and the posts that call writes carry
``recheck_of``. The record's ``models`` holds the configured ``routine`` and
``alert`` models and ``call_events`` the events behind recent calls.
Driver agents never see these records.
"""

import json
import logging
import time
from typing import Any, Optional

LOGGER = logging.getLogger(__name__)

AGENT_LOG_TOPIC = "AGENT"
OPERATOR_KINDS = ("operator", "stop")
ANSWER_KINDS = OPERATOR_KINDS + ("finalize", "close")
ASSISTANT_NAME = "assistant"
ASSISTANT_KIND = "assistant"
ASSISTANT_POST_KINDS = ("summary", "advice", "alert", "answer")


def assistant_uid(t: Optional[float] = None) -> str:
    """
    UID of the assistant's record for the local day containing ``t``.

    Parameters
    ----------
    t : float, optional
        Epoch seconds; now if omitted.

    Returns
    -------
    str
        ``"assistant-YYYYMMDD"``.
    """
    return f"{ASSISTANT_NAME}-{time.strftime('%Y%m%d', time.localtime(t))}"


def is_assistant_record(record: Optional[dict[str, Any]], agent_uid: str = "") -> bool:
    """
    Whether a record belongs to the beamline assistant.

    Parameters
    ----------
    record : dict or None
        Agent-log record.
    agent_uid : str, optional
        The record's UID, checked too in case the record is incomplete.

    Returns
    -------
    bool
        True for assistant records.
    """
    if str(agent_uid).startswith(f"{ASSISTANT_NAME}-"):
        return True
    record = record or {}
    return record.get("kind") == ASSISTANT_KIND or record.get("agent_name") == ASSISTANT_NAME


class AgentLog:
    """
    Read and write agent-log records in Redis.

    Parameters
    ----------
    redis_client : redis.Redis
        Connected Redis client.
    prefix : str, optional
        Beamline Redis prefix, for example ``"claude-"``.
    topic : str, optional
        Topic appended to the prefix.
    """

    def __init__(self, redis_client: Any, prefix: str = "", topic: str = AGENT_LOG_TOPIC) -> None:
        self.redis = redis_client
        self.prefix = prefix
        self.topic = topic

    @classmethod
    def from_settings(cls, settings: dict[str, Any], topic: str = AGENT_LOG_TOPIC) -> "AgentLog":
        """
        Build an agent log from nbs-style Redis settings.

        Parameters
        ----------
        settings : dict
            Keys ``host``, and optionally ``port``, ``db`` and ``prefix``.
        topic : str, optional
            Topic appended to the prefix.

        Returns
        -------
        AgentLog
            Agent log bound to a new Redis client.
        """
        from .redisclient import connect_settings

        client = connect_settings(settings)
        return cls(client, settings.get("prefix", ""), topic)

    @property
    def key_prefix(self) -> str:
        """
        Full Redis key prefix for agent records.

        Returns
        -------
        str
            ``prefix + topic``.
        """
        return f"{self.prefix}{self.topic}"

    # Kept outside key_prefix so records() never sees it
    HEARTBEAT_KEY = "NBS_AGENTS_RUNNER_HEARTBEAT"
    TILED_LOGIN_KEY = "NBS_AGENTS_TILED_LOGIN"
    HEARTBEAT_STALE_S = 20.0

    @property
    def heartbeat_key(self) -> str:
        return f"{self.prefix}{self.HEARTBEAT_KEY}"

    def heartbeat(self) -> None:
        """Record that an agent runner (supervisor) is alive now."""
        self.redis.set(self.heartbeat_key, str(time.time()))

    def runner_alive(self) -> bool:
        """
        Whether an agent runner wrote a heartbeat recently.

        Returns
        -------
        bool
            True if the last heartbeat is newer than ``HEARTBEAT_STALE_S``.
        """
        raw = self.redis.get(self.heartbeat_key)
        if raw is None:
            return False
        try:
            return time.time() - float(raw) < self.HEARTBEAT_STALE_S
        except ValueError:
            return False

    def set_tiled_login(self, url: Optional[str], code: Optional[str] = None) -> None:
        """
        Show (or, with ``url=None``, clear) a pending Tiled login link and code.

        The runner sets it while it waits for the user to log in, so the web
        page can show the link even when the runner's terminal is hidden.
        """
        key = f"{self.prefix}{self.TILED_LOGIN_KEY}"
        if url is None:
            self.redis.delete(key)
        else:
            self.redis.set(key, json.dumps({"url": url, "code": code, "t": time.time()}))

    def tiled_login(self) -> Optional[dict[str, Any]]:
        """The pending Tiled login (``url``, ``code``, ``t``), or None."""
        raw = self.redis.get(f"{self.prefix}{self.TILED_LOGIN_KEY}")
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None

    def _key(self, agent_uid: str) -> str:
        return f"{self.key_prefix}{agent_uid}"

    def get(self, agent_uid: str) -> Optional[dict[str, Any]]:
        """
        Read one agent record.

        Parameters
        ----------
        agent_uid : str
            Agent session UID.

        Returns
        -------
        dict or None
            The record, or ``None`` if it does not exist.
        """
        raw = self.redis.get(self._key(agent_uid))
        return json.loads(raw) if raw else None

    def records(self) -> dict[str, dict[str, Any]]:
        """
        Read every agent record.

        Returns
        -------
        dict
            Records keyed by agent UID.
        """
        out = {}
        prefix = self.key_prefix
        for key in self.redis.scan_iter(match=f"{prefix}*"):
            key = key.decode() if isinstance(key, bytes) else key
            raw = self.redis.get(key)
            if raw:
                out[key[len(prefix):]] = json.loads(raw)
        return out

    def _modify(self, agent_uid: str, func) -> dict[str, Any]:
        """
        Atomically read, modify and write a record.

        Parameters
        ----------
        agent_uid : str
            Agent session UID.
        func : callable
            Called with the current record (or ``None``); returns the new record.

        Returns
        -------
        dict
            The record as written.
        """
        import redis

        key = self._key(agent_uid)
        with self.redis.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(key)
                    raw = pipe.get(key)
                    record = func(json.loads(raw) if raw else None)
                    record["updated"] = time.time()
                    pipe.multi()
                    pipe.set(key, json.dumps(record, default=str))
                    pipe.execute()
                    return record
                except redis.WatchError:
                    continue

    def open(
        self,
        agent_uid: str,
        agent_name: str,
        *,
        goal: Optional[str] = None,
        budget: Optional[dict[str, Any]] = None,
        status: str = "active",
        **extra: Any,
    ) -> dict[str, Any]:
        """
        Create a record, or fill in missing fields of an existing one.

        Parameters
        ----------
        agent_uid : str
            Agent session UID.
        agent_name : str
            Registered agent name.
        goal : str, optional
            Human-readable goal for the session.
        budget : dict, optional
            Budget, for example ``{"time_s": 7200}``.
        status : str, optional
            Initial status.
        **extra
            Additional top-level fields.

        Returns
        -------
        dict
            The record as written.
        """

        def update(record):
            now = time.time()
            if record is None:
                record = {
                    "agent_uid": agent_uid,
                    "agent_name": agent_name,
                    "goal": goal,
                    "status": status,
                    "budget": budget,
                    "opened": now,
                    "log": [],
                }
            else:
                record.setdefault("agent_uid", agent_uid)
                record.setdefault("agent_name", agent_name)
                record.setdefault("opened", now)
                record.setdefault("log", [])
                if goal is not None:
                    record["goal"] = goal
                if budget is not None:
                    record["budget"] = budget
                record["status"] = status
            record.update(extra)
            return record

        return self._modify(agent_uid, update)

    def append(self, agent_uid: str, kind: str, text: str, **refs: Any) -> dict[str, Any]:
        """
        Append an entry to an agent's log.

        Parameters
        ----------
        agent_uid : str
            Agent session UID.
        kind : str
            Entry kind, for example ``"note"`` or ``"run"``.
        text : str
            Human-readable text.
        **refs
            Optional references such as ``item_uid`` or ``run_uid``.

        Returns
        -------
        dict
            The appended entry.
        """
        entry = {"t": time.time(), "kind": kind, "text": text}
        entry.update({k: v for k, v in refs.items() if v is not None})

        def update(record):
            if record is None:
                record = {"agent_uid": agent_uid, "status": "unknown", "log": []}
            record.setdefault("log", []).append(entry)
            if kind in ANSWER_KINDS:
                record.pop("awaiting_input", None)
            return record

        self._modify(agent_uid, update)
        return entry

    def ask(self, agent_uid: str, text: str, source: Optional[str] = None, **refs: Any) -> dict[str, Any]:
        """
        Ask the operator a question and mark the agent as awaiting input.

        Parameters
        ----------
        agent_uid : str
            Agent session UID.
        text : str
            The question.
        source : str, optional
            Who asked.
        **refs
            Optional references, as for ``append``.

        Returns
        -------
        dict
            The appended entry.
        """
        entry = {"t": time.time(), "kind": "question", "text": text}
        if source is not None:
            entry["source"] = source
        entry.update({k: v for k, v in refs.items() if v is not None})

        def update(record):
            if record is None:
                record = {"agent_uid": agent_uid, "status": "unknown", "log": []}
            record.setdefault("log", []).append(entry)
            record["awaiting_input"] = dict(entry)
            return record

        self._modify(agent_uid, update)
        return entry

    def annotate(self, agent_uid: str, match, **fields: Any) -> int:
        """
        Set fields on the log entries ``match`` picks.

        Parameters
        ----------
        agent_uid : str
            Agent session UID.
        match : callable
            Called with each entry; true for the entries to change.
        **fields
            Fields to set on them.

        Returns
        -------
        int
            How many entries changed.
        """
        changed = [0]

        def update(record):
            changed[0] = 0
            if record is None:
                return {"agent_uid": agent_uid, "status": "unknown", "log": []}
            for entry in record.get("log") or []:
                if match(entry):
                    entry.update(fields)
                    changed[0] += 1
            return record

        self._modify(agent_uid, update)
        return changed[0]

    def set_status(self, agent_uid: str, status: str, **fields: Any) -> dict[str, Any]:
        """
        Set an agent's status and any other top-level fields.

        Parameters
        ----------
        agent_uid : str
            Agent session UID.
        status : str
            New status.
        **fields
            Additional top-level fields to set.

        Returns
        -------
        dict
            The record as written.
        """

        def update(record):
            if record is None:
                record = {"agent_uid": agent_uid, "log": []}
            record["status"] = status
            record.update(fields)
            return record

        return self._modify(agent_uid, update)

    def update(self, agent_uid: str, **fields: Any) -> dict[str, Any]:
        """
        Set top-level fields without touching the status.

        Parameters
        ----------
        agent_uid : str
            Agent session UID.
        **fields
            Top-level fields to set.

        Returns
        -------
        dict
            The record as written.
        """

        def update(record):
            if record is None:
                record = {"agent_uid": agent_uid, "status": "unknown", "log": []}
            record.update(fields)
            return record

        return self._modify(agent_uid, update)

    def message(self, agent_uid: str, text: str, source: str = "operator") -> dict[str, Any]:
        """
        Leave an operator message for a running agent.

        Parameters
        ----------
        agent_uid : str
            Agent session UID.
        text : str
            Message text.
        source : str, optional
            Who sent it.

        Returns
        -------
        dict
            The appended entry.
        """
        return self.append(agent_uid, "operator", text, source=source)

    def request_stop(self, agent_uid: str, text: str = "", source: str = "operator") -> dict[str, Any]:
        """
        Ask a running agent to stop adding work and wrap up.

        Parameters
        ----------
        agent_uid : str
            Agent session UID.
        text : str, optional
            Reason for the stop.
        source : str, optional
            Who asked.

        Returns
        -------
        dict
            The appended entry.
        """
        text = text or "Stop requested"
        entry = {"t": time.time(), "kind": "stop", "text": text, "source": source}

        def update(record):
            if record is None:
                record = {"agent_uid": agent_uid, "status": "unknown", "log": []}
            record.setdefault("log", []).append(entry)
            record.pop("awaiting_input", None)
            record["stop_requested"] = {"t": entry["t"], "text": text, "source": source}
            return record

        self._modify(agent_uid, update)
        return entry

    def request_recheck(
        self, agent_uid: str, about: Any, model: Optional[str] = None, text: str = "", source: str = "operator"
    ) -> dict[str, Any]:
        """
        Ask the assistant for a blind second look at one of its posts.

        Parameters
        ----------
        agent_uid : str
            The assistant record's UID.
        about : float or str
            The post, as ``find_assistant_post`` takes it.
        model : str, optional
            Model for the re-check; the runner uses its ``alert_model`` without one.
        text : str, optional
            What to look at in particular.
        source : str, optional
            Who asked.

        Returns
        -------
        dict
            The appended ``operator`` entry, with ``recheck``.

        Raises
        ------
        ValueError
            If no single post matches ``about``.
        """
        post = find_assistant_post(self.get(agent_uid) or {}, about)
        when = time.strftime("%H:%M:%S", time.localtime(post["t"]))
        message = f"Re-check the {when} {post['kind']}"
        if model:
            message += f" with {model}"
        if text.strip():
            message += f": {text.strip()}"
        recheck = {"t": post["t"], "model": model or None, "note": text.strip() or None}
        return self.append(agent_uid, "operator", message, source=source, recheck=recheck)

    def ensure_assistant_record(self, t: Optional[float] = None, **fields: Any) -> str:
        """
        Create the assistant's record for the day containing ``t`` if missing.

        Parameters
        ----------
        t : float, optional
            Epoch seconds; now if omitted.
        **fields
            Top-level fields for a new record.

        Returns
        -------
        str
            The record's UID.
        """
        uid = assistant_uid(t)
        if self.get(uid) is None:
            fields.setdefault("status", "idle")
            self.open(
                uid,
                ASSISTANT_NAME,
                goal="Read-only beamline assistant",
                kind=ASSISTANT_KIND,
                calls=0,
                **fields,
            )
        return uid

    def take_operator_messages(self, agent_uid: str) -> list[dict[str, Any]]:
        """
        Return operator entries the agent has not seen yet, and mark them seen.

        Parameters
        ----------
        agent_uid : str
            Agent session UID.

        Returns
        -------
        list of dict
            Unseen ``operator`` and ``stop`` entries, oldest first.
        """
        unread: list[dict[str, Any]] = []

        def update(record):
            unread.clear()
            if record is None:
                return {"agent_uid": agent_uid, "status": "unknown", "log": []}
            unread.extend(unread_operator_entries(record))
            if unread:
                record["operator_read"] = unread[-1]["t"]
            return record

        if unread_operator_entries(self.get(agent_uid) or {}):
            self._modify(agent_uid, update)
        return unread


def find_assistant_post(record: dict[str, Any], about: Any) -> dict[str, Any]:
    """
    One of the assistant's posts in its record.

    Parameters
    ----------
    record : dict
        The assistant's record.
    about : float or str
        The post's time: epoch seconds (as in ``nbs-cli log``), a local
        ``HH:MM`` or ``HH:MM:SS``, or ``"last"`` for the newest post.

    Returns
    -------
    dict
        The post.

    Raises
    ------
    ValueError
        If no post, or more than one, matches.
    """
    posts = [e for e in record.get("log") or [] if e.get("kind") in ASSISTANT_POST_KINDS]
    about = str(about).strip()
    if about == "last":
        if not posts:
            raise ValueError("the assistant has posted nothing today")
        return posts[-1]
    try:
        t = float(about)
    except ValueError:
        t = None
    if t is not None:
        # Exact first: the web page sends the time back as it got it
        found = [e for e in posts if e.get("t") == t] or [e for e in posts if abs((e.get("t") or 0) - t) < 1e-3]
    elif about.count(":") in (1, 2):
        fmt = "%H:%M:%S" if about.count(":") == 2 else "%H:%M"
        found = [e for e in posts if time.strftime(fmt, time.localtime(e.get("t") or 0)) == about]
    else:
        raise ValueError(f"name the post by its time (epoch seconds, HH:MM or HH:MM:SS) or 'last', not {about!r}")
    if not found:
        raise ValueError(f"no assistant post at {about}")
    if len(found) > 1:
        times = ", ".join(time.strftime("%H:%M:%S", time.localtime(e["t"])) for e in found)
        raise ValueError(f"{len(found)} posts at {about} ({times}); give the seconds too")
    return found[0]


def unread_operator_entries(record: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Operator entries newer than the record's ``operator_read`` mark.

    Parameters
    ----------
    record : dict
        Agent-log record.

    Returns
    -------
    list of dict
        Unseen ``operator`` and ``stop`` entries, oldest first.
    """
    seen = record.get("operator_read") or 0
    return [e for e in record.get("log") or [] if e.get("kind") in OPERATOR_KINDS and (e.get("t") or 0) > seen]
