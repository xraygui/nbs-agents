"""
Settings every nbs-agents program shares: where the QueueServer, the
document stream, the agent log and Tiled are.

They come from, in increasing priority: built-in defaults, a TOML file
(``$NBS_CLI_CONFIG`` or ``config/nbs-cli.toml`` under ``$NBS_AGENTS_HOME``;
see ``nbs_agents.paths``; table ``[nbs_cli]``), and ``NBS_<NAME>``
environment variables. ``[agent_log]`` says where the agent log lives.
Packages built on nbs-agents subclass ``AgentsConfig`` to add their own
settings to the same file.

A beamline package can change the built-in defaults through the
``nbs_agents.config_defaults`` entry point group: each entry is a dict of
tables, such as ``{"nbs_cli": {"signal": "sc"}, "assistant": {...}}``,
applied under the file and the environment (see ``plugin_defaults``).
It can also add settings of its own to ``[nbs_cli]`` through the
``nbs_agents.settings`` group: each entry is a dict of setting names and
defaults (see ``plugin_settings``); their values land in ``AgentsConfig.extra``.
"""

import copy
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
DEFAULT_AGENT_LOG_PORT = 60740
NO_AGENT_LOG = "the agent log needs a Redis of its own: [agent_log] port must be set"
CONFIG_DEFAULTS_ENTRY_POINT = "nbs_agents.config_defaults"
SETTINGS_ENTRY_POINT = "nbs_agents.settings"


def plugin_settings() -> dict[str, Any]:
    """
    ``[nbs_cli]`` settings that installed beamline packages add, with their defaults.

    Returns
    -------
    dict
        Name to default from every ``nbs_agents.settings`` entry point, in
        name order (a later one wins on a clash).
    """
    from importlib.metadata import entry_points

    out: dict[str, Any] = {}
    for entry in sorted(entry_points(group=SETTINGS_ENTRY_POINT), key=lambda e: e.name):
        out.update(entry.load() or {})
    return copy.deepcopy(out)


def plugin_defaults(table: str) -> dict[str, Any]:
    """
    Default settings for one table from installed beamline packages.

    Parameters
    ----------
    table : str
        Table name, such as ``"nbs_cli"`` or ``"assistant"``.

    Returns
    -------
    dict
        Settings from every ``nbs_agents.config_defaults`` entry point, in
        name order (a later one wins on a clash).
    """
    from importlib.metadata import entry_points

    out: dict[str, Any] = {}
    for entry in sorted(entry_points(group=CONFIG_DEFAULTS_ENTRY_POINT), key=lambda e: e.name):
        out.update((entry.load() or {}).get(table) or {})
    return copy.deepcopy(out)


@dataclass
class AgentLogConfig:
    """
    Where the agent log lives, from the ``[agent_log]`` table.

    The agent log lives in a Redis of its own. By default nbs-agents-runner
    serves it on 127.0.0.1:60740, so no ``[agent_log]`` table is needed; give
    another ``port``, a ``host`` elsewhere (such as beside an agent container)
    or ``serve = false`` for one that runs on its own.

    Attributes
    ----------
    host : str
        Redis host; empty for ``localhost``.
    port : int
        Redis port.
    db : int
        Redis database.
    prefix : str or None
        Key prefix; None for ``""``.
    serve : bool
        Whether nbs-agents-runner starts ``redis-server`` on ``port`` (on
        127.0.0.1) unless one already answers there. Default true.
    data_dir : str
        Folder for the served Redis's snapshot, ``agent-log.rdb``, by default
        ``state/agent-log`` under ``$NBS_AGENTS_HOME``. It keeps the log across
        runner restarts; delete it to start a new beamtime.
    redis_server : str
        ``redis-server`` command.
    """

    host: str = ""
    port: Optional[int] = DEFAULT_AGENT_LOG_PORT
    db: int = 0
    prefix: Optional[str] = None
    serve: bool = True
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
    agent_log : AgentLogConfig
        Where the agent log lives (``[agent_log]`` table).
    extra : dict
        Settings beamline packages add (``nbs_agents.settings``), by name.
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
    agent_log: AgentLogConfig = field(default_factory=AgentLogConfig)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def agent_log_settings(self) -> dict[str, Any]:
        """
        Redis settings for the agent log.

        Returns
        -------
        dict
            ``host``, ``port``, ``db`` and ``prefix`` (see
            ``nbs_agents.redisclient.connect_settings``).

        Raises
        ------
        ValueError
            If ``[agent_log]`` has no ``port``.
        """
        log = self.agent_log
        if not log.dedicated:
            raise ValueError(NO_AGENT_LOG)
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
        known = {f.name: f.type for f in fields(cls) if f.name not in (AGENT_LOG_TABLE, "extra")}
        added = plugin_settings()
        types = {**known, **{name: type(default) for name, default in added.items()}}
        values = {**table, **cls._from_env(types)}
        if values.get("tiled_api_key") is None:
            values["tiled_api_key"] = os.environ.get("TILED_API_KEY")
        unknown = set(values) - set(types)
        if unknown and cls.strict:
            message = f"Unknown nbs-cli settings: {sorted(unknown)}"
            if any(name.startswith("redis_") for name in unknown):
                message += (" (the beamline Redis settings belong to a beamline package such as "
                            "sst-assistant: install it, or remove them)")
            raise ValueError(message)
        values = {**plugin_defaults(MAIN_TABLE), **values}
        extra = {name: values.get(name, default) for name, default in added.items()}
        values = {k: v for k, v in values.items() if k in known}
        values["agent_log"] = AgentLogConfig.from_table(data.get(AGENT_LOG_TABLE))
        return cls(**values, extra=extra)

    @staticmethod
    def _from_env(types: dict[str, Any]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for name, spec in types.items():
            env = os.environ.get(f"NBS_{name.upper()}")
            if env is None:
                continue
            if name in LIST_SETTINGS or spec is list:
                values[name] = [p.strip() for p in env.split(",") if p.strip()]
            elif spec in (bool, "bool"):
                values[name] = env.strip().lower() in ("1", "true", "yes", "on")
            elif spec in (int, "int"):
                values[name] = int(env)
            elif spec in (float, "float"):
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
