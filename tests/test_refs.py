import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import numpy as np

from nbs_agents.cli.config import CliConfig, read_catalogs
from nbs_agents.cli.main import add_note_refs, main
from nbs_agents.cli.kb import KnowledgeBase
from nbs_agents.cli.refs import RefError, ReferenceLibrary, baseline_start, scan_ranges
from nbs_agents.tiled_auth import TiledLoginRequired

UID = "4f1c0a7e-1111-2222-3333-444455556666"
OTHER_UID = "9a9a9a9a-1111-2222-3333-444455556666"

CONFIG = """
[nbs_cli]
tiled_uri = "https://tiled.example.org"
tiled_path = "bl/raw"
ref_dir = "{root}/references"
ref_agent_dir = "{root}/agent"
ref_review_dir = "{root}/review"
kb_agent_dir = "{root}/kb"

[references.catalogs.eng]
uri = "https://tiled.example.org"
path = "eng/raw"

[references.catalogs.elsewhere]
uri = "https://other.example.org"
path = "x/raw"
"""


class FakeArray:
    def __init__(self, values):
        self.values = np.asarray(values)

    def read(self):
        return self.values


class FakeDataset:
    def __init__(self, columns):
        self.columns = {k: FakeArray(v) for k, v in columns.items()}
        self.data_vars = list(columns)

    def __getitem__(self, key):
        return self.columns[key]


class FakeStream:
    def __init__(self, columns):
        self.columns = columns

    def read(self):
        return FakeDataset(self.columns)


class FakeRun:
    """A Bluesky run as Tiled serves it: start/stop metadata, primary and baseline streams."""

    def __init__(self, uid, scan_id, primary=None, baseline=None, plan="nbs_scan"):
        start = {"uid": uid, "scan_id": scan_id, "plan_name": plan, "time": 1759414265.0,
                 "motors": ["m3_pitch"], "detectors": ["i0"], "num_points": 3,
                 "plan_args": {"start": -1, "stop": 1, "energies": list(range(400))}}
        self.metadata = {"start": start, "stop": {"exit_status": "success", "time": 1759414300.0}}
        self.streams = {}
        if primary is not None:
            self.streams["primary"] = FakeStream(primary)
        if baseline is not None:
            self.streams["baseline"] = FakeStream(baseline)

    def __contains__(self, key):
        return key in self.streams

    def __getitem__(self, key):
        return self.streams[key]


class FakeCatalog:
    """Enough of a Tiled catalog for get_run: uid (prefix) lookup, newest-last values, scan_id search."""

    def __init__(self, runs):
        self.runs = list(runs)

    def __getitem__(self, key):
        for run in self.runs:
            if run.metadata["start"]["uid"].startswith(key):
                return run
        raise KeyError(key)

    def values(self):
        return self.runs

    def search(self, query):
        return FakeCatalog([r for r in self.runs if r.metadata["start"].get(query.key) == query.value])


def make_run(uid=UID, scan_id=81234):
    return FakeRun(
        uid, scan_id,
        primary={"m3_pitch_setpoint": [-1.0, 0.0, 1.0], "m3_pitch": [-1.0, 0.0, 1.0], "i0": [1.0, 3.0, 1.0]},
        baseline={"ring_current": [400.123456, 399.0], "m3_pitch": [0.0, 0.0], "en_energy": [700.0, 700.0],
                  "image": np.zeros((2, 4, 4)), "mode": ["top-off", "top-off"]},
    )


class RefTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.human = self.root / "references"
        self.human.mkdir()
        (self.human / "README.md").write_text("How to write references.\n")
        self.lib = ReferenceLibrary(self.human, self.root / "agent", self.root / "review", clock=lambda: 0)

    def draft(self, title="M3 pitch rocking curve", uid=UID, scan_id=81234, request="", **lists):
        run = {"uid": uid, "scan_id": scan_id, "catalog": "default", "tiled_uri": "https://tiled.example.org",
               "tiled_path": "bl/raw", "taken": "2025-10-02T14:11:05", "plan": "nbs_scan"}
        lists.setdefault("devices", ["m3_pitch", "i0"])
        return self.lib.create(title, "Peak at 0 mrad after the realignment.", "assistant", run,
                               {"start": {"uid": uid}, "baseline": {"ring_current": 400.1}}, request=request, **lists)


class TestLibrary(RefTestCase):
    def test_drafts_stay_out_until_approved(self):
        draft = self.draft(nodes=["m3"])
        self.assertEqual(draft["id"], "draft/m3-pitch-rocking-curve")
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(self.lib.entries(), [])
        self.assertEqual(self.lib.search("pitch"), [])
        self.assertEqual(self.lib.refs(device="m3_pitch"), [])
        self.assertEqual([d.id for d in self.lib.drafts()], [draft["id"]])
        snapshot = self.lib.get(draft["id"]).snapshot()
        self.assertEqual(snapshot["baseline"], {"ring_current": 400.1})

    def test_approve_writes_entry_and_snapshot_into_the_profile(self):
        draft = self.draft()
        out = self.lib.approve(draft["id"], "Ana", body="Edited by staff.", tags=["alignment"])
        self.assertEqual(out["id"], "m3-pitch-rocking-curve")
        entry = self.lib.get(out["id"])
        self.assertEqual(entry.meta["approved_by"], "Ana")
        self.assertEqual(entry.meta["created_by"], "assistant")
        self.assertEqual(entry.meta["uid"], UID)
        self.assertEqual(entry.meta["tiled_path"], "bl/raw")
        self.assertEqual(entry.meta["tags"], ["alignment"])
        self.assertEqual(entry.body.strip(), "Edited by staff.")
        self.assertTrue((self.human / "m3-pitch-rocking-curve.snapshot.json").is_file())
        self.assertEqual(entry.snapshot()["start"]["uid"], UID)
        self.assertEqual(self.lib.drafts(), [])
        self.assertEqual(len(list((self.root / "review" / "archive").rglob("*.md"))), 1)
        self.assertEqual(self.lib.refs(device="m3_pitch")[0]["scan_id"], 81234)
        self.assertEqual(self.lib.search("rocking")[0]["id"], out["id"])
        self.assertEqual(self.lib.search("alignment")[0]["id"], out["id"])

    def test_staff_name_is_required(self):
        draft = self.draft()
        with self.assertRaises(RefError):
            self.lib.approve(draft["id"], " ")

    def test_supersede_hides_the_old_entry(self):
        old = self.lib.approve(self.draft()["id"], "Ana")
        new = self.lib.approve(self.draft(title="M3 pitch after bake", uid=OTHER_UID, scan_id=90000)["id"],
                               "Ana", supersedes=old["id"])
        old_entry = self.lib.get(old["id"])
        self.assertEqual((old_entry.status, old_entry.meta["superseded_by"]), ("superseded", new["id"]))
        self.assertEqual([e.id for e in self.lib.select()], [new["id"]])
        self.assertEqual(len(self.lib.select(all_statuses=True)), 2)
        self.assertEqual([r["id"] for r in self.lib.refs(device="m3_pitch")], [new["id"]])
        self.lib.set_status(old["id"], "Ana", "current")
        self.assertNotIn("superseded_by", self.lib.get(old["id"]).meta)
        with self.assertRaises(RefError):
            self.lib.set_status(old["id"], "Ana", "superseded")
        with self.assertRaises(RefError):
            self.lib.set_status(old["id"], "Ana", "superseded", superseded_by=old["id"])
        with self.assertRaises(RefError):
            self.lib.set_status(old["id"], "Ana", "gone")
        self.lib.set_status(old["id"], "Ana", "retired")
        self.assertEqual(self.lib.get(old["id"]).status, "retired")

    def test_edit_keeps_where_the_run_is(self):
        entry = self.lib.approve(self.draft()["id"], "Ana")
        self.lib.edit(entry["id"], "Ben", title="M3 pitch curve", body="New text.", aliases=["mirror 3"])
        edited = self.lib.get(entry["id"])
        self.assertEqual((edited.title, edited.meta["uid"], edited.meta["edited_by"]), ("M3 pitch curve", UID, "Ben"))
        self.assertEqual(self.lib.search("mirror")[0]["id"], entry["id"])
        with self.assertRaises(RefError):
            self.lib.edit(self.draft()["id"], "Ben", body="x")

    def test_reject_moves_draft_and_closes_its_request(self):
        request = self.lib.add_request("Ana", "default", "81234", "M3 alignment reference")
        draft = self.draft(request=request["id"])
        self.assertEqual(self.lib.pending_requests(), [])
        out = self.lib.reject(draft["id"], "Ana", "wrong scan")
        self.assertTrue(Path(out["file"]).is_file())
        self.assertTrue(Path(out["file"][:-3] + ".snapshot.json").is_file())
        self.assertEqual(self.lib.drafts(), [])
        self.assertEqual(self.lib.pending_requests(), [])  # closed, not pending again

    def test_requests_stay_pending_until_a_matching_draft(self):
        by_scan = self.lib.add_request("Ana", "default", "81234", "M3 alignment reference")
        by_uid = self.lib.add_request("Ana", "eng", OTHER_UID[:8], "other catalog")
        self.assertEqual(self.lib.review_queue()["count"], 2)
        self.draft()  # scan 81234 in default answers the first
        self.assertEqual([r["id"] for r in self.lib.pending_requests()], [by_uid["id"]])
        self.draft(title="Wrong catalog", uid=OTHER_UID)  # same uid, but in "default"
        self.assertEqual([r["id"] for r in self.lib.pending_requests()], [by_uid["id"]])
        self.lib.cancel_request(by_uid["id"], "Ana")
        self.assertEqual(self.lib.pending_requests(), [])
        with self.assertRaises(RefError):
            self.lib.cancel_request(by_uid["id"], "Ana")
        with self.assertRaises(RefError):
            self.lib.add_request("Ana", "default", "../etc", "x")
        with self.assertRaises(RefError):
            self.lib.add_request("Ana", "default", "81234", " ")
        self.assertTrue(by_scan["id"].startswith("req-"))

    def test_ids_are_unique_and_safe(self):
        self.assertEqual(self.draft()["id"], "draft/m3-pitch-rocking-curve")
        self.assertEqual(self.draft()["id"], "draft/m3-pitch-rocking-curve-2")
        with self.assertRaises(RefError):
            self.lib.get("../secrets")
        with self.assertRaises(RefError):
            self.lib.create("t", "b", "a", {}, {})

    def test_refs_on_device_and_diagnose(self):
        self.lib.approve(self.draft(nodes=["m3"])["id"], "Ana")
        kb = KnowledgeBase(None, self.root / "kb")
        device = add_note_refs(kb, "device", {"m3_pitch": {}, "eslit": {}}, self.lib)
        self.assertEqual(device["m3_pitch"]["references"][0]["id"], "m3-pitch-rocking-curve")
        self.assertNotIn("references", device["eslit"])
        diag = add_note_refs(kb, "diagnose", {"path": [{"node": "m3", "devices": [{"name": "i0"}]}]}, self.lib)
        self.assertEqual(diag["path"][0]["references"][0]["id"], "m3-pitch-rocking-curve")
        self.assertEqual(diag["path"][0]["devices"][0]["references"][0]["id"], "m3-pitch-rocking-curve")


class TestSnapshot(unittest.TestCase):
    def test_baseline_keeps_every_key_at_the_start(self):
        out = baseline_start(make_run())
        self.assertEqual(out["ring_current"], 400.123)
        self.assertEqual(out["en_energy"], 700.0)
        self.assertEqual(out["image"], {"shape": [4, 4]})
        self.assertEqual(out["mode"], "top-off")
        self.assertEqual(baseline_start(FakeRun(UID, 1)), {})

    def test_scanned_range(self):
        start = {"motors": ["m3_pitch", "missing"]}
        self.assertEqual(scan_ranges(start, {"m3_pitch": [1.0, 0.0, -1.0]}), {"m3_pitch": [1.0, -1.0, -1.0, 1.0]})
        self.assertEqual(scan_ranges(start, {"m3_pitch_readback": [2.0, 3.0]}), {"m3_pitch": [2.0, 3.0, 2.0, 3.0]})


class TestConfig(unittest.TestCase):
    def test_catalog_table_is_checked(self):
        self.assertEqual(read_catalogs({"catalogs": {"eng": {"uri": "https://t", "path": "/eng/raw/"}}}),
                         {"eng": {"uri": "https://t", "path": "eng/raw"}})
        for bad in ({"catalogs": {"default": {"uri": "x"}}}, {"catalogs": {"eng": {"path": "x"}}},
                    {"catalogs": {"eng": {"uri": "x", "key": "secret"}}}, {"other": 1}):
            with self.assertRaises(ValueError):
                read_catalogs(bad)

    def test_catalogs_are_not_nbs_cli_settings(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml") as f:
            f.write('[nbs_cli]\nref_catalogs = {}\n')
            f.flush()
            with self.assertRaises(ValueError):
                CliConfig.load(f.name)


class TestRefCommand(RefTestCase):
    def setUp(self):
        super().setUp()
        self.config = self.root / "nbs-cli.toml"
        self.config.write_text(CONFIG.format(root=self.root))
        self.catalogs = {"bl/raw": FakeCatalog([make_run(UID, 81234)]),
                         "eng/raw": FakeCatalog([make_run(OTHER_UID, 5)])}
        self.connected = []

        def connect(config, interactive=None, on_link=None):
            self.connected.append((config.tiled_uri, config.tiled_path))
            return self.catalogs[config.tiled_path]

        patcher = mock.patch("nbs_agents.tiled_auth.connect", connect)
        patcher.start()
        self.addCleanup(patcher.stop)
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)

    def cli(self, *argv, role="assistant"):
        out = io.StringIO()
        cwd = os.getcwd()
        os.chdir(self.work)
        try:
            with mock.patch.dict(os.environ, {"NBS_CLI_ROLE": role}), redirect_stdout(out):
                status = main(["--config", str(self.config), *argv])
        finally:
            os.chdir(cwd)
        return status, json.loads(out.getvalue())

    def test_create_writes_a_draft_with_location_and_snapshot(self):
        (self.work / "desc.md").write_text("Rocking curve of M3 pitch.\n")
        status, out = self.cli("ref", "create", "81234", "--title", "M3 pitch", "--file", "desc.md",
                               "--nodes", "m3")
        self.assertEqual(status, 0, out)
        self.assertEqual(out["id"], "draft/m3-pitch")
        self.assertEqual(out["created_by"], "assistant")
        self.assertEqual(out["devices"], ["m3_pitch", "i0"])  # from the run
        entry = self.lib.get("draft/m3-pitch")
        self.assertEqual({k: entry.meta[k] for k in ("uid", "scan_id", "catalog", "tiled_uri", "tiled_path", "plan")},
                         {"uid": UID, "scan_id": 81234, "catalog": "default",
                          "tiled_uri": "https://tiled.example.org", "tiled_path": "bl/raw", "plan": "nbs_scan"})
        snap = entry.snapshot()
        self.assertEqual(snap["start"]["ranges"], {"m3_pitch": [-1.0, 1.0, -1.0, 1.0]})
        self.assertTrue(snap["start"]["plan_args"]["energies"].endswith("..."))
        self.assertEqual(snap["baseline"]["ring_current"], 400.123)
        self.assertEqual(snap["summary"]["uid"], UID)
        self.assertEqual(snap["summary"]["status"], "success")
        # Drafts only: nothing goes into the profile folder
        self.assertEqual([p.name for p in self.human.iterdir()], ["README.md"])

    def test_create_in_another_catalog_on_the_same_server(self):
        status, out = self.cli("ref", "create", OTHER_UID[:8], "--catalog", "eng", "--title", "Eng scan",
                               "--text", "From the engineering catalog.", "--devices", "m3_pitch")
        self.assertEqual(status, 0, out)
        meta = self.lib.get(out["id"]).meta
        self.assertEqual((meta["catalog"], meta["tiled_path"], meta["uid"]), ("eng", "eng/raw", OTHER_UID))
        self.assertIn(("https://tiled.example.org", "eng/raw"), self.connected)

    def test_another_server_gets_no_key(self):
        with mock.patch("nbs_agents.tiled_auth.login_context",
                        side_effect=TiledLoginRequired("Tiled at https://other.example.org needs a login.")):
            status, out = self.cli("ref", "create", "-1", "--catalog", "elsewhere", "--title", "t", "--text", "b")
        self.assertEqual(status, 1)
        self.assertIn("agents hold a key only for https://tiled.example.org", out["error"])
        self.assertNotIn(("https://other.example.org", "x/raw"), self.connected)

    def test_unknown_catalog_and_run(self):
        status, out = self.cli("ref", "create", "1", "--catalog", "nope", "--title", "t", "--text", "b")
        self.assertEqual(status, 1)
        self.assertIn("default, eng, elsewhere", out["error"])
        status, out = self.cli("ref", "create", "777", "--title", "t", "--text", "b")
        self.assertEqual((status, out["error"]), (1, "no run with scan_id 777"))

    def test_list_search_show_and_verify(self):
        _, made = self.cli("ref", "create", "-1", "--title", "M3 pitch", "--text", "Rocking curve.")
        _, drafts = self.cli("ref", "list", "--drafts")
        self.assertEqual([d["id"] for d in drafts], [made["id"]])
        self.lib.approve(made["id"], "Ana")
        _, listed = self.cli("ref", "list", "--device", "i0")
        self.assertEqual([e["id"] for e in listed], ["m3-pitch"])
        _, hits = self.cli("ref", "search", "rocking")
        self.assertEqual(hits[0]["id"], "m3-pitch")
        status, shown = self.cli("ref", "show", "m3-pitch")
        self.assertEqual((status, shown["scan"]), (0, {"resolves": True}))
        self.assertEqual(shown["snapshot"]["start"]["uid"], UID)
        _, report = self.cli("ref", "verify")
        self.assertEqual((report["checked"], report["problems"]), (1, []))
        self.catalogs["bl/raw"].runs.clear()
        _, report = self.cli("ref", "verify")
        self.assertEqual(report["problems"][0]["id"], "m3-pitch")
        self.assertIs(report["problems"][0]["resolves"], False)


if __name__ == "__main__":
    unittest.main()
