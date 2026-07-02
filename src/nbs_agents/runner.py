import logging

from bluesky_queueserver_api.zmq import REManagerAPI
from bluesky.callbacks.zmq import RemoteDispatcher
from .catalog import ZMQCatalog
from .agents.resubmit import submit_nbs_count_on_stop

LOGGER = logging.getLogger(__name__)
ZMQ_ADDRESS = "localhost:5578"


def main() -> None:
    """
    Start the ZMQ listener and agentic QueueServer loop.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    manager = REManagerAPI()
    dispatcher = RemoteDispatcher(ZMQ_ADDRESS)
    catalog = ZMQCatalog(dispatcher)
    catalog.subscribe_stop(submit_nbs_count_on_stop(manager))
    LOGGER.info("Listening for Bluesky documents at %s", ZMQ_ADDRESS)

    try:
        dispatcher.start()
    except KeyboardInterrupt:
        LOGGER.info("Stopping agentic loop")


if __name__ == "__main__":
    main()