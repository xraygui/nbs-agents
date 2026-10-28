import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from bluesky_queueserver_api import BInst, BPlan, BItem
from bluesky_queueserver_api.zmq import REManagerAPI

from .constants import (
    AGENT_NAME_KEY,
    AGENT_PARAMETERS_KEY,
    AGENT_SEQUENCE_KEY,
    AGENT_UID_KEY,
    CLOSE_AGENT_PLAN,
    OPEN_AGENT_PLAN,
    QUEUE_STOP_INSTRUCTION,
)

LOGGER = logging.getLogger(__name__)

def make_queue_stop() -> BInst:
    """
    Build a QueueServer stop instruction item.

    Returns
    -------
    BInst
        ``queue_stop`` instruction for the plan queue.
    """
    return BInst(QUEUE_STOP_INSTRUCTION)


def make_open_agent(
    agent_name: str,
    agent_parameters: Optional[dict[str, Any]] = None,
) -> BPlan:
    """
    Build an ``open_agent`` queue item.

    Parameters
    ----------
    agent_name : str
        Registered agent name.
    agent_parameters : dict, optional
        Agent-specific parameters.

    Returns
    -------
    BPlan
        Queue item for ``open_agent``.
    """
    return BPlan(OPEN_AGENT_PLAN, agent_name, agent_parameters or {})


def make_close_agent(agent_uid: Optional[str] = None) -> BPlan:
    """
    Build a ``close_agent`` queue item.

    Parameters
    ----------
    agent_uid : str, optional
        Agent UID assigned by a prior ``open_agent`` run.

    Returns
    -------
    BPlan
        Queue item for ``close_agent``.
    """
    if agent_uid is None:
        return BPlan(CLOSE_AGENT_PLAN)
    return BPlan(CLOSE_AGENT_PLAN, agent_uid)


def make_open_agent_block(
    agent_name: str,
    agent_parameters: Optional[dict[str, Any]] = None,
    agent_uid: Optional[str] = None,
) -> list[BItem]:
    """
    Build the standard agent control block for queue insertion.

    Parameters
    ----------
    agent_name : str
        Registered agent name.
    agent_parameters : dict, optional
        Agent-specific parameters.
    agent_uid : str, optional
        Known agent UID for the close sentinel.

    Returns
    -------
    list of BItem
        ``open_agent``, ``queue_stop``, and ``close_agent`` items in order.
    """
    return [
        make_open_agent(agent_name, agent_parameters),
        make_queue_stop(),
        make_close_agent(agent_uid),
    ]


def submit_agent_block(
    manager: REManagerAPI,
    agent_name: str,
    agent_parameters: Optional[dict[str, Any]] = None,
    *,
    start: bool = True,
    **add_kwargs: Any,
) -> dict[str, Any]:
    """
    Append an agent control block to the queue and start it if it is next.

    Used by both ``nbs-cli open`` and the GUI's "New agent" button.

    Parameters
    ----------
    manager : REManagerAPI
        QueueServer API client.
    agent_name : str
        Registered agent name.
    agent_parameters : dict, optional
        Agent-specific parameters.
    start : bool, optional
        Start the queue if it is idle and the block is at its head.
    **add_kwargs
        Passed to ``item_add_batch``, for example ``user`` and ``user_group``.

    Returns
    -------
    dict
        ``items`` (the added items as the server returned them), ``started``,
        and ``position`` (index of ``open_agent`` in the queue, or ``None``).

    Raises
    ------
    RuntimeError
        If the server refuses the block or the queue start.
    """
    block = make_open_agent_block(agent_name, agent_parameters)
    response = manager.item_add_batch(block, **add_kwargs)
    if not response.get("success", False):
        raise RuntimeError(f"item_add_batch failed: {response.get('msg', response)}")
    added = list(response.get("items", []))
    open_item_uid = added[0]["item_uid"]

    items = get_queue_items(manager)
    position = next((i for i, it in enumerate(items) if it.get("item_uid") == open_item_uid), None)
    started = False
    if start and position == 0 and manager.status().get("manager_state") == "idle":
        start_response = manager.queue_start()
        if not start_response.get("success", False):
            raise RuntimeError(f"queue_start failed: {start_response.get('msg', start_response)}")
        started = True
    return {"items": added, "started": started, "position": position}


def get_queue_items(
    manager: REManagerAPI,
    *,
    reload: bool = False,
) -> list[dict[str, Any]]:
    """
    Return the current plan queue items.

    Parameters
    ----------
    manager : REManagerAPI
        QueueServer API client.
    reload : bool, optional
        Whether to reload queue state from the server.

    Returns
    -------
    list of dict
        Queue items as dictionaries.
    """
    response = manager.queue_get(reload=reload)
    if not response.get("success", False):
        raise RuntimeError(response.get("msg", "queue_get failed"))
    return list(response.get("items", []))


@dataclass
class AgentSentinelPair:
    """
    Identifiers for an agent's queue sentinel pair.

    Parameters
    ----------
    queue_stop_item : dict
        ``queue_stop`` instruction item.
    close_agent_item : dict
        ``close_agent`` plan item.
    close_index : int
        Index of ``close_agent`` in the queue.
    """

    queue_stop_item: dict[str, Any]
    close_agent_item: dict[str, Any]
    close_index: int

    @property
    def queue_stop_uid(self) -> str:
        """
        UID of the ``queue_stop`` instruction.

        Returns
        -------
        str
            Queue item UID.
        """
        return self.queue_stop_item["item_uid"]

    @property
    def close_agent_uid(self) -> str:
        """
        UID of the ``close_agent`` plan item.

        Returns
        -------
        str
            Queue item UID.
        """
        return self.close_agent_item["item_uid"]


def _item_agent_uid(item: dict[str, Any]) -> Optional[str]:
    """
    Extract an agent UID from a queue item.

    Parameters
    ----------
    item : dict
        Queue item dictionary.

    Returns
    -------
    str or None
        Agent UID if present.
    """
    if item.get("name") == CLOSE_AGENT_PLAN:
        args = item.get("args", [])
        if args:
            return args[0]
        kwargs = item.get("kwargs", {})
        return kwargs.get(AGENT_UID_KEY)
    meta = item.get("meta", {})
    return meta.get(AGENT_UID_KEY)


def find_close_agent_index(
    items: list[dict[str, Any]],
    agent_uid: Optional[str] = None,
) -> Optional[int]:
    """
    Find a ``close_agent`` item in the queue.

    Parameters
    ----------
    items : list of dict
        Queue items.
    agent_uid : str, optional
        Required agent UID match.

    Returns
    -------
    int or None
        Index of the matching ``close_agent`` item.
    """
    for index, item in enumerate(items):
        if item.get("item_type") != "plan" or item.get("name") != CLOSE_AGENT_PLAN:
            continue
        item_uid_value = _item_agent_uid(item)
        if agent_uid is None or item_uid_value in (None, agent_uid):
            return index
    return None


def find_sentinel_pair(
    items: list[dict[str, Any]],
    agent_uid: Optional[str] = None,
) -> Optional[AgentSentinelPair]:
    """
    Locate a ``queue_stop`` / ``close_agent`` sentinel pair.

    Parameters
    ----------
    items : list of dict
        Queue items.
    agent_uid : str, optional
        Required agent UID for the close sentinel.

    Returns
    -------
    AgentSentinelPair or None
        Sentinel pair if found.
    """
    close_index = find_close_agent_index(items, agent_uid=agent_uid)
    if close_index is None:
        return None

    close_agent_item = items[close_index]
    if close_index == 0:
        return AgentSentinelPair({}, close_agent_item, close_index)

    previous = items[close_index - 1]
    if (
        previous.get("item_type") == "instruction"
        and previous.get("name") == QUEUE_STOP_INSTRUCTION
    ):
        return AgentSentinelPair(previous, close_agent_item, close_index)

    return AgentSentinelPair({}, close_agent_item, close_index)


def ensure_queue_stop_before_close(
    manager: Any,
    agent_uid: Optional[str] = None,
) -> Optional[AgentSentinelPair]:
    """
    Ensure an active ``close_agent`` item has a preceding ``queue_stop``.

    Parameters
    ----------
    manager : REManagerAPI
        QueueServer API client.
    agent_uid : str, optional
        Agent UID used to locate the sentinel pair.

    Returns
    -------
    AgentSentinelPair or None
        Sentinel pair after repair, or ``None`` if ``close_agent`` is missing.
    """
    items = get_queue_items(manager, reload=True)
    pair = find_sentinel_pair(items, agent_uid=agent_uid)
    if pair is None:
        return None

    if pair.queue_stop_item:
        return pair

    stop_item = make_queue_stop()
    response = manager.item_add(stop_item, before_uid=pair.close_agent_uid)
    if not response.get("success", False):
        raise RuntimeError(response.get("msg", "failed to insert queue_stop"))

    items = get_queue_items(manager, reload=True)
    return find_sentinel_pair(items, agent_uid=agent_uid)


def update_close_agent_uid(
    manager: Any,
    close_item_uid: str,
    agent_uid: str,
) -> dict[str, Any]:
    """
    Set the ``agent_uid`` argument on a ``close_agent`` queue item.

    Parameters
    ----------
    manager : REManagerAPI
        QueueServer API client.
    close_item_uid : str
        Queue item UID for the close sentinel.
    agent_uid : str
        Agent UID from the ``open_agent`` run.

    Returns
    -------
    dict
        QueueServer response.
    """
    response = manager.item_get(uid=close_item_uid)
    if not response.get("success", False):
        raise RuntimeError(response.get("msg", "item_get failed"))

    item = response["item"]
    item["args"] = [agent_uid]
    item["kwargs"] = {}
    update_response = manager.item_update(item)
    if not update_response.get("success", False):
        raise RuntimeError(update_response.get("msg", "item_update failed"))
    return update_response


def insert_before_queue_stop(
    manager: Any,
    item: BItem,
    agent_uid: str,
) -> dict[str, Any]:
    """
    Insert a queue item immediately before an agent's ``queue_stop`` sentinel.

    Parameters
    ----------
    manager : REManagerAPI
        QueueServer API client.
    item : BItem
        Queue item to insert.
     agent_uid : str
        Agent UID for sentinel lookup.

    Returns
    -------
    dict
        QueueServer response.
    """
    pair = ensure_queue_stop_before_close(manager, agent_uid=agent_uid)
    if pair is None:
        raise RuntimeError(f"close_agent sentinel not found for agent {agent_uid}")

    response = manager.item_add(item, before_uid=pair.queue_stop_uid)
    if not response.get("success", False):
        raise RuntimeError(response.get("msg", "item_add failed"))

    ensure_queue_stop_before_close(manager, agent_uid=agent_uid)
    return response


def tag_agent_metadata(
    item: BItem,
    agent_uid: str,
    agent_name: str,
    agent_sequence: int,
) -> BItem:
    """
    Attach shared agent metadata to a plan queue item.

    Parameters
    ----------
    item : BItem
        Queue item to tag.
    agent_uid : str
        Shared agent UID.
    agent_name : str
        Agent name.
    agent_sequence : int
        Sequence number for this agent-emitted item.

    Returns
    -------
    BItem
        The tagged queue item.
    """
    if item.item_type != "plan":
        return item
    item.meta = {
        AGENT_UID_KEY: agent_uid,
        AGENT_NAME_KEY: agent_name,
        AGENT_SEQUENCE_KEY: agent_sequence,
    }
    return item


def release_queue_toward_close(manager: REManagerAPI) -> None:
    """
    Clear a pending queue stop and restart execution toward ``close_agent``.

    Parameters
    ----------
    manager : REManagerAPI
        QueueServer API client.
    """
    cancel_response = manager.queue_stop_cancel()
    if not cancel_response.get("success", False):
        raise RuntimeError(cancel_response.get("msg", "queue_stop_cancel failed"))

    try:
        manager.wait_for_idle(timeout=60)
    except manager.WaitTimeoutError:
        LOGGER.warning(
            "Timed out waiting for QueueServer to become idle before queue_start"
        )

    start_response = manager.queue_start()
    if not start_response.get("success", False):
        raise RuntimeError(start_response.get("msg", "queue_start failed"))
