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
    launcher = None
    if (settings.command or "").strip():
        launcher = ClaudeLauncher(settings.command, settings.workdir, settings.log_dir)
    beamline = Beamline(dataclasses.replace(config, role="assistant"))
    assistant = AssistantAgent(
        settings, beamline, catalog, agent_log, launcher=launcher,
        signal=config.signal, norm=config.norm, reference=config.reference,
    )
    assistant.start()
    LOGGER.info(
        "Assistant watching every run (record %s, workdir %s, models %s/%s)",
        assistant.agent_uid, settings.workdir, settings.model, settings.alert_model,
    )
    return assistant
