"""Assistant role workers: the plotter (nbs-reduce without a model call), health and integrity."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from fakes import FakeRedis
from nbs_agents.agentlog import AgentLog, assistant_uid
from nbs_agents.agents.external import ExternalAgent
from nbs_agents.assistant.agent import AssistantAgent
from nbs_agents.assistant.attachments import AttachmentStore
from nbs_agents.assistant.workers import cache_key, health_tile, key_args, status_signature, weak_signals
from nbs_agents.cli.config import AssistantConfig, read_roles
from nbs_agents.tiles import TileStore

from test_assistant_agent import Clock, FakeLauncher, xas_run

ROLES = {
    "manager": {"mode": "chat"},
    "integrity": {"triggers": ["alert", "flag"], "worker": "integrity", "model": "opus"},
    "plotter": {"triggers": ["run", "group_end"], "worker": "plotter", "model": "haiku"},
    "health": {"triggers": ["beam"], "worker": "health", "model": "haiku"},
}
PNG = b"\x89PNG\r\n\x1a\nfake"


def summary_out(signal="sc", norm="i0", flags=(), problems=()):
    return {
        "run": {"uid": "r1"},
        "keys": {"signal": [signal], "norm": norm, "x": "en_energy",
                 "reasons": {"signal": "[reduce.defaults.c_xas] rule 1"}},
        "signals": {f"{signal}/{norm}": {"check": {"problems": list(problems)}, "edge_jump": 0.85,
                                         "jump_to_noise": 46.0, "flags": list(flags)}},
        "flags": list(flags),
    }


class FakeReduce:
    """Answers nbs-reduce commands from a table; writes a PNG for each plot."""

    def __init__(self, summary=None, status=0):
        self.summary = summary or summary_out()
        self.status = status
        self.plot_error = None
        self.calls = []

    def __call__(self, args, cwd):
        self.calls.append(args)
        if args[0] == "summary":
            return self.status, self.summary
        if args[0] == "plot":
            if self.plot_error:
                return 1, {"error": self.plot_error}
            out = args[args.index("-o") + 1]
            path = Path(cwd) / out
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(PNG + out.encode())
            return 0, {"file": str(path), "caption": f"caption for {out}"}
        return 1, {"error": "unexpected"}


class WorkerTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(time.time())
        self.log = AgentLog(FakeRedis(), prefix="bl-")
        self.beamline = MagicMock()
        self.beamline.info.xas_plans.return_value = {}
        self.beamline.beam.return_value = {"expected": True, "endstation": "es1", "reason": None}
        self.beamline.status_pvs.return_value = {"shutter": {"value": "Open"}, "ring_current": {"value": 400.2}}
        self.beamline.manager.status.return_value = {"manager_state": "executing_queue"}
        self.launchers = {name: FakeLauncher() for name in ROLES}
        self.workdir = tempfile.mkdtemp()
        config = AssistantConfig(enabled=True, workdir=self.workdir, model="sonnet", alert_model="opus",
                                 roles=read_roles(ROLES), reduce_wait_s=0)
        self.agent = AssistantAgent(config, self.beamline, None, self.log, signal="sc", norm="i0",
                                    clock=self.clock, launchers=self.launchers)
        self.agent.open_log()
        self.plotter = self.agent.plotters["plotter"]
        self.plotter.submit = self.plotter.handle  # no thread: plot at once
        self.plotter.attachments = AttachmentStore(tempfile.mkdtemp())
        self.reduce = FakeReduce()
        self.plotter.reduce = self.reduce
        self.tiles = TileStore.for_agent_log(self.log)

    def calls(self, name):
        return self.launchers[name].calls

    def record(self, role):
        return self.log.get(assistant_uid(self.clock.t, role)) or {}


class ConfigTests(unittest.TestCase):
    def test_worker_settings_are_checked(self):
        chat = {"manager": {"mode": "chat"}}
        bad = [
            ({"p": {"worker": "painter", "triggers": ["run"]}}, "worker must be one of"),
            ({"p": {"worker": "plotter", "triggers": ["run"]}}, r"needs triggers \['group_end'\]"),
            ({"h": {"worker": "health"}}, r"needs triggers \['beam'\]"),
            ({"h": {"worker": "health", "triggers": ["beam"], "sinks": ["record"]}}, "needs the tile sink"),
        ]
        for table, message in bad:
            with self.subTest(table=table), self.assertRaisesRegex(ValueError, message):
                read_roles({**chat, **table})
        with self.assertRaisesRegex(ValueError, "chat role has no worker"):
            read_roles({"manager": {"mode": "chat", "worker": "integrity"}})
        roles = AssistantConfig(roles=read_roles(ROLES)).resolved_roles()
        self.assertEqual(roles["plotter"].worker, "plotter")
        # beam is no default trigger: without a health role nothing takes it
        self.assertNotIn("beam", AssistantConfig().resolved_roles()["manager"].triggers)

    def test_helpers(self):
        self.assertEqual(key_args(None), [])
        self.assertEqual(key_args({"signal": ["a", "b"], "norm": None}), ["--signal", "a", "--signal", "b", "--norm", "none"])
        self.assertEqual(cache_key("c_xas", ["b", "a"]), cache_key("c_xas", ["a", "b"]))
        self.assertNotEqual(cache_key("c_xas", ["a"]), cache_key("c_xas", ["a", "b"]))
        self.assertEqual(weak_signals(summary_out(problems=["flat"])), ["sc/i0 (flat)"])
        self.assertEqual(weak_signals(summary_out(problems=["clipped"])), [])


class PlotterTests(WorkerTestCase):
    def test_a_run_is_plotted_without_a_model_call(self):
        self.agent.on_run_stop(xas_run("r1", 1))
        self.assertEqual(self.calls("plotter"), [])
        self.assertEqual(self.calls("manager"), [])
        verbs = [(c[0], c[c.index("--kind") + 1] if "--kind" in c else None) for c in self.reduce.calls]
        self.assertEqual(verbs, [("summary", None), ("plot", "normalized"), ("plot", "raw")])
        # The plots use the keys the summary settled, explicitly
        self.assertIn("--signal", self.reduce.calls[1])
        tile = self.tiles.get("plotter")
        self.assertEqual(tile["severity"], "ok")
        self.assertIn("scan 1 c_xas on s1: sc/i0; sc/i0 jump 0.85, J/N 46", tile["message"])
        self.assertIn("[reduce.defaults.c_xas] rule 1", tile["message"])
        self.assertTrue(tile["figure"].startswith("att:"))
        self.assertIsNotNone(self.plotter.attachments.path(tile["figure"][4:]))
        entries = self.record("plotter")["log"]
        summary = [e for e in entries if e["kind"] == "summary"][0]
        self.assertEqual(summary["text"].count("](att:"), 2)
        self.assertEqual(summary["run_uids"], ["r1"])

    def test_failed_plots_show_on_the_tile(self):
        self.reduce.plot_error = "FileNotFoundError: plots/scan1-normalized.png"
        self.agent.on_run_stop(xas_run("r1", 1))
        tile = self.tiles.get("plotter")
        self.assertEqual(tile["severity"], "warn")
        self.assertIsNone(tile["figure"])
        self.assertIn("normalized plot failed: FileNotFoundError", tile["message"])

    def test_the_overlay_holds_the_recent_runs_of_the_same_sample(self):
        for uid, n in (("r1", 1), ("r2", 2), ("r3", 3)):
            self.agent.on_run_stop(xas_run(uid, n))
        overlay = [c for c in self.reduce.calls if c[0] == "plot" and "normalized" in c][-1]
        self.assertEqual(overlay[1:4], ["r1", "r2", "r3"])
        self.agent.config.plot_runs = 2
        self.plotter.recent.clear()
        for uid, n in (("r4", 4), ("r5", 5), ("r6", 6)):
            self.agent.on_run_stop(xas_run(uid, n))
        overlay = [c for c in self.reduce.calls if c[0] == "plot" and "normalized" in c][-1]
        self.assertEqual(overlay[1:3], ["r5", "r6"])
        self.assertEqual(overlay[3], "--kind")

    def test_reducer_flags_go_to_the_integrity_role(self):
        self.reduce.summary = summary_out(flags=["sc/i0: inverted_edge"])
        self.agent.on_run_stop(xas_run("r1", 1))
        self.assertEqual(self.tiles.get("plotter")["severity"], "info")
        self.assertEqual(len(self.calls("integrity")), 1)
        prompt = self.calls("integrity")[0]["env"]["NBS_ASSISTANT_PROMPT"]
        self.assertIn("reducer flag sc/i0: inverted_edge", prompt)
        self.assertIn("differential diagnosis", prompt)
        self.assertIn("Do not read the driver's record", prompt)
        self.assertNotIn("## Driver sessions", prompt)
        self.assertEqual(self.calls("integrity")[0]["args"], ["--role", "assistant", "--model", "opus"])

    def test_ambiguous_keys_ask_the_model_once_then_use_its_choice(self):
        self.reduce.status = 3
        self.reduce.summary = {"ambiguous": True, "error": "two detectors", "candidates": {"signal": ["sc", "pey"]}}
        self.agent.on_run_stop(xas_run("r1", 1))
        self.assertEqual([c[0] for c in self.reduce.calls], ["summary"])
        self.assertEqual(len(self.calls("plotter")), 1)
        call = self.calls("plotter")[0]
        self.assertEqual(call["args"], ["--role", "assistant", "--model", "haiku"])
        prompt = call["env"]["NBS_ASSISTANT_PROMPT"]
        self.assertIn("[keys]", prompt)
        self.assertIn("two detectors", prompt)
        self.assertIn("nbs-reduce keys RUN", prompt)
        self.assertIn("could not settle", self.tiles.get("plotter")["message"])
        key = cache_key("c_xas", xas_run("x", 1).keys)
        choice_file = f"keys-choice/{key}.json"
        self.assertIn(choice_file, prompt)
        # The call writes its choice; the runner keeps it when the call ends
        path = Path(self.workdir + "-plotter") / choice_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"signal": "pey", "norm": "i0", "why": "sc is flat"}))
        self.launchers["plotter"].finish(0)
        self.assertFalse(path.exists())
        self.assertEqual(self.plotter.choice(key)["signal"], ["pey"])
        # The next such run is plotted at once with those keys
        self.reduce.status, self.reduce.summary = 0, summary_out(signal="pey")
        self.agent.on_run_stop(xas_run("r2", 2))
        self.assertEqual(len(self.calls("plotter")), 1)
        self.assertEqual(self.reduce.calls[1], ["summary", "r2", "--signal", "pey", "--norm", "i0"])

    def test_a_flat_signal_asks_for_keys_unless_a_choice_is_kept(self):
        self.reduce.summary = summary_out(problems=["flat"])
        self.agent.on_run_stop(xas_run("r1", 1))
        self.assertEqual(len(self.calls("plotter")), 1)
        self.assertIn("shows nothing: sc/i0 (flat)", self.calls("plotter")[0]["env"]["NBS_ASSISTANT_PROMPT"])
        self.launchers["plotter"].finish(0)
        self.plotter.remember(cache_key("c_xas", xas_run("x", 1).keys), {"signal": ["sc"], "norm": "i0"})
        self.agent.on_run_stop(xas_run("r2", 2))
        # A kept choice is used even when flat: the tile says so instead of calling again
        self.assertEqual(len(self.calls("plotter")), 1)
        self.assertIn("weak: sc/i0 (flat)", self.tiles.get("plotter")["message"])

    def test_a_run_tiled_lacks_is_tried_again_then_reported(self):
        self.agent.config.reduce_wait_s = 0.05
        sleeps = []
        self.plotter.sleep = lambda s: (sleeps.append(s), time.sleep(0.02))
        self.reduce.status, self.reduce.summary = 1, {"error": "no run r1 in catalog default"}
        self.agent.on_run_stop(xas_run("r1", 1))
        self.assertGreaterEqual(len(sleeps), 1)
        tile = self.tiles.get("plotter")
        self.assertEqual(tile["severity"], "warn")
        self.assertIn("no run r1", tile["message"])
        self.assertEqual(self.calls("plotter"), [])

    def test_a_group_end_overlays_the_group(self):
        self.agent.on_run_stop(xas_run("r1", 1))
        self.agent.on_run_stop(xas_run("r2", 2))
        self.agent.on_run_stop(xas_run("r3", 3, sample="s2"))
        group = [c for c in self.reduce.calls if c[0] == "plot" and any(a.startswith("plots/group-") for a in c)]
        self.assertEqual(len(group), 1)
        self.assertEqual(group[0][1:3], ["r1", "r2"])
        self.assertEqual(self.calls("manager"), [])
        group_tiles = [e for e in self.record("plotter")["log"] if e["kind"] == "tile" and "group finished" in e["text"]]
        self.assertEqual(len(group_tiles), 1)


class HealthTests(WorkerTestCase):
    def setUp(self):
        super().setUp()
        self.health = self.agent.health["health"]

    def test_tile_follows_the_beam_map_and_status_states(self):
        out = self.health.check()
        self.assertEqual(out["severity"], "ok")
        self.assertEqual(self.tiles.get("health")["message"], "Beam expected at es1")
        # The ring current is a number: it changes all the time and is not compared
        self.beamline.status_pvs.return_value = {"shutter": {"value": "Open"}, "ring_current": {"value": 399.0}}
        self.assertIsNone(self.health.check())
        self.assertEqual(self.calls("health"), [])

    def test_beam_loss_during_a_plan_alerts_and_asks_for_an_explanation(self):
        self.health.check()
        self.beamline.beam.return_value = {"expected": False, "endstation": "es1", "reason": "FE shutter closed"}
        self.beamline.status_pvs.return_value = {"shutter": {"value": "Closed"}}
        out = self.health.check()
        self.assertEqual(out["severity"], "alert")
        tile = self.tiles.get("health")
        self.assertIn("No beam expected at es1: FE shutter closed (a plan is running)", tile["message"])
        self.assertIn("shutter: Open -> Closed", tile["message"])
        self.assertEqual(len(self.calls("health")), 1)
        call = self.calls("health")[0]
        self.assertEqual(call["args"], ["--role", "assistant", "--model", "haiku"])
        self.assertIn("[beam]", call["env"]["NBS_ASSISTANT_PROMPT"])
        self.assertIn("already written your tile", call["env"]["NBS_ASSISTANT_PROMPT"])
        # Beam back: the sticky alert is cleared but held, and no call is made
        self.launchers["health"].finish(0)
        self.beamline.beam.return_value = {"expected": True, "endstation": "es1"}
        self.beamline.status_pvs.return_value = {"shutter": {"value": "Open"}}
        self.health.check()
        tile = self.tiles.get("health")
        self.assertEqual((tile["severity"], tile["cleared"]), ("alert", True))
        self.assertEqual(len(self.calls("health")), 1)

    def test_no_beam_while_idle_is_a_warning(self):
        self.beamline.manager.status.return_value = {"manager_state": "idle"}
        self.beamline.beam.return_value = {"expected": False, "endstation": "es1", "reason": "maintenance"}
        self.assertEqual(self.health.check()["severity"], "warn")
        self.assertEqual(len(self.calls("health")), 1)

    def test_runs_raise_no_beam_alert_when_a_health_role_has_it(self):
        self.beamline.beam.return_value = {"expected": False}
        self.agent.on_run_stop(xas_run("r1", 1))
        self.assertEqual(self.calls("integrity"), [])
        run = [e for e in self.log.get(assistant_uid(self.clock.t))["log"] if e["kind"] == "run"][0]
        self.assertIsNone(run.get("alerts"))

    def test_signature_and_tile_helpers(self):
        sig = status_signature(None, {"x": {"value": "not connected: X:PV"}})
        severity, message, changes = health_tile(sig, None, running=False)
        self.assertEqual(severity, "info")
        self.assertIn("No beam map", message)
        self.assertIn("not connected: x", message)
        self.assertEqual(changes, [])


class IntegrityTests(WorkerTestCase):
    def test_integrity_prompt_leaves_out_the_driver(self):
        agent = ExternalAgent.__new__(ExternalAgent)
        agent.agent_uid, agent.parameters = "drv1", {"goal": "map U6"}
        self.agent.on_agent_opened(agent)
        self.agent.on_run_stop(xas_run("bad", 1, status="fail"))
        prompt = self.calls("integrity")[0]["env"]["NBS_ASSISTANT_PROMPT"]
        self.assertNotIn("map U6", prompt)
        self.assertIn("at least three causes", prompt)


if __name__ == "__main__":
    unittest.main()
