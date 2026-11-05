import io
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from fakes import FakeRedis, StaticInfo
from nbs_agents.agentlog import AgentLog
from nbs_agents.cli.config import CliConfig
from nbs_agents.cli.core import Beamline, CliError, plain_reason
from nbs_agents.cli.main import build_parser, main, parse_duration, parse_plan_params, run_command
from nbs_agents.cli.summaries import SummaryContext, summarize
from nbs_agents.constants import AGENT_UID_KEY, CLOSE_AGENT_PLAN, QUEUE_STOP_INSTRUCTION


class SummaryTests(unittest.TestCase):
    def test_xas_edge_jump(self):
        x = np.linspace(270, 310, 81)
        rng = np.random.default_rng(0)
        y = 1 + 2 / (1 + np.exp(-(x - 285))) + rng.normal(0, 0.01, x.size)
        start = {"plan_name": "c_xas", "scantype": "xas", "edge": "C", "detectors": ["sc", "i0"],
                 "hints": {"dimensions": [[["en_energy"], "primary"]]}}
        data = {"en_energy": x, "sc": y * 100, "i0": np.full(x.size, 100.0)}
        out = summarize(start, data, SummaryContext("sc", "i0"))
        self.assertEqual(out["kind"], "xas")
        self.assertAlmostEqual(out["edge_jump"], 2.0, delta=0.05)
        self.assertLess(abs(out["e0_max_slope"] - 285), 1)
        self.assertGreater(out["jump_to_noise"], 50)

    def test_generic(self):
        start = {"plan_name": "nbs_count", "detectors": ["sc"]}
        out = summarize(start, {"sc": np.array([1.0, 2.0, 3.0])}, SummaryContext("sc", "i0"))
        self.assertEqual(out["kind"], "generic")
        self.assertEqual(out["detectors"]["sc"]["mean"], 2.0)


class ParseTests(unittest.TestCase):
    def test_plan_params(self):
        args, kwargs = parse_plan_params(["s1", "dwell=0.5", "sample=s2", "region=[1,2,3]"])
        self.assertEqual(args, ["s1"])
        self.assertEqual(kwargs, {"dwell": 0.5, "sample": "s2", "region": [1, 2, 3]})
        with self.assertRaises(CliError):
            parse_plan_params(["a=1", "b"])

    def test_duration(self):
        self.assertEqual(parse_duration("2h"), 7200)
        self.assertEqual(parse_duration("45m"), 2700)
        self.assertEqual(parse_duration("90"), 90)


class AgentLogTests(unittest.TestCase):
    def test_open_append_status(self):
        log = AgentLog(FakeRedis(), prefix="bl-")
        log.open("u1", "claude", goal="find N", budget={"time_s": 60})
        log.append("u1", "note", "hello", item_uid="i1", run_uid=None)
        log.set_status("u1", "closed")
        record = log.get("u1")
        self.assertEqual(record["goal"], "find N")
        self.assertEqual(record["status"], "closed")
        self.assertEqual(record["log"][0]["kind"], "note")
        self.assertNotIn("run_uid", record["log"][0])
        self.assertEqual(list(log.records()), ["u1"])
        self.assertIn("bl-AGENTu1", log.redis.store)

    def test_operator_messages_are_read_once(self):
        log = AgentLog(FakeRedis(), prefix="bl-")
        log.open("u1", "claude")
        log.message("u1", "skip s3")
        self.assertEqual([m["text"] for m in log.take_operator_messages("u1")], ["skip s3"])
        self.assertEqual(log.take_operator_messages("u1"), [])
        log.request_stop("u1", "beam dump soon")
        taken = log.take_operator_messages("u1")
        self.assertEqual([m["kind"] for m in taken], ["stop"])
        self.assertEqual(log.get("u1")["stop_requested"]["text"], "beam dump soon")

    def test_ask_sets_awaiting_input_until_answered(self):
        log = AgentLog(FakeRedis(), prefix="bl-")
        log.open("u1", "claude")
        log.ask("u1", "which motors?", source="nbs-cli")
        record = log.get("u1")
        self.assertEqual(record["log"][-1]["kind"], "question")
        self.assertEqual(record["awaiting_input"]["text"], "which motors?")
        log.append("u1", "note", "still thinking")
        self.assertIn("awaiting_input", log.get("u1"))
        log.message("u1", "x and y")
        self.assertNotIn("awaiting_input", log.get("u1"))
        log.ask("u1", "ok to go?")
        log.request_stop("u1")
        self.assertNotIn("awaiting_input", log.get("u1"))
        log.ask("u1", "summary?")
        log.append("u1", "finalize", "done")
        self.assertNotIn("awaiting_input", log.get("u1"))


def _queue(uid):
    return [
        {"item_type": "plan", "name": "other", "item_uid": "o1"},
        {"item_type": "plan", "name": "c_xas", "item_uid": "a1", "meta": {AGENT_UID_KEY: uid}},
        {"item_type": "instruction", "name": QUEUE_STOP_INSTRUCTION, "item_uid": "qs"},
        {"item_type": "plan", "name": CLOSE_AGENT_PLAN, "item_uid": "ca", "args": [uid]},
        {"item_type": "plan", "name": "later", "item_uid": "o2"},
    ]


class BeamlineVerbTests(unittest.TestCase):
    def make(self, budget=None):
        bl = Beamline(CliConfig())
        bl._agent_log = AgentLog(FakeRedis())
        bl.agent_log.open("u1", "claude", goal="g", budget=budget)
        manager = MagicMock()
        manager.queue_get.return_value = {"success": True, "items": _queue("u1"), "running_item": {}}
        manager.plans_allowed.return_value = {
            "success": True,
            "plans_allowed": {"c_xas": {"parameters": [{"name": "dwell", "kind": {"name": "KEYWORD_ONLY"}}]}},
        }
        manager.item_add.return_value = {"success": True, "item": {"item_uid": "new"}}
        manager.item_remove.return_value = {"success": True}
        manager.status.return_value = {"manager_state": "executing_queue"}
        bl._manager = manager
        return bl, manager

    def test_devices_lists_group_devices(self):
        bl, manager = self.make()
        manager.devices_allowed.return_value = {
            "success": True,
            "devices_allowed": {
                "manipx": {"classname": "FlyableMotor", "is_movable": True, "is_readable": True, "components": {}},
                "i0": {"classname": "EpicsSignalRO", "is_movable": False, "is_readable": True},
            },
        }
        self.assertEqual(
            bl.devices(), {
                "i0": {"class": "EpicsSignalRO", "movable": False, "readable": True, "node": None, "desc": None},
                "manipx": {"class": "FlyableMotor", "movable": True, "readable": True, "node": None, "desc": None},
            }
        )
        self.assertEqual(list(bl.devices(movable_only=True)), ["manipx"])
        self.assertEqual(manager.devices_allowed.call_args.kwargs["user_group"], bl.config.qs_user_group)

    def test_split_queue(self):
        split = Beamline.split_queue(_queue("u1"), "u1")
        self.assertEqual([i["item_uid"] for i in split["block"]], ["a1"])
        self.assertEqual(split["queue_stop"]["item_uid"], "qs")
        self.assertEqual((split["before"], split["after"]), (1, 1))
        self.assertFalse(split["block_at_head"])

    def test_add_requires_reason_and_known_params(self):
        bl, manager = self.make()
        with self.assertRaises(CliError):
            bl.add("c_xas", [], {}, "")
        with self.assertRaises(CliError):
            bl.add("c_xas", [], {"bogus": 1}, "why")
        with self.assertRaises(CliError):
            bl.add("mv", [], {}, "why")

    def test_add_inserts_before_stop_with_metadata(self):
        bl, manager = self.make()
        out = bl.add("c_xas", [], {"dwell": 1}, "carbon edge on S1")
        item = manager.item_add.call_args.args[0]
        self.assertEqual(manager.item_add.call_args.kwargs["before_uid"], "qs")
        self.assertEqual(item.meta["agent_uid"], "u1")
        self.assertEqual(item.meta["agent_reason"], "carbon edge on S1")
        self.assertEqual(item.meta["agent_sequence"], 1)
        self.assertEqual(out["item"]["item_uid"], "new")
        self.assertEqual(bl.agent_log.get("u1")["log"][-1]["kind"], "add")

    def test_add_respects_budget(self):
        bl, manager = self.make(budget={"time_s": 10})
        bl._info = StaticInfo(estimate=lambda item: 60.0)
        with self.assertRaises(CliError) as ctx:
            bl.add("c_xas", [], {}, "too long")
        self.assertIn("over budget", str(ctx.exception))
        manager.item_add.assert_not_called()

    def test_remove_only_own_items(self):
        bl, manager = self.make()
        with self.assertRaises(CliError):
            bl.remove("o1", "not mine")
        bl.remove("a1", "changed my mind")
        manager.item_remove.assert_called_once_with(uid="a1")

    def test_finalize_removes_stop(self):
        bl, manager = self.make()
        out = bl.finalize("done")
        manager.item_remove.assert_called_once_with(uid="qs")
        self.assertEqual(bl.agent_log.get("u1")["status"], "finalizing")
        self.assertFalse(out["started"])

    def test_failed_open_removes_its_block(self):
        bl = Beamline(CliConfig())
        bl._agent_log = AgentLog(FakeRedis())
        manager = MagicMock()
        block = [
            {"item_type": "plan", "name": "open_agent", "item_uid": "op"},
            {"item_type": "instruction", "name": QUEUE_STOP_INSTRUCTION, "item_uid": "qs"},
            {"item_type": "plan", "name": CLOSE_AGENT_PLAN, "item_uid": "ca"},
        ]
        manager.item_add_batch.return_value = {"success": True, "items": block}
        manager.queue_get.return_value = {"success": True, "items": block, "running_item": {}}
        manager.status.return_value = {"manager_state": "idle", "worker_environment_exists": True}
        manager.queue_start.return_value = {"success": True}
        manager.history_get.return_value = {"items": [{"item_uid": "op", "result": {"exit_status": "failed"}}]}
        manager.item_remove.return_value = {"success": True}
        bl._manager = manager
        with self.assertRaises(CliError) as ctx:
            bl.open_session("g", wait_s=5)
        self.assertIn("nbs-agents-runner is not running", str(ctx.exception))
        manager.item_add_batch.assert_not_called()
        bl.agent_log.heartbeat()
        with self.assertRaises(CliError) as ctx:
            bl.open_session("g", wait_s=5)
        self.assertIn("open_agent failed", str(ctx.exception))
        self.assertEqual([c.kwargs["uid"] for c in manager.item_remove.call_args_list], ["op", "qs", "ca"])
        self.assertEqual(bl.agent_log.records(), {})

    def test_ask_needs_text_and_session(self):
        bl, manager = self.make()
        with self.assertRaises(CliError):
            bl.ask("  ")
        entry = bl.ask("go?")
        self.assertEqual(entry["kind"], "question")
        self.assertEqual(bl.agent_log.get("u1")["awaiting_input"]["text"], "go?")

    def test_add_returns_operator_messages(self):
        bl, manager = self.make()
        bl.agent_log.message("u1", "try nitrogen next")
        out = bl.add("c_xas", [], {"dwell": 1}, "carbon edge on S1")
        self.assertEqual(out["operator"]["messages"][0]["text"], "try nitrogen next")
        self.assertFalse(out["operator"]["stop_requested"])
        self.assertIsNone(bl.operator_inbox("u1"))

    def test_abort_removes_requeued_copy(self):
        bl, manager = self.make()
        running = {"name": "c_xas", "item_uid": "r1", "kwargs": {"sample": "s1"},
                   "meta": {AGENT_UID_KEY: "u1", "agent_sequence": 3}}
        copy = dict(running, item_uid="r2")
        manager.queue_get.side_effect = [
            {"success": True, "items": [], "running_item": running},
            {"success": True, "items": [copy] + _queue("u1")[2:4], "running_item": {}},
        ]
        manager.status.side_effect = [
            {"re_state": "running"},
            {"re_state": "paused"},
            {"manager_state": "idle", "re_state": "idle"},
        ]
        manager.re_pause.return_value = {"success": True}
        manager.re_abort.return_value = {"success": True}
        out = bl.abort("beam lost")
        manager.re_pause.assert_called_once_with(option="immediate")
        manager.item_remove.assert_called_once_with(uid="r2")
        self.assertTrue(out["requeued_copy_removed"])
        self.assertEqual(bl.agent_log.get("u1")["log"][-1]["kind"], "abort")

    def test_state_without_runs_reads_nothing_from_tiled(self):
        bl, manager = self.make()
        running = {"item_type": "plan", "name": "c_xas", "item_uid": "r1"}
        manager.queue_get.return_value = {"success": True, "items": _queue("u1"), "running_item": running}
        bl._tiled = MagicMock()
        bl._tiled.values.side_effect = AssertionError("Tiled was read")
        out = bl.state(n_runs=0)
        self.assertNotIn("recent_runs", out)
        self.assertNotIn("elapsed_s", out["running"])
        self.assertNotIn("errors", out)
        bl._tiled.values.side_effect = None
        bl._tiled.values.return_value = []
        self.assertIn("recent_runs", bl.state(n_runs=2))

    def test_metadata_verb_and_state_summary(self):
        bl, manager = self.make()
        md = {"proposal": {"proposal_id": "317000"}, "users": ["A. Scientist"], "notes": "x" * 1000}
        bl._info = StaticInfo(metadata=md)
        self.assertEqual(run_command(bl, build_parser().parse_args(["metadata", "users"])), {"users": ["A. Scientist"]})
        self.assertEqual(run_command(bl, build_parser().parse_args(["metadata"]))["notes"], "x" * 1000)
        bl._tiled = MagicMock()
        out = bl.state(n_runs=0)
        self.assertIn("metadata", out["capabilities"])
        self.assertEqual(out["metadata"]["proposal"], {"proposal_id": "317000"})
        self.assertEqual(out["metadata"]["notes"], "(1002 chars; nbs-cli metadata notes)")
        bl._info = StaticInfo()
        self.assertNotIn("metadata", bl.state(n_runs=0))
        with self.assertRaisesRegex(CliError, "not available at this beamline"):
            run_command(bl, build_parser().parse_args(["metadata"]))

    def test_add_passes_on_the_beamline_warning(self):
        bl, manager = self.make()
        bl._info = StaticInfo(warning="sample s2 is not in the beam")
        out = bl.add("c_xas", [], {"dwell": 1}, "PS", dry_run=True)
        self.assertEqual(out["warning"], "sample s2 is not in the beam")
        bl._info = StaticInfo()
        self.assertNotIn("warning", bl.add("c_xas", [], {"dwell": 1}, "PS", dry_run=True))

    def test_abort_refuses_other_plans(self):
        bl, manager = self.make()
        manager.queue_get.return_value = {"success": True, "items": [], "running_item": {"name": "other", "meta": {}}}
        with self.assertRaises(CliError):
            bl.abort("not mine")
        manager.re_abort.assert_not_called()

    def test_observer_peeks_without_marking_read(self):
        bl, manager = self.make()
        bl.agent_log.set_status("u1", "active", driver="agent-token")
        bl.agent_log.message("u1", "go")
        observer = bl.operator_inbox("u1")
        self.assertTrue(observer["peek"])
        self.assertEqual(observer["messages"][0]["text"], "go")
        bl.config.driver = "agent-token"
        inbox = bl.operator_inbox("u1")
        self.assertNotIn("peek", inbox)
        self.assertEqual(inbox["messages"][0]["text"], "go")
        self.assertIsNone(bl.operator_inbox("u1"))

    def test_add_refused_after_stop(self):
        bl, manager = self.make()
        bl.agent_log.request_stop("u1", "need the beamline")
        with self.assertRaises(CliError) as ctx:
            bl.add("c_xas", [], {"dwell": 1}, "one more")
        self.assertIn("need the beamline", str(ctx.exception))
        manager.item_add.assert_not_called()
        inbox = bl.operator_inbox("u1")
        self.assertTrue(inbox["stop_requested"])
        self.assertIn("finalize", inbox["instruction"])


if __name__ == "__main__":
    unittest.main()


class AgentUidEnvTests(unittest.TestCase):
    def make(self):
        bl = Beamline(CliConfig())
        bl._agent_log = AgentLog(FakeRedis())
        bl.agent_log.open("old", "claude", goal="older")
        bl.agent_log.open("launched", "claude", goal="from the GUI")
        bl.agent_log.set_status("old", "active", opened=time.time() + 10)  # newest
        bl._manager = MagicMock()
        return bl

    def test_nbs_agent_uid_selects_the_session(self):
        bl = self.make()
        args = build_parser().parse_args(["note", "hello"])
        with patch.dict("os.environ", {"NBS_AGENT_UID": "launched"}):
            run_command(bl, args)
        self.assertEqual(bl.agent_log.get("launched")["log"][-1]["text"], "hello")
        run_command(bl, build_parser().parse_args(["note", "other"]))
        self.assertEqual(bl.agent_log.get("old")["log"][-1]["text"], "other")
        with patch.dict("os.environ", {"NBS_AGENT_UID": "launched"}):
            run_command(bl, build_parser().parse_args(["--agent-uid", "old", "note", "explicit"]))
        self.assertEqual(bl.agent_log.get("old")["log"][-1]["text"], "explicit")

    def test_open_is_refused_when_attached(self):
        bl = self.make()
        with patch.dict("os.environ", {"NBS_AGENT_UID": "launched"}):
            with self.assertRaises(CliError) as ctx:
                run_command(bl, build_parser().parse_args(["open", "--goal", "g"]))
        self.assertIn("already open", str(ctx.exception))
        bl.manager.item_add_batch.assert_not_called()


class BeamTests(unittest.TestCase):
    MAP = {
        "name": "claude",
        "prefix": "NBS_MAP:",
        "endstation": "es_chamber",
        "beam_expected": False,
        "reason": "psh7 blocked: NBS_SIM:psh7:Pos-Sts = 1",
        "updated": "2026-10-27T20:00:00",
        "nodes": {
            "psh7": {"upstream": "branch_es", "children": ["es_chamber"], "desc": "",
                     "blocking": True, "path_clear": True, "beam_expected": False,
                     "reason": "psh7 blocked: NBS_SIM:psh7:Pos-Sts = 1"},
        },
    }

    def make(self, data):
        bl = Beamline(CliConfig())
        bl._info = StaticInfo(beam_map=data or None)
        return bl

    def test_beam_summary(self):
        bl = self.make(self.MAP)
        self.assertEqual(
            bl.beam(),
            {
                "endstation": "es_chamber",
                "expected": False,
                "reason": "psh7 blocked",
                "updated": "2026-10-27T20:00:00",
            },
        )

    def test_beam_map(self):
        out = self.make(self.MAP).beam(full=True)
        self.assertEqual(out["nodes"]["psh7"]["children"], ["es_chamber"])
        self.assertFalse(out["expected"])

    def test_no_beam_map(self):
        self.assertIsNone(self.make({}).beam())

    def test_cli_says_when_there_is_no_beam_map(self):
        bl = self.make({})
        args = build_parser().parse_args(["beam"])
        out = run_command(bl, args)
        self.assertFalse(out["beam_map"])
        self.assertIn("no beam map", out["reason"])

    def map_file(self, text):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        path = os.path.join(d, "beam_map.toml")
        with open(path, "w") as f:
            f.write(text)
        return path

    MAP_FILE = """
[map]
name = "t"

[status.ring_status]
pv = "SR-OPS{}Mode-Sts"
desc = "Beam operation status"

[status]
ring_current = "SR:I"

[nodes.a]
desc = "a node"
"""

    def fake_pv(self, name, type_="ctrl_double", value=None, string=None, units="", connected=True):
        pv = MagicMock(pvname=name, type=type_, units=units)
        pv.wait_for_connection.return_value = connected
        pv.get.side_effect = lambda as_string=False, timeout=None: string if as_string else value
        return pv

    def test_beam_includes_status_pvs(self):
        bl = self.make({})
        bl.config.beam_map_file = self.map_file(self.MAP_FILE)
        pvs = {
            "SR-OPS{}Mode-Sts": self.fake_pv("SR-OPS{}Mode-Sts", "ctrl_enum", 5, "Maintenance"),
            "SR:I": self.fake_pv("SR:I", value=np.float64(401.2345), string="401.235", units="mA"),
        }
        with patch("epics.get_pv", side_effect=lambda name, **kw: pvs[name]) as get_pv:
            out = run_command(bl, build_parser().parse_args(["beam"]))
        self.assertEqual(get_pv.call_args.kwargs["form"], "ctrl")
        self.assertEqual(
            out["status"],
            {
                "ring_status": {"value": "Maintenance", "desc": "Beam operation status"},
                "ring_current": {"value": 401.2345, "units": "mA"},
            },
        )
        self.assertIs(type(out["status"]["ring_current"]["value"]), float)
        self.assertFalse(out["beam_map"])

    def test_status_pv_not_connected(self):
        bl = self.make({})
        bl.config.beam_map_file = self.map_file(self.MAP_FILE)
        with patch("epics.get_pv", side_effect=lambda name, **kw: self.fake_pv(name, connected=False)):
            out = run_command(bl, build_parser().parse_args(["beam"]))
        self.assertEqual(out["status"]["ring_current"], {"value": "not connected: SR:I"})

    def test_no_beam_map_file_reads_nothing(self):
        bl = self.make(self.MAP)
        with patch("epics.get_pv") as get_pv:
            out = run_command(bl, build_parser().parse_args(["beam"]))
        get_pv.assert_not_called()
        self.assertNotIn("status", out)

    def test_status_failure_keeps_the_beam_map(self):
        bl = self.make(self.MAP)
        bl.config.beam_map_file = self.map_file(self.MAP_FILE)
        with patch("epics.get_pv", side_effect=RuntimeError("no CA")):
            out = run_command(bl, build_parser().parse_args(["beam"]))
        self.assertEqual(out["endstation"], "es_chamber")
        self.assertIn("no CA", out["status"]["error"])

    def test_unreadable_beam_map_file_is_reported(self):
        bl = self.make(self.MAP)
        bl.config.beam_map_file = "/nonexistent/beam_map.toml"
        out = run_command(bl, build_parser().parse_args(["beam"]))
        self.assertIn("cannot read beam_map_file", out["status"]["error"])

    def test_post_reads_its_text_from_a_file(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        with open(os.path.join(d, "post.md"), "w") as f:
            f.write("Edge at $E_0 = 284.8$ eV; run `nbs-cli diagnose`.")
        bl = MagicMock()
        cwd = os.getcwd()
        os.chdir(d)
        try:
            run_command(bl, build_parser().parse_args(["post", "--kind", "answer", "--file", "post.md"]))
        finally:
            os.chdir(cwd)
        bl.post.assert_called_once_with("answer", "Edge at $E_0 = 284.8$ eV; run `nbs-cli diagnose`.", runs=[])

    def test_post_takes_text_or_file_not_both(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["post", "--kind", "answer", "hi", "--file", "post.md"])
        args = build_parser().parse_args(["post", "--kind", "answer", "hi"])
        self.assertEqual((args.text, args.file), ("hi", None))

    def test_success_never_prints_bare_null(self):
        with patch("nbs_agents.cli.main.Beamline"), \
                patch("nbs_agents.cli.main.run_command", return_value=None), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            status = main(["--config", "/dev/null", "diagnose"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(out.getvalue())["result"], None)
        self.assertIn("nothing to report", out.getvalue())


class DeviceInfoTests(unittest.TestCase):
    INFO = {
        "manipulator": {"class": "Manipulator", "node": "es_chamber", "groups": ["manipulators"],
                        "roles": ["primary_sampleholder"], "desc": "4-axis sample manipulator"},
        "manipx": {"class": "FlyableMotor", "node": "es_chamber", "groups": ["motors"],
                   "alias_of": "manipulator.x"},
        "psh7": {"class": "EPS_Shutter", "node": "psh7", "groups": ["shutters"],
                 "roles": ["default_shutter"], "desc": "Endstation photon shutter."},
        "fe_shutter": {"class": "EPS_Shutter", "node": "front_end", "groups": ["shutters"],
                       "desc": "Front-end photon shutter."},
    }
    MAP = {
        "name": "claude",
        "prefix": "NBS_MAP:",
        "endstation": "es_chamber",
        "beam_expected": False,
        "reason": "psh7 blocked: NBS_SIM:psh7:Pos-Sts = 1",
        "updated": "2026-10-27T20:00:00",
        "nodes": {
            "front_end": {"upstream": None, "children": ["psh7"], "desc": "Front end", "devices": ["fe_shutter"],
                          "blocking": True, "path_clear": True, "beam_expected": False,
                          "reason": "front_end blocked: NBS_SIM:fe_shutter:Pos-Sts = 1"},
            "psh7": {"upstream": "front_end", "children": ["es_chamber"], "desc": "Shutter", "devices": ["psh7"],
                     "blocking": True, "path_clear": False, "beam_expected": False,
                     "reason": "psh7 blocked: NBS_SIM:psh7:Pos-Sts = 1"},
            "es_chamber": {"upstream": "psh7", "children": [], "desc": "Chamber",
                           "devices": ["manipulator", "manipx"],
                           "blocking": False, "path_clear": False, "beam_expected": False,
                           "reason": "psh7 blocked: NBS_SIM:psh7:Pos-Sts = 1"},
        },
    }
    ALLOWED = {
        "success": True,
        "devices_allowed": {
            "manipx": {"classname": "FlyableMotor", "is_movable": True, "is_readable": True},
            "psh7": {"classname": "EPS_Shutter", "is_movable": True, "is_readable": True},
            "fe_shutter": {"classname": "EPS_Shutter", "is_movable": True, "is_readable": True},
        },
    }

    def make(self, info=None, bmap=None):
        bl = Beamline(CliConfig())
        bl._info = StaticInfo(beam_map=bmap, device_info=info)
        bl._manager = MagicMock()
        bl._manager.devices_allowed.return_value = self.ALLOWED
        return bl

    def test_plain_reason_drops_pv_names(self):
        self.assertEqual(plain_reason("psh7 blocked: NBS_SIM:psh7:Pos-Sts = 1"), "psh7 blocked")
        self.assertEqual(plain_reason("branch_es not selected: XF:07ID1-OP{Mir:M3ABC-Ax:X}Mtr_MON = 7"),
                         "branch_es not selected")
        self.assertEqual(plain_reason("psh7: NBS_SIM:psh7:Pos-Sts disconnected"), "psh7: input disconnected")
        self.assertEqual(plain_reason("beam off: SIM:beam_available = 0"), "beam off")
        self.assertEqual(plain_reason(""), "")
        self.assertIsNone(plain_reason(None))

    def test_devices_have_node_and_desc(self):
        out = self.make(self.INFO).devices()
        self.assertEqual(out["psh7"]["node"], "psh7")
        self.assertEqual(out["psh7"]["desc"], "Endstation photon shutter.")
        self.assertEqual(out["manipx"]["node"], "es_chamber")
        self.assertIsNone(out["manipx"]["desc"])

    def test_devices_node_falls_back_to_beam_map(self):
        out = self.make(bmap=self.MAP).devices()
        self.assertEqual(out["fe_shutter"]["node"], "front_end")
        self.assertEqual(out["manipx"]["node"], "es_chamber")
        self.assertIsNone(out["psh7"]["desc"])

    def test_devices_filters(self):
        bl = self.make(self.INFO)
        self.assertEqual(list(bl.devices(node="es_chamber")), ["manipx"])
        self.assertEqual(list(bl.devices(role="default_shutter")), ["psh7"])
        self.assertEqual(bl.devices(node="nope"), {})

    def test_device_from_the_provider(self):
        bl = self.make(self.INFO)
        out = bl.device(["manipulator", "nope"])
        self.assertEqual(out["manipulator"]["roles"], ["primary_sampleholder"])
        self.assertEqual(out["nope"], {"error": "unknown device"})

    def test_device_without_device_information(self):
        bl = self.make()
        with self.assertRaises(CliError) as ctx:
            bl.device(["psh7"])
        self.assertIn("not available at this beamline", str(ctx.exception))

    def test_beam_map_lists_devices_per_node(self):
        out = self.make(bmap=self.MAP).beam(full=True)
        self.assertEqual(out["nodes"]["es_chamber"]["devices"], ["manipulator", "manipx"])
        self.assertEqual(out["nodes"]["psh7"]["reason"], "psh7 blocked")

    def test_diagnose_walks_upstream(self):
        out = self.make(self.INFO, self.MAP).diagnose()
        self.assertEqual([p["node"] for p in out["path"]], ["es_chamber", "psh7", "front_end"])
        self.assertEqual(out["blocking"], ["front_end", "psh7"])
        self.assertFalse(out["beam_expected"])
        self.assertEqual(out["reason"], "psh7 blocked")
        es, psh7, fe = out["path"]
        self.assertEqual(es["devices"], [
            {"name": "manipulator", "desc": "4-axis sample manipulator", "roles": ["primary_sampleholder"]},
        ])
        self.assertEqual(psh7["devices"][0]["roles"], ["default_shutter"])
        self.assertEqual(fe["reason"], "front_end blocked")
        self.assertTrue(fe["blocking"])
        self.assertEqual(set(es), {"node", "desc", "blocking", "path_clear", "beam_expected", "reason", "devices"})

    def test_diagnose_output_has_no_pv_names(self):
        import json

        text = json.dumps(self.make(self.INFO, self.MAP).diagnose())
        for pv in ("NBS_SIM", "NBS_MAP", "Pos-Sts"):
            self.assertNotIn(pv, text)

    def test_diagnose_without_device_info_uses_map_devices(self):
        out = self.make(bmap=self.MAP).diagnose()
        self.assertEqual([d["name"] for d in out["path"][0]["devices"]], ["manipulator", "manipx"])

    def test_diagnose_without_a_map(self):
        with self.assertRaises(CliError):
            self.make(self.INFO).diagnose()


class RunSaveTests(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        old = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, old)
        self.bl = MagicMock()
        self.bl.run_summary.return_value = {"scan_id": 7}
        self.bl.run_data.return_value = {"energy": np.array([280.0, 290.0])}

    def run_save(self, name):
        return run_command(self.bl, build_parser().parse_args(["run", "7", "--save", name]))

    def test_save_writes_data_to_a_file_not_stdout(self):
        import json
        from pathlib import Path

        out = self.run_save("data/scan7.json")
        self.assertNotIn("data", out)
        saved = Path(out["saved"])
        self.assertEqual(saved, Path(self.tmp.name).resolve() / "data" / "scan7.json")
        self.assertEqual(json.loads(saved.read_text()), {"scan_id": 7, "data": {"energy": [280.0, 290.0]}})

    def test_save_refuses_paths_outside_the_folder(self):
        for name in ("/tmp/x.json", "../x.json", "a/../../x.json", "."):
            with self.assertRaises(CliError, msg=name):
                self.run_save(name)


class FinalizeSummaryFileTests(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        old = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, old)
        self.bl = MagicMock()

    def finalize(self, *argv):
        return run_command(self.bl, build_parser().parse_args(["finalize", *argv]))

    def test_summary_file_is_read_from_the_folder(self):
        from pathlib import Path

        Path("notes").mkdir()
        Path("notes/summary.md").write_text("# Done\n\nTwo scans; $E_0$ = 284.2 eV.\n")
        self.finalize("--summary-file", "notes/summary.md", "--drop-pending")
        self.bl.finalize.assert_called_once_with(
            "# Done\n\nTwo scans; $E_0$ = 284.2 eV.\n", drop_pending=True, agent_uid=None
        )

    def test_summary_text_still_works(self):
        self.finalize("--summary", "done")
        self.bl.finalize.assert_called_once_with("done", drop_pending=False, agent_uid=None)

    def test_summary_file_refuses_paths_outside_the_folder(self):
        for name in ("/etc/hostname", "../x.md", "a/../../x.md", "."):
            with self.assertRaises(CliError, msg=name):
                self.finalize("--summary-file", name)
        self.bl.finalize.assert_not_called()

    def test_missing_summary_file(self):
        with self.assertRaises(CliError):
            self.finalize("--summary-file", "nope.md")
        self.bl.finalize.assert_not_called()

    def test_summary_and_summary_file_are_exclusive(self):
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            build_parser().parse_args(["finalize", "--summary", "x", "--summary-file", "s.md"])
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            build_parser().parse_args(["finalize"])
