"""Objects the plugin tests name in fake entry points."""

from pathlib import Path

DEFAULTS = {"nbs_cli": {"signal": "sc", "baseline_keys": ["ring_current"]}, "assistant": {"model": "haiku"}}
ADDENDA = Path(__file__).parent / "addenda"


def reducer(start, data, ctx, summary):
    return {"extra": summary["kind"]} if ctx.reference else None


def layout(samples):
    return {"n": len(samples)}

SETTINGS = {"redis_host": "localhost", "redis_port": 6379, "redis_ssl": False, "keys": ["a"]}


def sandbox_paths(config):
    return [("/etc/bl.secret", "NBS_BL_SECRET")] if config.extra.get("redis_ssl") else []


def info(beamline):
    from fakes import StaticInfo

    return StaticInfo(samples={"s1": {"name": "graphite"}})
