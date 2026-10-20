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

``kind`` is free-form but the shared vocabulary is ``open``, ``note``, ``add``,
``remove``, ``run``, ``status``, ``finalize``, ``close`` and ``error``. Optional
refs include ``item_uid``, ``run_uid``, ``plan`` and ``source`` (who wrote it).
"""

import json
import logging
import time
from typing import Any, Optional

LOGGER = logging.getLogger(__name__)

AGENT_LOG_TOPIC = "AGENT"


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
