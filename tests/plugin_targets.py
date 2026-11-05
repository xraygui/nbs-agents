"""Objects the plugin tests name in fake entry points."""

from pathlib import Path

DEFAULTS = {"nbs_cli": {"signal": "sc", "baseline_keys": ["ring_current"]}, "assistant": {"model": "haiku"}}
ADDENDA = Path(__file__).parent / "addenda"


def reducer(start, data, ctx, summary):
    return {"extra": summary["kind"]} if ctx.reference else None


def layout(samples):
    return {"n": len(samples)}
