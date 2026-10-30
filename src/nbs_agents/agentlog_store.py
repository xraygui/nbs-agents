"""
A Redis of its own for the agent log.

The agent log used to share the beamline Redis. With an ``[agent_log]`` port
in the nbs-cli config it lives in a separate Redis instead, which
nbs-agents-runner can start (``serve = true``) as a child ``redis-server`` on
127.0.0.1, or which runs elsewhere, such as beside an agent container.
Durable logs are meant to go to Tiled later; this store only needs to last a
beamtime, so it keeps a snapshot in ``data_dir`` and nothing more.

nbs-gui usually runs under a beamline operator account without an nbs-cli
config, so the runner publishes the agent log's address in the beamline Redis
under ``{prefix}NBS_AGENT_LOG_LOCATION``, and the Qt tabs look there first.
"""

import json
import logging
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from .cli.config import AgentLogConfig

LOGGER = logging.getLogger(__name__)

LOCATION_KEY = "NBS_AGENT_LOG_LOCATION"
SNAPSHOT_FILE = "agent-log.rdb"
START_TIMEOUT_S = 10.0
STOP_TIMEOUT_S = 10.0


def location_key(beamline_prefix: str) -> str:
    """Beamline Redis key holding the agent log's address."""
    return f"{beamline_prefix}{LOCATION_KEY}"


def publish_location(beamline_redis: Any, beamline_prefix: str, settings: Optional[dict[str, Any]]) -> None:
    """
    Tell clients without an nbs-cli config (nbs-gui) where the agent log is.

    Parameters
    ----------
    beamline_redis : redis.Redis
        The beamline Redis.
    beamline_prefix : str
        Its key prefix.
    settings : dict or None
        ``host``, ``port``, ``db`` and ``prefix`` of the agent log's Redis, or
        None to remove the entry (the agent log shares the beamline Redis).
    """
    key = location_key(beamline_prefix)
    if settings is None:
        beamline_redis.delete(key)
    else:
        beamline_redis.set(key, json.dumps({k: settings[k] for k in ("host", "port", "db", "prefix")}))


def read_location(beamline_redis: Any, beamline_prefix: str) -> Optional[dict[str, Any]]:
    """
    The agent log's Redis settings as published by the runner.

    Parameters
    ----------
    beamline_redis : redis.Redis
        The beamline Redis.
    beamline_prefix : str
        Its key prefix.

    Returns
    -------
    dict or None
        ``host``, ``port``, ``db`` and ``prefix``, or None if nothing valid is
        published (the agent log is in the beamline Redis).
    """
    raw = beamline_redis.get(location_key(beamline_prefix))
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return {"host": str(data["host"]), "port": int(data["port"]), "db": int(data.get("db", 0)),
                "prefix": str(data.get("prefix") or "")}
    except (ValueError, KeyError, TypeError):
        LOGGER.warning("Ignoring malformed %s: %r", location_key(beamline_prefix), raw)
        return None


def _answers(host: str, port: int) -> bool:
    import redis

    try:
        return bool(redis.Redis(host=host, port=port, socket_timeout=1, socket_connect_timeout=1).ping())
    except redis.RedisError:
        return False


class AgentLogServer:
    """
    Start and stop the agent log's own ``redis-server``.

    Parameters
    ----------
    config : AgentLogConfig
        ``[agent_log]`` settings; ``port`` is required.
    """

    def __init__(self, config: AgentLogConfig) -> None:
        if not config.dedicated:
            raise ValueError("the agent log server needs an [agent_log] port")
        self.config = config
        self.process: Optional[subprocess.Popen] = None

    @property
    def data_dir(self) -> Path:
        return Path(self.config.data_dir).expanduser()

    def command(self) -> list[str]:
        """
        The ``redis-server`` command line.

        It listens on 127.0.0.1 only, sends the keyspace notifications nbs-gui
        watches, and snapshots to ``data_dir`` after changes and on shutdown.
        """
        parts = shlex.split(self.config.redis_server)
        exe = shutil.which(parts[0]) or parts[0]
        return [exe, *parts[1:],
                "--bind", "127.0.0.1", "--port", str(self.config.port),
                "--dir", str(self.data_dir), "--dbfilename", SNAPSHOT_FILE,
                "--save", "60 1", "--appendonly", "no",
                "--notify-keyspace-events", "KEA",
                "--daemonize", "no", "--logfile", ""]

    def start(self) -> bool:
        """
        Start ``redis-server`` unless one already answers on the port.

        Returns
        -------
        bool
            True if this call started it (and ``stop`` will stop it).

        Raises
        ------
        RuntimeError
            If it exits or does not answer within ``START_TIMEOUT_S``.
        """
        host = "127.0.0.1"
        if _answers(host, self.config.port):
            LOGGER.info("Agent log Redis already running on port %s; using it", self.config.port)
            return False
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        log_path = self.data_dir / "redis.log"
        with open(log_path, "ab") as log:
            self.process = subprocess.Popen(
                self.command(), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"agent log redis-server exited with {self.process.returncode}; see {log_path}")
            if _answers(host, self.config.port):
                LOGGER.info("Started agent log Redis on port %s (snapshot in %s)", self.config.port, self.data_dir)
                return True
            time.sleep(0.1)
        self.stop()
        raise RuntimeError(f"agent log redis-server did not answer on port {self.config.port}; see {log_path}")

    def stop(self) -> None:
        """Stop the ``redis-server`` this object started; it saves a snapshot first."""
        process, self.process = self.process, None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            LOGGER.warning("Agent log Redis did not stop; killing it")
            process.kill()
            process.wait()
