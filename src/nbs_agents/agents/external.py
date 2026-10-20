import logging

from .base import BaseAgent

LOGGER = logging.getLogger(__name__)


class ExternalAgent(BaseAgent):
    """
    Passive agent for a driver outside the supervisor.

    It holds the ``queue_stop`` / ``close_agent`` block open while something
    else (Claude through nbs-cli, a script, or a person) decides what to run.
    The supervisor records the session and its runs in the agent log; this
    class only checks the sentinels at start.
    """

    def start(self) -> None:
        """
        Check that the sentinel pair is in place.
        """
        if self.ensure_sentinel_pair() is None:
            self.log("error", "close_agent sentinel missing at start")
            self.stop("close_agent sentinel missing at start")
