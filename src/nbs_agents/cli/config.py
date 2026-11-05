"""
Settings for nbs-cli and the Claude agents.

``CliConfig`` adds nbs-cli's own settings to the shared connection settings
(``nbs_agents.config.AgentsConfig``) in the same TOML file and table. The
runner's assistant reads the ``[assistant]`` table of the same file.
"""

import os
from dataclasses import dataclass, field, fields
from typing import Any, Optional

from nbs_agents.config import AGENT_LOG_TABLE, AgentLogConfig, AgentsConfig, plugin_defaults, read_config_file  # noqa: F401
from nbs_agents.paths import paths

ASSISTANT_TABLE = "assistant"
DRIVER_ROLE = "driver"
ASSISTANT_ROLE = "assistant"
ROLES = (DRIVER_ROLE, ASSISTANT_ROLE)


@dataclass
class CliConfig(AgentsConfig):
    """
    nbs-cli and Claude agent settings, on top of the shared connection
    settings (see ``AgentsConfig`` for those).

    Attributes
    ----------
    drivers : bool
        Whether Claude agents that drive the beamline are allowed. False makes
        the install assistant-only: the runner launches no driver and closes
        any ``claude`` session that opens, "New agent" is refused, and
        nbs-cli refuses every verb that queues or steers (as in the assistant
        role). Also ``NBS_DRIVERS``. Give the driver's QueueServer group no
        plans as well, so nothing queued by hand can run.
    signal, norm : str
        Default detector and normalization keys for run summaries.
    reference : str
        Energy-reference detector key, for reducer plugins (sst-assistant's
        reports its edge in XAS summaries as ``ref_e0_max_slope``). Empty to skip.
    agent_name : str
        Agent name used by ``open``.
    allowed_plans : list of str or None
        Client-side plan allowlist for ``add``; ``None`` defers to the server.
    driver : str or None
        Token naming the process that drives a session (``NBS_DRIVER``).
        ``open`` records it, and only calls carrying the same token mark the
        session's operator messages read; other clients just see them.
    claude_command : str
        Command nbs-agents-runner starts for an ``open_agent("claude", ...)``
        block whose parameters have ``launch: true``. It gets ``--headless``.
    claude_workdir : str
        Working folder for a launched Claude; ``{uid8}`` is replaced by the
        first eight characters of the session UID.
    claude_log_dir : str
        Folder for the launched Claude's stdout/stderr, one file per session.
    claude_stop_timeout_s : float
        Seconds between SIGTERM and SIGKILL when the runner stops a launched Claude.
    claude_sandbox : str
        Wrapper ``claude-beamline`` runs Claude in: ``"none"`` (default) or
        ``"bwclaude"`` (n2snscripts, through the NSLS-II API gateway). Also
        ``NBS_CLAUDE_SANDBOX`` or ``claude-beamline --sandbox``.
    role : str
        ``"driver"`` (default) or ``"assistant"``. The assistant role is
        read-only: verbs that queue, remove, abort, start, finalize or message
        a session are refused, and ``post`` writes only to the assistant's own
        daily record. Also set by ``NBS_CLI_ROLE`` or ``--role``.
    assistant_user_group : str
        QueueServer user group the assistant role submits as. It should allow
        no plans, so the server refuses anything nbs-cli let through.
    attachments_dir : str
        Folder for figures that messages link as ``att:ID``; empty for
        ``$NBS_ATTACHMENTS_DIR`` or ``data/attachments`` under ``$NBS_AGENTS_HOME``.
        nbs-agents-web must read the same folder.
    kb_dir : str
        Folder of human-written knowledge-base notes (``nbs-cli kb``), usually
        ``knowledge/`` in the beamline profile; empty for none. nbs-cli never
        writes there; nbs-agents-web writes the notes staff approve or write.
    baseline_keys : list of str
        Baseline readings that run summaries report as ``[start, end]``, such as
        the ring current and vacuum gauges.
    beam_map_file : str
        The beamline profile's ``beam_map.toml``. The PVs its ``[status]``
        table names are read straight from EPICS for ``beam`` and ``state``:
        readings the assistant must always see, such as the ring mode, even
        when no Bluesky session publishes them. Enum PVs read as their state
        strings. Empty for none.
    epics_ca_addr_list : str
        ``EPICS_CA_ADDR_LIST`` for those reads (auto address list off); empty to
        use the environment's. bwclaude drops the environment, so agents need it.
    status_pv_timeout_s : float
        Seconds to wait for each status PV.
    kb_agent_dir : str
        Folder for notes and appendices agents write through ``nbs-cli kb``;
        empty for ``$NBS_KB_AGENT_DIR`` or ``data/kb`` under ``$NBS_AGENTS_HOME``.
    kb_review_dir : str
        Folder for nbs-agents-web's review record (kept and reviewed items,
        originals of approved notes, rejected ones); empty for
        ``$NBS_KB_REVIEW_DIR`` or ``kb-review`` beside the agent folder.
        Agents get it read-only.
    """

    strict = True

    drivers: bool = True

    signal: str = ""
    norm: str = ""
    reference: str = ""
    agent_name: str = "claude"
    allowed_plans: Optional[list[str]] = field(default=None)
    driver: Optional[str] = None
    claude_command: str = "claude-beamline"
    claude_workdir: str = field(default_factory=lambda: str(paths().sessions / "{uid8}"))
    claude_log_dir: str = field(default_factory=lambda: str(paths().logs))
    claude_stop_timeout_s: float = 10.0
    claude_sandbox: str = "none"
    role: str = "driver"
    assistant_user_group: str = "assistant"
    attachments_dir: str = ""
    kb_dir: str = ""
    kb_agent_dir: str = ""
    kb_review_dir: str = ""
    baseline_keys: list[str] = field(default_factory=list)
    beam_map_file: str = ""
    epics_ca_addr_list: str = ""
    status_pv_timeout_s: float = 2.0

    @classmethod
    def load(cls, path: Optional[str] = None) -> "CliConfig":
        """
        Load settings from defaults, a TOML file and the environment.

        Parameters
        ----------
        path : str, optional
            Explicit TOML file path.

        Returns
        -------
        CliConfig
            Resolved settings.
        """
        config = super().load(path)
        if os.environ.get("NBS_CLI_ROLE"):
            config.role = os.environ["NBS_CLI_ROLE"]
        if config.role not in ROLES:
            raise ValueError(f"Unknown nbs-cli role {config.role!r}; use one of {list(ROLES)}")
        return config

    @property
    def read_only(self) -> bool:
        """Whether this client may only read: the assistant role, or any role with drivers off."""
        return self.role == ASSISTANT_ROLE or not self.drivers


@dataclass
class AssistantConfig:
    """
    Settings for the runner's read-only beamline assistant, from the
    ``[assistant]`` table of the nbs-cli TOML file.

    Attributes
    ----------
    enabled : bool
        Whether nbs-agents-runner hosts the assistant.
    command : str
        Command started for each call, with ``--headless --role assistant``.
    workdir : str
        The assistant's working folder; its ``notebook/`` is memory between calls.
    log_dir : str
        Folder for each call's stdout/stderr.
    model, alert_model : str
        Claude model for routine calls, and for alerts and operator questions.
    min_jump_to_noise : float
        Flag an XAS run whose edge jump to noise ratio is below this.
    i0_drop_fraction : float
        Flag a run whose mean i0 is below this fraction of the recent baseline.
    i0_baseline_runs : int
        Number of recent runs whose median i0 is the baseline.
    change_fraction : float
        Flag a run whose edge jump differs by more than this fraction from the
        last run on the same sample and edge.
    group_by : list of str
        Run fields (``plan``, ``sample``, ``edge``) whose change ends a group.
    checkin_s : float
        Seconds without any assistant post or operator message before an
        hourly check-in, if anything ran since.
    poll_s : float
        Seconds between checks of the queue, operator messages and the timer.
    call_timeout_s : float
        Seconds before a call is stopped.
    snapshot_chars : int
        Most characters of each part of the snapshot (beamline state, the
        assistant's recent record, its notebook) put into every call's prompt;
        0 for no snapshot.
    """

    enabled: bool = False
    command: str = "claude-beamline"
    workdir: str = field(default_factory=lambda: str(paths().assistant))
    log_dir: str = field(default_factory=lambda: str(paths().logs))
    model: str = "sonnet"
    alert_model: str = "opus"
    min_jump_to_noise: float = 10.0
    i0_drop_fraction: float = 0.5
    i0_baseline_runs: int = 5
    change_fraction: float = 0.5
    group_by: list[str] = field(default_factory=lambda: ["plan", "sample", "edge"])
    checkin_s: float = 3600.0
    poll_s: float = 5.0
    call_timeout_s: float = 900.0
    snapshot_chars: int = 12000

    @classmethod
    def load(cls, path: Optional[str] = None) -> "AssistantConfig":
        """
        Load the ``[assistant]`` table.

        Parameters
        ----------
        path : str, optional
            Explicit TOML file path.

        Returns
        -------
        AssistantConfig
            Settings; defaults (disabled, or a beamline package's
            ``nbs_agents.config_defaults``) where the table is silent.
        """
        values = dict(read_config_file(path).get(ASSISTANT_TABLE) or {})
        known = {f.name for f in fields(cls)}
        unknown = set(values) - known
        if unknown:
            raise ValueError(f"Unknown [assistant] settings: {sorted(unknown)}")
        defaults = {k: v for k, v in plugin_defaults(ASSISTANT_TABLE).items() if k in known}
        return cls(**{**defaults, **values})
