"""
Facts about a beamline beyond what the QueueServer, the document stream and
Tiled give: whether beam reaches the endstation, what each device is, the
loaded samples, plan time estimates.

Every beamline keeps these its own way (nbs-bl keeps them in status dicts in
a beamline Redis), so nbs-agents asks a provider. A beamline package
provides one through the ``nbs_agents.beamline_info`` entry point group: each
entry is a callable taking the nbs-cli ``Beamline`` (for its settings and
QueueServer client) and returning a ``BeamlineInfo``. The first entry in name
order is used. Without one, nbs-cli uses ``BeamlineInfo`` itself, which knows
none of them, and its verbs say so.
"""

from typing import Any, Callable, Optional

BEAMLINE_INFO_ENTRY_POINT = "nbs_agents.beamline_info"

# What a provider can say, as listed in ``nbs-cli state`` under ``capabilities``
BEAM_MAP = "beam_map"
DEVICES = "devices"
SAMPLES = "samples"
ESTIMATES = "estimates"
XAS_PLANS = "xas_plans"


class BeamlineInfo:
    """
    A beamline that knows nothing beyond the QueueServer, the document stream
    and Tiled. Providers subclass it and override what they know, listing it
    in ``capabilities``.

    Parameters
    ----------
    beamline : nbs_agents.cli.core.Beamline, optional
        The client the provider serves, for its ``config`` and ``manager``.
    """

    name = "none"
    capabilities: frozenset = frozenset()

    def __init__(self, beamline: Any = None) -> None:
        self.beamline = beamline

    def beam_map(self) -> Optional[dict[str, Any]]:
        """
        The beam map: how beam reaches the endstation.

        Returns
        -------
        dict or None
            ``endstation``, ``beam_expected``, ``reason``, ``updated`` and
            ``nodes``: name to ``{"upstream", "children", "desc", "devices",
            "blocking", "path_clear", "beam_expected", "reason"}``. None when
            the beamline has none.
        """
        return None

    def device_info(self, names: Optional[list[str]] = None) -> dict[str, Any]:
        """
        Static descriptions of devices: class, beam map ``node``, ``groups``,
        ``roles``, ``desc`` and so on. No live values.

        Parameters
        ----------
        names : list of str, optional
            Devices wanted; all known ones if omitted.

        Returns
        -------
        dict
            Name to description; empty when the beamline has none.
        """
        return {}

    def samples(self) -> Optional[dict[str, Any]]:
        """
        Loaded samples.

        Returns
        -------
        dict or None
            Sample id to its fields (``name``, ``description``, ``position``
            and so on); None when the beamline does not track samples.
        """
        return None

    def estimator(self) -> Optional[Callable[[dict[str, Any]], Optional[float]]]:
        """
        A function estimating a queue item's duration in seconds.

        Returns
        -------
        callable or None
            ``estimate(item) -> seconds or None``, on one snapshot of the
            rules; None when the beamline has no estimates.
        """
        return None

    def plan_extras(self, name: str) -> dict[str, Any]:
        """
        Beamline facts to add to ``nbs-cli plans NAME``, such as a time rule.

        Returns
        -------
        dict
            Extra fields; empty for none.
        """
        return {}

    def xas_plans(self) -> dict[str, Any]:
        """
        The beamline's XAS plans: plan name to ``{"name", "edge", "element"}``.

        Returns
        -------
        dict
            Empty for none.
        """
        return {}

    def add_warning(self, plan: str, kwargs: dict[str, Any], parameters: set[str],
                    block: list[dict[str, Any]]) -> Optional[str]:
        """
        A warning for ``nbs-cli add``, such as a sample argument that only
        labels the run.

        Parameters
        ----------
        plan : str
            Plan name.
        kwargs : dict
            Its keyword arguments.
        parameters : set of str
            The plan's parameter names.
        block : list of dict
            Items already pending in the session's block.

        Returns
        -------
        str or None
            The warning, or None.
        """
        return None

    def close(self) -> None:
        """Close any connections."""


def load_beamline_info(beamline: Any = None) -> BeamlineInfo:
    """
    The installed provider for ``beamline``, or a ``BeamlineInfo`` that knows nothing.

    Parameters
    ----------
    beamline : nbs_agents.cli.core.Beamline, optional
        The client the provider serves.

    Returns
    -------
    BeamlineInfo
        The provider.
    """
    from importlib.metadata import entry_points

    found = sorted(entry_points(group=BEAMLINE_INFO_ENTRY_POINT), key=lambda e: e.name)
    if not found:
        return BeamlineInfo(beamline)
    return found[0].load()(beamline)
