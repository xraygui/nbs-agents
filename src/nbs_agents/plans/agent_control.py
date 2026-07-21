import uuid
from typing import Any, Optional

from bluesky import plan_stubs as bps

from ..constants import (
    AGENT_NAME_KEY,
    AGENT_PARAMETERS_KEY,
    AGENT_UID_KEY,
    AGENT_CLOSE_KEY,
)


def _maybe_register_plan(plan_func):
    """
    Register a plan with nbs-bl when available.

    Parameters
    ----------
    plan_func : callable
        Bluesky plan generator function.

    Returns
    -------
    callable
        The plan function, optionally registered.
    """
    try:
        from nbs_bl.help import add_to_plan_list
    except ImportError:
        return plan_func
    return add_to_plan_list(plan_func)


@_maybe_register_plan
def open_agent(
    agent_name: str,
    agent_parameters: Optional[dict[str, Any]] = None,
    *,
    md: Optional[dict[str, Any]] = None,
):
    """
    Open an agent session and emit identifying run metadata.

    Parameters
    ----------
    agent_name : str
        Registered agent name for the runner to instantiate.
    agent_parameters : dict, optional
        Agent-specific configuration passed to the runner.
    md : dict, optional
        Additional run metadata merged into the start document.
    """
    agent_uid = str(uuid.uuid4())
    run_md = {
        AGENT_UID_KEY: agent_uid,
        AGENT_NAME_KEY: agent_name,
        AGENT_PARAMETERS_KEY: agent_parameters or {},
    }
    if md:
        run_md.update(md)
    yield from bps.open_run(md=run_md)
    yield from bps.close_run()


@_maybe_register_plan
def close_agent(
    agent_uid: Optional[str] = None,
    *,
    md: Optional[dict[str, Any]] = None,
):
    """
    Close an agent session sentinel plan.

    Parameters
    ----------
    agent_uid : str, optional
        UID linking this sentinel to an ``open_agent`` run.
    md : dict, optional
        Additional run metadata merged into the start document.
    """
    run_md = {
        AGENT_CLOSE_KEY: True,
    }
    if agent_uid is not None:
        run_md[AGENT_UID_KEY] = agent_uid
    if md:
        run_md.update(md)
    yield from bps.open_run(md=run_md)
    yield from bps.close_run()
