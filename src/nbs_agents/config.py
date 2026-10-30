"""
Settings every nbs-agents program shares: where the QueueServer, the
document stream, Redis, the agent log and Tiled are.

They come from, in increasing priority: built-in defaults, a TOML file
(``$NBS_CLI_CONFIG`` or ``config/nbs-cli.toml`` under ``$NBS_AGENTS_HOME``;
see ``nbs_agents.paths``; table ``[nbs_cli]``), and ``NBS_<NAME>``
environment variables. ``[agent_log]`` says where the agent log lives.
Packages built on nbs-agents subclass ``AgentsConfig`` to add their own
settings to the same file.
"""

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, ClassVar, Optional

from .paths import paths

try:
    import tomllib
except ImportError:  # pragma: no cover
    import tomli as tomllib

MAIN_TABLE = "nbs_cli"
AGENT_LOG_TABLE = "agent_log"
# Tables other than [nbs_cli] that packages read on their own
OTHER_TABLES = {AGENT_LOG_TABLE, "assistant"}
LIST_SETTINGS = ("allowed_plans", "baseline_keys")


@dataclass
class AgentLogConfig:
    """
    Where the agent log lives, from the ``[agent_log]`` table.

    By default the agent log shares the beamline Redis. Give it a ``port`` to
    use a Redis of its own, which nbs-agents-runner starts if ``serve`` is true
    (or which runs elsewhere, such as beside an agent container). The runner
    then publishes the address in the beamline Redis, so nbs-gui finds it
    without an nbs-cli config.

    Attributes
    ----------
    host : str
        Redis host; empty for ``localhost`` with a ``port``, or the beamline
        Redis without one.
    port : int or None
        Redis port; None to share the beamline Redis.
    db : int
        Redis database.
    prefix : str or None
        Key prefix; None for ``""`` on a Redis of its own, or the beamline
        prefix when shared.
    serve : bool
        Whether nbs-agents-runner starts ``redis-server`` on ``port`` (on
        127.0.0.1) unless one already answers there.
    data_dir : str
        Folder for the served Redis's snapshot, ``agent-log.rdb``, by default
        ``state/agent-log`` under ``$NBS_AGENTS_HOME``. It keeps the log across
        runner restarts; delete it to start a new beamtime.
    redis_server : str
        ``redis-server`` command.
    """

    host: str = ""
    port: Optional[int] = None
    db: int = 0
    prefix: Optional[str] = None
    serve: bool = False
    data_dir: str = field(default_factory=lambda: str(paths().agent_log_dir))
    redis_server: str = "redis-server"

    @property
    def dedicated(self) -> bool:
        """Whether the agent log has a Redis of its own."""
        return self.port is not None

    @classmethod
    def from_table(cls, table: Optional[dict[str, Any]]) -> "AgentLogConfig":
        """
        Settings from the ``[agent_log]`` table.

        Raises
        ------
        ValueError
            On unknown keys, or ``serve`` without a ``port``.
        """
        values = dict(table or {})
        unknown = set(values) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"Unknown [agent_log] settings: {sorted(unknown)}")
        config = cls(**values)
        if config.serve and not config.dedicated:
            raise ValueError("[agent_log] serve = true needs a port")
        return config


@dataclass
class AgentsConfig:
    """
    Connection settings shared by every nbs-agents program.

    Attributes
    ----------
    qs_addr : str
        QueueServer 0MQ control address.
    qs_user : str
        User name recorded on queue items.
    qs_user_group : str
        QueueServer user group used for submissions and ``plans_allowed``.
    zmq_addr : str
        Bluesky document stream address (``host:port``).
    tiled_uri : str
        Tiled server URI.
    tiled_api_key : str or None
        Fixed Tiled API key (or ``$TILED_API_KEY``). Usually unset: see
        ``nbs_agents.tiled_auth`` for logins and the agents' key.
    tiled_api_key_file : str
        The agents' read-only Tiled key, made by nbs-agents-runner; empty for
        ``state/tiled-agent/api-key.json`` under ``$NBS_AGENTS_HOME``.
    tiled_key_lifetime_s : float
        Life of each agent key; the runner replaces it after two thirds of that.
    tiled_path : str
        Path of the run catalog inside Tiled; empty for the root.
    redis_host, redis_port, redis_db, redis_prefix
        Beamline Redis holding nbs-bl status dicts (and the agent log, unless
        ``[agent_log]`` gives it a Redis of its own).
    agent_log : AgentLogConfig
        Where the agent log lives (``[agent_log]`` table).
    """

    # Subclasses that own the whole [nbs_cli] table refuse settings they don't know
    strict: ClassVar[bool] = False

    qs_addr: str = "tcp://localhost:60615"
    qs_user: str = "nbs-cli"
    qs_user_group: str = "primary"
    zmq_addr: str = "localhost:5578"
    tiled_uri: str = "http://localhost:8000"
    tiled_api_key: Optional[str] = None
    tiled_api_key_file: str = ""
    tiled_key_lifetime_s: float = 24 * 3600.0
    tiled_path: str = ""
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_prefix: str = ""
    agent_log: AgentLogConfig = field(default_factory=AgentLogConfig)

    @property
    def redis_settings(self) -> dict[str, Any]:
        """
        Redis settings in the nbs ``settings.redis`` shape.

        Returns
        -------
        dict
            ``host``, ``port``, ``db`` and ``prefix``.
        """
        return {
            "host": self.redis_host,
            "port": self.redis_port,
            "db": self.redis_db,
            "prefix": self.redis_prefix,
        }

    @property
    def agent_log_settings(self) -> dict[str, Any]:
        """
        Redis settings for the agent log, in the same shape as ``redis_settings``.

        Returns
        -------
        dict
            The ``[agent_log]`` Redis if it has a port, else the beamline Redis.
        """
        log = self.agent_log
        if not log.dedicated:
            out = self.redis_settings
            if log.prefix is not None:
                out["prefix"] = log.prefix
            return out
        return {"host": log.host or "localhost", "port": log.port, "db": log.db, "prefix": log.prefix or ""}

    @classmethod
    def load(cls, path: Optional[str] = None):
        """
        Load settings from defaults, a TOML file and the environment.

        Parameters
        ----------
        path : str, optional
            Explicit TOML file path.

        Returns
        -------
        AgentsConfig
            Resolved settings, as an instance of ``cls``.

        Raises
        ------
        ValueError
            On settings ``cls`` does not know, if ``cls.strict``.
        """
        data = read_config_file(path)
        table = {k: v for k, v in data.get(MAIN_TABLE, data).items() if k not in OTHER_TABLES}
        known = {f.name: f for f in fields(cls)}
        values = cls._from_env(known)
        values = {**table, **values}
        if values.get("tiled_api_key") is None:
            values["tiled_api_key"] = os.environ.get("TILED_API_KEY")
        unknown = set(values) - set(known)
        if unknown and cls.strict:
            raise ValueError(f"Unknown nbs-cli settings: {sorted(unknown)}")
        values = {k: v for k, v in values.items() if k in known}
        values["agent_log"] = AgentLogConfig.from_table(data.get(AGENT_LOG_TABLE))
        return cls(**values)

    @staticmethod
    def _from_env(known: dict[str, Any]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for name, spec in known.items():
            env = os.environ.get(f"NBS_{name.upper()}")
            if env is None or name == AGENT_LOG_TABLE:
                continue
            if name in LIST_SETTINGS:
                values[name] = [p.strip() for p in env.split(",") if p.strip()]
            elif spec.type in (bool, "bool"):
                values[name] = env.strip().lower() in ("1", "true", "yes", "on")
            elif spec.type in (int, "int"):
                values[name] = int(env)
            elif spec.type in (float, "float"):
                values[name] = float(env)
            else:
                values[name] = env
        return values


def read_config_file(path: Optional[str] = None) -> dict[str, Any]:
    """
    Read the nbs-cli TOML file.

    Parameters
    ----------
    path : str, optional
        Explicit path; otherwise ``$NBS_CLI_CONFIG`` or ``paths().config_file``.

    Returns
    -------
    dict
        File contents, or an empty dict if the default file does not exist.

    Raises
    ------
    FileNotFoundError
        If an explicitly named file does not exist.
    """
    config_path = path or os.environ.get("NBS_CLI_CONFIG")
    candidate = Path(config_path).expanduser() if config_path else paths().config_file
    if candidate.exists():
        with open(candidate, "rb") as f:
            return tomllib.load(f)
    if config_path:
        raise FileNotFoundError(f"nbs-cli config not found: {candidate}")
    return {}
