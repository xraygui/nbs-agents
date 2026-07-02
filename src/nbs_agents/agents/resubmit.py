from ..catalog import ZMQRun
import asyncio
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from itertools import count
from typing import Any, Callable, Optional

import numpy as np
from bluesky_queueserver_api import BPlan
from bluesky_queueserver_api.zmq import REManagerAPI
from bluesky.callbacks.zmq import RemoteDispatcher
from IPython.terminal.embed import InteractiveShellEmbed

LOGGER = logging.getLogger(__name__)

_IPYTHON_EXECUTOR = ThreadPoolExecutor(max_workers=1)


def _run_embedded_ipython(header: str, namespace: dict[str, Any]) -> None:
    """
    Run IPython with an explicit namespace.

    Parameters
    ----------
    header : str
        Header shown when IPython starts.
    namespace : dict
        Variables exposed in the embedded IPython session.
    """
    shell = InteractiveShellEmbed()
    shell(header=header, local_ns=namespace)


async def embed_ipython(header: str, namespace: dict[str, Any]) -> None:
    """
    Open IPython in a worker thread.

    Parameters
    ----------
    header : str
        Header shown when IPython starts.
    namespace : dict
        Variables exposed in the embedded IPython session.
    """
    await asyncio.to_thread(_run_embedded_ipython, header, namespace)




def embed_ipython_for_run(run: ZMQRun, manager: REManagerAPI) -> None:
    """
    Open an embedded IPython session for a finished run.

    Parameters
    ----------
    run : ZMQRun
        Finished run that triggered the callback.
    manager : REManagerAPI
        QueueServer API client.
    """
    header = f"Finished run {run.uid}. Exit IPython to submit nbs_count."
    namespace = {
        "run": run,
        "manager": manager,
        "BPlan": BPlan,
        "np": np,
    }
    future = _IPYTHON_EXECUTOR.submit(
        lambda: asyncio.run(embed_ipython(header, namespace))
    )
    future.result()


def submit_nbs_count_on_stop(manager: REManagerAPI) -> Callable[[ZMQRun], None]:
    """
    Build a callback that submits ``nbs_count`` after each completed run.

    Parameters
    ----------
    manager : REManagerAPI
        QueueServer API client.

    Returns
    -------
    callable
        Callback accepting a finished ``ZMQRun``.
    """

    def callback(run: ZMQRun) -> None:
        """
        Submit the next prototype plan.

        Parameters
        ----------
        run : ZMQRun
            Finished run that triggered the submission.
        """
        LOGGER.info("Opening IPython console after run %s", run.uid)
        embed_ipython_for_run(run, manager)
        LOGGER.info("Submitting nbs_count after run %s", run.uid)
        manager.item_add(BPlan("nbs_count", num=5))

    return callback
