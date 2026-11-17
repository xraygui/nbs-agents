"""
Runner plugin for Claude agents and the beamline assistant.

nbs-agents-runner loads it through the ``nbs_agents.runner`` entry point. It
adds two supervisor observers: one that launches a Claude child for each
``"claude"`` session opened with ``launch: true`` (``ClaudeLaunchHook``), and
the read-only beamline assistant if ``[assistant] enabled = true``.
"""

import dataclasses
import logging

from .agent import AssistantAgent
from nbs_agents.catalog import ZMQCatalog
from ..cli.config import AssistantConfig, CliConfig
from ..cli.core import Beamline
from .launcher import CLAUDE_AGENT, ClaudeLaunchHook, ClaudeLauncher
from .resources import SKILLS_DIR
from nbs_agents.queue import release_queue_toward_close

LOGGER = logging.getLogger(__name__)


DRIVERS_OFF = "Claude agents that drive the beamline are off here ([nbs_cli] drivers = false)"


class DriverRefusal:
    """Close every ``claude`` session as soon as it opens, when drivers are off."""

    def on_agent_opened(self, agent) -> None:
        if agent.agent_name != CLAUDE_AGENT:
            return
        agent.log("error", DRIVERS_OFF)
        try:
            # Drop the session's queue_stop so the queue runs on to close_agent
            release_queue_toward_close(agent.manager)
        except Exception:
            LOGGER.exception("Could not release the queue for refused session %s", agent.agent_uid)
        agent.stop(DRIVERS_OFF)


def plugin(runtime) -> list:
    """
    Make the Claude observers for nbs-agents-runner.

    Parameters
    ----------
    runtime : nbs_agents.runner.Runtime
        The runner's catalog and agent log.

    Returns
    -------
    list
        Supervisor observers.
    """
    config = CliConfig.load()
    if not config.drivers:
        LOGGER.info("Assistant only: Claude agents that drive the beamline are off ([nbs_cli] drivers = false)")
        observers: list = [DriverRefusal()]
    else:
        launcher = ClaudeLauncher.from_config(config)
        if launcher is None:
            LOGGER.info("Claude launching off (claude_command is empty)")
        else:
            LOGGER.info("Launching Claude for GUI-opened sessions with %r", launcher.command)
        observers = [ClaudeLaunchHook(launcher, runtime.agent_log)]
    assistant = start_assistant(config, runtime.catalog, runtime.agent_log)
    if assistant is not None:
        observers.append(assistant)
    return observers


def start_assistant(config: CliConfig, catalog: ZMQCatalog, agent_log) -> "AssistantAgent | None":
    """
    Start the read-only beamline assistant if ``[assistant] enabled = true``.

    Bad ``[assistant]`` settings, such as two roles sharing a trigger or a
    role naming a skill that is not installed, leave it off with an error.

    Parameters
    ----------
    config : CliConfig
        nbs-cli settings, for read access to the beamline.
    catalog : ZMQCatalog
        Document catalog.
    agent_log : AgentLog or None
        Agent log; the assistant needs one.

    Returns
    -------
    AssistantAgent or None
        The running assistant.
    """
    try:
        settings = AssistantConfig.load()
    except Exception:
        LOGGER.exception("Assistant disabled: bad [assistant] settings")
        return None
    if not settings.enabled:
        LOGGER.info("Assistant off ([assistant] enabled is not true)")
        return None
    if agent_log is None:
        LOGGER.error("Assistant disabled: it needs the agent log in Redis")
        return None
    roles = settings.resolved_roles()
    missing = [f"{r.name}: {r.skill}" for r in roles.values() if not (SKILLS_DIR / r.skill / "SKILL.md").is_file()]
    if missing:
        LOGGER.error("Assistant disabled: [assistant.roles] name skills that are not installed: %s", missing)
        return None
    launchers = {}
    if (settings.command or "").strip():
        launchers = {r.name: ClaudeLauncher(settings.command, r.workdir, settings.log_dir) for r in roles.values()}
    beamline = Beamline(dataclasses.replace(config, role="assistant"))
    assistant = AssistantAgent(
        settings, beamline, catalog, agent_log, launchers=launchers,
        signal=config.signal, norm=config.norm, reference=config.reference,
    )
    assistant.start()
    LOGGER.info("Assistant watching every run (record %s)", assistant.agent_uid)
    for role in roles.values():
        LOGGER.info(
            "Assistant role %s (%s, skill %s, models %s/%s, workdir %s): triggers %s, sinks %s",
            role.name, role.mode, role.skill, role.model, role.alert_model, role.workdir, role.triggers, role.sinks,
        )
    return assistant
