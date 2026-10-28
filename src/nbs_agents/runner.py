import logging
import threading
import time

from bluesky.callbacks.zmq import RemoteDispatcher
from bluesky_queueserver_api.zmq import REManagerAPI

from .agentlog import AgentLog
from .catalog import ZMQCatalog
from .cli.config import CliConfig
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
    manager = REManagerAPI(zmq_control_addr=config.qs_addr)
    dispatcher = RemoteDispatcher(config.zmq_addr)
    catalog = ZMQCatalog(dispatcher)
    agent_log = None
    try:
        agent_log = AgentLog.from_settings(config.redis_settings)
        agent_log.redis.ping()
        LOGGER.info("Writing agent log to Redis %s:%s key %s*", config.redis_host, config.redis_port, agent_log.key_prefix)
    except Exception:
        LOGGER.exception("Agent log disabled: could not reach Redis at %s:%s", config.redis_host, config.redis_port)
        agent_log = None
    AgentSupervisor(manager, catalog, agent_log=agent_log)
    if agent_log is not None:
        _start_heartbeat(agent_log)
    LOGGER.info("Agent runner listening for Bluesky documents at %s", config.zmq_addr)

    try:
        dispatcher.start()
    except KeyboardInterrupt:
        LOGGER.info("Stopping agent runner")


if __name__ == "__main__":
    main()
