import dataclasses
import logging
import threading
import time

from bluesky.callbacks.zmq import RemoteDispatcher
from bluesky_queueserver_api.zmq import REManagerAPI

from .agentlog import AgentLog
from .agentlog_store import AgentLogServer, publish_location
from .catalog import ZMQCatalog
from .agents.assistant import AssistantAgent
from .cli.config import AssistantConfig, CliConfig
from .cli.core import Beamline
from .launcher import ClaudeLauncher
from .supervisor import AgentSupervisor
from .tiled_auth import AgentKeyKeeper

LOGGER = logging.getLogger(__name__)


def _start_heartbeat(agent_log: AgentLog, config: CliConfig, period_s: float = 5.0) -> None:
    """
    Write a heartbeat to Redis so nbs-cli open can tell the runner is up.

    With a separate agent-log Redis, also keep its address published in the
    beamline Redis for nbs-gui, in case the beamline Redis restarted.
    """
    beamline = None
    location = config.agent_log_settings if config.agent_log.dedicated else None

    def beat():
        nonlocal beamline
        while True:
            try:
                agent_log.heartbeat()
            except Exception:
                LOGGER.exception("Could not write the runner heartbeat")
            try:
                if beamline is None:
                    beamline = _beamline_redis(config)
                publish_location(beamline, config.redis_prefix, location)
            except Exception:
                LOGGER.warning("Could not publish the agent log location in the beamline Redis", exc_info=True)
            time.sleep(period_s)

    threading.Thread(target=beat, name="runner-heartbeat", daemon=True).start()


def _beamline_redis(config: CliConfig):
    import redis

    return redis.Redis(host=config.redis_host, port=config.redis_port, db=config.redis_db, socket_timeout=5)


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
    (``NBS_QS_ADDR``, ``NBS_ZMQ_ADDR``, ``NBS_REDIS_*`` or a TOML file). With
    ``[agent_log] serve = true`` it also starts the agent log's own Redis.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = CliConfig.load()
    manager = REManagerAPI(zmq_control_addr=config.qs_addr)
    dispatcher = RemoteDispatcher(config.zmq_addr)
    catalog = ZMQCatalog(dispatcher)
    server = None
    if config.agent_log.serve:
        server = AgentLogServer(config.agent_log)
        try:
            server.start()
        except Exception:
            LOGGER.exception("Could not start the agent log Redis")
    agent_log = None
    settings = config.agent_log_settings
    try:
        agent_log = AgentLog.from_settings(settings)
        agent_log.redis.ping()
        LOGGER.info("Writing agent log to Redis %s:%s key %s*", settings["host"], settings["port"], agent_log.key_prefix)
    except Exception:
        LOGGER.exception("Agent log disabled: could not reach Redis at %s:%s", settings["host"], settings["port"])
        agent_log = None
    # Log in to Tiled (a link is printed and shown on the web page if needed) and
    # keep a read-only key for agents
    keeper = AgentKeyKeeper(config, on_link=agent_log.set_tiled_login if agent_log is not None else None)
    keeper.start()
    launcher = ClaudeLauncher.from_config(config)
    if launcher is None:
        LOGGER.info("Claude launching off (claude_command is empty)")
    else:
        LOGGER.info("Launching Claude for GUI-opened sessions with %r", launcher.command)
    observers = []
    assistant = start_assistant(config, catalog, agent_log)
    if assistant is not None:
        observers.append(assistant)
    AgentSupervisor(manager, catalog, agent_log=agent_log, launcher=launcher, observers=observers)
    if agent_log is not None:
        _start_heartbeat(agent_log, config)
    LOGGER.info("Agent runner listening for Bluesky documents at %s", config.zmq_addr)

    try:
        dispatcher.start()
    except KeyboardInterrupt:
        LOGGER.info("Stopping agent runner")
    finally:
        if assistant is not None:
            assistant.stop("runner stopped")
        keeper.stop()
        if server is not None:
            server.stop()


if __name__ == "__main__":
    main()
