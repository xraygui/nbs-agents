"""
JSON values in the agent-log Redis, one key per item: ``{prefix}{topic}{id}``.

The base of the manager's thread and directive stores. Each change is a
read-modify-write under ``WATCH``, as for the agent log and the tiles.
"""

import json
from typing import Any, Callable, Optional


class JsonStore:
    """
    Read and write JSON items under one topic of the agent-log Redis.

    Parameters
    ----------
    redis_client : redis.Redis
        Connected Redis client (the agent log's).
    prefix : str, optional
        Beamline Redis prefix, as the agent log's.
    """

    topic = ""

    def __init__(self, redis_client: Any, prefix: str = "") -> None:
        self.redis = redis_client
        self.prefix = prefix

    @classmethod
    def for_agent_log(cls, agent_log: Any):
        """The store beside an ``AgentLog``."""
        return cls(agent_log.redis, agent_log.prefix)

    def _key(self, item_id: str) -> str:
        return f"{self.prefix}{self.topic}{item_id}"

    def get(self, item_id: str) -> Optional[dict[str, Any]]:
        """One item, or None."""
        raw = self.redis.get(self._key(item_id))
        return json.loads(raw) if raw else None

    def all(self) -> dict[str, dict[str, Any]]:
        """Every item, by id."""
        out = {}
        prefix = f"{self.prefix}{self.topic}"
        for key in self.redis.scan_iter(match=f"{prefix}*"):
            key = key.decode() if isinstance(key, bytes) else key
            raw = self.redis.get(key)
            if raw:
                out[key[len(prefix):]] = json.loads(raw)
        return out

    def delete(self, item_id: str) -> bool:
        """Remove an item; whether it was there."""
        return bool(self.redis.delete(self._key(item_id)))

    def _modify(self, item_id: str, func: Callable[[Optional[dict[str, Any]]], dict[str, Any]]) -> dict[str, Any]:
        import redis

        key = self._key(item_id)
        with self.redis.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(key)
                    raw = pipe.get(key)
                    item = func(json.loads(raw) if raw else None)
                    pipe.multi()
                    pipe.set(key, json.dumps(item, default=str))
                    pipe.execute()
                    return item
                except redis.WatchError:
                    continue
