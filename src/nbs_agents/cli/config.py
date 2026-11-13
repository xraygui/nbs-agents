"""
Settings for nbs-cli and the Claude agents.

``CliConfig`` adds nbs-cli's own settings to the shared connection settings
(``nbs_agents.config.AgentsConfig``) in the same TOML file and table. The
runner's assistant reads the ``[assistant]`` table of the same file.
"""

import os
import re
from dataclasses import dataclass, field, fields, replace
from typing import Any, Optional

from nbs_agents.config import AGENT_LOG_TABLE, AgentLogConfig, AgentsConfig, plugin_defaults, read_config_file  # noqa: F401
from nbs_agents.paths import paths

ASSISTANT_TABLE = "assistant"
ROLES_TABLE = "roles"
REFERENCES_TABLE = "references"
DEFAULT_CATALOG = "default"
DRIVER_ROLE = "driver"
ASSISTANT_ROLE = "assistant"
ROLES = (DRIVER_ROLE, ASSISTANT_ROLE)
# A headless assistant role's nbs-cli role is "assistant:NAME"
ASSISTANT_ROLE_PREFIX = f"{ASSISTANT_ROLE}:"

# [assistant.roles.NAME]
ROLE_MODES = ("headless", "chat")
ROLE_SINKS = ("tile", "record")
# Events that triage and the timers raise; each goes to the one role whose
# ``triggers`` name it. Operator questions, re-checks and the replies to a
# ``role request`` always go to the chat role, and a request to the role it names.
# ``digest`` replaces ``group_end`` for a driver's runs and ``checkin`` while a driver runs.
ROLE_TRIGGERS = ("run", "alert", "flag", "group_end", "checkin", "digest")
# What today's single assistant reacts to, and what the chat role takes by default
DEFAULT_TRIGGERS = ("alert", "flag", "group_end", "checkin", "digest")
# The chat role when no [assistant.roles] are configured
DEFAULT_CHAT_ROLE = "manager"
ROLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


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
    ref_dir : str
        Folder of approved reference scan entries (``nbs-cli ref``), usually
        ``references/`` in the beamline profile; empty for none. nbs-cli never
        writes there; nbs-agents-web writes the entries staff approve.
    ref_agent_dir : str
        Folder for the reference drafts agents write; empty for
        ``$NBS_REF_AGENT_DIR`` or ``data/references`` under ``$NBS_AGENTS_HOME``.
    ref_review_dir : str
        Folder for nbs-agents-web's reference review record (originals of
        approved drafts, rejected ones, staff requests for drafts); empty for
        ``$NBS_REF_REVIEW_DIR`` or ``references-review`` beside the agent folder.
        Agents get it read-only.
    ref_catalogs : dict
        Other Tiled catalogs reference scans may come from, by name, each
        ``{"uri": ..., "path": ...}``: the ``[references.catalogs.NAME]``
        tables, not ``[nbs_cli]``. The install's own ``tiled_uri`` and
        ``tiled_path`` are always the catalog ``default``.
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
    ref_dir: str = ""
    ref_agent_dir: str = ""
    ref_review_dir: str = ""
    ref_catalogs: dict[str, dict[str, str]] = field(default_factory=dict, metadata={"table": REFERENCES_TABLE})
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
        config.ref_catalogs = read_catalogs(read_config_file(path).get(REFERENCES_TABLE))
        if os.environ.get("NBS_CLI_ROLE"):
            config.role = os.environ["NBS_CLI_ROLE"]
        check_cli_role(config.role)
        return config

    @property
    def read_only(self) -> bool:
        """Whether this client may only read: an assistant role, or any role with drivers off."""
        return self.is_assistant or not self.drivers

    @property
    def is_assistant(self) -> bool:
        """Whether this client is the assistant: its chat role or one of its headless roles."""
        return self.role == ASSISTANT_ROLE or self.role.startswith(ASSISTANT_ROLE_PREFIX)

    @property
    def assistant_role(self) -> Optional[str]:
        """The headless assistant role this client acts for (``assistant:NAME``), or None."""
        if self.role.startswith(ASSISTANT_ROLE_PREFIX):
            return self.role[len(ASSISTANT_ROLE_PREFIX):]
        return None


def check_cli_role(role: str) -> None:
    """
    Check an nbs-cli role: ``driver``, ``assistant`` (the assistant's chat
    role) or ``assistant:NAME`` (one of its headless roles).

    Raises
    ------
    ValueError
        For anything else.
    """
    if role in ROLES:
        return
    if role.startswith(ASSISTANT_ROLE_PREFIX) and ROLE_NAME_RE.match(role[len(ASSISTANT_ROLE_PREFIX):]):
        return
    raise ValueError(f"Unknown nbs-cli role {role!r}; use one of {list(ROLES)} or assistant:NAME")


def read_catalogs(table: Optional[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """
    Check the ``[references]`` table's catalogs.

    Parameters
    ----------
    table : dict or None
        The ``[references]`` table: ``catalogs.NAME = {uri = ..., path = ...}``.

    Returns
    -------
    dict
        ``{name: {"uri": ..., "path": ...}}``.

    Raises
    ------
    ValueError
        On unknown settings, a catalog without a ``uri``, or one named ``default``.
    """
    table = dict(table or {})
    unknown = set(table) - {"catalogs"}
    if unknown:
        raise ValueError(f"Unknown [references] settings: {sorted(unknown)}")
    out = {}
    for name, entry in (table.get("catalogs") or {}).items():
        if name == DEFAULT_CATALOG:
            raise ValueError("[references.catalogs.default] is reserved: it is the install's own tiled_uri/tiled_path")
        if not isinstance(entry, dict) or not str(entry.get("uri") or "").strip():
            raise ValueError(f"[references.catalogs.{name}] needs a uri")
        extra = set(entry) - {"uri", "path"}
        if extra:
            raise ValueError(f"Unknown [references.catalogs.{name}] settings: {sorted(extra)}")
        out[name] = {"uri": str(entry["uri"]).strip(), "path": str(entry.get("path") or "").strip("/")}
    return out


@dataclass
class RoleConfig:
    """
    One assistant role, from an ``[assistant.roles.NAME]`` table.

    Attributes
    ----------
    name : str
        The role's name: lower case letters, digits and ``_``.
    mode : {"headless", "chat"}
        ``chat`` is the one role people talk to (the manager): it gets the
        operator's questions and re-checks and writes today's main assistant
        record. ``headless`` roles have no chat; they write a tile and their
        own record, ``assistant-NAME-YYYYMMDD``.
    skill : str
        Skill (under ``nbs_agents/assistant/skills``) put in the role's system prompt.
    model, alert_model : str
        Models for routine events, and for alerts, questions, re-checks and
        requests; empty for the ``[assistant]`` ones.
    triggers : list of str or None
        Events the role reacts to (``ROLE_TRIGGERS``). No two roles may share
        one. None for the chat role means every default trigger no headless
        role takes; for a headless role, none (it only answers requests).
    sinks : list of str or None
        Where the role may post: ``tile`` (``nbs-cli post --tile``) and
        ``record`` (``nbs-cli post --kind``). None for ``["record"]`` on the
        chat role and ``["tile", "record"]`` on a headless one.
    workdir : str
        The role's working folder; empty for ``[assistant] workdir`` (chat
        role) or that folder with ``-NAME`` added (headless roles).
    """

    name: str
    mode: str = "headless"
    skill: str = "beamline-assistant"
    model: str = ""
    alert_model: str = ""
    triggers: Optional[list[str]] = None
    sinks: Optional[list[str]] = None
    workdir: str = ""

    @property
    def chat(self) -> bool:
        return self.mode == "chat"


def read_roles(table: Optional[dict[str, Any]]) -> dict[str, RoleConfig]:
    """
    Check the ``[assistant.roles]`` tables.

    Parameters
    ----------
    table : dict or None
        ``{name: {mode, skill, model, alert_model, triggers, sinks, workdir}}``.

    Returns
    -------
    dict
        ``{name: RoleConfig}``, as written (defaults are filled by
        ``AssistantConfig.resolved_roles``).

    Raises
    ------
    ValueError
        On a bad name or setting, unless exactly one role is ``chat``, or when
        two roles share a trigger.
    """
    out: dict[str, RoleConfig] = {}
    known = {f.name for f in fields(RoleConfig)} - {"name"}
    for name, values in (table or {}).items():
        where = f"[assistant.roles.{name}]"
        if not ROLE_NAME_RE.match(name):
            raise ValueError(f"{where}: role names are lower case letters, digits and _ (at most 32)")
        if not isinstance(values, dict):
            raise ValueError(f"{where} must be a table")
        unknown = set(values) - known
        if unknown:
            raise ValueError(f"Unknown {where} settings: {sorted(unknown)}")
        role = RoleConfig(name=name, **values)
        if role.mode not in ROLE_MODES:
            raise ValueError(f"{where} mode must be one of {list(ROLE_MODES)}, not {role.mode!r}")
        for setting, allowed in (("triggers", ROLE_TRIGGERS), ("sinks", ROLE_SINKS)):
            value = getattr(role, setting)
            if value is None:
                continue
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError(f"{where} {setting} must be a list of names")
            bad = sorted(set(value) - set(allowed))
            if bad:
                hint = ""
                if setting == "triggers":
                    hint = " (questions, re-checks and replies always go to the chat role)"
                raise ValueError(f"{where} unknown {setting} {bad}; use {list(allowed)}{hint}")
        out[name] = role
    if not out:
        return out
    chat = [r.name for r in out.values() if r.chat]
    if len(chat) != 1:
        raise ValueError(f"[assistant.roles] need exactly one role with mode = \"chat\", not {chat or 'none'}")
    owner: dict[str, str] = {}
    for role in out.values():
        for trigger in role.triggers or []:
            if trigger in owner:
                raise ValueError(
                    f"[assistant.roles] {owner[trigger]} and {role.name} both have trigger {trigger!r}; "
                    "each event goes to exactly one role"
                )
            owner[trigger] = role.name
    return out


@dataclass
class AssistantConfig:
    """
    Settings for the runner's read-only beamline assistant, from the
    ``[assistant]`` table of the nbs-cli TOML file.

    Roles (``[assistant.roles.NAME]``, see ``RoleConfig``) split the
    assistant into one chat role and headless roles that each take their own
    events. Without any, the assistant is one chat role, ``manager``, with
    the settings below: the assistant as it was before roles.

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
    digest_min_s : float
        While a driver runs, the end of one of its groups of runs asks for a
        digest at most this often; later group ends wait for it (the hourly
        digest and the one at the end of the session do not).
    digest_chars : int
        Most characters of the driver's record since the last digest put into
        a digest call's prompt; all of it is also in ``digest/`` in the
        working folder.
    threads : bool
        Every question to the assistant opens its own thread (a Claude session
        resumed for each follow-up); False keeps questions in the main record.
    thread_idle_s : float
        Seconds a thread stays quiet before it closes with a summary in the
        chat role's ``notebook/threads.md``.
    thread_calls : int
        Most thread calls running at once (beside each role's own call).
    thread_keep_days : float
        Closed threads are deleted from Redis after this many days.
    beamtime_s : float
        Longest a directive that expires with the ``beamtime`` lasts, if the
        proposal does not change first.
    roles : dict
        ``{name: RoleConfig}`` from ``[assistant.roles]``; empty for one chat role.
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
    digest_min_s: float = 900.0
    digest_chars: int = 40000
    threads: bool = True
    thread_idle_s: float = 1800.0
    thread_calls: int = 2
    thread_keep_days: float = 14.0
    beamtime_s: float = 3 * 86400.0
    roles: dict[str, RoleConfig] = field(default_factory=dict)

    def resolved_roles(self) -> dict[str, RoleConfig]:
        """
        Every role with its defaults filled in, the chat role first.

        Returns
        -------
        dict
            ``{name: RoleConfig}``; ``{"manager": ...}`` with today's single
            assistant's settings when no roles are configured.
        """
        roles = self.roles or {DEFAULT_CHAT_ROLE: RoleConfig(DEFAULT_CHAT_ROLE, mode="chat")}
        taken = {t for r in roles.values() if not r.chat for t in r.triggers or []}
        out = {}
        for role in sorted(roles.values(), key=lambda r: not r.chat):
            if role.chat:
                triggers = role.triggers if role.triggers is not None else [t for t in DEFAULT_TRIGGERS if t not in taken]
                sinks = role.sinks if role.sinks is not None else ["record"]
                workdir = role.workdir or self.workdir
            else:
                triggers = list(role.triggers or [])
                sinks = role.sinks if role.sinks is not None else ["tile", "record"]
                workdir = role.workdir or f"{self.workdir.rstrip('/')}-{role.name}"
            out[role.name] = replace(
                role,
                model=role.model or self.model,
                alert_model=role.alert_model or self.alert_model,
                triggers=list(triggers),
                sinks=list(sinks),
                workdir=workdir,
            )
        return out

    def chat_role(self) -> RoleConfig:
        """The chat role, with its defaults filled in."""
        return next(r for r in self.resolved_roles().values() if r.chat)

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
        merged = {**defaults, **values}
        merged[ROLES_TABLE] = read_roles(merged.get(ROLES_TABLE))
        return cls(**merged)
