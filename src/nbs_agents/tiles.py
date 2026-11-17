"""
Role tiles: the latest word of each headless assistant role.

Each role has one tile, a JSON value in the agent-log Redis under
``{prefix}tile:{role}``::

    {
        "role": str,
        "severity": "ok" | "info" | "warn" | "alert",
        "message": str,
        "ts": float,             # when this post was made
        "run_uids": [str],
        "figure": str or None,   # att:ID of a figure (nbs-cli attach)
        "model": str or None,
        "cleared": bool,         # an unacknowledged alert whose condition has since cleared
        "latest": dict or None,  # the newest post while an alert is held (same fields)
        "ack": {"by", "ts"} or None,
        "last_ack": {"by", "ts", "message"} or None,
    }

Alerts are sticky. While an ``alert`` is unacknowledged, a later post of
lower severity does not replace it: it sets ``cleared`` and is kept as
``latest``. Only a person's acknowledgement (``acknowledge``, from the web UI
or ``nbs-cli ack ROLE``) lets it go: a cleared alert then gives way to
``latest``, and one that has not cleared stays shown but acknowledged, so the
next post replaces it. Each acknowledgement is logged as an ``ack`` entry in
the role's record. A new alert always replaces the tile.
"""

import json
import time
from typing import Any, Optional

SEVERITIES = ("ok", "info", "warn", "alert")
TILE_TOPIC = "tile:"
POST_FIELDS = ("severity", "message", "ts", "run_uids", "figure", "model")


class TileError(ValueError):
    """A tile post or acknowledgement that cannot be done."""


def held(tile: Optional[dict[str, Any]]) -> bool:
    """Whether a tile shows an alert no one has acknowledged yet."""
    return bool(tile) and tile.get("severity") == "alert" and not tile.get("ack")


def next_tile(current: Optional[dict[str, Any]], post: dict[str, Any]) -> dict[str, Any]:
    """
    The tile after a role's post.

    Parameters
    ----------
    current : dict or None
        The tile now.
    post : dict
        ``POST_FIELDS`` of the new post, plus ``role``.

    Returns
    -------
    dict
        The new tile.
    """
    if held(current) and post["severity"] != "alert":
        tile = dict(current)
        tile["cleared"] = True
        tile["latest"] = {k: post.get(k) for k in POST_FIELDS}
        return tile
    tile = {k: post.get(k) for k in POST_FIELDS}
    tile.update(role=post["role"], cleared=False, latest=None, ack=None)
    tile["last_ack"] = (current or {}).get("last_ack")
    return tile


def acked_tile(current: Optional[dict[str, Any]], by: str, t: float) -> dict[str, Any]:
    """
    The tile after a person acknowledges it.

    Parameters
    ----------
    current : dict or None
        The tile now.
    by : str
        Who acknowledged it.
    t : float
        When.

    Returns
    -------
    dict
        The new tile.

    Raises
    ------
    TileError
        If the tile holds no unacknowledged alert.
    """
    if not held(current):
        raise TileError("nothing to acknowledge: the tile shows no unacknowledged alert")
    last_ack = {"by": by, "ts": t, "message": current.get("message")}
    if current.get("cleared") and current.get("latest"):
        tile = {k: current["latest"].get(k) for k in POST_FIELDS}
        tile.update(role=current.get("role"), cleared=False, latest=None, ack=None, last_ack=last_ack)
        return tile
    tile = dict(current)
    tile.update(ack={"by": by, "ts": t}, last_ack=last_ack)
    return tile


class TileStore:
    """
    Read and write role tiles in the agent-log Redis.

    Parameters
    ----------
    redis_client : redis.Redis
        Connected Redis client (the agent log's).
    prefix : str, optional
        Beamline Redis prefix, as the agent log's.
    """

    def __init__(self, redis_client: Any, prefix: str = "") -> None:
        self.redis = redis_client
        self.prefix = prefix

    @classmethod
    def for_agent_log(cls, agent_log: Any) -> "TileStore":
        """The tiles beside an ``AgentLog``."""
        return cls(agent_log.redis, agent_log.prefix)

    def _key(self, role: str) -> str:
        return f"{self.prefix}{TILE_TOPIC}{role}"

    def get(self, role: str) -> Optional[dict[str, Any]]:
        """One role's tile, or None."""
        raw = self.redis.get(self._key(role))
        return json.loads(raw) if raw else None

    def all(self) -> dict[str, dict[str, Any]]:
        """Every tile, by role."""
        out = {}
        prefix = f"{self.prefix}{TILE_TOPIC}"
        for key in self.redis.scan_iter(match=f"{prefix}*"):
            key = key.decode() if isinstance(key, bytes) else key
            raw = self.redis.get(key)
            if raw:
                out[key[len(prefix):]] = json.loads(raw)
        return out

    def _modify(self, role: str, func) -> dict[str, Any]:
        import redis

        key = self._key(role)
        with self.redis.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(key)
                    raw = pipe.get(key)
                    tile = func(json.loads(raw) if raw else None)
                    pipe.multi()
                    pipe.set(key, json.dumps(tile, default=str))
                    pipe.execute()
                    return tile
                except redis.WatchError:
                    continue

    def post(
        self,
        role: str,
        severity: str,
        message: str,
        run_uids: Optional[list[str]] = None,
        figure: Optional[str] = None,
        model: Optional[str] = None,
        t: Optional[float] = None,
    ) -> dict[str, Any]:
        """
        Post to a role's tile (see ``next_tile`` for the sticky alerts).

        Returns
        -------
        dict
            The tile as written.

        Raises
        ------
        TileError
            On an unknown severity or an empty message.
        """
        if severity not in SEVERITIES:
            raise TileError(f"unknown severity {severity!r}; use one of {list(SEVERITIES)}")
        if not (message or "").strip():
            raise TileError("tile message is required")
        post = {
            "role": role, "severity": severity, "message": message.strip(), "ts": t or time.time(),
            "run_uids": list(run_uids or []), "figure": figure or None, "model": model or None,
        }
        return self._modify(role, lambda current: next_tile(current, post))

    def ack(self, role: str, by: str, t: Optional[float] = None) -> dict[str, Any]:
        """
        Acknowledge a role's alert (see ``acked_tile``). Use ``acknowledge``
        to log it as well.

        Returns
        -------
        dict
            The tile as written.

        Raises
        ------
        TileError
            If there is nothing to acknowledge.
        """
        when = t or time.time()
        return self._modify(role, lambda current: acked_tile(current, by, when))


def acknowledge(agent_log: Any, role: str, by: str, source: str = "operator", chat: bool = False) -> dict[str, Any]:
    """
    Acknowledge a role's alert and log it in the role's record for today.

    Parameters
    ----------
    agent_log : AgentLog
        The agent log; its Redis holds the tiles.
    role : str
        The role.
    by : str
        The person acknowledging.
    source : str, optional
        Where it came from (``nbs-cli``, ``web``).
    chat : bool, optional
        True when ``role`` is the chat role, whose record is the main one.

    Returns
    -------
    dict
        ``tile`` (as written) and ``entry`` (the ``ack`` entry).

    Raises
    ------
    TileError
        If there is nothing to acknowledge.
    """
    store = TileStore.for_agent_log(agent_log)
    before = store.get(role)
    tile = store.ack(role, by)
    what = "condition had cleared" if (before or {}).get("cleared") else "condition not cleared yet"
    uid = agent_log.ensure_assistant_record(role=None if chat else role)
    entry = agent_log.append(
        uid, "ack", f"{by} acknowledged the alert: {(before or {}).get('message')} ({what})",
        source=source, by=by, alert_ts=(before or {}).get("ts"), run_uids=(before or {}).get("run_uids") or None,
    )
    return {"agent_uid": uid, "tile": tile, "entry": entry}
