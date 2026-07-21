import logging

from bluesky.callbacks.zmq import RemoteDispatcher
from bluesky_queueserver_api.zmq import REManagerAPI

from .catalog import ZMQCatalog
from .supervisor import AgentSupervisor

LOGGER = logging.getLogger(__name__)
ZMQ_ADDRESS = "localhost:5578"


def main() -> None:
    """
    Start the persistent agent runner.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    manager = REManagerAPI()
    dispatcher = RemoteDispatcher(ZMQ_ADDRESS)
    catalog = ZMQCatalog(dispatcher)
    AgentSupervisor(manager, catalog)
    LOGGER.info("Agent runner listening for Bluesky documents at %s", ZMQ_ADDRESS)

    try:
        dispatcher.start()
    except KeyboardInterrupt:
        LOGGER.info("Stopping agent runner")


if __name__ == "__main__":
    main()
