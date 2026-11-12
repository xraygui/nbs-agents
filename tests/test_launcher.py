import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fakes import FakeRedis
from nbs_agents.agentlog import AgentLog

from nbs_agents.assistant.launcher import ClaudeLaunchHook, ClaudeLauncher, LaunchedAgent, claude_launch_parameters
from nbs_agents.catalog import ZMQRun
from nbs_agents.constants import AGENT_NAME_KEY, AGENT_PARAMETERS_KEY, AGENT_UID_KEY
from nbs_agents.supervisor import AgentSupervisor, default_registry


def _sentinel_manager():
    manager = MagicMock()
    manager.queue_get.return_value = {
        "success": True,
        "items": [
            {"item_type": "instruction", "name": "queue_stop", "item_uid": "stop"},
            {"item_type": "plan", "name": "close_agent", "item_uid": "close", "args": []},
        ],
    }
    manager.item_get.return_value = {
        "success": True,
        "item": {"item_type": "plan", "name": "close_agent", "item_uid": "close", "args": [], "kwargs": {}},
    }
    manager.item_update.return_value = {"success": True}
    return manager


def _open_run(parameters, name="claude", uid="uid-12345678"):
    run = ZMQRun(
        {
            "uid": "run-1",
            "plan_name": "open_agent",
            AGENT_UID_KEY: uid,
            AGENT_NAME_KEY: name,
            AGENT_PARAMETERS_KEY: parameters,
        }
    )
    run.process_stop({"exit_status": "success", "run_start": "run-1"})
    return run


class FakeLauncher:
    def __init__(self, fail=None):
        self.fail = fail
        self.on_exit = None
        self.launched = []
        self.envs = []
        self.terminated = []

    def launch(self, agent_uid, goal, driver, env=None):
        if self.fail:
            raise self.fail
        self.launched.append((agent_uid, goal, driver))
        self.envs.append(env)
        process = SimpleNamespace(pid=4242)
        return LaunchedAgent(agent_uid, process, ["claude-beamline", "--headless"], "/w", "/logs/c.log")

    def terminate(self, agent_uid):
        self.terminated.append(agent_uid)
        return True


class ClaudeLaunchTests(unittest.TestCase):
    def make(self, launcher):
        self.log = AgentLog(FakeRedis(), "bl-")
        self.hook = ClaudeLaunchHook(launcher, self.log)
        return AgentSupervisor(_sentinel_manager(), MagicMock(), agent_log=self.log, observers=[self.hook])

    def test_launches_claude_with_goal_and_driver(self):
        launcher = FakeLauncher()
        supervisor = self.make(launcher)
        supervisor._activate_agent(_open_run({"goal": "map C", "launch": True}))
        self.assertEqual(launcher.launched, [("uid-12345678", "map C", "runner-launch-uid-1234")])
        record = self.log.get("uid-12345678")
        self.assertEqual(record["driver"], "runner-launch-uid-1234")
        self.assertEqual(record["launch"]["pid"], 4242)
        self.assertEqual(record["launch"]["log_path"], "/logs/c.log")
        self.assertEqual(record["status"], "active")
        entry = record["log"][-1]
        self.assertEqual((entry["kind"], entry["pid"]), ("launch", 4242))
        self.assertEqual(launcher.on_exit, self.hook._on_exit)
        supervisor.shutdown()

    def test_launch_carries_the_owning_runner(self):
        launcher = FakeLauncher()
        supervisor = self.make(launcher)
        supervisor._activate_agent(_open_run({"goal": "g", "launch": True}))
        self.assertEqual(launcher.envs, [{"NBS_AGENT_OWNER": supervisor.owner["id"]}])
        self.assertEqual(self.log.get("uid-12345678")["runner"], supervisor.owner)
        supervisor.shutdown()

    def test_no_launch_for_a_block_another_runner_owns(self):
        launcher = FakeLauncher()
        supervisor = self.make(launcher)
        other = {"id": "other-runner", "host": "elsewhere", "pid": 1, "started": 0}
        supervisor.manager.item_get.return_value["item"]["meta"] = {"agent_owner": other}
        supervisor._activate_agent(_open_run({"goal": "g", "launch": True}))
        self.assertEqual(launcher.launched, [])
        self.assertNotIn("uid-12345678", supervisor.active_agents)
        supervisor.manager.item_update.assert_not_called()
        supervisor.shutdown()

    def test_no_launch_without_flag_or_for_other_agents(self):
        launcher = FakeLauncher()
        supervisor = self.make(launcher)
        supervisor._activate_agent(_open_run({"goal": "by hand"}))
        supervisor._activate_agent(_open_run({"launch": True}, name="external", uid="uid-2"))
        self.assertEqual(launcher.launched, [])
        supervisor.shutdown()

    def test_close_terminates_child_and_logs_exit(self):
        launcher = FakeLauncher()
        supervisor = self.make(launcher)
        supervisor._activate_agent(_open_run({"goal": "g", "launch": True}))
        close = ZMQRun({"uid": "run-2", "plan_name": "close_agent", AGENT_UID_KEY: "uid-12345678"})
        supervisor._deactivate_agent_from_close(close)
        self.assertEqual(launcher.terminated, ["uid-12345678"])
        self.assertNotIn("uid-12345678", supervisor.active_agents)
        launched = LaunchedAgent("uid-12345678", SimpleNamespace(pid=4242), [], "/w", "/logs/c.log")
        self.hook._on_exit(launched, -15)
        entry = self.log.get("uid-12345678")["log"][-1]
        self.assertEqual((entry["kind"], entry["exit_status"]), ("launch", -15))
        supervisor.shutdown()

    def test_close_after_a_runner_restart_closes_the_session(self):
        supervisor = self.make(FakeLauncher())
        self.log.open("uid-orphan", "claude", goal="g")
        close = ZMQRun({"uid": "run-2", "plan_name": "close_agent", AGENT_UID_KEY: "uid-orphan"})
        supervisor._deactivate_agent_from_close(close)
        record = self.log.get("uid-orphan")
        self.assertEqual(record["status"], "closed")
        self.assertIn("restart", record["close_reason"])
        supervisor.shutdown()

    def test_restarted_runner_adopts_a_queued_session_and_relaunches(self):
        launcher = FakeLauncher()
        supervisor = self.make(launcher)
        self.log.open("uid-12345678", "claude", goal="g", parameters={"goal": "g", "launch": True})
        old = {"id": "old", "host": "elsewhere", "pid": 1, "started": 0}
        self.log.update("uid-12345678", launch={"pid": 999999}, runner=old)
        close = supervisor.manager.queue_get.return_value["items"][1]
        close["args"] = ["uid-12345678"]
        supervisor.manager.item_get.return_value["item"]["meta"] = {"agent_owner": old}
        self.assertEqual(supervisor.adopt_open_sessions(), ["uid-12345678"])
        self.assertIn("uid-12345678", supervisor.active_agents)
        self.assertEqual(launcher.launched[0][0], "uid-12345678")
        self.assertEqual(launcher.envs, [{"NBS_AGENT_OWNER": supervisor.owner["id"]}])
        updated = supervisor.manager.item_update.call_args.args[0]
        self.assertEqual(updated["meta"]["agent_owner"]["id"], supervisor.owner["id"])
        record = self.log.get("uid-12345678")
        self.assertEqual(record["runner"], supervisor.owner)
        self.assertIn("adopt", [e["kind"] for e in record["log"]])
        supervisor.shutdown()

    def test_restarted_runner_closes_sessions_it_cannot_take_over(self):
        launcher = FakeLauncher()
        supervisor = self.make(launcher)
        self.log.open("uid-gone", "claude", goal="g", parameters={"launch": True})
        self.log.open("uid-repeat", "repeat", goal="g")
        supervisor.manager.queue_get.return_value["items"][1]["args"] = ["uid-repeat"]
        self.assertEqual(supervisor.adopt_open_sessions(), [])
        self.assertEqual(self.log.get("uid-gone")["status"], "closed")
        self.assertIn("no longer queued", self.log.get("uid-gone")["close_reason"])
        self.assertIn("cannot resume", self.log.get("uid-repeat")["close_reason"])
        self.assertEqual(launcher.launched, [])
        supervisor.shutdown()

    def test_runner_carries_out_a_stop_the_driver_ignores(self):
        supervisor = self.make(FakeLauncher())
        manager = supervisor.manager
        supervisor._activate_agent(_open_run({"goal": "g", "launch": True}))
        items = [
            {"item_type": "plan", "name": "c_xas", "item_uid": "p1", "meta": {AGENT_UID_KEY: "uid-12345678"}},
            {"item_type": "instruction", "name": "queue_stop", "item_uid": "stop"},
            {"item_type": "plan", "name": "close_agent", "item_uid": "close", "args": ["uid-12345678"]},
        ]
        manager.queue_get.return_value = {"success": True, "items": items}
        manager.item_remove.return_value = {"success": True}
        manager.queue_start.return_value = {"success": True}
        manager.status.return_value = {"manager_state": "executing_queue", "worker_environment_exists": True}
        self.log.request_stop("uid-12345678", "enough")
        t = self.log.get("uid-12345678")["stop_requested"]["t"]

        supervisor.check_stops(now=t + 10)  # the driver still has time
        manager.item_remove.assert_not_called()
        self.assertEqual(self.log.get("uid-12345678")["status"], "active")

        supervisor.check_stops(now=t + supervisor.STOP_GRACE_S)
        removed = [c.kwargs["uid"] for c in manager.item_remove.call_args_list]
        self.assertEqual(removed, ["p1", "stop"])
        self.assertEqual(self.log.get("uid-12345678")["status"], "finalizing")
        manager.queue_start.assert_not_called()  # busy

        items[:] = items[2:]
        manager.status.return_value = {"manager_state": "idle", "worker_environment_exists": True}
        supervisor.check_stops(now=t + supervisor.STOP_GRACE_S + 10)
        manager.queue_start.assert_called_once()
        supervisor.shutdown()

    def test_unexpected_exit_while_active_is_an_error(self):
        supervisor = self.make(FakeLauncher())
        supervisor._activate_agent(_open_run({"goal": "g", "launch": True}))
        launched = LaunchedAgent("uid-12345678", SimpleNamespace(pid=4242), [], "/w", "/logs/c.log")
        self.hook._on_exit(launched, 1)
        self.assertEqual(self.log.get("uid-12345678")["log"][-1]["kind"], "error")
        supervisor.shutdown()

    def test_launch_failure_is_logged_not_raised(self):
        supervisor = self.make(FakeLauncher(fail=FileNotFoundError("no claude-beamline")))
        supervisor._activate_agent(_open_run({"goal": "g", "launch": True}))
        entry = self.log.get("uid-12345678")["log"][-1]
        self.assertEqual(entry["kind"], "error")
        self.assertIn("no claude-beamline", entry["text"])
        self.assertIn("uid-12345678", supervisor.active_agents)
        supervisor.shutdown()

    def test_launch_without_launcher_is_logged(self):
        supervisor = self.make(None)
        supervisor._activate_agent(_open_run({"goal": "g", "launch": True}))
        entry = self.log.get("uid-12345678")["log"][-1]
        self.assertEqual(entry["kind"], "error")
        self.assertIn("claude_command", entry["text"])
        supervisor.shutdown()


class ClaudeLauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.script = os.path.join(self.tmp.name, "fake-claude")
        with open(self.script, "w") as f:
            f.write(
                f"#!{sys.executable}\n"
                "import os, sys, time\n"
                "print(sys.argv[1:], os.getcwd(), os.environ['NBS_AGENT_UID'],"
                " os.environ['NBS_AGENT_GOAL'], os.environ['NBS_DRIVER'], flush=True)\n"
                "if os.environ.get('SLEEP'): time.sleep(60)\n"
            )
        os.chmod(self.script, 0o755)
        self.exits = []

    def launcher(self, **kw):
        launcher = ClaudeLauncher(
            f"{self.script} --extra",
            os.path.join(self.tmp.name, "work", "{uid8}"),
            os.path.join(self.tmp.name, "logs"),
            on_exit=lambda launched, code: self.exits.append(code),
            **kw,
        )
        return launcher

    def test_stop_leftover_stops_only_that_session_s_claude(self):
        import subprocess

        def spawn(uid):
            env = dict(os.environ, NBS_AGENT_UID=uid)
            return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], env=env, start_new_session=True)

        ours, other = spawn("uid-1"), spawn("uid-2")
        self.addCleanup(other.kill)
        self.addCleanup(ours.kill)
        launcher = self.launcher(stop_timeout_s=1)
        self.assertFalse(launcher.stop_leftover(other.pid, "uid-1"))
        self.assertTrue(launcher.stop_leftover(ours.pid, "uid-1"))
        self.assertIsNotNone(ours.wait(timeout=5))
        self.assertIsNone(other.poll())
        self.assertFalse(launcher.stop_leftover(99999999, "uid-1"))

    def wait_for_exit(self, n=1, timeout=10):
        deadline = time.time() + timeout
        while len(self.exits) < n and time.time() < deadline:
            time.sleep(0.05)

    def test_launch_runs_headless_with_env_and_log(self):
        launcher = self.launcher()
        launched = launcher.launch("abcdef0123456", "map C", "drv")
        self.wait_for_exit()
        self.assertEqual(self.exits, [0])
        self.assertEqual(launched.workdir, os.path.join(self.tmp.name, "work", "abcdef01"))
        with open(launched.log_path) as f:
            out = f.read()
        self.assertIn("['--headless', '--extra']", out)
        self.assertIn(launched.workdir, out)
        self.assertIn("abcdef0123456 map C drv", out)
        self.assertNotIn("abcdef0123456", launcher.children)

    def test_terminate_stops_child(self):
        launcher = self.launcher(stop_timeout_s=0.5)
        with patch.dict(os.environ, {"SLEEP": "1"}):
            launched = launcher.launch("u2", "g", "drv")
        self.assertRaises(RuntimeError, launcher.launch, "u2", "g", "drv")
        self.assertTrue(launcher.terminate("u2"))
        self.wait_for_exit()
        self.assertEqual(self.exits, [-15])
        self.assertIsNotNone(launched.process.poll())
        self.assertFalse(launcher.terminate("u2"))

    def test_missing_command_raises(self):
        launcher = ClaudeLauncher("no-such-claude-xyz", self.tmp.name, self.tmp.name)
        self.assertRaises(FileNotFoundError, launcher.launch, "u3", "g", "drv")

    def test_from_config_and_parameters(self):
        from nbs_agents.cli.config import CliConfig

        self.assertIsNone(ClaudeLauncher.from_config(CliConfig(claude_command="")))
        launcher = ClaudeLauncher.from_config(CliConfig())
        self.assertTrue(launcher.resolve_command()[0].endswith("claude-beamline"))
        self.assertEqual(
            claude_launch_parameters(" g ", "90m"), {"goal": "g", "launch": True, "budget": {"time_s": 5400.0}}
        )
        self.assertRaises(ValueError, claude_launch_parameters, "  ")
        self.assertRaises(ValueError, claude_launch_parameters, "g", "later")
