"""
Redis clients that work with old servers.

redis-py 8 opens every connection with ``HELLO 3`` (RESP3), which Redis
servers older than 6 refuse ("unknown command `HELLO`"), and some beamline
Redis servers are that old. Every nbs-agents client asks for RESP2 instead,
which every server speaks; the agent log stores plain JSON strings, so RESP3
adds nothing here.
"""

from typing import Any

# The protocol every Redis server speaks (no HELLO)
RESP2 = 2


def connect(host: str, port: int = 6379, db: int = 0, **kwargs: Any):
    """
    A ``redis.Redis`` client using RESP2.

    Parameters
    ----------
    host, port, db
        Server and database.
    **kwargs
        Passed to ``redis.Redis`` (``socket_timeout`` and so on).

    Returns
    -------
    redis.Redis
        The client; it connects on first use.
    """
    import redis

    kwargs.setdefault("protocol", RESP2)
    return redis.Redis(host=host, port=int(port), db=int(db), **kwargs)
