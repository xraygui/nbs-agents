"""
Overlay plots of reduced runs, with a caption that records the keys used.
"""

from typing import Any

import numpy as np

from .analysis import _sorted, normalize, spectrum
from .runs import ReduceError, Run

KINDS = ("ratio", "normalized", "raw")


def caption(entries: list[tuple[Run, dict[str, Any]]], kind: str) -> str:
    """
    One line per run saying what was plotted and why those keys.

    Parameters
    ----------
    entries : list of (Run, dict)
        Each run with its settled keys.
    kind : str
        ``ratio``, ``normalized`` or ``raw``.

    Returns
    -------
    str
        The caption, e.g. ``scan 41 U6 (c_xas): sc/i0, normalized; keys from [reduce.defaults.c_xas] rule 1``.
    """
    lines = []
    for run, keys in entries:
        what = ", ".join(f"{s}/{keys['norm']}" if keys["norm"] and kind != "raw" else s for s in keys["signal"])
        if kind == "raw" and keys["norm"]:
            what += f" and {keys['norm']}"
        why = keys.get("reasons", {}).get("signal", "")
        lines.append(f"{run.label()} ({run.plan}): {what}, {kind}" + (f"; keys from {why}" if why else ""))
    return "\n".join(lines)


def plot(entries: list[tuple[Run, dict[str, Any]]], path: str, kind: str = "ratio", title: str = "") -> dict[str, Any]:
    """
    Save an overlay of runs to a PNG.

    Parameters
    ----------
    entries : list of (Run, dict)
        Each run with its settled keys (``resolve_keys``).
    path : str
        Output file.
    kind : str, optional
        ``ratio`` (signal over normalization), ``normalized`` (edge-step
        normalized) or ``raw`` (signals above, normalization below).
    title : str, optional
        Figure title; default names the runs.

    Returns
    -------
    dict
        ``path``, ``caption`` and ``warnings`` (spectra left out, and why).
    """
    if kind not in KINDS:
        raise ReduceError(f"unknown plot kind {kind!r}; use one of {', '.join(KINDS)}")
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    raw = kind == "raw"
    fig, axes = plt.subplots(2 if raw else 1, 1, figsize=(7, 6 if raw else 4.5), sharex=True, squeeze=False)
    axes = axes[:, 0]
    warnings = []
    x_names = set()
    for run, keys in entries:
        for signal in keys["signal"]:
            spec = spectrum(run, signal, None if raw else keys["norm"], keys["x"])
            x_names.add(spec["x_name"])
            label = f"{run.label()} {spec['name']}"
            x, y = _sorted(spec)
            if kind == "normalized":
                try:
                    y = normalize(x, y)
                except ReduceError as exc:
                    warnings.append(f"{label}: {exc}; left out")
                    continue
            axes[0].plot(x, y, ".-", ms=3, lw=1, label=label)
        if raw and keys["norm"]:
            spec = spectrum(run, keys["norm"], None, keys["x"])
            x, y = _sorted(spec)
            axes[1].plot(x, y, ".-", ms=3, lw=1, label=f"{run.label()} {keys['norm']}")
    axes[0].set_ylabel({"ratio": "signal / norm", "normalized": "normalized", "raw": "signal"}[kind])
    if raw:
        axes[1].set_ylabel("normalization")
        if axes[1].lines:
            axes[1].legend(fontsize=7)
    if axes[0].lines:
        axes[0].legend(fontsize=7)
    axes[-1].set_xlabel(" / ".join(sorted(x_names)) or "point")
    text = caption(entries, kind)
    fig.suptitle(title or ", ".join(run.label() for run, _ in entries), fontsize=10)
    n_lines = text.count("\n") + 1
    fig.text(0.01, 0.01, text, fontsize=7, va="bottom", ha="left", family="monospace")
    fig.tight_layout(rect=(0, min(0.4, 0.035 * n_lines + 0.01), 1, 1))
    fig.savefig(path, dpi=100)
    plt.close(fig)
    if not any(np.size(line.get_xdata()) for ax in axes for line in ax.lines):
        warnings.append("nothing was plotted")
    return {"path": path, "caption": text, "warnings": warnings}
