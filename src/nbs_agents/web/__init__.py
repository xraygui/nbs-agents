"""Localhost web UI for the agent log, mirroring the nbs-gui agent tabs."""

from .app import create_app

__all__ = ["create_app"]
