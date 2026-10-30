"""
Agents the runner starts from a goal, such as Claude.

A package that can launch an agent from a goal declares a function under the
``nbs_agents.launchers`` entry-point group, named after the agent::

    [project.entry-points."nbs_agents.launchers"]
    claude = "some_package.launcher:claude_launch_parameters"

The function is called as ``func(goal, budget)`` and returns the
``open_agent`` parameters for that agent (it raises ``ValueError`` on a bad
goal or budget). The Qt "New agent…" dialog and agent block offer every
installed launcher; with none installed they offer nothing.
"""

import logging
from functools import lru_cache
from typing import Callable, Optional

LOGGER = logging.getLogger(__name__)

LAUNCHERS_ENTRY_POINT = "nbs_agents.launchers"


@lru_cache(maxsize=1)
def _launchers() -> dict[str, Callable]:
    from importlib.metadata import entry_points

    found: dict[str, Callable] = {}
    for entry in entry_points(group=LAUNCHERS_ENTRY_POINT):
        try:
            found[entry.name] = entry.load()
        except Exception:
            LOGGER.exception("Could not load launcher %s from %s", entry.name, entry.value)
    return found


def launcher_names() -> list[str]:
    """Agent names that can be launched from a goal, in a stable order."""
    return sorted(_launchers())


def launch_parameters(name: str, goal: str, budget: Optional[str] = None) -> dict:
    """
    ``open_agent`` parameters to launch agent ``name`` with ``goal``.

    Raises
    ------
    ValueError
        If ``name`` has no launcher, or the goal or budget is not valid.
    """
    func = _launchers().get(name)
    if func is None:
        raise ValueError(f"no launcher installed for agent {name!r}")
    return func(goal, budget)
