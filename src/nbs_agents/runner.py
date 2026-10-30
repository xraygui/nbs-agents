import dataclasses
import logging
import threading
import time

from bluesky.callbacks.zmq import RemoteDispatcher
from bluesky_queueserver_api.zmq import REManagerAPI

from .agentlog import AgentLog
from .auth import LaunchVerifier, Signer, token_path
from .catalog import ZMQCatalog
from .agents.assistant import AssistantAgent
from .cli.config import AssistantConfig, CliConfig
from .cli.core import Beamline
from .launcher import ClaudeLauncher
from .supervisor import AgentSupervisor

LOGGER = logging.getLogger(__name__)


def _start_heartbeat(agent_log: AgentLog, period_s: float = 5.0) -> None:
    """Write a heartbeat to Redis so nbs-cli open can tell the runner is up."""

    def beat():
        while True:
            try:
                agent_log.heartbeat()
            except Exception:
                LOGGER.exception("Could not write the runner heartbeat")
            time.sleep(period_s)

    threading.Thread(target=beat, name="runner-heartbeat", daemon=True).start()


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
        settings, beamline, catalog, agent_log, launcher=launcher, signal=config.signal, norm=config.norm
    )
    assistant.start()
    LOGGER.info(
        "Assistant watching every run (record %s, workdir %s, models %s/%s)",
        assistant.agent_uid, settings.workdir, settings.model, settings.alert_model,
    )
    return assistant


def main() -> None:
    """
    Start the persistent agent runner.

    Connection settings come from the same sources as nbs-cli
    (``NBS_QS_ADDR``, ``NBS_ZMQ_ADDR``, ``NBS_REDIS_*`` or a TOML file).
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = CliConfig.load()
    # Only this user's launch requests and operator messages are acted on
    signer = Signer.load(create=True)
    LOGGER.info("Using the nbs-agents token in %s", token_path())
    manager = REManagerAPI(zmq_control_addr=config.qs_addr)
    dispatcher = RemoteDispatcher(config.zmq_addr)
    catalog = ZMQCatalog(dispatcher)
    agent_log = None
    try:
        agent_log = AgentLog.from_settings(config.redis_settings, signer=signer)
        agent_log.redis.ping()
        LOGGER.info("Writing agent log to Redis %s:%s key %s*", config.redis_host, config.redis_port, agent_log.key_prefix)
    except Exception:
        LOGGER.exception("Agent log disabled: could not reach Redis at %s:%s", config.redis_host, config.redis_port)
        agent_log = None
    launcher = ClaudeLauncher.from_config(config)
    if launcher is None:
        LOGGER.info("Claude launching off (claude_command is empty)")
    else:
        LOGGER.info("Launching Claude for GUI-opened sessions with %r", launcher.command)
    observers = []
    assistant = start_assistant(config, catalog, agent_log)
    if assistant is not None:
        observers.append(assistant)
    AgentSupervisor(
        manager, catalog, agent_log=agent_log, launcher=launcher, observers=observers,
        launch_verifier=LaunchVerifier(signer),
    )
    if agent_log is not None:
        _start_heartbeat(agent_log)
    LOGGER.info("Agent runner listening for Bluesky documents at %s", config.zmq_addr)

    try:
        dispatcher.start()
    except KeyboardInterrupt:
        LOGGER.info("Stopping agent runner")
    finally:
        if assistant is not None:
            assistant.stop("runner stopped")


if __name__ == "__main__":
    main()
