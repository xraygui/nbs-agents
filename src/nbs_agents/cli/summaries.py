"""
Small per-plan-type reducers that turn a run into a compact summary.

Reducers take plain numpy arrays so they can run on Tiled data or on
documents buffered from the ZMQ stream.

A beamline package adds to every summary through the ``nbs_agents.reducers``
entry point group: each entry is a callable ``reducer(start, data, ctx,
summary)`` returning a dict of extra values (or None), applied in name order
after the built-in reducer.
"""

from functools import lru_cache
from typing import Any, Callable, Optional

import numpy as np

REDUCERS_ENTRY_POINT = "nbs_agents.reducers"


class SummaryContext:
    """
    Settings a reducer may need.

    Parameters
    ----------
    signal : str
        Default detector key.
    norm : str
        Default normalization key.
    xas_plans : dict, optional
        The beamline ``XAS_PLANS`` dict.
    reference : str
        Energy-reference detector key, for reducer plugins. Empty to skip.
    """

    def __init__(self, signal: str = "", norm: str = "", xas_plans: Optional[dict] = None,
                 reference: str = ""):
        self.signal = signal
        self.norm = norm
        self.xas_plans = xas_plans or {}
        self.reference = reference


def _round(value: Any, digits: int = 4) -> Any:
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return value
    if not np.isfinite(value):
        return None
    if value == 0:
        return 0.0
    return float(f"{value:.{digits}g}")


def dimension_keys(start: dict[str, Any]) -> list[str]:
    """
    Independent-axis keys of a run, from the start document hints.

    Parameters
    ----------
    start : dict
        Start document.

    Returns
    -------
    list of str
        Data keys used as scan dimensions.
    """
    keys = []
    for fields, stream in start.get("hints", {}).get("dimensions", []):
        if stream == "primary" and fields != ["time"]:
            keys.extend(fields)
    if not keys:
        keys = list(start.get("motors", []))
    return keys


def generic_summary(start: dict[str, Any], data: dict[str, np.ndarray], ctx: SummaryContext) -> dict[str, Any]:
    """
    Point count, motor ranges and detector statistics.

    Parameters
    ----------
    start : dict
        Start document.
    data : dict
        Primary-stream arrays keyed by data key.
    ctx : SummaryContext
        Reducer settings.

    Returns
    -------
    dict
        Summary values.
    """
    npts = max((len(v) for v in data.values()), default=0)
    out: dict[str, Any] = {"kind": "generic", "points": npts}
    motors = {}
    for key in dimension_keys(start):
        values = np.asarray(data.get(key, []), dtype=float)
        if values.size:
            motors[key] = [_round(np.nanmin(values)), _round(np.nanmax(values))]
    if motors:
        out["motors"] = motors
    detectors = {}
    for key in start.get("detectors", []):
        values = data.get(key)
        if values is None:
            continue
        values = np.asarray(values)
        if values.ndim != 1 or not np.issubdtype(values.dtype, np.number) or not values.size:
            continue
        finite = values[np.isfinite(values)]
        if not finite.size:
            continue
        detectors[key] = {
            "min": _round(finite.min()),
            "max": _round(finite.max()),
            "mean": _round(finite.mean()),
        }
        if finite.size < values.size:
            detectors[key]["non_finite"] = int(values.size - finite.size)
    if detectors:
        out["detectors"] = detectors
    return out


def noise_estimate(y: np.ndarray) -> Optional[float]:
    """
    Point-to-point noise of a smooth curve from its second differences.

    Parameters
    ----------
    y : numpy.ndarray
        Signal values in scan order.

    Returns
    -------
    float or None
        Robust standard-deviation estimate, or ``None`` for short curves.
    """
    if y.size < 5:
        return None
    d2 = np.diff(y, 2)
    return float(np.median(np.abs(d2 - np.median(d2))) / 0.6745 / np.sqrt(6))


def xas_summary(start: dict[str, Any], data: dict[str, np.ndarray], ctx: SummaryContext) -> dict[str, Any]:
    """
    Edge jump, noise and edge position for an energy scan.

    The signal is ``ctx.signal`` divided by ``ctx.norm`` when both exist.
    Pre- and post-edge levels are the means of the first and last 10% of the
    points (at least three each).

    Parameters
    ----------
    start : dict
        Start document.
    data : dict
        Primary-stream arrays keyed by data key.
    ctx : SummaryContext
        Reducer settings.

    Returns
    -------
    dict
        Summary values.
    """
    out = generic_summary(start, data, ctx)
    out["kind"] = "xas"
    out.pop("motors", None)
    for key in ("edge", "element"):
        if start.get(key):
            out[key] = start[key]
    dims = dimension_keys(start)
    if not dims or dims[0] not in data or ctx.signal not in data:
        out["warning"] = "missing energy or signal data"
        return out
    x = np.asarray(data[dims[0]], dtype=float)
    y = np.asarray(data[ctx.signal], dtype=float)
    signal_name = ctx.signal
    if ctx.norm in data:
        norm = np.asarray(data[ctx.norm], dtype=float)
        with np.errstate(divide="ignore", invalid="ignore"):
            y = y / norm
        signal_name = f"{ctx.signal}/{ctx.norm}"
    good = np.isfinite(x) & np.isfinite(y)
    x, y = x[good], y[good]
    out["signal"] = signal_name
    out["energy_range"] = [_round(x.min()), _round(x.max())] if x.size else None
    if x.size < 6:
        out["warning"] = "too few points for edge analysis"
        return out
    order = np.argsort(x, kind="stable")
    xs, ys = x[order], y[order]
    n_edge = max(3, xs.size // 10)
    pre = float(ys[:n_edge].mean())
    post = float(ys[-n_edge:].mean())
    jump = post - pre
    noise = noise_estimate(y)
    dydx = np.gradient(ys, xs)
    peak = int(np.argmax(ys))
    out.update(
        {
            "pre_edge": _round(pre),
            "post_edge": _round(post),
            "edge_jump": _round(jump),
            "noise": _round(noise),
            "jump_to_noise": _round(abs(jump) / noise, 3) if noise else None,
            "e0_max_slope": _round(xs[int(np.argmax(dydx))], 6),
            "peak": {"energy": _round(xs[peak], 6), "value": _round(ys[peak])},
        }
    )
    return out


def is_xas(start: dict[str, Any], ctx: SummaryContext) -> bool:
    """
    Whether a run looks like an energy scan.

    Parameters
    ----------
    start : dict
        Start document.
    ctx : SummaryContext
        Reducer settings.

    Returns
    -------
    bool
        True for XAS plans and scans over an energy axis.
    """
    if start.get("scantype") == "xas" or start.get("plan_name") in ctx.xas_plans:
        return True
    return any("energy" in key for key in dimension_keys(start))


def summarize(start: dict[str, Any], data: dict[str, np.ndarray], ctx: SummaryContext) -> dict[str, Any]:
    """
    Pick a reducer for a run and summarize it.

    Parameters
    ----------
    start : dict
        Start document.
    data : dict
        Primary-stream arrays keyed by data key.
    ctx : SummaryContext
        Reducer settings.

    Returns
    -------
    dict
        Summary values.
    """
    if not data:
        return {"kind": "empty", "points": 0}
    if is_xas(start, ctx):
        out = xas_summary(start, data, ctx)
    else:
        out = generic_summary(start, data, ctx)
    for reducer in plugin_reducers():
        extra = reducer(start, data, ctx, out)
        if extra:
            out.update(extra)
    return out


@lru_cache(maxsize=1)
def plugin_reducers() -> tuple[Callable, ...]:
    """Reducers from installed beamline packages (``nbs_agents.reducers``), in name order."""
    from importlib.metadata import entry_points

    return tuple(entry.load() for entry in sorted(entry_points(group=REDUCERS_ENTRY_POINT), key=lambda e: e.name))
