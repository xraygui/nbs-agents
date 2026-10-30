"""
Redis clients that work with old servers.

redis-py 8 opens every connection with ``HELLO 3`` (RESP3), which Redis
servers older than 6 refuse ("unknown command `HELLO`"), and some beamline
Redis servers are that old. Every nbs-agents client asks for RESP2 instead,
which every server speaks; the agent log stores plain JSON strings, so RESP3
adds nothing here.

NSLS-II's beamline Redis is encrypted: TLS with a password, as
``nslsii.utils.open_redis_client`` connects. ``connect_settings`` does the
same from ``ssl``/``password_file`` settings.
"""

import os
from pathlib import Path
from typing import Any, Optional

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


DEFAULT_SECRET_FILE = "/etc/bluesky/redis.secret"


def redis_password(password_file: Optional[str] = None) -> str:
    """
    The encrypted beamline Redis password, found as nslsii finds it.

    ``$REDIS_PASSWORD`` if set, else the first line of ``password_file``,
    ``$REDIS_SECRET_FILE`` or ``/etc/bluesky/redis.secret``.

    Raises
    ------
    OSError
        If no password is set and the file cannot be read.
    """
    if os.environ.get("REDIS_PASSWORD"):
        return os.environ["REDIS_PASSWORD"]
    path = password_file or os.environ.get("REDIS_SECRET_FILE") or DEFAULT_SECRET_FILE
    return Path(path).expanduser().read_text(encoding="utf-8").strip()


def connect_settings(settings: dict[str, Any], **kwargs: Any):
    """
    A RESP2 client from nbs-style Redis settings.

    Parameters
    ----------
    settings : dict
        ``host``, and optionally ``port``, ``db``, ``ssl`` (TLS with a
        password; see ``redis_password``) and ``password_file``.
    **kwargs
        Passed to ``redis.Redis``.
    """
    if settings.get("ssl"):
        kwargs.setdefault("ssl", True)
        kwargs.setdefault("password", redis_password(settings.get("password_file") or None))
    return connect(settings["host"], settings.get("port", 6379), settings.get("db", 0), **kwargs)
