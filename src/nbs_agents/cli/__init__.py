"""nbs-cli: a state snapshot, a few verbs and a blocking wait for driving a beamline."""

__all__ = ["Beamline", "CliConfig", "CliError"]


def __getattr__(name):
    # Lazy, so that importing a submodule (config, refs) does not load core and
    # the QueueServer client: nbs-reduce reads the settings without either
    if name == "CliConfig":
        from .config import CliConfig

        return CliConfig
    if name in ("Beamline", "CliError"):
        from . import core

        return getattr(core, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
