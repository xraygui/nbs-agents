"""Import nbs-agents plans into a beamline startup namespace."""

from .plans.agent_control import close_agent, open_agent

__all__ = ["open_agent", "close_agent"]


def load_plans(namespace: dict) -> None:
    """
    Add agent control plans to a startup namespace.

    Parameters
    ----------
    namespace : dict
        IPython or startup namespace to update.
    """
    namespace["open_agent"] = open_agent
    namespace["close_agent"] = close_agent
