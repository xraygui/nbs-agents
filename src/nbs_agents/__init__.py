"""NBS Agent Framework."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("nbs-agents")
except PackageNotFoundError:
    __version__ = "0.0.0"
