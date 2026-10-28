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

Operators steer a running agent through the same record: ``operator`` entries
are messages to the agent, and a ``stop`` entry plus a top-level
``stop_requested`` field ask it to wrap up. ``operator_read`` holds the time of
the newest operator entry the agent has seen.

An agent that needs an answer writes a ``question`` entry with ``ask``, which
also sets a top-level ``awaiting_input`` field (``{"t", "text", "source"}``).
The next operator, ``finalize`` or ``close`` entry clears it, so a GUI can show
that the agent is waiting until someone replies.
"""

import json
import logging
import time
from typing import Any, Optional

LOGGER = logging.getLogger(__name__)

AGENT_LOG_TOPIC = "AGENT"
OPERATOR_KINDS = ("operator", "stop")
ANSWER_KINDS = OPERATOR_KINDS + ("finalize", "close")


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
        import redis

        client = redis.Redis(
            host=settings["host"],
            port=int(settings.get("port", 6379)),
            db=int(settings.get("db", 0)),
        )
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

    def ask(self, agent_uid: str, text: str, source: Optional[str] = None) -> dict[str, Any]:
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

        Returns
        -------
        dict
            The appended entry.
        """
        entry = {"t": time.time(), "kind": "question", "text": text}
        if source is not None:
            entry["source"] = source

        def update(record):
            if record is None:
                record = {"agent_uid": agent_uid, "status": "unknown", "log": []}
            record.setdefault("log", []).append(entry)
            record["awaiting_input"] = dict(entry)
            return record

        self._modify(agent_uid, update)
        return entry

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
