import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fakes import FakeRedis
from nbs_agents.agentlog import AgentLog

from nbs_agents.agents.repeat import RepeatAgent
from nbs_agents.launcher import ClaudeLauncher, LaunchedAgent, claude_launch_parameters
from nbs_agents.catalog import ZMQRun
from nbs_agents.constants import AGENT_NAME_KEY, AGENT_PARAMETERS_KEY, AGENT_UID_KEY
from nbs_agents.supervisor import AgentSupervisor, default_registry


class SupervisorTests(unittest.TestCase):
    def test_default_registry_contains_repeat(self):
        registry = default_registry()
        self.assertEqual(registry.get("repeat"), RepeatAgent)

    def test_on_run_stop_dispatches_to_worker(self):
        manager = MagicMock()
        catalog = MagicMock()
        supervisor = AgentSupervisor(manager, catalog)
        run = ZMQRun({"uid": "run-1", "plan_name": "count"})

        with patch.object(supervisor._executor, "submit") as submit_mock:
            supervisor._on_run_stop(run)
            submit_mock.assert_called_once_with(supervisor._handle_run_stop, run)

        supervisor.shutdown()

    def test_activate_agent_starts_repeat_agent(self):
        manager = MagicMock()
        manager.queue_get.return_value = {
            "success": True,
            "items": [
                {"item_type": "instruction", "name": "queue_stop", "item_uid": "stop"},
                {
                    "item_type": "plan",
                    "name": "close_agent",
                    "item_uid": "close",
                    "args": [],
                },
            ],
        }
        manager.item_get.return_value = {
            "success": True,
            "item": {
                "item_type": "plan",
                "name": "close_agent",
                "item_uid": "close",
                "args": [],
                "kwargs": {},
            },
        }
        manager.item_update.return_value = {"success": True}
        manager.item_add.return_value = {"success": True}

        catalog = MagicMock()
        supervisor = AgentSupervisor(manager, catalog)

        run = ZMQRun(
            {
                "uid": "run-1",
                "plan_name": "open_agent",
                AGENT_UID_KEY: "uid-1",
                AGENT_NAME_KEY: "repeat",
                AGENT_PARAMETERS_KEY: {"repeat_count": 1, "plan_name": "count", "plan_args": [[]], "plan_kwargs": {"num": 1}},
            }
        )
        run.process_stop({"exit_status": "success", "run_start": "run-1"})

        with patch.object(RepeatAgent, "start") as start_mock:
            supervisor._activate_agent(run)
            start_mock.assert_called_once()
            self.assertIn("uid-1", supervisor.active_agents)


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
        self.terminated = []

    def launch(self, agent_uid, goal, driver):
        if self.fail:
            raise self.fail
        self.launched.append((agent_uid, goal, driver))
        process = SimpleNamespace(pid=4242)
        return LaunchedAgent(agent_uid, process, ["claude-beamline", "--headless"], "/w", "/logs/c.log")

    def terminate(self, agent_uid):
        self.terminated.append(agent_uid)
        return True


class ClaudeLaunchTests(unittest.TestCase):
    def make(self, launcher):
        self.log = AgentLog(FakeRedis(), "bl-")
        return AgentSupervisor(_sentinel_manager(), MagicMock(), agent_log=self.log, launcher=launcher)

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
        self.assertEqual(launcher.on_exit, supervisor._on_launched_exit)
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
        supervisor._on_launched_exit(launched, -15)
        entry = self.log.get("uid-12345678")["log"][-1]
        self.assertEqual((entry["kind"], entry["exit_status"]), ("launch", -15))
        supervisor.shutdown()

    def test_unexpected_exit_while_active_is_an_error(self):
        supervisor = self.make(FakeLauncher())
        supervisor._activate_agent(_open_run({"goal": "g", "launch": True}))
        launched = LaunchedAgent("uid-12345678", SimpleNamespace(pid=4242), [], "/w", "/logs/c.log")
        supervisor._on_launched_exit(launched, 1)
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


class RepeatAgentTests(unittest.TestCase):
    def test_repeat_agent_finalizes_after_target_count(self):
        manager = MagicMock()
        manager.queue_get.return_value = {
            "success": True,
            "items": [
                {"item_type": "instruction", "name": "queue_stop", "item_uid": "stop"},
                {
                    "item_type": "plan",
                    "name": "close_agent",
                    "item_uid": "close",
                    "args": ["uid-1"],
                },
            ],
        }
        manager.item_add.return_value = {"success": True}
        manager.queue_stop_cancel.return_value = {"success": True}
        manager.queue_start.return_value = {"success": True}
        manager.wait_for_idle.return_value = None

        catalog = MagicMock()
        open_run = ZMQRun({"uid": "open", "plan_name": "open_agent", AGENT_UID_KEY: "uid-1"})
        agent = RepeatAgent(
            agent_uid="uid-1",
            agent_name="repeat",
            parameters={"repeat_count": 1, "plan_name": "count", "plan_args": [[]], "plan_kwargs": {"num": 1}},
            manager=manager,
            catalog=catalog,
            open_run=open_run,
        )

        agent.start()
        self.assertTrue(manager.item_add.called)

        completed = ZMQRun(
            {
                "uid": "done",
                "plan_name": "count",
                AGENT_UID_KEY: "uid-1",
            }
        )
        completed.process_stop({"exit_status": "success", "run_start": "done"})
        agent.on_run_stop(completed)

        self.assertFalse(agent.active)
        manager.wait_for_idle.assert_called_once()
        manager.queue_stop_cancel.assert_called_once()
        manager.queue_start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
