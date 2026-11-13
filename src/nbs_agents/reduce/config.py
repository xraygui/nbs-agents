"""
nbs-reduce settings: the ``[reduce]`` table of the nbs-cli TOML file.

::

    [reduce]
    devices_file = "~/profile/startup/devices.toml"   # _desc and _role of each device

    # Default keys per plan name or scantype; the first rule whose `when`
    # matches the run's baseline at its start is used
    [[reduce.defaults.c_xas]]
    when = {beam_map_screen_status = "in"}
    signal = ["screen_diode"]
    norm = "i0"

    [[reduce.defaults.c_xas]]
    signal = ["sc"]
    norm = "i0"
"""

import os
import tomllib
from dataclasses import dataclass, field, fields
from typing import Any, Optional

from nbs_agents.config import read_config_file

REDUCE_TABLE = "reduce"
RULE_KEYS = {"signal", "norm", "when"}


@dataclass
class ReduceConfig:
    """
    Settings for nbs-reduce.

    Attributes
    ----------
    devices_file : str
        The beamline profile's devices.toml. nbs-reduce reads only each
        device's ``_desc`` and ``_role`` from it, never a prefix. Also
        ``NBS_DEVICES_FILE``. Empty for none.
    defaults : dict
        Key rules by plan name or scantype: each a list of
        ``{"signal": [...], "norm": ..., "when": {baseline key: value}}``.
    min_jump_to_noise : float
        Flag a spectrum whose edge jump to noise ratio is below this.
    max_pre_edge_slope : float
        Flag a pre-edge line that moves more than this many edge jumps across
        the pre-edge region (the first 20% of the points).
    max_post_edge_slope : float
        The same for the post-edge line (the last 20% of the points).
    max_pre_edge_curvature : float
        Flag a pre-edge whose quadratic term is more than this many edge jumps.
    norm_drop_fraction : float
        Flag a normalization whose minimum is below this fraction of its median.
    flat_fraction : float
        A key is flat when its range is below this fraction of its median size.
    min_structure : float
        A key is noise only when its standard deviation is below this many
        times its point-to-point noise (white noise gives about 1).
    """

    devices_file: str = ""
    defaults: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    min_jump_to_noise: float = 10.0
    max_pre_edge_slope: float = 0.3
    max_post_edge_slope: float = 0.5
    max_pre_edge_curvature: float = 0.3
    norm_drop_fraction: float = 0.5
    flat_fraction: float = 1e-3
    min_structure: float = 3.0

    @classmethod
    def load(cls, path: Optional[str] = None) -> "ReduceConfig":
        """
        Load the ``[reduce]`` table.

        Parameters
        ----------
        path : str, optional
            Explicit TOML file path.

        Returns
        -------
        ReduceConfig
            Settings.

        Raises
        ------
        ValueError
            On unknown settings or a malformed rule.
        """
        return cls.from_table(read_config_file(path).get(REDUCE_TABLE))

    @classmethod
    def from_table(cls, table: Optional[dict[str, Any]]) -> "ReduceConfig":
        """Settings from a parsed ``[reduce]`` table (see ``load``)."""
        values = dict(table or {})
        known = {f.name for f in fields(cls)}
        unknown = set(values) - known
        if unknown:
            raise ValueError(f"Unknown [reduce] settings: {sorted(unknown)}")
        values["defaults"] = read_rules(values.get("defaults"))
        config = cls(**values)
        if os.environ.get("NBS_DEVICES_FILE"):
            config.devices_file = os.environ["NBS_DEVICES_FILE"]
        return config

    def devices(self) -> dict[str, dict[str, Any]]:
        """
        ``_desc`` and ``_role`` of each device in ``devices_file``.

        Returns
        -------
        dict
            ``{name: {"desc": str or None, "roles": [str]}}``; empty without a file.
        """
        if not self.devices_file:
            return {}
        with open(os.path.expanduser(self.devices_file), "rb") as f:
            table = tomllib.load(f)
        out = {}
        for name, entry in table.items():
            if not isinstance(entry, dict):
                continue
            roles = entry.get("_role") or []
            out[name] = {
                "desc": entry.get("_desc"),
                "roles": [roles] if isinstance(roles, str) else [r for r in roles if r],
            }
        return out


def read_rules(table: Optional[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """
    Check ``[reduce.defaults]``.

    Parameters
    ----------
    table : dict or None
        Plan name or scantype to one rule table or a list of them.

    Returns
    -------
    dict
        Name to a list of rules, each with ``signal`` (list of str), ``norm``
        (str, or None for no normalization: ``norm = "none"``), ``has_norm``
        (whether the rule sets ``norm`` at all) and ``when`` (dict).
    """
    out = {}
    for name, rules in (table or {}).items():
        rules = [rules] if isinstance(rules, dict) else list(rules)
        checked = []
        for i, rule in enumerate(rules):
            where = f"[reduce.defaults.{name}] rule {i + 1}"
            if not isinstance(rule, dict):
                raise ValueError(f"{where} must be a table")
            unknown = set(rule) - RULE_KEYS
            if unknown:
                raise ValueError(f"{where}: unknown settings {sorted(unknown)}")
            signal = rule.get("signal") or []
            signal = [signal] if isinstance(signal, str) else list(signal)
            if not signal and "norm" not in rule:
                raise ValueError(f"{where} needs signal or norm")
            when = rule.get("when") or {}
            if not isinstance(when, dict):
                raise ValueError(f"{where}: when must be a table of baseline key = value")
            norm = rule.get("norm")
            checked.append({"signal": signal, "norm": None if norm in (None, "", "none") else str(norm),
                            "has_norm": "norm" in rule, "when": when})
        out[name] = checked
    return out
