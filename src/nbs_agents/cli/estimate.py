"""
Plan time estimates from the nbs-bl ``PLAN_TIME_DICT``.

These are small copies of the common estimators in
``nbs_bl.plans.time_estimation``, because importing ``nbs_bl.plans`` pulls in
the whole beamline environment.
"""

from numbers import Number
from typing import Any, Optional


def _gscan_points(*args: float) -> list[float]:
    """
    Expand ``(start, step, stop, step, stop, ...)`` into scan points.

    Mirrors ``nbs_bl.plans.scan_base._make_gscan_points``.
    """
    if len(args) == 1:
        return [float(args[0])]
    if (len(args) - 1) % 2 != 0:
        raise TypeError("gscan region needs start, step, stop[, step, stop ...]")
    points: list[float] = []
    for i in range(1, len(args) - 1, 2):
        estart = float(args[i - 1])
        delta = abs(float(args[i]))
        estop = float(args[i + 1])
        if delta == 0:
            raise ValueError("gscan step must be non-zero")
        step = delta if estop > estart else -delta
        if not points or points[-1] != estart:
            points.append(estart)
        next_point = estart + step
        while (step > 0 and next_point < estop - step / 2.0) or (
            step < 0 and next_point > estop - step / 2.0
        ):
            points.append(next_point)
            next_point += step
        points.append(estop)
    return points


def _dwell(plan_args: dict[str, Any], est: dict[str, Any]) -> float:
    dwell = est.get("dwell", "dwell")
    if isinstance(dwell, str):
        value = plan_args.get(dwell)
        if value is None:
            # The plan keeps the detectors' current exposure
            value = est.get("default_dwell", 0)
    else:
        value = dwell
    return float(value or 0)


def _generic(plan_args, est):
    points = est.get("points")
    if isinstance(points, str):
        n = len(plan_args.get("args", [])) if points == "args" else plan_args.get(points, 0)
    else:
        n = points or 0
    return est.get("fixed", 0) + (est.get("overhead", 0) + _dwell(plan_args, est)) * n


def _list_scan(plan_args, est):
    n = len(plan_args.get("args", ["", []])[1])
    return est.get("fixed", 0) + (est.get("overhead", 0) + _dwell(plan_args, est)) * n


def _gscan(plan_args, est):
    region = est.get("region") or plan_args.get("args")
    if region is None:
        return None
    region = list(region)
    if region and not isinstance(region[0], Number):
        region = region[1:]
    n = len(_gscan_points(*region))
    return est.get("fixed", 0) + (est.get("overhead", 0) + _dwell(plan_args, est)) * n


LOCAL_ESTIMATORS = {
    "generic_estimate": _generic,
    "list_scan_estimate": _list_scan,
    "gscan_estimate": _gscan,
}


def estimate_item(item: dict[str, Any], plan_time_dict: dict[str, Any]) -> Optional[float]:
    """
    Estimate the run time of a queue item in seconds.

    Parameters
    ----------
    item : dict
        Queue item with ``name``, ``args`` and ``kwargs``.
    plan_time_dict : dict
        The beamline ``PLAN_TIME_DICT``.

    Returns
    -------
    float or None
        Estimate in seconds, or ``None`` if the plan has no estimator.
    """
    if item.get("item_type", "plan") != "plan":
        return 0.0
    est = plan_time_dict.get(item.get("name"))
    if not est:
        return None
    plan_args = dict(item.get("kwargs", {}))
    if item.get("args"):
        plan_args["args"] = item["args"]
    func = LOCAL_ESTIMATORS.get(est.get("estimator", "generic_estimate"))
    if func is None:
        return None
    try:
        base = func(plan_args, est)
    except Exception:
        return None
    if base is None:
        return None
    repeat = int(plan_args.get("repeat", 1) or 1)
    return float(base * repeat + (est.get("reset", est.get("reset_time", 0)) or 0) * (repeat - 1))
