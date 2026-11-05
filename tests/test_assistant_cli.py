"""The read-only assistant role in nbs-cli and the driver-side firewall."""

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fakes import FakeRedis
from nbs_agents.agentlog import AgentLog, assistant_uid, is_assistant_record
from nbs_agents.cli.config import AssistantConfig, CliConfig
from nbs_agents.cli.core import Beamline, CliError
from nbs_agents.cli.main import build_parser, error_text, main, run_command
from nbs_agents.constants import CLOSE_AGENT_PLAN, QUEUE_STOP_INSTRUCTION


def _queue(uid):
    return [
        {"item_type": "plan", "name": "c_xas", "item_uid": "a1", "meta": {"agent_uid": uid}},
        {"item_type": "instruction", "name": QUEUE_STOP_INSTRUCTION, "item_uid": "qs"},
        {"item_type": "plan", "name": CLOSE_AGENT_PLAN, "item_uid": "ca", "args": [uid]},
    ]


def make(role="driver"):
    bl = Beamline(CliConfig(role=role))
    bl._redis = FakeRedis()
    bl.agent_log.open("u1", "claude", goal="g")
    bl.agent_log.ensure_assistant_record()
    bl.agent_log.message(assistant_uid(), "is s4 ok?", source="gui")
    manager = MagicMock()
    manager.queue_get.return_value = {"success": True, "items": _queue("u1"), "running_item": {}}
    manager.status.return_value = {"manager_state": "idle", "worker_environment_exists": True}
    manager.plans_allowed.return_value = {"success": True, "plans_allowed": {"c_xas": {"parameters": []}}}
    bl._manager = manager
    return bl, manager


def run(bl, *argv):
    return run_command(bl, build_parser().parse_args(list(argv)))


class AssistantRecordTests(unittest.TestCase):
    def test_daily_uid_and_detection(self):
        self.assertRegex(assistant_uid(), r"^assistant-\d{8}$")
        log = AgentLog(FakeRedis())
        uid = log.ensure_assistant_record()
        record = log.get(uid)
        self.assertEqual(record["kind"], "assistant")
        self.assertEqual(record["calls"], 0)
        self.assertTrue(is_assistant_record(record, uid))
        self.assertTrue(is_assistant_record(None, "assistant-20261028"))
        self.assertFalse(is_assistant_record({"agent_name": "claude"}, "u1"))
        # A second call keeps the record
        log.append(uid, "advice", "x")
        log.ensure_assistant_record()
        self.assertEqual(len(log.get(uid)["log"]), 1)


class ReadOnlyRoleTests(unittest.TestCase):
    WRITES = [
        ["open", "--goal", "g"],
        ["add", "c_xas", "--reason", "r"],
        ["add", "c_xas", "--reason", "r", "--dry-run"],
        ["remove", "a1", "--reason", "r"],
        ["abort", "--reason", "r"],
        ["finalize", "--summary", "s"],
        ["note", "hello"],
        ["ask", "why?"],
        ["tell", "do it"],
        ["stop"],
    ]

    def test_write_verbs_are_refused(self):
        bl, manager = make("assistant")
        before = {k: v for k, v in bl.redis.store.items()}
        for argv in self.WRITES:
            with self.subTest(argv=argv):
                with self.assertRaises(CliError) as ctx:
                    run(bl, *argv)
                self.assertIn("read-only assistant", str(ctx.exception))
        self.assertEqual(bl.redis.store, before)
        for name in ("item_add", "item_add_batch", "item_remove", "queue_start", "re_abort", "queue_stop"):
            getattr(manager, name).assert_not_called()

    def test_queue_start_is_refused(self):
        bl, manager = make("assistant")
        with self.assertRaises(CliError):
            bl._maybe_start("u1")
        manager.queue_start.assert_not_called()

    def test_manager_uses_the_assistant_group_but_reads_plans_with_the_main_group(self):
        bl = Beamline(CliConfig(role="assistant", qs_user_group="claude", assistant_user_group="assistant"))
        with patch("bluesky_queueserver_api.zmq.REManagerAPI") as api:
            manager = bl.manager
        self.assertEqual(manager.user_group, "assistant")
        api.return_value.plans_allowed.return_value = {"success": True, "plans_allowed": {}}
        bl.plans_allowed()
        self.assertEqual(api.return_value.plans_allowed.call_args.kwargs["user_group"], "claude")

    def test_post_writes_only_to_todays_assistant_record(self):
        bl, manager = make("assistant")
        out = run(bl, "post", "--kind", "advice", "s4 looks displaced", "--runs", "r1", "r2")
        uid = assistant_uid()
        self.assertEqual(out["agent_uid"], uid)
        entry = bl.agent_log.get(uid)["log"][-1]
        self.assertEqual((entry["kind"], entry["text"], entry["run_uids"]), ("advice", "s4 looks displaced", ["r1", "r2"]))
        self.assertEqual(entry["source"], "assistant")
        self.assertEqual(len(bl.agent_log.get("u1")["log"]), 0)
        # --agent-uid does not redirect a post
        run(bl, "--agent-uid", "u1", "post", "--kind", "alert", "x")
        self.assertEqual(len(bl.agent_log.get("u1")["log"]), 0)

    def test_ask_assistant_adds_an_operator_question(self):
        bl, _ = make("driver")
        out = run(bl, "ask-assistant", "Why is i0 low?")
        self.assertEqual(out["agent_uid"], assistant_uid())
        entry = bl.agent_log.get(assistant_uid())["log"][-1]
        self.assertEqual((entry["kind"], entry["text"], entry["source"]), ("operator", "Why is i0 low?", "nbs-cli"))
        abl, _ = make("assistant")
        with self.assertRaises(CliError):
            run(abl, "ask-assistant", "x")

    def test_post_validates(self):
        bl, _ = make("assistant")
        with self.assertRaises(CliError):
            bl.post("note", "x")
        with self.assertRaises(CliError):
            bl.post("advice", "  ")
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["post", "--kind", "note", "x"])

    def test_post_creates_the_record_if_missing(self):
        bl = Beamline(CliConfig(role="assistant"))
        bl._redis = FakeRedis()
        bl.post("summary", "quiet hour")
        record = bl.agent_log.get(assistant_uid())
        self.assertEqual(record["kind"], "assistant")
        self.assertEqual(record["log"][-1]["kind"], "summary")

    def test_driver_cannot_post(self):
        bl, _ = make("driver")
        with self.assertRaises(CliError):
            run(bl, "post", "--kind", "advice", "x")

    def test_assistant_reads_without_taking_driver_messages(self):
        bl, manager = make("assistant")
        bl.agent_log.message("u1", "skip s3")
        bl.status_dict = lambda name: {}
        bl.samples = lambda full=False: {}
        bl.recent_runs = lambda *a, **k: []
        out = run(bl, "state")
        self.assertEqual(out["agent"]["agent_uid"], "u1")
        self.assertNotIn("operator", out)
        self.assertNotIn("operator_read", bl.agent_log.get("u1"))
        self.assertFalse(bl.is_driver(bl.agent_log.get("u1")))

    def test_assistant_log_shows_its_own_record_and_all(self):
        bl, _ = make("assistant")
        own = run(bl, "log")
        self.assertEqual(list(own), [assistant_uid()])
        self.assertEqual(set(run(bl, "log", "--all")), {"u1", assistant_uid()})

    def test_role_flag_and_env(self):
        with patch.dict(os.environ, {"NBS_CLI_ROLE": "assistant"}):
            self.assertTrue(CliConfig.load("/dev/null").read_only)
        with patch.dict(os.environ, {"NBS_CLI_ROLE": "boss"}):
            with self.assertRaises(ValueError):
                CliConfig.load("/dev/null")
        args = build_parser().parse_args(["--role", "assistant", "devices", "--role", "detector"])
        self.assertEqual((args.cli_role, args.role), ("assistant", "detector"))
        # devices keeps its own --role filter
        args = build_parser().parse_args(["devices", "--role", "detector"])
        self.assertEqual((args.role, args.command), ("detector", "devices"))

    def test_role_flag_cannot_lift_assistant_env(self):
        with patch.dict(os.environ, {"NBS_CLI_ROLE": "assistant"}), patch("sys.stdout") as out:
            with patch("nbs_agents.cli.main.Beamline") as beamline:
                status = main(["--config", "/dev/null", "--role", "driver", "note", "x"])
        self.assertEqual(status, 1)
        beamline.assert_not_called()
        self.assertIn("cannot lift", "".join(c.args[0] for c in out.write.call_args_list))


class DriverFirewallTests(unittest.TestCase):
    def test_log_all_skips_assistant_records(self):
        bl, _ = make("driver")
        self.assertEqual(set(run(bl, "log", "--all")), {"u1"})

    def test_driver_cannot_address_the_assistant_record(self):
        bl, _ = make("driver")
        for argv in (["log"], ["tell", "hi"], ["note", "hi"]):
            with self.subTest(argv=argv):
                with self.assertRaises(CliError):
                    run(bl, "--agent-uid", assistant_uid(), *argv)
        bl.status_dict = lambda name: {}
        bl.samples = lambda full=False: {}
        bl.recent_runs = lambda *a, **k: []
        out = run(bl, "--agent-uid", assistant_uid(), "state")
        self.assertIsNone(out["agent"])
        self.assertIn("agent", out["errors"])
        self.assertEqual(len(bl.agent_log.get(assistant_uid())["log"]), 1)

    def test_driver_never_picks_the_assistant_as_its_session(self):
        bl = Beamline(CliConfig(agent_name="assistant"))
        bl._redis = FakeRedis()
        bl.agent_log.ensure_assistant_record(status="active")
        self.assertIsNone(bl.find_session())

    def test_driver_state_has_no_assistant_messages(self):
        bl, _ = make("driver")
        bl.status_dict = lambda name: {}
        bl.samples = lambda full=False: {}
        bl.recent_runs = lambda *a, **k: []
        out = run(bl, "state", "--full")
        self.assertNotIn("is s4 ok?", str(out))
        self.assertNotIn("assistant-", str(out))


class AssistantConfigTests(unittest.TestCase):
    def test_table_is_read_and_ignored_by_cli_config(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write('[nbs_cli]\nqs_user_group = "claude"\n\n[assistant]\nenabled = true\nmodel = "haiku"\n')
        try:
            self.assertEqual(CliConfig.load(f.name).qs_user_group, "claude")
            cfg = AssistantConfig.load(f.name)
            self.assertTrue(cfg.enabled)
            self.assertEqual(cfg.model, "haiku")
            self.assertEqual(cfg.alert_model, "opus")
        finally:
            os.unlink(f.name)

    def test_defaults_disabled_and_unknown_keys_rejected(self):
        self.assertFalse(AssistantConfig.load("/dev/null").enabled)
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write("[assistant]\nenable = true\n")
        try:
            with self.assertRaises(ValueError):
                AssistantConfig.load(f.name)
        finally:
            os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()


class ConnectionErrorTextTests(unittest.TestCase):
    def config(self):
        from nbs_agents.config import AgentLogConfig

        return CliConfig(redis_host="bl-redis", redis_port=6380, agent_log=AgentLogConfig(port=60740))

    def refused(self, address="localhost:60740"):
        from redis.exceptions import ConnectionError as RedisConnectionError

        return RedisConnectionError(f"Error 111 connecting to {address}. Connection refused.")

    def test_names_the_service_from_its_address(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SANDBOX_RUNTIME", None)
            text = error_text(self.refused(), self.config())
            self.assertIn("(localhost:60740 is the agent log, served by the runner)", text)
            self.assertNotIn("sandbox", text)
            self.assertIn("(bl-redis:6380 is the beamline Redis)", error_text(self.refused("bl-redis:6380"), self.config()))

    def test_says_when_the_call_ran_in_claudes_sandbox(self):
        with patch.dict(os.environ, {"SANDBOX_RUNTIME": "1"}):
            text = error_text(self.refused(), self.config())
        self.assertIn("inside Claude Code's Bash sandbox", text)
        self.assertIn("backticks", text)

    def test_other_errors_are_unchanged(self):
        with patch.dict(os.environ, {"SANDBOX_RUNTIME": "1"}):
            self.assertEqual(error_text(ValueError("bad"), self.config()), "ValueError: bad")
