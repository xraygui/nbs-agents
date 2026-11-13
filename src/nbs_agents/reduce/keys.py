"""
Which data keys to reduce, and what each key of a run is.

nbs-reduce never guesses a key. Signal and normalization keys are settled,
each on its own, by the first of:

1. the caller (``--signal``, ``--norm``);
2. a ``[reduce.defaults.<plan or scantype>]`` rule whose ``when`` matches the
   run's baseline at its start;
3. the roles the run recorded: nbs-bl writes the active detector set's
   ``primary``, ``normalization`` and ``reference`` detectors into the start
   document's ``plot_hints``;
4. the beamline-wide ``signal`` / ``norm`` of ``[nbs_cli]``;
5. the run's hints: the normalization is the one hinted key whose device has
   the ``intensity_detector`` role, and the signal the one hinted detector
   left once that, the energy reference and the axes are set aside.

When none of these settles a key, resolution stops with ``Ambiguous`` and the
candidates, and the caller (a person, or an agent reading ``keys``) decides.
"""

from typing import Any, Optional

import numpy as np

from .config import ReduceConfig
from .metrics import _round, noise_estimate
from .runs import ReduceError, Run

NORM_ROLE = "intensity_detector"
# A steady normalization is flat or only noise, as it should be; these are its real problems
NORM_PROBLEMS = ("all_non_finite", "zero", "clipped")


class Ambiguous(ReduceError):
    """
    No rule settles a key.

    Attributes
    ----------
    candidates : dict
        ``{"signal": [...], "norm": [...]}``: the keys to choose from.
    reasons : dict
        Why each key was or was not settled.
    """

    def __init__(self, message: str, candidates: dict[str, list[str]], reasons: dict[str, str]) -> None:
        super().__init__(message)
        self.candidates = candidates
        self.reasons = reasons


def signal_check(values: np.ndarray, config: ReduceConfig) -> dict[str, Any]:
    """
    Is there signal in a key: its range, non-finite fraction and problems.

    Parameters
    ----------
    values : numpy.ndarray
        One key's values in scan order.
    config : ReduceConfig
        Thresholds.

    Returns
    -------
    dict
        ``min``, ``max``, ``mean``, ``non_finite_fraction`` (when any) and
        ``problems``: any of ``all_non_finite``, ``zero``, ``flat`` (range
        below ``flat_fraction`` of its size), ``no_structure`` (standard
        deviation below ``min_structure`` times its point-to-point noise, as
        for white noise with no spectrum under it) and ``clipped``
        (pinned at its maximum for several points, as a saturated detector is).
    """
    values = np.asarray(values, dtype=float)
    finite = values[np.isfinite(values)]
    out: dict[str, Any] = {"points": int(values.size)}
    problems = []
    if finite.size < values.size:
        out["non_finite_fraction"] = _round((values.size - finite.size) / values.size, 3)
    if not finite.size:
        out["problems"] = ["all_non_finite"]
        return out
    lo, hi = float(finite.min()), float(finite.max())
    out.update(min=_round(lo), max=_round(hi), mean=_round(finite.mean()))
    span = hi - lo
    size = float(np.median(np.abs(finite)))
    if hi == 0 and lo == 0:
        problems.append("zero")
    elif span <= config.flat_fraction * size or span == 0:
        problems.append("flat")
    else:
        noise = noise_estimate(finite)
        if noise and float(np.std(finite)) < config.min_structure * noise:
            problems.append("no_structure")
        at_max = int(np.count_nonzero(finite == hi))
        if at_max >= max(3, finite.size // 20):
            problems.append("clipped")
    out["problems"] = problems
    return out


def describe_keys(run: Run, config: ReduceConfig, devices: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """
    Every primary-stream key of a run with what it is and whether it has signal.

    Parameters
    ----------
    run : Run
        The run.
    config : ReduceConfig
        Thresholds.
    devices : dict, optional
        ``ReduceConfig.devices()``: each device's ``desc`` and ``roles``.

    Returns
    -------
    dict
        ``{key: {"role", "hinted", "plot_role", "desc", "device_roles", "units",
        "shape", "check"}}``; ``plot_role`` is the role the run recorded for it.
    """
    devices = devices if devices is not None else {}
    recorded = {k: role for role, keys in recorded_roles(run).items() for k in keys}
    out = {}
    for key, info in run.data_keys.items():
        device = info.get("object_name") or key
        static = devices.get(device) or devices.get(key) or {}
        entry: dict[str, Any] = {"role": run.role(key), "hinted": key in run.hinted}
        if key in recorded:
            entry["plot_role"] = recorded[key]
        if key.endswith("_setpoint"):
            entry["setpoint"] = True
        if device != key:
            entry["device"] = device
        if static.get("desc"):
            entry["desc"] = static["desc"]
        if static.get("roles"):
            entry["device_roles"] = static["roles"]
        for field in ("units", "shape"):
            if info.get(field):
                entry[field] = info[field]
        values = run.data.get(key)
        if values is not None and np.asarray(values).ndim == 1 and np.issubdtype(np.asarray(values).dtype, np.number):
            entry["check"] = signal_check(values, config)
        else:
            entry["check"] = None
        out[key] = entry
    return out


def _matches(expected: Any, value: Any) -> bool:
    options = expected if isinstance(expected, list) else [expected]
    for option in options:
        if isinstance(option, (int, float)) and not isinstance(option, bool) and isinstance(value, (int, float)):
            if float(option) == float(value):
                return True
        elif str(option).strip().lower() == str(value).strip().lower():
            return True
    return False


def matching_rule(run: Run, config: ReduceConfig) -> tuple[Optional[dict[str, Any]], str]:
    """
    The first ``[reduce.defaults]`` rule for this run whose ``when`` matches.

    Rules for the plan name are tried before rules for the scantype. A
    ``when`` compares the baseline reading at the start of the run.

    Returns
    -------
    tuple
        The rule (or None) and a description of it, or of why none matched.
    """
    tried = []
    for name in (run.plan, run.start.get("scantype")):
        if not name or name not in config.defaults:
            continue
        for i, rule in enumerate(config.defaults[name]):
            where = f"[reduce.defaults.{name}] rule {i + 1}"
            missed = []
            for key, expected in rule["when"].items():
                reading = run.baseline.get(key)
                if reading is None:
                    missed.append(f"{key} not in the baseline")
                elif not _matches(expected, reading[0]):
                    missed.append(f"{key} was {reading[0]!r}")
            if not missed:
                when = ", ".join(f"{k} = {v!r}" for k, v in rule["when"].items())
                return rule, where + (f" (when {when})" if when else "")
            tried.append(f"{where}: {'; '.join(missed)}")
    if tried:
        return None, "no [reduce.defaults] rule matched: " + " | ".join(tried)
    return None, f"no [reduce.defaults] rule for plan {run.plan!r} or scantype {run.start.get('scantype')!r}"


def resolve_keys(
    run: Run,
    config: ReduceConfig,
    signal: Optional[list[str]] = None,
    norm: Optional[str] = None,
    beamline_signal: str = "",
    beamline_norm: str = "",
    reference: str = "",
    devices: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    Settle the signal and normalization keys for a run (see the module notes).

    Parameters
    ----------
    run : Run
        The run.
    config : ReduceConfig
        ``[reduce.defaults]`` rules.
    signal : list of str, optional
        Signal keys the caller chose.
    norm : str, optional
        Normalization key the caller chose; ``"none"`` for no normalization.
    beamline_signal, beamline_norm : str, optional
        ``[nbs_cli]`` ``signal`` and ``norm``.
    reference : str, optional
        ``[nbs_cli]`` ``reference``: the energy-reference key, never a signal by hints.
    devices : dict, optional
        ``ReduceConfig.devices()``, for the ``intensity_detector`` role.

    Returns
    -------
    dict
        ``signal`` (list), ``norm`` (str or None), ``x`` (the scan axis or
        None) and ``reasons``: where each came from.

    Raises
    ------
    Ambiguous
        If a key is left unsettled.
    ReduceError
        If the caller named a key the run does not have.
    """
    devices = devices if devices is not None else {}
    present = set(run.data)
    for key in (signal or []) + ([norm] if norm and norm != "none" else []):
        if key not in present:
            run.array(key)  # raises with the keys it has
    reasons: dict[str, str] = {}
    rule, rule_text = (None, "") if signal and norm else matching_rule(run, config)

    def settle_signal() -> Optional[list[str]]:
        if signal:
            reasons["signal"] = "given"
            return list(signal)
        if rule and rule["signal"]:
            missing = [k for k in rule["signal"] if k not in present]
            if not missing:
                reasons["signal"] = rule_text
                return list(rule["signal"])
            reasons["signal_skipped"] = f"{rule_text} names {', '.join(missing)}, not in this run"
        primary = [k for k in plot_roles.get("primary", []) if k in present]
        if primary:
            reasons["signal"] = "the run's plot_hints: primary"
            return primary
        if beamline_signal and beamline_signal in present:
            reasons["signal"] = "[nbs_cli] signal"
            return [beamline_signal]
        return None

    def settle_norm() -> tuple[bool, Optional[str]]:
        if norm:
            reasons["norm"] = "given"
            return True, None if norm == "none" else norm
        if rule and rule["has_norm"]:
            if rule["norm"] is None or rule["norm"] in present:
                reasons["norm"] = rule_text
                return True, rule["norm"]
            reasons["norm_skipped"] = f"{rule_text} names {rule['norm']}, not in this run"
        normalization = [k for k in plot_roles.get("normalization", []) if k in present]
        if len(normalization) == 1:
            reasons["norm"] = "the run's plot_hints: normalization"
            return True, normalization[0]
        if beamline_norm and beamline_norm in present:
            reasons["norm"] = "[nbs_cli] norm"
            return True, beamline_norm
        hinted_norm = [k for k in run.hinted if k in present and NORM_ROLE in _roles(run, k, devices)]
        if len(hinted_norm) == 1:
            reasons["norm"] = f"hints: the one hinted key with the {NORM_ROLE} role"
            return True, hinted_norm[0]
        return False, None

    plot_roles = recorded_roles(run)
    signal_keys = settle_signal()
    norm_known, norm_key = settle_norm()
    axes = set(run.dimensions)
    candidates = [
        k for k in (run.hinted or sorted(present))
        if k in present and k not in axes and k != norm_key and k != reference
        and k not in plot_roles.get("reference", [])
        and run.role(k) == "detector" and not k.endswith("_setpoint")
    ]
    if signal_keys is None and len(candidates) == 1 and norm_known:
        signal_keys = candidates
        reasons["signal"] = "hints: the one hinted detector besides the normalization and energy reference"
    if signal_keys is None or not norm_known:
        norm_candidates = [k for k in candidates if k not in (signal_keys or [])]
        if norm_key is None and not norm_known:
            norm_candidates = [k for k in run.hinted if k in present and k not in axes] or norm_candidates
        unsettled = [name for name, done in (("signal", signal_keys is not None), ("norm", norm_known)) if not done]
        for name in unsettled:
            reasons.setdefault(name, rule_text or "no rule")
        raise Ambiguous(
            f"cannot settle {' and '.join(unsettled)} for {run.label()} ({run.plan}); "
            "choose from the candidates (nbs-reduce keys shows what each one is) and pass "
            + " and ".join(f"--{name}" for name in unsettled),
            {"signal": candidates if signal_keys is None else signal_keys,
             "norm": norm_candidates if not norm_known else [norm_key]},
            reasons,
        )
    return {
        "signal": signal_keys,
        "norm": norm_key,
        "x": run.dimensions[0] if run.dimensions else None,
        "reasons": reasons,
    }


def recorded_roles(run: Run) -> dict[str, list[str]]:
    """
    Detector roles the run recorded in its start document's ``plot_hints``
    (``{"primary": [...], "normalization": [...], "reference": [...]}``);
    empty when it has none. ``auxiliary`` alone says nothing, so it is dropped.
    """
    hints = run.start.get("plot_hints")
    if not isinstance(hints, dict):
        return {}
    out = {}
    for role in ("primary", "normalization", "reference"):
        keys = hints.get(role) or []
        keys = [keys] if isinstance(keys, str) else [k for k in keys if isinstance(k, str)]
        if keys:
            out[role] = keys
    return out


def _roles(run: Run, key: str, devices: dict[str, Any]) -> list[str]:
    device = run.data_keys.get(key, {}).get("object_name") or key
    return (devices.get(device) or devices.get(key) or {}).get("roles") or []


def key_warnings(run: Run, keys: dict[str, Any], config: ReduceConfig) -> list[str]:
    """
    Problems the signal check finds in the settled keys, as ``"sc: flat"``
    lines. A flat or noise-only normalization is a steady beam, not a problem.
    """
    out = []
    for key in list(keys["signal"]) + ([keys["norm"]] if keys["norm"] else []):
        check = signal_check(run.array(key), config)
        for problem in check.get("problems") or []:
            if key in keys["signal"] or problem in NORM_PROBLEMS:
                out.append(f"{key}: {problem}")
    return out
