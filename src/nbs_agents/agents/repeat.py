import logging
from typing import Any

from bluesky_queueserver_api import BPlan

from ..catalog import ZMQRun
from ..constants import AGENT_PARAMETERS_KEY
from .base import BaseAgent

LOGGER = logging.getLogger(__name__)

DEFAULT_REPEAT_COUNT = 3
DEFAULT_REPEAT_PLAN = "count"
DEFAULT_REPEAT_ARGS: list[Any] = [[]]
DEFAULT_REPEAT_KWARGS: dict[str, Any] = {"num": 1}


class RepeatAgent(BaseAgent):
    """
    Agent that repeats a plan a fixed number of times.

    The first prototype uses hardcoded defaults unless parameters are supplied
    through ``open_agent``.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        params = self.parameters or {}
        self.repeat_count = int(params.get("repeat_count", DEFAULT_REPEAT_COUNT))
        self.plan_name = params.get("plan_name", DEFAULT_REPEAT_PLAN)
        self.plan_args = params.get("plan_args", DEFAULT_REPEAT_ARGS)
        self.plan_kwargs = dict(params.get("plan_kwargs", DEFAULT_REPEAT_KWARGS))
        self._completed = 0

    def start(self) -> None:
        """
        Validate the sentinel pair and insert the first repeated plan.
        """
        if self.ensure_sentinel_pair() is None:
            self.stop("close_agent sentinel missing at start")
            return
        self._insert_next_plan()

    def on_run_stop(self, run: ZMQRun) -> None:
        """
        Count completed agent plans and insert the next repeat or finalize.

        Parameters
        ----------
        run : ZMQRun
            Finished run from the catalog.
        """
        if not self.active or not self.run_belongs_to_agent(run):
            return

        if not run.succeeded:
            self.stop(f"repeat plan failed with status {run.stop.get('exit_status')}")
            return

        self._completed += 1
        LOGGER.info(
            "Repeat agent %s completed %s/%s",
            self.agent_uid,
            self._completed,
            self.repeat_count,
        )

        if self._completed >= self.repeat_count:
            self.finalize()
            return

        self._insert_next_plan()

    def _insert_next_plan(self) -> None:
        """
        Insert the next repeated plan before the queue stop sentinel.
        """
        if self.ensure_sentinel_pair() is None:
            self.stop("close_agent sentinel missing during repeat")
            return

        item = BPlan(self.plan_name, *self.plan_args, **self.plan_kwargs)
        self.insert_plan(item)
