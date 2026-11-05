"""The runner-hosted read-only assistant: run lines, triage, batching and timers."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np

from fakes import FakeRedis
from nbs_agents.agentlog import AgentLog, assistant_uid
from nbs_agents.assistant.agent import (
    AssistantAgent,
    Triage,
    prepare_workdir,
    run_facts,
    run_line,
)
from nbs_agents.catalog import ZMQRun
from nbs_agents.cli.config import AssistantConfig
from nbs_agents.assistant.launcher import ClaudeLauncher
from nbs_agents.assistant.resources import SCRIPTS_DIR
from nbs_agents.supervisor import AgentSupervisor



def facts(uid="r1", scan_id=1, sample="s1", edge="C", status="success", jtn=50.0, jump=1.0, i0=100.0, **extra):
    out = {
        "uid": uid, "scan_id": scan_id, "plan": "c_xas", "sample": sample, "edge": edge,
        "status": status, "reason": None, "kind": "xas", "points": 50, "edge_jump": jump,
        "jump_to_noise": jtn, "i0": i0, "non_finite": {}, "t": 0.0,
    }
    out.update(extra)
    return out


def xas_run(uid, scan_id, sample="s1", jump=2.0, noise=0.01, i0=100.0, status="success", plan="c_xas"):
    run = ZMQRun({
        "uid": uid, "scan_id": scan_id, "plan_name": plan, "scantype": "xas", "edge": "C",
        "sample_name": sample, "detectors": ["sc", "i0"],
        "hints": {"dimensions": [[["en_energy"], "primary"]]},
    })
    run.process_descriptor({"uid": f"d-{uid}", "run_start": uid, "name": "primary"})
    x = np.linspace(270, 310, 60)
    rng = np.random.default_rng(scan_id)
    y = (1 + jump / (1 + np.exp(-(x - 285))) + rng.normal(0, noise, x.size)) * i0
    for xi, yi in zip(x, y):
        run.process_event({"descriptor": f"d-{uid}", "data": {"en_energy": xi, "sc": yi, "i0": i0}})
    run.process_stop({"run_start": uid, "exit_status": status, "time": 0.0})
    return run


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeLauncher:
    def __init__(self):
        self.on_exit = None
        self.calls = []
        self.terminated = []

    def launch(self, agent_uid, goal, driver, extra_args=None, env=None):
        self.calls.append({"uid": agent_uid, "driver": driver, "args": extra_args, "env": env})
        return SimpleNamespace(pid=1000 + len(self.calls), log_path=f"/tmp/call{len(self.calls)}.log")

    def terminate(self, agent_uid):
        self.terminated.append(agent_uid)
        return True

    def finish(self, returncode=0):
        self.on_exit(SimpleNamespace(log_path="/tmp/x.log"), returncode)


class TriageTests(unittest.TestCase):
    def setUp(self):
        self.triage = Triage(AssistantConfig())

    def test_good_runs_are_quiet(self):
        out = self.triage.observe(facts(), beam_expected=True)
        self.assertEqual((out["alerts"], out["flags"], out["ended_group"]), ([], [], []))

    def test_alerts(self):
        out = self.triage.observe(facts(status="abort", reason="user", non_finite={"sc": 3}), beam_expected=False)
        self.assertEqual(len(out["alerts"]), 3)
        self.assertIn("run abort: user", out["alerts"][0])
        # Beam loss alerts once per outage
        self.assertEqual(self.triage.observe(facts(uid="r2"), beam_expected=False)["alerts"], [])
        self.triage.observe(facts(uid="r3"), beam_expected=True)
        self.assertEqual(len(self.triage.observe(facts(uid="r4"), beam_expected=False)["alerts"]), 1)

    def test_low_jump_to_noise(self):
        flags = self.triage.observe(facts(jtn=3.0))["flags"]
        self.assertEqual(len(flags), 1)
        self.assertIn("low jump to noise", flags[0])
        self.assertEqual(self.triage.observe(facts(jtn=3.0, kind="generic"))["flags"], [])

    def test_i0_drop_against_baseline(self):
        for i in range(3):
            self.assertEqual(self.triage.observe(facts(uid=f"r{i}", i0=100.0))["flags"], [])
        flags = self.triage.observe(facts(uid="low", i0=30.0))["flags"]
        self.assertTrue(any("i0 30" in f for f in flags), flags)

    def test_change_against_last_run_on_same_sample_and_edge(self):
        self.triage.observe(facts(uid="a", jump=1.0))
        self.triage.observe(facts(uid="b", sample="s2", jump=0.1))
        flags = self.triage.observe(facts(uid="c", jump=0.3))["flags"]
        self.assertTrue(any("changed 70%" in f for f in flags), flags)

    def test_groups_end_when_sample_changes(self):
        self.triage.observe(facts(uid="a"))
        self.triage.observe(facts(uid="b"))
        out = self.triage.observe(facts(uid="c", sample="s4"))
        self.assertEqual([r["uid"] for r in out["ended_group"]], ["a", "b"])
        self.assertEqual([r["uid"] for r in self.triage.end_group()], ["c"])
        self.assertEqual(self.triage.end_group(), [])


class RunLineTests(unittest.TestCase):
    def test_line_from_a_real_reduction(self):
        from nbs_agents.cli.summaries import SummaryContext, summarize

        run = xas_run("u1", 41)
        summary = summarize(run.start, {k: run.data(k) for k in run.keys}, SummaryContext("sc", "i0"))
        f = run_facts(run, summary, "i0")
        self.assertEqual((f["sample"], f["edge"], f["status"], f["i0"]), ("s1", "C", "success", 100.0))
        line = run_line(f)
        self.assertTrue(line.startswith("scan 41 c_xas success on s1, C edge, 60 pts, jump "), line)
        self.assertIn("J/N", line)
        self.assertIn("i0 100", line)

    def test_sample_id_is_preferred_like_nbs_cli(self):
        run = ZMQRun({"uid": "u", "plan_name": "c_xas", "sample_id": "STD1", "sample_name": "polystyrene_A"})
        run.process_stop({"run_start": "u", "exit_status": "success"})
        self.assertEqual(run_facts(run, {})["sample"], "STD1")


class AssistantAgentTests(unittest.TestCase):
    def make(self, **cfg):
        self.clock = Clock()
        self.log = AgentLog(FakeRedis(), prefix="bl-")
        self.beamline = MagicMock()
        self.beamline.info.xas_plans.return_value = {}
        self.beamline.beam.return_value = {"expected": True}
        self.beamline.manager.status.return_value = {"manager_state": "executing_queue"}
        self.launcher = FakeLauncher()
        config = AssistantConfig(enabled=True, workdir=tempfile.mkdtemp(), **cfg)
        agent = AssistantAgent(config, self.beamline, None, self.log, launcher=self.launcher,
                               signal="sc", norm="i0", clock=self.clock)
        agent.open_log()
        self.uid = assistant_uid(self.clock.t)
        return agent

    def record(self):
        return self.log.get(self.uid)

    def test_every_run_gets_a_line_and_good_runs_make_no_call(self):
        agent = self.make()
        agent.on_run_stop(xas_run("r1", 1))
        agent.on_run_stop(xas_run("r2", 2))
        runs = [e for e in self.record()["log"] if e["kind"] == "run"]
        self.assertEqual([e["run_uid"] for e in runs], ["r1", "r2"])
        self.assertEqual(runs[0]["source"], "assistant")
        self.assertEqual(self.launcher.calls, [])
        self.assertEqual(self.record()["kind"], "assistant")

    def test_sentinel_runs_are_skipped(self):
        agent = self.make()
        agent.on_run_stop(ZMQRun({"uid": "o", "plan_name": "open_agent"}))
        self.assertEqual(self.record()["log"], [])

    def test_alert_calls_at_once_with_the_strong_model(self):
        agent = self.make(model="haiku", alert_model="opus")
        agent.on_run_stop(xas_run("bad", 3, status="fail"))
        self.assertEqual(len(self.launcher.calls), 1)
        call = self.launcher.calls[0]
        self.assertEqual(call["args"], ["--role", "assistant", "--model", "opus"])
        self.assertEqual(call["env"]["NBS_CLI_ROLE"], "assistant")
        self.assertEqual(call["env"]["NBS_AGENT_UID"], "")
        prompt = call["env"]["NBS_ASSISTANT_PROMPT"]
        self.assertIn("[alert]", prompt)
        self.assertIn("bad", prompt)
        self.assertIn(self.uid, prompt)
        record = self.record()
        self.assertEqual(record["calls"], 1)
        self.assertEqual(record["status"], "calling")
        self.assertEqual(record["last_call"]["reasons"], ["alert"])
        self.assertEqual(record["last_call"]["run_uids"], ["bad"])
        run_entry = [e for e in record["log"] if e["kind"] == "run"][-1]
        self.assertIn("run fail", run_entry["alerts"][0])

    def test_prompt_carries_a_snapshot(self):
        agent = self.make()
        self.beamline.state.return_value = {"beam": {"expected": False, "reason": "s2 blocked"},
                                            "status": {"ring_status": {"value": "Maintenance"}}}
        notebook = Path(agent.config.workdir) / "notebook"
        notebook.mkdir(exist_ok=True)
        (notebook / "shift.md").write_text("# Shift\nCu L edge for Smith")
        agent.on_run_stop(xas_run("bad", 3, status="fail"))
        prompt = self.launcher.calls[0]["env"]["NBS_ASSISTANT_PROMPT"]
        self.assertIn('"reason":"s2 blocked"', prompt)
        self.assertIn('"Maintenance"', prompt)
        self.assertIn("Cu L edge for Smith", prompt)
        self.assertIn('"run_uid":"bad"', prompt)
        self.beamline.state.assert_called_with(full=False, agent_uid=None, n_runs=0)

    def test_snapshot_parts_are_cut_and_failures_noted(self):
        agent = self.make(snapshot_chars=50)
        self.beamline.state.side_effect = RuntimeError("queue server down")
        notebook = Path(agent.config.workdir) / "notebook"
        notebook.mkdir(exist_ok=True)
        (notebook / "samples.md").write_text("x" * 500)
        lines = agent.snapshot()
        self.assertIn("(unavailable: RuntimeError: queue server down)", lines)
        self.assertTrue(any(line.endswith("(cut; fetch it for the rest)") for line in lines))
        self.assertEqual(self.make(snapshot_chars=0).snapshot(), [])

    def test_events_during_a_call_are_batched_into_the_next(self):
        agent = self.make(model="haiku", alert_model="opus")
        agent.on_run_stop(xas_run("a", 1, status="fail"))
        agent.on_run_stop(xas_run("b", 2, jump=0.05, noise=0.05))  # weak: low J/N
        agent.on_run_stop(xas_run("c", 3, sample="s4", jump=0.05, noise=0.05))
        self.assertEqual(len(self.launcher.calls), 1)
        self.assertGreaterEqual(len(agent.pending), 2)
        self.launcher.finish(0)
        self.assertEqual(len(self.launcher.calls), 2)
        second = self.launcher.calls[1]
        self.assertEqual(second["args"][-1], "haiku")
        prompt = second["env"]["NBS_ASSISTANT_PROMPT"]
        self.assertIn("[flag]", prompt)
        self.assertIn("[group_end]", prompt)
        self.assertEqual(self.record()["calls"], 2)
        self.assertEqual(self.record()["last_call"]["n"], 2)
        self.launcher.finish(0)
        self.assertEqual(self.record()["status"], "watching")
        self.assertEqual(self.record()["last_call"]["returncode"], 0)

    def test_failed_call_is_logged(self):
        agent = self.make()
        agent.on_run_stop(xas_run("a", 1, status="fail"))
        self.launcher.finish(1)
        self.assertEqual(self.record()["log"][-1]["kind"], "error")

    def test_queue_idle_ends_the_group(self):
        agent = self.make()
        agent.tick()
        agent.on_run_stop(xas_run("a", 1))
        agent.on_run_stop(xas_run("b", 2))
        self.assertEqual(self.launcher.calls, [])
        self.beamline.manager.status.return_value = {"manager_state": "idle"}
        agent.tick()
        self.assertEqual(len(self.launcher.calls), 1)
        prompt = self.launcher.calls[0]["env"]["NBS_ASSISTANT_PROMPT"]
        self.assertIn("queue went idle", prompt)
        self.assertIn("a b", prompt)
        self.launcher.finish()
        agent.tick()
        self.assertEqual(len(self.launcher.calls), 1)

    def test_operator_question_calls_with_the_strong_model(self):
        agent = self.make(model="haiku", alert_model="opus")
        self.log.message(self.uid, "is s6 worth repeating?", source="gui")
        agent.tick()
        call = self.launcher.calls[0]
        self.assertEqual(call["args"][-1], "opus")
        self.assertIn("operator asked: is s6 worth repeating?", call["env"]["NBS_ASSISTANT_PROMPT"])
        self.launcher.finish()
        agent.tick()
        self.assertEqual(len(self.launcher.calls), 1)

    def test_question_call_that_posts_no_answer_is_flagged(self):
        agent = self.make()
        log_path = os.path.join(agent.config.workdir, "call.log")
        with open(log_path, "w") as f:
            f.write('{"type": "assistant"}\nnot json\n{"type": "result", "result": "L2 on gold is fine."}\n')
        self.log.message(self.uid, "which stripe at 6 keV?", source="gui")
        agent.tick()
        self.launcher.on_exit(SimpleNamespace(log_path=log_path), 0)
        last = self.record()["log"][-1]
        self.assertEqual(last["kind"], "error")
        self.assertIn("without posting an answer", last["text"])
        self.assertIn("L2 on gold is fine.", last["text"])

    def test_question_call_that_answers_is_not_flagged(self):
        agent = self.make()
        self.log.message(self.uid, "which stripe at 6 keV?", source="gui")
        agent.tick()
        self.log.append(self.uid, "answer", "gold", source="assistant")
        self.launcher.finish(0)
        self.assertEqual(self.record()["log"][-1]["kind"], "answer")

    def test_hourly_checkin_only_after_runs_and_restarts_on_posts(self):
        agent = self.make(checkin_s=3600)
        self.clock.t += 4000
        agent.tick()
        self.assertEqual(self.launcher.calls, [], "nothing ran, no check-in")
        agent.on_run_stop(xas_run("a", 1))
        agent.tick()
        self.assertEqual(len(self.launcher.calls), 1)
        self.assertIn("[checkin]", self.launcher.calls[0]["env"]["NBS_ASSISTANT_PROMPT"])
        self.launcher.finish()
        # The check-in itself restarts the timer
        self.clock.t += 1000
        agent.on_run_stop(xas_run("b", 2))
        agent.tick()
        self.assertEqual(len(self.launcher.calls), 1)
        # A post restarts it too
        self.clock.t += 3000
        with patch("nbs_agents.agentlog.time.time", return_value=self.clock.t):
            self.log.append(self.uid, "summary", "all fine", source="assistant")
        self.clock.t += 1000
        agent.on_run_stop(xas_run("c", 3))
        agent.tick()
        self.assertEqual(len(self.launcher.calls), 1)
        self.clock.t += 3000
        agent.tick()
        self.assertEqual(len(self.launcher.calls), 2)

    def test_call_timeout_terminates(self):
        agent = self.make(call_timeout_s=60)
        agent.on_run_stop(xas_run("a", 1, status="fail"))
        self.clock.t += 61
        agent.tick()
        self.assertEqual(self.launcher.terminated, [self.uid])
        self.launcher.finish(-15)
        self.assertIn("timed out", self.record()["log"][-1]["text"])

    def test_day_rollover_starts_a_new_record(self):
        agent = self.make()
        agent.on_run_stop(xas_run("a", 1))
        self.clock.t += 86400
        agent.tick()
        new = assistant_uid(self.clock.t)
        self.assertNotEqual(new, self.uid)
        self.assertEqual(self.log.get(self.uid)["status"], "closed")
        self.assertEqual(self.log.get(new)["kind"], "assistant")
        agent.on_run_stop(xas_run("b", 2))
        self.assertEqual(self.log.get(new)["log"][-1]["run_uid"], "b")

    def test_heartbeat(self):
        agent = self.make()
        first = self.record()["heartbeat"]
        self.clock.t += 31
        agent.tick()
        self.assertEqual(self.record()["heartbeat"], first + 31)
        self.assertEqual(self.record()["status"], "watching")


class SupervisorObserverTests(unittest.TestCase):
    def test_observers_see_every_run(self):
        observer = MagicMock()
        supervisor = AgentSupervisor(MagicMock(), MagicMock(), observers=[observer])
        run = ZMQRun({"uid": "r", "plan_name": "c_xas"})
        supervisor._handle_run_stop(run)
        observer.on_run_stop.assert_called_once_with(run)
        observer.on_run_stop.side_effect = RuntimeError("boom")
        supervisor._handle_run_stop(run)  # logged, not raised
        supervisor.shutdown()


class WorkdirTests(unittest.TestCase):
    def test_prepare_keeps_existing_notes(self):
        path = Path(tempfile.mkdtemp()) / "assistant"
        prepare_workdir(str(path))
        for name in ("shift.md", "samples.md", "advice.md"):
            self.assertTrue((path / "notebook" / name).is_file(), name)
        settings = json.loads((path / ".claude" / "settings.json").read_text())
        self.assertIn("Bash(nbs-cli post:*)", settings["permissions"]["allow"])
        self.assertIn("Bash(nbs-cli add:*)", settings["permissions"]["deny"])
        self.assertNotIn("Bash(nbs-cli:*)", settings["permissions"]["allow"])
        # The skill reaches the assistant as its system prompt, not as a skill
        self.assertFalse((path / ".claude" / "skills" / "beamline-assistant").exists())
        notes = path / "notebook" / "advice.md"
        notes.write_text("kept")
        prepare_workdir(str(path))
        self.assertEqual(notes.read_text(), "kept")


class LauncherArgsTests(unittest.TestCase):
    def test_extra_args_and_env_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fake"
            script.write_text('#!/bin/sh\necho "$@" > args.txt\necho "$NBS_AGENT_UID|$NBS_CLI_ROLE" > env.txt\n')
            script.chmod(0o755)
            launcher = ClaudeLauncher(str(script), f"{tmp}/work", f"{tmp}/logs")
            launched = launcher.launch(
                "assistant-20261028", None, "d", extra_args=["--role", "assistant"],
                env={"NBS_AGENT_UID": "", "NBS_CLI_ROLE": "assistant"},
            )
            launched.process.wait(10)
            self.assertEqual((Path(tmp) / "work" / "args.txt").read_text().split(), ["--headless", "--role", "assistant"])
            self.assertEqual((Path(tmp) / "work" / "env.txt").read_text().strip(), "|assistant")


class ScriptRoleTests(unittest.TestCase):
    def run_script(self, *args, env=None):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "claude"
            fake.write_text(
                "#!/usr/bin/env python3\nimport json, os, sys\n"
                "json.dump({'argv': sys.argv[1:], 'role': os.environ.get('NBS_CLI_ROLE'),"
                " 'uid': os.environ.get('NBS_AGENT_UID')}, sys.stdout)\n"
            )
            fake.chmod(0o755)
            nbs = Path(tmp) / "nbs-cli"
            nbs.write_text("#!/bin/sh\n")
            nbs.chmod(0o755)
            # A fake claude on PATH, run directly rather than in bwclaude
            full_env = dict(os.environ, PATH=f"{tmp}:{os.environ['PATH']}", NBS_CLAUDE_SANDBOX="none",
                            NBS_ENV_DIR=sys.prefix, **(env or {}))
            out = subprocess.run(
                [str(SCRIPTS_DIR / "claude-beamline"), *args], env=full_env,
                capture_output=True, text=True, check=True, cwd=tmp,
            )
            return json.loads(out.stdout)

    def test_bwclaude_gets_resolved_kb_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "profile" / "knowledge").mkdir(parents=True)
            (tmp / "link").symlink_to(tmp / "profile")
            config = tmp / "nbs-cli.toml"
            config.write_text(f'kb_dir = "{tmp}/link/knowledge"\n')
            fake = tmp / "bwclaude"
            fake.write_text("#!/usr/bin/env python3\nimport json, sys\njson.dump(sys.argv[1:], sys.stdout)\n")
            fake.chmod(0o755)
            env = dict(
                os.environ, NBS_BWCLAUDE=str(fake), NBS_CLAUDE_SANDBOX="bwclaude", NBS_CLI_CONFIG=str(config),
                NBS_AGENTS_HOME=str(tmp / "home"), AIFAPIM_HOST="gw", AIFAPIM_API_KEY="k", NBS_ENV_DIR=sys.prefix,
            )
            for name in ("NBS_KB_DIR", "NBS_KB_AGENT_DIR", "NBS_KB_REVIEW_DIR", "NBS_ATTACHMENTS_DIR"):
                env.pop(name, None)
            out = subprocess.run(
                [str(SCRIPTS_DIR / "claude-beamline"), "--headless", "--role", "assistant"],
                env=env, capture_output=True, text=True, check=True, cwd=tmp,
            )
            argv = json.loads(out.stdout)
            real = str((tmp / "profile" / "knowledge").resolve())
            self.assertIn(real, argv)
            self.assertIn(f"NBS_KB_DIR={real}", argv)
            self.assertNotIn(f"{tmp}/link/knowledge", argv)

    def test_inner_run_in_bwclaude_gets_its_environment_back(self):
        # A bwclaude that runs the inner command as the real one does: with a clean environment
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fake = tmp / "bwclaude"
            fake.write_text(
                "#!/usr/bin/env python3\nimport os, sys\n"
                "argv = sys.argv[1:]\ncmd = argv[argv.index('--exec') + 1:]\n"
                f"os.execve(cmd[0], cmd, {{'PATH': '{tmp}:/usr/bin:/bin', 'HOME': '{tmp}'}})\n"
            )
            fake.chmod(0o755)
            claude = tmp / "claude"
            claude.write_text(
                "#!/usr/bin/env python3\nimport json, os, sys\n"
                "json.dump({'argv': sys.argv[1:], 'env_dir': os.environ.get('NBS_ENV_DIR')}, sys.stdout)\n"
            )
            claude.chmod(0o755)
            config = tmp / "nbs-cli.toml"
            config.write_text("[nbs_cli]\n")
            env = dict(
                os.environ, PATH=f"{tmp}:{os.environ['PATH']}", NBS_BWCLAUDE=str(fake), NBS_CLAUDE_SANDBOX="bwclaude",
                NBS_CLI_CONFIG=str(config), NBS_AGENTS_HOME=str(tmp / "home"), AIFAPIM_HOST="gw", AIFAPIM_API_KEY="k", NBS_ENV_DIR=sys.prefix,
                NBS_ASSISTANT_PROMPT="events",
            )
            out = subprocess.run(
                [str(SCRIPTS_DIR / "claude-beamline"), "--headless", "--role", "assistant"],
                env=env, capture_output=True, text=True, cwd=tmp,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            result = json.loads(out.stdout)
            self.assertEqual(result["env_dir"], sys.prefix)
            prompt = result["argv"][result["argv"].index("--append-system-prompt-file") + 1]
            self.assertTrue(Path(prompt).read_text().startswith("---\nname: beamline-assistant"))

    def test_assistant_role(self):
        out = self.run_script(
            "--headless", "--role", "assistant", "--model", "haiku",
            env={"NBS_ASSISTANT_PROMPT": "events here", "NBS_AGENT_UID": "driver-uid"},
        )
        argv = out["argv"]
        self.assertEqual(argv[:2], ["-p", "events here"])
        self.assertEqual(out["role"], "assistant")
        self.assertIsNone(out["uid"])
        self.assertIn("Bash(nbs-cli post:*)", argv)
        self.assertIn("Bash(nbs-cli state:*)", argv)
        self.assertIn("Edit(./notebook/**)", argv)
        self.assertNotIn("Bash(nbs-cli:*)", argv)
        self.assertEqual(argv[-2:], ["--model", "haiku"])

    def test_driver_role_unchanged(self):
        out = self.run_script("--headless", env={"NBS_AGENT_UID": "u1"})
        self.assertIn("Bash(nbs-cli:*)", out["argv"])
        self.assertIn("Agent session u1 is already open", out["argv"][1])
        self.assertIsNone(out["role"])


if __name__ == "__main__":
    unittest.main()
