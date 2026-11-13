"""
Reductions over settled keys: summaries with surprise flags, normalized
spectra and comparisons between runs.
"""

from typing import Any, Optional

import numpy as np

from .config import ReduceConfig
from .keys import NORM_PROBLEMS, signal_check
from .metrics import _round, baseline_metrics, edge_metrics
from .runs import ReduceError, Run

# Fraction of the points in the pre-edge line and the post-edge level of a normalized spectrum
PRE_REGION = 0.2
POST_REGION = 0.1


def spectrum(run: Run, signal: str, norm: Optional[str], x: Optional[str]) -> dict[str, Any]:
    """
    One signal of a run over its scan axis, divided by the normalization.

    Parameters
    ----------
    run : Run
        The run.
    signal : str
        Signal key.
    norm : str or None
        Normalization key, or None to use the raw signal.
    x : str or None
        Axis key; None for the point number.

    Returns
    -------
    dict
        ``x``, ``y`` (finite points only, in scan order), ``name``
        (``sc/i0``), ``x_name`` and ``dropped`` (non-finite points left out).
    """
    y = run.array(signal)
    name = signal
    if norm:
        with np.errstate(divide="ignore", invalid="ignore"):
            y = y / run.array(norm)
        name = f"{signal}/{norm}"
    xv = run.array(x) if x else np.arange(y.size, dtype=float)
    n = min(xv.size, y.size)
    xv, y = xv[:n], y[:n]
    good = np.isfinite(xv) & np.isfinite(y)
    return {"x": xv[good], "y": y[good], "name": name, "x_name": x or "point", "dropped": int(n - good.sum())}


def normalize(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """
    Edge-step normalization: subtract the pre-edge line, divide by the post-edge level.

    Parameters
    ----------
    x, y : numpy.ndarray
        Finite points, sorted by ``x``.

    Returns
    -------
    numpy.ndarray
        Normalized values: about 0 before the edge and 1 after it.

    Raises
    ------
    ReduceError
        If there are too few points or no edge step.
    """
    if x.size < 6:
        raise ReduceError("too few points to normalize")
    n_pre = max(3, int(round(x.size * PRE_REGION)))
    n_post = max(3, int(round(x.size * POST_REGION)))
    fit = np.polyfit(x[:n_pre], y[:n_pre], 1) if np.ptp(x[:n_pre]) else np.array([0.0, y[:n_pre].mean()])
    flat = y - np.polyval(fit, x)
    step = float(flat[-n_post:].mean())
    if not np.isfinite(step) or step == 0:
        raise ReduceError("no edge step to normalize by")
    return flat / step


def _sorted(spec: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    order = np.argsort(spec["x"], kind="stable")
    return spec["x"][order], spec["y"][order]


def summary(run: Run, keys: dict[str, Any], config: ReduceConfig) -> dict[str, Any]:
    """
    Edge and baseline metrics of each signal, with surprise flags.

    Parameters
    ----------
    run : Run
        The run.
    keys : dict
        Settled keys (``resolve_keys``).
    config : ReduceConfig
        Thresholds.

    Returns
    -------
    dict
        ``signals``: per signal, the edge metrics (``edge_metrics``), the
        baseline metrics (``baseline_metrics``) and ``flags``; ``norm``: the
        normalization's check and ``drop`` (minimum over median); ``flags``:
        every flag, as ``"sc/i0: inverted_edge"``. A flag marks a run as
        surprising; it does not say why.
    """
    out: dict[str, Any] = {"signals": {}, "flags": []}
    norm = keys["norm"]
    if norm:
        values = run.array(norm)
        check = signal_check(values, config)
        finite = values[np.isfinite(values)]
        entry: dict[str, Any] = {"key": norm, "check": check}
        median = float(np.median(finite)) if finite.size else 0.0
        if median > 0:
            entry["drop"] = _round(float(finite.min()) / median, 3)
            if finite.min() < config.norm_drop_fraction * median:
                out["flags"].append(f"{norm}: norm_drop")
        out["flags"] += [f"{norm}: {p}" for p in check.get("problems") or [] if p in NORM_PROBLEMS]
        if check.get("non_finite_fraction"):
            out["flags"].append(f"{norm}: non_finite")
        out["norm"] = entry
    for signal in keys["signal"]:
        check = signal_check(run.array(signal), config)
        spec = spectrum(run, signal, norm, keys["x"])
        name = spec["name"]
        entry = {"check": check, "points": int(spec["x"].size)}
        flags = [f"{signal}: {p}" for p in check.get("problems") or []]
        if spec["dropped"]:
            entry["non_finite"] = spec["dropped"]
            flags.append(f"{name}: non_finite")
        if spec["x"].size >= 6 and np.ptp(spec["x"]) > 0:
            entry.update(edge_metrics(spec["x"], spec["y"]))
            entry.update(baseline_metrics(spec["x"], spec["y"], PRE_REGION))
            flags += [f"{name}: {f}" for f in surprise_flags(entry, config)]
        else:
            entry["warning"] = "too few points, or no scan axis, for edge metrics"
        entry["flags"] = flags
        out["signals"][name] = entry
        out["flags"] += [f for f in flags if f not in out["flags"]]
    return out


def surprise_flags(metrics: dict[str, Any], config: ReduceConfig) -> list[str]:
    """
    Names of the deterministic surprises in one signal's metrics.

    ``inverted_edge`` (pre-edge above post-edge), ``low_jump_to_noise``,
    ``steep_pre_edge``, ``steep_post_edge`` and ``curved_pre_edge``, against
    the ``[reduce]`` thresholds.
    """
    flags = []
    if metrics.get("inverted"):
        flags.append("inverted_edge")
    jtn = metrics.get("jump_to_noise")
    if jtn is not None and jtn < config.min_jump_to_noise:
        flags.append("low_jump_to_noise")
    for key, limit, flag in (("pre_edge_slope_rel", config.max_pre_edge_slope, "steep_pre_edge"),
                             ("post_edge_slope_rel", config.max_post_edge_slope, "steep_post_edge"),
                             ("pre_edge_curvature_rel", config.max_pre_edge_curvature, "curved_pre_edge")):
        value = metrics.get(key)
        if value is not None and abs(value) > limit:
            flags.append(flag)
    return flags


def table(run: Run, keys: dict[str, Any], normalized: bool = True) -> dict[str, Any]:
    """
    The reduced spectra of a run as columns, sorted by the scan axis.

    Parameters
    ----------
    run : Run
        The run.
    keys : dict
        Settled keys.
    normalized : bool, optional
        Add an edge-step normalized column per signal (``sc/i0 norm``).

    Returns
    -------
    dict
        ``columns``: name to list of values, all the same length (points
        where any column is non-finite are left out); ``warnings``.
    """
    specs = [spectrum(run, s, keys["norm"], keys["x"]) for s in keys["signal"]]
    x_name = specs[0]["x_name"]
    # Points finite in every signal, so the columns line up
    y_all = [run.array(s) for s in keys["signal"]]
    if keys["norm"]:
        with np.errstate(divide="ignore", invalid="ignore"):
            y_all = [y / run.array(keys["norm"]) for y in y_all]
    x = run.array(keys["x"]) if keys["x"] else np.arange(y_all[0].size, dtype=float)
    n = min([x.size] + [y.size for y in y_all])
    good = np.isfinite(x[:n]) & np.all([np.isfinite(y[:n]) for y in y_all], axis=0)
    order = np.argsort(x[:n][good], kind="stable")
    columns = {x_name: x[:n][good][order]}
    warnings = []
    for spec, y in zip(specs, y_all):
        ys = y[:n][good][order]
        columns[spec["name"]] = ys
        if normalized:
            try:
                columns[f"{spec['name']} norm"] = normalize(columns[x_name], ys)
            except ReduceError as exc:
                warnings.append(f"{spec['name']}: {exc}")
    return {"columns": {k: [_round(v, 6) for v in vals] for k, vals in columns.items()}, "warnings": warnings}


def compare(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """
    Compare two spectra after edge-step normalization.

    Parameters
    ----------
    a, b : dict
        ``spectrum`` outputs: the run, and what it is compared to.

    Returns
    -------
    dict
        ``shift`` (e0 of ``a`` minus e0 of ``b``, by maximum slope),
        ``scale`` (least-squares factor on ``b``'s normalized spectrum after
        the shift), ``residual_rms`` (of the normalized difference, in edge
        jumps), ``jump_ratio`` (raw edge jumps, ``a`` over ``b``) and
        ``overlap`` (axis range and points used).
    """
    xa, ya = _sorted(a)
    xb, yb = _sorted(b)
    na, nb = normalize(xa, ya), normalize(xb, yb)
    ea, eb = edge_metrics(xa, ya), edge_metrics(xb, yb)
    shift = (ea["e0_max_slope"] or 0) - (eb["e0_max_slope"] or 0)
    xb_shifted = xb + shift
    lo, hi = max(xa[0], xb_shifted[0]), min(xa[-1], xb_shifted[-1])
    inside = (xa >= lo) & (xa <= hi)
    if inside.sum() < 6:
        raise ReduceError("the two spectra overlap at fewer than six points")
    ref = np.interp(xa[inside], xb_shifted, nb)
    target = na[inside]
    denom = float(np.dot(ref, ref))
    scale = float(np.dot(target, ref) / denom) if denom else None
    residual = target - (scale or 1.0) * ref
    return {
        "shift": _round(shift, 6),
        "scale": _round(scale),
        "residual_rms": _round(float(np.sqrt(np.mean(residual**2)))),
        "residual_max": _round(float(np.max(np.abs(residual)))),
        "jump_ratio": _round(ea["edge_jump"] / eb["edge_jump"]) if eb["edge_jump"] else None,
        "overlap": {"range": [_round(lo, 6), _round(hi, 6)], "points": int(inside.sum())},
    }
