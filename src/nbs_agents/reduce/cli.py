"""
``nbs-reduce``: reduce and plot runs from Tiled, with explicit keys.

Every command prints one JSON document on stdout (``--text`` for people).
Errors print ``{"error": ...}`` and exit with status 1. When the signal or
normalization key cannot be settled, it prints ``{"ambiguous": true, ...}``
with the candidates and exits with status 3: run ``keys`` and pass
``--signal`` / ``--norm``.

It reads only Tiled (and the settings, the reference library and the
profile's devices.toml): never the QueueServer, the document stream or EPICS.
"""

import argparse
import csv
import io
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

import numpy as np

from nbs_agents.paths import BeamlineError, add_beamline_argument, use_beamline
from .analysis import compare, spectrum, summary, table
from .config import ReduceConfig
from .keys import Ambiguous, describe_keys, key_warnings, resolve_keys
from .plot import KINDS, plot
from .runs import ReduceError, Run, find_run

AMBIGUOUS_STATUS = 3
DEFAULT_CATALOG = "default"
REF_PREFIX = "ref:"
RUN_HELP = "uid, uid prefix, scan id, or -1 for the latest run"


class Reducer:
    """
    Settings and Tiled clients for one nbs-reduce call.

    Parameters
    ----------
    cli_config : CliConfig
        nbs-cli settings: Tiled, the catalogs, the reference library and the
        beamline-wide ``signal``, ``norm`` and ``reference``.
    config : ReduceConfig
        The ``[reduce]`` table.
    """

    def __init__(self, cli_config: Any, config: ReduceConfig) -> None:
        self.cli_config = cli_config
        self.config = config
        self._catalogs: dict[tuple[str, str], Any] = {}
        self._devices: Optional[dict[str, Any]] = None

    @property
    def devices(self) -> dict[str, Any]:
        """``_desc`` and ``_role`` per device from ``devices_file``; empty when it cannot be read."""
        if self._devices is None:
            try:
                self._devices = self.config.devices()
            except (OSError, ValueError) as exc:
                raise ReduceError(f"cannot read [reduce] devices_file {self.config.devices_file}: {exc}") from exc
        return self._devices

    def catalog(self, name: str = DEFAULT_CATALOG, uri: str = "", path: Optional[str] = None):
        """A Tiled catalog by name (``default`` or a ``[references.catalogs]`` one), or by URI and path."""
        from nbs_agents.cli.refs import RefError, catalogs, open_catalog

        if not uri:
            found = catalogs(self.cli_config)
            if name not in found:
                raise ReduceError(f"no catalog {name!r}; have {', '.join(found)} ([references.catalogs] in nbs-cli.toml)")
            uri, path = found[name]["uri"], found[name]["path"]
        where = (uri, path or "")
        if where not in self._catalogs:
            try:
                if name == DEFAULT_CATALOG and where == (self.cli_config.tiled_uri, (self.cli_config.tiled_path or "").strip("/")):
                    from nbs_agents.tiled_auth import TiledLoginRequired, connect

                    try:
                        self._catalogs[where] = connect(self.cli_config)
                    except TiledLoginRequired as exc:
                        raise ReduceError(str(exc)) from exc
                else:
                    self._catalogs[where] = open_catalog(self.cli_config, *where)
            except RefError as exc:
                raise ReduceError(str(exc)) from exc
        return self._catalogs[where]

    def run(self, ref: str, catalog: str = DEFAULT_CATALOG) -> Run:
        """
        A run by reference, or ``ref:ID`` for a reference library entry's run.

        Raises
        ------
        ReduceError
            If there is no such run or entry.
        """
        if ref.startswith(REF_PREFIX):
            from nbs_agents.cli.refs import ReferenceLibrary, RefError

            c = self.cli_config
            try:
                entry = ReferenceLibrary(c.ref_dir, c.ref_agent_dir, c.ref_review_dir).get(ref[len(REF_PREFIX):])
            except RefError as exc:
                raise ReduceError(str(exc)) from exc
            meta = entry.meta
            if not meta.get("uid"):
                raise ReduceError(f"reference {entry.id} names no run")
            client = self.catalog(str(meta.get("catalog") or DEFAULT_CATALOG),
                                  uri=str(meta.get("tiled_uri") or c.tiled_uri), path=str(meta.get("tiled_path") or ""))
            try:
                return Run(find_run(client, str(meta["uid"])), catalog=f"{REF_PREFIX}{entry.id}")
            except LookupError as exc:
                raise ReduceError(f"reference {entry.id}: {exc}") from exc
        try:
            return Run(find_run(self.catalog(catalog), ref), catalog=catalog)
        except LookupError as exc:
            raise ReduceError(f"{exc} in catalog {catalog}") from exc

    def keys_for(self, run: Run, args: argparse.Namespace) -> dict[str, Any]:
        """Settle the keys for a run from the command line and the rules (``resolve_keys``)."""
        c = self.cli_config
        keys = resolve_keys(
            run, self.config, signal=split_keys(args.signal), norm=args.norm,
            beamline_signal=c.signal, beamline_norm=c.norm, reference=c.reference, devices=self.devices,
        )
        if args.x:
            run.array(args.x)
            keys["x"] = args.x
            keys["reasons"]["x"] = "given"
        elif keys["x"]:
            keys["reasons"]["x"] = "the plan's scan axis (hints dimensions)"
        else:
            keys["reasons"]["x"] = "no scan axis in the hints: point number"
        keys["warnings"] = key_warnings(run, keys, self.config)
        return keys


def split_keys(values: Optional[list[str]]) -> Optional[list[str]]:
    """``--signal a,b --signal c`` as ``["a", "b", "c"]``; None when not given."""
    if not values:
        return None
    return [k.strip() for v in values for k in v.split(",") if k.strip()]


def cwd_path(name: str, flag: str) -> Path:
    """
    Resolve an output file name, refusing anything outside the current folder.

    Raises
    ------
    ReduceError
        If the name is absolute or resolves outside the current folder.
    """
    cwd = Path.cwd().resolve()
    path = (cwd / name).resolve()
    if Path(name).is_absolute() or path == cwd or cwd not in path.parents:
        raise ReduceError(f"{flag} needs a relative path inside {cwd}")
    return path


def _keys_out(keys: dict[str, Any]) -> dict[str, Any]:
    out = {k: keys[k] for k in ("signal", "norm", "x", "reasons")}
    if keys.get("warnings"):
        out["warnings"] = keys["warnings"]
    return out


# ------------------------------------------------------------------ verbs


def cmd_keys(r: Reducer, args) -> dict[str, Any]:
    run = r.run(args.run, args.catalog)
    out: dict[str, Any] = {"run": run.ident(), "x": run.dimensions[0] if run.dimensions else None,
                           "keys": describe_keys(run, r.config, r.devices)}
    if not r.devices:
        out["note"] = "no [reduce] devices_file, so no device descriptions or roles"
    watched = {k for rules in r.config.defaults.values() for rule_ in rules for k in rule_["when"]}
    state = {k: v for k, v in run.baseline.items() if k.startswith("beam_map") or k in watched}
    if state:
        out["state"] = state
    try:
        out["defaults"] = _keys_out(r.keys_for(run, argparse.Namespace(signal=None, norm=None, x=None)))
    except Ambiguous as exc:
        out["defaults"] = {"ambiguous": True, "why": str(exc), "candidates": exc.candidates, "reasons": exc.reasons}
    return out


def cmd_summary(r: Reducer, args) -> dict[str, Any]:
    run = r.run(args.run, args.catalog)
    keys = r.keys_for(run, args)
    return {"run": run.ident(), "keys": _keys_out(keys), **summary(run, keys, r.config)}


def cmd_norm(r: Reducer, args) -> dict[str, Any]:
    run = r.run(args.run, args.catalog)
    keys = r.keys_for(run, args)
    result = table(run, keys, normalized=not args.no_normalized)
    out: dict[str, Any] = {"run": run.ident(), "keys": _keys_out(keys)}
    if result["warnings"]:
        out["warnings"] = result["warnings"]
    if args.output:
        path = cwd_path(args.output, "-o")
        if args.format == "csv":
            path.write_text(to_csv(result["columns"]))
        else:
            path.write_text(json.dumps(result["columns"]))
        out.update(file=str(path), points=len(next(iter(result["columns"].values()))))
    else:
        out["columns"] = result["columns"]
    return out


def to_csv(columns: dict[str, list]) -> str:
    """Columns as CSV text with a header row."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(list(columns))
    writer.writerows(zip(*columns.values()))
    return buf.getvalue()


def cmd_compare(r: Reducer, args) -> dict[str, Any]:
    run = r.run(args.run, args.catalog)
    other = r.run(args.to, args.to_catalog or args.catalog)
    keys = r.keys_for(run, args)
    # The other run is reduced with the same keys where it has them
    same = all(k in other.data for k in keys["signal"] + ([keys["norm"]] if keys["norm"] else []))
    if same:
        other_keys = {**keys, "x": keys["x"] if keys["x"] in other.data else (other.dimensions or [None])[0],
                      "reasons": {"signal": "the same keys as the run"}}
    else:
        other_keys = r.keys_for(other, argparse.Namespace(signal=None, norm=None, x=None))
        if len(other_keys["signal"]) != len(keys["signal"]):
            raise ReduceError(f"{other.label()} settles to {other_keys['signal']}, not one signal per signal "
                              f"of {run.label()} ({keys['signal']}); compare one signal at a time")
    results = {}
    for mine, theirs in zip(keys["signal"], other_keys["signal"]):
        a = spectrum(run, mine, keys["norm"], keys["x"])
        b = spectrum(other, theirs, other_keys["norm"], other_keys["x"])
        try:
            results[f"{a['name']} vs {b['name']}"] = compare(a, b)
        except ReduceError as exc:
            results[f"{a['name']} vs {b['name']}"] = {"error": str(exc)}
    return {"run": run.ident(), "to": other.ident(), "keys": _keys_out(keys),
            "to_keys": _keys_out(other_keys), "comparison": results}


def cmd_plot(r: Reducer, args) -> dict[str, Any]:
    path = cwd_path(args.output, "-o")
    entries = []
    for ref in args.runs:
        run = r.run(ref, args.catalog)
        entries.append((run, r.keys_for(run, args)))
    result = plot(entries, str(path), kind=args.kind, title=args.title)
    out = {"file": str(path), "caption": result["caption"],
           "runs": [{**run.ident(), "keys": _keys_out(keys)} for run, keys in entries]}
    if result["warnings"]:
        out["warnings"] = result["warnings"]
    return out


VERBS = {"keys": cmd_keys, "summary": cmd_summary, "norm": cmd_norm, "compare": cmd_compare, "plot": cmd_plot}


def build_parser() -> argparse.ArgumentParser:
    """
    Build the argument parser.

    Returns
    -------
    argparse.ArgumentParser
        Parser for all subcommands.
    """
    parser = argparse.ArgumentParser(
        prog="nbs-reduce",
        description="Reduce and plot runs from Tiled. Keys are never guessed: `keys RUN` shows what each "
                    "data key is and which keys the rules settle. Prints JSON.",
    )
    parser.add_argument("--config", help="TOML settings file (default $NBS_CLI_CONFIG or the beamline's config/nbs-cli.toml)")
    add_beamline_argument(parser)
    parser.add_argument("--indent", type=int, default=1, help="JSON indent; 0 for one line")
    parser.add_argument("--text", action="store_true", help="plain text for people instead of JSON")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--catalog", default=DEFAULT_CATALOG,
                        help="Tiled catalog: default (this beamline's), or a name from `nbs-cli ref catalogs`")
    chosen = argparse.ArgumentParser(add_help=False)
    chosen.add_argument("--signal", action="append", metavar="KEY",
                        help="signal key(s); repeat or comma-separate (default: the rules, see `keys`)")
    chosen.add_argument("--norm", metavar="KEY", help="normalization key, or none (default: the rules)")
    chosen.add_argument("--x", metavar="KEY", help="axis key (default: the plan's scan axis)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("keys", parents=[common],
                       help="each data key's role, description, hints and signal check, and the keys the rules settle")
    p.add_argument("run", help=RUN_HELP)

    p = sub.add_parser("summary", parents=[common, chosen],
                       help="edge jump, e0, noise, jump to noise, baseline shape and surprise flags")
    p.add_argument("run", help=RUN_HELP)

    p = sub.add_parser("norm", parents=[common, chosen], help="the signal over the normalization, and edge-step normalized")
    p.add_argument("run", help=RUN_HELP)
    p.add_argument("-o", "--output", metavar="FILE", help="write the columns to FILE (relative, inside the current folder)")
    p.add_argument("--format", choices=("csv", "json"), default="csv", help="file format for -o")
    p.add_argument("--no-normalized", action="store_true", help="leave out the edge-step normalized columns")

    p = sub.add_parser("compare", parents=[common, chosen],
                       help="shift, scale and residual against another run or a reference (ref:ID)")
    p.add_argument("run", help=RUN_HELP)
    p.add_argument("--to", required=True, help=RUN_HELP + ", or ref:ID for a reference scan")
    p.add_argument("--to-catalog", help="catalog of --to when it is a run (default: --catalog)")

    p = sub.add_parser("plot", parents=[common, chosen], help="overlay runs in a PNG with a caption naming the keys")
    p.add_argument("runs", nargs="+", metavar="RUN", help=RUN_HELP + "; ref:ID for a reference scan")
    p.add_argument("-o", "--output", required=True, metavar="FILE.png", help="relative, inside the current folder")
    p.add_argument("--kind", choices=KINDS, default="ratio",
                   help="ratio (signal/norm), normalized (edge step) or raw (signal and norm apart)")
    p.add_argument("--title", default="")
    return parser


def to_text(result: Any, depth: int = 0) -> str:
    """Nested results as indented ``key: value`` lines."""
    pad = "  " * depth
    if isinstance(result, dict):
        lines = []
        for key, value in result.items():
            if isinstance(value, (dict, list)) and value and not _flat_list(value):
                lines.append(f"{pad}{key}:")
                lines.append(to_text(value, depth + 1))
            else:
                lines.append(f"{pad}{key}: {_short(value)}")
        return "\n".join(lines)
    if isinstance(result, list):
        return "\n".join(f"{pad}- {_short(v)}" if not isinstance(v, (dict, list)) else to_text(v, depth + 1)
                         for v in result)
    return f"{pad}{_short(result)}"


def _flat_list(value: Any) -> bool:
    return isinstance(value, list) and all(not isinstance(v, (dict, list)) for v in value)


def _short(value: Any) -> str:
    if isinstance(value, list):
        text = ", ".join(str(v) for v in value[:8])
        return f"[{text}{', …' if len(value) > 8 else ''}]"
    return str(value)


def _json_default(obj: Any) -> Any:
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, np.generic):
        return obj.item()
    return str(obj)


def main(argv: Optional[list[str]] = None) -> int:
    """
    Entry point for ``nbs-reduce``.

    Parameters
    ----------
    argv : list of str, optional
        Arguments; defaults to ``sys.argv[1:]``.

    Returns
    -------
    int
        Exit status: 0, 1 on an error, 3 when keys are ambiguous.
    """
    args = build_parser().parse_args(argv)
    try:
        try:
            use_beamline(args.beamline)
        except BeamlineError as exc:
            if not (args.config or os.environ.get("NBS_CLI_CONFIG")):
                raise ReduceError(str(exc))
        from nbs_agents.cli.config import CliConfig

        reducer = Reducer(CliConfig.load(args.config), ReduceConfig.load(args.config))
        result, status = VERBS[args.command](reducer, args), 0
    except Ambiguous as exc:
        result = {"ambiguous": True, "error": str(exc), "candidates": exc.candidates, "reasons": exc.reasons}
        status = AMBIGUOUS_STATUS
    except ReduceError as exc:
        result, status = {"error": str(exc)}, 1
    except Exception as exc:  # report connection problems as JSON too
        result, status = {"error": f"{type(exc).__name__}: {exc}"}, 1
    if args.text:
        sys.stdout.write(to_text(json.loads(json.dumps(result, default=_json_default))) + "\n")
    else:
        json.dump(result, sys.stdout, indent=args.indent or None, default=_json_default)
        sys.stdout.write("\n")
    return status


if __name__ == "__main__":
    sys.exit(main())
