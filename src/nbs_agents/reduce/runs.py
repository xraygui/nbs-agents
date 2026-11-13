"""
Runs as nbs-reduce reads them from Tiled: the start document, the primary
stream's arrays and data keys, the plan's hints and the baseline.

Nothing here reaches the beamline: no QueueServer, document stream or EPICS.
Data keys are passed on without their ``source``, so no PV name leaves this
module.
"""

from functools import cached_property
from typing import Any, Optional

import numpy as np

from .metrics import dimension_keys

# Descriptor data-key fields passed on; ``source`` (the PV) never is
DATA_KEY_FIELDS = ("dtype", "shape", "units", "object_name")


class ReduceError(RuntimeError):
    """A problem nbs-reduce reports as ``{"error": ...}``."""


def find_run(catalog, ref: Any):
    """
    Look up a run by UID, UID prefix, scan id, or negative index.

    Parameters
    ----------
    catalog : tiled container
        Catalog of runs.
    ref : str or int
        ``"-1"`` is the latest run; digits are a scan id; anything else a UID.

    Returns
    -------
    BlueskyRun
        Tiled run.

    Raises
    ------
    LookupError
        If there is no such run.
    """
    from tiled.queries import Key

    ref = str(ref)
    if ref.lstrip("-").isdigit():
        n = int(ref)
        if n < 0:
            runs = list(catalog.values()[-1 : n - 1 : -1])
            if len(runs) < -n:
                raise LookupError(f"only {len(runs)} runs in the catalog")
            return runs[-1]
        matches = list(catalog.search(Key("scan_id") == n).values()[-1:-2:-1])
        if not matches:
            # A plain Tiled catalog (TiledWriter into SQL) keeps it under start
            matches = list(catalog.search(Key("start.scan_id") == n).values()[-1:-2:-1])
        if not matches:
            raise LookupError(f"no run with scan_id {n}")
        return matches[0]
    try:
        return catalog[ref]
    except KeyError:
        raise LookupError(f"no run {ref!r}") from None


def _scalar(value: Any) -> Any:
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, bytes):
        value = value.decode(errors="replace")
    if isinstance(value, float):
        return float(f"{value:.6g}") if np.isfinite(value) else None
    return value


class Run:
    """
    One Bluesky run, read lazily from Tiled.

    Parameters
    ----------
    run : BlueskyRun
        Tiled run (``BlueskyRunV3``, or the older layout with descriptors in
        the stream metadata).
    catalog : str, optional
        Name of the catalog it came from, for the output.
    """

    def __init__(self, run: Any, catalog: str = "default") -> None:
        self._run = run
        self.catalog = catalog
        self.start = dict(run.metadata["start"])
        self.stop = dict(run.metadata.get("stop") or {})

    @property
    def uid(self) -> str:
        return self.start["uid"]

    @property
    def scan_id(self) -> Optional[int]:
        return self.start.get("scan_id")

    @property
    def plan(self) -> Optional[str]:
        return self.start.get("plan_name")

    @property
    def sample(self) -> Optional[str]:
        return self.start.get("sample_name") or self.start.get("sample_id")

    def ident(self) -> dict[str, Any]:
        """Who the run is: uid, scan id, plan, sample, scantype and catalog."""
        out = {"uid": self.uid, "scan_id": self.scan_id, "plan": self.plan, "catalog": self.catalog}
        for key, value in (("sample", self.sample), ("scantype", self.start.get("scantype")),
                           ("edge", self.start.get("edge")), ("status", self.stop.get("exit_status", "running"))):
            if value:
                out[key] = value
        return out

    def label(self) -> str:
        """Short name for a legend or caption: ``scan 41 U6``."""
        parts = [f"scan {self.scan_id}" if self.scan_id is not None else self.uid[:8]]
        if self.sample:
            parts.append(str(self.sample))
        return " ".join(parts)

    @cached_property
    def _primary(self):
        if "primary" not in self._run:
            return None
        return self._run["primary"]

    @cached_property
    def data(self) -> dict[str, np.ndarray]:
        """Primary-stream arrays keyed by data key; empty without a primary stream."""
        if self._primary is None:
            return {}
        ds = self._primary.read()
        keys = set(self.data_keys) or set(ds.data_vars)
        return {k: np.asarray(ds[k].values) for k in ds.data_vars if k in keys}

    @cached_property
    def _descriptor(self) -> dict[str, Any]:
        if self._primary is None:
            return {}
        meta = self._primary.metadata
        if "data_keys" in meta:
            return dict(meta)
        descriptors = meta.get("descriptors") or []
        return dict(descriptors[0]) if descriptors else {}

    @cached_property
    def data_keys(self) -> dict[str, dict[str, Any]]:
        """Primary-stream data keys with ``dtype``, ``shape``, ``units`` and ``object_name``; never ``source``."""
        out = {}
        for key, info in (self._descriptor.get("data_keys") or {}).items():
            out[key] = {f: info.get(f) for f in DATA_KEY_FIELDS if info.get(f) not in (None, "")}
        return out

    @cached_property
    def hinted(self) -> list[str]:
        """Data keys the plan's devices hint (the descriptor's ``hints`` fields), in order."""
        out = []
        for hint in (self._descriptor.get("hints") or {}).values():
            for name in (hint or {}).get("fields") or []:
                if name not in out:
                    out.append(name)
        return out

    @property
    def dimensions(self) -> list[str]:
        """Scan axis keys present in the data."""
        return [k for k in dimension_keys(self.start) if k in self.data_keys or k in self.data]

    def role(self, key: str) -> str:
        """``motor``, ``detector`` or ``other``, from the start document and the key's device."""
        device = self.data_keys.get(key, {}).get("object_name") or key
        if key in self.dimensions or device in (self.start.get("motors") or []):
            return "motor"
        if device in (self.start.get("detectors") or []) or key in (self.start.get("detectors") or []):
            return "detector"
        return "other"

    @cached_property
    def baseline(self) -> dict[str, list]:
        """``{key: [start, end]}`` for every scalar baseline reading; empty without a baseline."""
        if "baseline" not in self._run:
            return {}
        try:
            ds = self._run["baseline"].read()
        except Exception as exc:
            raise ReduceError(f"cannot read the baseline of {self.label()}: {type(exc).__name__}: {exc}") from exc
        out = {}
        for key in ds.data_vars:
            values = np.asarray(ds[key].values)
            if values.ndim != 1 or not values.size:
                continue
            out[key] = [_scalar(values[0]), _scalar(values[-1])]
        return out

    def array(self, key: str) -> np.ndarray:
        """
        One primary-stream key as a 1-D float array.

        Raises
        ------
        ReduceError
            If the run has no such key, or it is not 1-D numbers.
        """
        if key not in self.data:
            have = ", ".join(sorted(self.data)) or "none"
            raise ReduceError(f"{self.label()} has no data key {key!r} (it has {have}); see nbs-reduce keys")
        values = np.asarray(self.data[key])
        if values.ndim != 1 or not np.issubdtype(values.dtype, np.number):
            raise ReduceError(f"{key!r} in {self.label()} is not a 1-D number per point")
        return values.astype(float)
