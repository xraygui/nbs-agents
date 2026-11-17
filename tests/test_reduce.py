"""
nbs-reduce against a real in-process Tiled server, with runs written by
TiledWriter: a clean edge, a charged run (inverted edge on a decaying
baseline, like U6 in the Run 2 trial) and a run with the main detector
blocked by a screen.
"""

import json
import os
import time

import numpy as np
import pytest

from nbs_agents.reduce import cli
from nbs_agents.reduce.config import ReduceConfig
from nbs_agents.reduce.keys import Ambiguous, resolve_keys, signal_check
from nbs_agents.reduce.metrics import baseline_metrics, edge_metrics
from nbs_agents.reduce.runs import Run

pytest.importorskip("tiled.server")
pytest.importorskip("bluesky_tiled_plugins")

API_KEY = "0123456789abcdef0123456789abcdef"  # Tiled wants hex
ENERGY = np.linspace(270.0, 310.0, 81)
E0 = 288.0
PV = "XF:TEST{secret-pv}"

DEVICES_TOML = '''
[i0]
_desc = "Gold mesh drain current; incoming intensity (normalization)."
_role = "intensity_detector"
prefix = "XF:TEST{i0}"

[sc]
_desc = "Sample drain current."
prefix = "XF:TEST{sc}"

[ref]
_desc = "Energy reference mesh."
prefix = "XF:TEST{ref}"

[screen_diode]
_desc = "Diode on the beam-viewing screen."
prefix = "XF:TEST{screen}"
'''

RULES_TOML = '''
[[reduce.defaults.c_xas]]
when = {beam_map_screen_status = "in"}
signal = ["screen_diode"]
norm = "i0"

[[reduce.defaults.c_xas]]
signal = ["sc"]
norm = "i0"
'''


def step(energy, height=1.0, width=1.0):
    return height * (0.5 + 0.5 * np.tanh((energy - E0) / width))


def write_run(writer, scan_id, sample, columns, screen="out", plan="c_xas", hint=None):
    """Write one run: primary stream over ENERGY and a two-reading baseline."""
    from event_model import compose_run

    hint = hint or [k for k in columns]
    bundle = compose_run(metadata={
        "plan_name": plan, "scan_id": scan_id, "sample_name": sample, "scantype": "xas", "edge": "C",
        "hints": {"dimensions": [[["en_energy"], "primary"]]},
        "detectors": [k for k in columns], "motors": ["en"],
    })
    writer("start", bundle.start_doc)
    data_keys = {"en_energy": {"dtype": "number", "shape": [], "source": f"PV:{PV}", "object_name": "en", "units": "eV"}}
    for key in columns:
        data_keys[key] = {"dtype": "number", "shape": [], "source": f"PV:{PV}", "object_name": key}
    desc = bundle.compose_descriptor(
        name="primary", data_keys=data_keys, object_keys={k: [k] for k in columns} | {"en": ["en_energy"]},
        hints={k: {"fields": [k]} for k in hint} | {"en": {"fields": ["en_energy"]}},
    )
    writer("descriptor", desc.descriptor_doc)
    for i, energy in enumerate(ENERGY):
        values = {"en_energy": float(energy)} | {k: float(v[i]) for k, v in columns.items()}
        writer("event", desc.compose_event(data=values, timestamps={k: time.time() for k in values}))
    base_keys = {"ring_current": {"dtype": "number", "shape": [], "source": f"PV:{PV}"},
                 "beam_map_screen_status": {"dtype": "string", "shape": [], "source": f"PV:{PV}"}}
    base = bundle.compose_descriptor(name="baseline", data_keys=base_keys,
                                     object_keys={"ring_current": ["ring_current"],
                                                  "beam_map_screen_status": ["beam_map_screen_status"]})
    writer("descriptor", base.descriptor_doc)
    for current in (400.0, 399.0):
        values = {"ring_current": current, "beam_map_screen_status": screen}
        writer("event", base.compose_event(data=values, timestamps={k: time.time() for k in values}))
    writer("stop", bundle.compose_stop())
    return bundle.start_doc["uid"]


@pytest.fixture(scope="module")
def tiled_runs(tmp_path_factory):
    """An in-process Tiled server holding four runs; yields its URI and their uids."""
    from bluesky_tiled_plugins import TiledWriter
    from tiled.client import from_uri
    from tiled.server import SimpleTiledServer

    server = SimpleTiledServer(directory=tmp_path_factory.mktemp("tiled"), api_key=API_KEY)
    uri = server.uri.split("?")[0]  # without the key, which nbs-reduce gets from TILED_API_KEY
    writer = TiledWriter(from_uri(uri, api_key=API_KEY))
    rng = np.random.default_rng(1)
    i0 = 100.0 * (1 + 0.001 * rng.standard_normal(ENERGY.size))
    ref = 40 + step(ENERGY - 1.5, 5.0)
    uids = {}
    # 1: a clean edge
    sc = (0.2 + step(ENERGY)) * i0 * (1 + 0.001 * rng.standard_normal(ENERGY.size))
    uids["clean"] = write_run(writer, 1, "STD", {"i0": i0, "sc": sc, "ref": ref})
    # 2: the same sample again, edge 0.4 eV higher
    sc2 = (0.2 + step(ENERGY - 0.4)) * i0 * (1 + 0.001 * rng.standard_normal(ENERGY.size))
    uids["again"] = write_run(writer, 2, "STD", {"i0": i0, "sc": sc2, "ref": ref})
    # 3: charging: a large decaying baseline above a small edge, so pre-edge > post-edge
    charged = (0.2 + 3.0 * np.exp(-(ENERGY - ENERGY[0]) / 12.0) + 0.3 * step(ENERGY)) * i0
    uids["charged"] = write_run(writer, 3, "U6", {"i0": i0, "sc": charged, "ref": ref})
    # 4: screen in: sc reads only an offset and noise; the screen diode sees the edge
    blocked = 0.05 + 1e-5 * rng.standard_normal(ENERGY.size)
    screen = (0.1 + step(ENERGY)) * i0
    uids["blocked"] = write_run(writer, 4, "STD", {"i0": i0, "sc": blocked, "ref": ref, "screen_diode": screen},
                                screen="in")
    yield uri, uids
    server.close()


@pytest.fixture
def config_file(tiled_runs, tmp_path, monkeypatch):
    """nbs-cli.toml for the test server, with the devices file and the c_xas rules."""
    uri, _ = tiled_runs
    devices = tmp_path / "devices.toml"
    devices.write_text(DEVICES_TOML)
    path = tmp_path / "nbs-cli.toml"
    path.write_text(f'[nbs_cli]\ntiled_uri = "{uri}"\nreference = "ref"\n\n'
                    f'[reduce]\ndevices_file = "{devices}"\n{RULES_TOML}')
    monkeypatch.setenv("NBS_CLI_CONFIG", str(path))
    monkeypatch.setenv("TILED_API_KEY", API_KEY)
    monkeypatch.chdir(tmp_path)
    return path


def reduce(capsys, *argv):
    status = cli.main(list(argv))
    out = capsys.readouterr().out
    return status, json.loads(out)


# ------------------------------------------------------------ against Tiled


def test_keys_describes_each_key_without_pv_names(config_file, capsys):
    status, out = reduce(capsys, "keys", "1")
    assert status == 0, out
    assert out["run"]["scan_id"] == 1 and out["x"] == "en_energy"
    keys = out["keys"]
    assert keys["i0"]["role"] == "detector" and keys["i0"]["device_roles"] == ["intensity_detector"]
    assert keys["sc"]["desc"] == "Sample drain current."
    assert keys["en_energy"]["role"] == "motor" and keys["en_energy"]["units"] == "eV"
    assert keys["sc"]["hinted"] and keys["sc"]["check"]["problems"] == []
    assert out["state"] == {"beam_map_screen_status": ["out", "out"]}
    assert out["defaults"]["signal"] == ["sc"] and out["defaults"]["norm"] == "i0"
    assert "[reduce.defaults.c_xas] rule 2" in out["defaults"]["reasons"]["signal"]
    text = json.dumps(out)
    assert "XF:TEST" not in text and "PV:" not in text and "source" not in text


def test_keys_flags_the_blocked_detector_and_the_screen_rule_picks_the_diode(config_file, capsys):
    status, out = reduce(capsys, "keys", "4")
    assert status == 0, out
    assert set(out["keys"]["sc"]["check"]["problems"]) & {"flat", "no_structure"}
    assert out["defaults"]["signal"] == ["screen_diode"]
    assert "beam_map_screen_status = 'in'" in out["defaults"]["reasons"]["signal"]


def test_summary_of_a_clean_edge_has_no_flags(config_file, capsys):
    status, out = reduce(capsys, "summary", "1")
    assert status == 0, out
    s = out["signals"]["sc/i0"]
    assert s["edge_jump"] == pytest.approx(1.0, rel=0.05)
    assert s["e0_max_slope"] == pytest.approx(E0, abs=0.6)
    assert not s["inverted"] and out["flags"] == []
    assert out["keys"]["reasons"]["norm"].startswith("[reduce.defaults.c_xas] rule 2")


def test_summary_flags_the_charged_run(config_file, capsys):
    status, out = reduce(capsys, "summary", "3")
    assert status == 0, out
    flags = out["flags"]
    assert "sc/i0: inverted_edge" in flags
    assert "sc/i0: steep_pre_edge" in flags or "sc/i0: curved_pre_edge" in flags


def test_summary_with_explicit_blocked_detector_flags_it(config_file, capsys):
    status, out = reduce(capsys, "summary", "4", "--signal", "sc", "--norm", "i0")
    assert status == 0, out
    assert out["keys"]["reasons"] == {"signal": "given", "norm": "given", "x": "the plan's scan axis (hints dimensions)"}
    assert any(w.startswith("sc: ") for w in out["keys"]["warnings"])
    assert any(f.startswith("sc: ") for f in out["flags"])


def test_ambiguous_without_rules_exits_3_with_candidates(config_file, capsys, tmp_path):
    config_file.write_text(config_file.read_text().split("[[reduce.defaults")[0].replace('reference = "ref"\n', ""))
    status, out = reduce(capsys, "summary", "1")
    assert status == cli.AMBIGUOUS_STATUS
    assert out["ambiguous"] is True
    # i0 has the intensity_detector role, so the norm settles; sc and ref are left
    assert out["candidates"]["norm"] == ["i0"]
    assert sorted(out["candidates"]["signal"]) == ["ref", "sc"]
    assert "--signal" in out["error"]


def test_beamline_reference_key_is_set_aside_so_hints_settle(config_file, capsys):
    config_file.write_text(config_file.read_text().split("[[reduce.defaults")[0])
    status, out = reduce(capsys, "keys", "1")
    assert out["defaults"]["signal"] == ["sc"] and out["defaults"]["norm"] == "i0"
    assert out["defaults"]["reasons"]["signal"].startswith("hints")


def test_norm_writes_csv_inside_the_folder_only(config_file, capsys, tmp_path):
    status, out = reduce(capsys, "norm", "1", "-o", "s1.csv")
    assert status == 0, out
    lines = (tmp_path / "s1.csv").read_text().splitlines()
    assert lines[0] == "en_energy,sc/i0,sc/i0 norm"
    last = [float(v) for v in lines[-1].split(",")]
    assert last[2] == pytest.approx(1.0, abs=0.05)
    status, out = reduce(capsys, "norm", "1", "-o", "../escape.csv")
    assert status == 1 and "inside" in out["error"]


def test_compare_finds_the_shift(config_file, capsys):
    status, out = reduce(capsys, "compare", "2", "--to", "1")
    assert status == 0, out
    result = out["comparison"]["sc/i0 vs sc/i0"]
    assert result["shift"] == pytest.approx(0.4, abs=0.6)
    assert result["scale"] == pytest.approx(1.0, abs=0.05)
    assert result["residual_rms"] < 0.05


def test_compare_to_a_reference_entry(config_file, capsys, tiled_runs, tmp_path):
    uri, uids = tiled_runs
    refs = tmp_path / "references"
    refs.mkdir()
    (refs / "std-c.md").write_text(f"---\ntitle: STD at the C edge\nuid: {uids['clean']}\ncatalog: default\n"
                                   f"tiled_uri: {uri}\ntiled_path: ''\n---\nA clean STD edge.\n")
    config_file.write_text(config_file.read_text().replace("[nbs_cli]\n", f'[nbs_cli]\nref_dir = "{refs}"\n'))
    status, out = reduce(capsys, "compare", "2", "--to", "ref:std-c")
    assert status == 0, out
    assert out["to"]["catalog"] == "ref:std-c" and out["to"]["scan_id"] == 1


def test_plot_records_keys_in_the_caption(config_file, capsys, tmp_path):
    # Into a folder that does not exist yet, as the plotter role asks
    status, out = reduce(capsys, "plot", "1", "2", "4", "-o", "plots/overlay.png", "--kind", "normalized")
    assert status == 0, out
    assert (tmp_path / "plots" / "overlay.png").stat().st_size > 1000
    caption = out["caption"].splitlines()
    assert caption[0].startswith("scan 1 STD (c_xas): sc/i0, normalized; keys from [reduce.defaults.c_xas] rule 2")
    assert caption[2].startswith("scan 4 STD (c_xas): screen_diode/i0")
    assert [r["keys"]["signal"] for r in out["runs"]] == [["sc"], ["sc"], ["screen_diode"]]


def test_unknown_key_is_an_error_naming_the_keys(config_file, capsys):
    status, out = reduce(capsys, "summary", "1", "--signal", "PEY")
    assert status == 1
    assert "no data key 'PEY'" in out["error"] and "sc" in out["error"]


def test_text_output(config_file, capsys):
    assert cli.main(["--text", "keys", "1"]) == 0
    text = capsys.readouterr().out
    assert "defaults:" in text and "signal: [sc]" in text


# ------------------------------------------------------------ pure parts


def test_signal_check_problems():
    config = ReduceConfig()
    x = np.linspace(0, 1, 50)
    assert signal_check(np.zeros(50), config)["problems"] == ["zero"]
    assert signal_check(np.full(50, 3.0), config)["problems"] == ["flat"]
    assert "clipped" in signal_check(np.minimum(x * 2, 1.0), config)["problems"]
    noisy = 1 + 0.01 * np.random.default_rng(0).standard_normal(50)
    assert signal_check(noisy, config)["problems"] == ["no_structure"]
    check = signal_check(np.r_[x, np.nan], config)
    assert check["problems"] == [] and check["non_finite_fraction"] == pytest.approx(0.0196, abs=1e-3)


def test_baseline_metrics_tell_charging_from_a_clean_edge():
    clean = 0.2 + step(ENERGY)
    charged = 0.2 + 3.0 * np.exp(-(ENERGY - ENERGY[0]) / 12.0) + 0.3 * step(ENERGY)
    m = baseline_metrics(ENERGY, clean)
    assert not m["inverted"] and abs(m["pre_edge_slope_rel"]) < 0.05
    m = baseline_metrics(ENERGY, charged)
    assert m["inverted"] and abs(m["pre_edge_slope_rel"]) > 0.5
    assert edge_metrics(ENERGY, clean)["edge_jump"] == pytest.approx(1.0, rel=0.01)


def test_rules_are_checked():
    with pytest.raises(ValueError, match="unknown settings"):
        ReduceConfig.from_table({"defaults": {"xas": {"signal": "sc", "nrom": "i0"}}})
    with pytest.raises(ValueError, match="Unknown \\[reduce\\]"):
        ReduceConfig.from_table({"default": {}})
    config = ReduceConfig.from_table({"defaults": {"xas": {"signal": "sc", "norm": "none"}}})
    assert config.defaults["xas"] == [{"signal": ["sc"], "norm": None, "has_norm": True, "when": {}}]


class FakeRun(Run):
    """A run without Tiled, for resolution alone."""

    def __init__(self, data, hinted, baseline=None, plan="scan", detectors=None):
        self.catalog = "default"
        self.start = {"uid": "u1", "scan_id": 7, "plan_name": plan, "detectors": detectors or list(data),
                      "hints": {"dimensions": [[["x"], "primary"]]}}
        self.stop = {}
        self.__dict__.update(data=data, data_keys={k: {} for k in data}, hinted=hinted, baseline=baseline or {})


def test_resolution_order_explicit_rule_beamline_hints():
    data = {k: np.arange(10.0) for k in ("x", "a", "b", "i0")}
    run = FakeRun(data, hinted=["a", "b", "i0"], detectors=["a", "b", "i0"])
    devices = {"i0": {"roles": ["intensity_detector"]}}
    config = ReduceConfig.from_table({"defaults": {"scan": [{"signal": ["b"], "norm": "i0"}]}})
    assert resolve_keys(run, config, signal=["a"], norm="none")["signal"] == ["a"]
    assert resolve_keys(run, config, signal=["a"], norm="none")["norm"] is None
    assert resolve_keys(run, config)["signal"] == ["b"]
    keys = resolve_keys(run, ReduceConfig(), beamline_signal="a", devices=devices)
    assert keys["signal"] == ["a"] and keys["norm"] == "i0" and keys["reasons"]["norm"].startswith("hints")
    with pytest.raises(Ambiguous) as err:
        resolve_keys(run, ReduceConfig(), devices=devices)
    assert err.value.candidates == {"signal": ["a", "b"], "norm": ["i0"]}
    keys = resolve_keys(run, ReduceConfig(), devices=devices, reference="b")
    assert keys["signal"] == ["a"]


def test_rule_naming_a_missing_key_falls_through():
    data = {k: np.arange(10.0) for k in ("x", "a", "i0")}
    run = FakeRun(data, hinted=["a", "i0"])
    config = ReduceConfig.from_table({"defaults": {"scan": {"signal": ["PEY"], "norm": "i0"}}})
    keys = resolve_keys(run, config, beamline_signal="a")
    assert keys["signal"] == ["a"] and "PEY" in keys["reasons"]["signal_skipped"]


def test_no_queue_or_zmq_imports():
    import subprocess
    import sys

    code = ("import sys, nbs_agents.reduce.cli, nbs_agents.reduce.plot, nbs_agents.cli.config, nbs_agents.cli.refs\n"
            "bad = [m for m in sys.modules if m.split('.')[0] in ('bluesky_queueserver_api', 'zmq', 'redis')"
            " or m in ('nbs_agents.cli.core', 'nbs_agents.runner', 'nbs_agents.queue')]\n"
            "print(bad)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=os.environ, check=True)
    assert out.stdout.strip() == "[]"


def test_recorded_plot_hints_roles_come_after_rules_and_before_nbs_cli():
    data = {k: np.arange(10.0) for k in ("x", "sc", "i0", "ref", "diode")}
    run = FakeRun(data, hinted=["sc", "i0", "ref", "diode"])
    run.start["plot_hints"] = {"primary": ["diode"], "normalization": ["i0"], "reference": ["ref"],
                               "auxiliary": ["sc"]}
    keys = resolve_keys(run, ReduceConfig(), beamline_signal="sc", beamline_norm="ref")
    assert keys["signal"] == ["diode"] and keys["norm"] == "i0"
    assert keys["reasons"]["signal"] == "the run's plot_hints: primary"
    config = ReduceConfig.from_table({"defaults": {"scan": {"signal": ["sc"], "norm": "i0"}}})
    assert resolve_keys(run, config)["signal"] == ["sc"]


def test_old_runs_with_only_auxiliary_plot_hints_fall_through():
    data = {k: np.arange(10.0) for k in ("x", "sc", "i0", "ref")}
    run = FakeRun(data, hinted=["sc", "i0", "ref"])
    run.start["plot_hints"] = {"auxiliary": ["i0", "ref", "sc"]}
    with pytest.raises(Ambiguous):
        resolve_keys(run, ReduceConfig())
    keys = resolve_keys(run, ReduceConfig(), beamline_signal="sc", beamline_norm="i0")
    assert keys["reasons"]["signal"] == "[nbs_cli] signal"


def test_recorded_reference_is_never_a_hinted_signal():
    data = {k: np.arange(10.0) for k in ("x", "sc", "i0", "ref")}
    run = FakeRun(data, hinted=["sc", "i0", "ref"])
    run.start["plot_hints"] = {"normalization": ["i0"], "reference": ["ref"]}
    keys = resolve_keys(run, ReduceConfig())
    assert keys["signal"] == ["sc"] and keys["norm"] == "i0"


def test_plot_average_reports_each_repeat(config_file, capsys, tmp_path):
    status, out = reduce(capsys, "plot", "1", "2", "-o", "rep.png", "--kind", "normalized", "--average",
                         "--signal", "sc", "--norm", "i0")
    assert status == 0, out
    assert [r["label"] for r in out["repeats"]] == ["scan 1 STD sc/i0", "scan 2 STD sc/i0"]
    assert all(r["residual"] >= 0 for r in out["repeats"])
    # Nothing matches when any distance is too much
    status, out = reduce(capsys, "plot", "1", "2", "-o", "rep.png", "--kind", "normalized", "--average",
                         "--signal", "sc", "--norm", "i0", "--mismatch", "-1")
    assert out["mismatch"] == ["scan 1 STD sc/i0", "scan 2 STD sc/i0"]


def test_repeat_check_finds_the_odd_one():
    from nbs_agents.reduce.plot import repeat_check

    x = np.linspace(0, 10, 50)
    edge = 1 / (1 + np.exp(-(x - 5)))
    run = lambda uid: type("R", (), {"uid": uid})()  # noqa: E731
    curves = [(run("a"), "a", x, edge), (run("b"), "b", x, edge + 0.005), (run("c"), "c", x, 0.5 * edge)]
    out = repeat_check(curves)
    assert out["mismatch"] == ["c"]
    assert len(out["mean"]) == 50
