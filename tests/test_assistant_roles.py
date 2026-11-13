"""Assistant roles: config, event routing, tiles with sticky alerts, role-scoped nbs-cli, driver_active."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fakes import FakeRedis
from nbs_agents.agentlog import AgentLog, assistant_uid
from nbs_agents.agents.external import ExternalAgent
from nbs_agents.assistant.agent import AssistantAgent
from nbs_agents.assistant.claude_runner import start_assistant
from nbs_agents.cli.config import AssistantConfig, CliConfig, RoleConfig, check_cli_role, read_roles
from nbs_agents.cli.core import Beamline, CliError, parse_since
from nbs_agents.cli.main import build_parser, main, run_command
from nbs_agents.tiles import TileError, TileStore, acked_tile, acknowledge, next_tile

import test_assistant_agent as base
from test_assistant_agent import Clock, FakeLauncher, xas_run

ROLES = {
    "manager": {"mode": "chat"},
    "integrity": {"triggers": ["alert", "flag"], "model": "opus", "skill": "beamline-reduce"},
    "plotter": {"triggers": ["run"], "model": "haiku", "sinks": ["tile"]},
}


def write_config(text):
    f = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
    f.write(text)
    f.close()
    return f.name


class RoleConfigTests(unittest.TestCase):
    def test_no_roles_is_todays_single_assistant(self):
        cfg = AssistantConfig(model="haiku", alert_model="opus", workdir="/w/assistant")
        roles = cfg.resolved_roles()
        self.assertEqual(list(roles), ["manager"])
        role = roles["manager"]
        self.assertTrue(role.chat)
        self.assertEqual((role.model, role.alert_model, role.workdir), ("haiku", "opus", "/w/assistant"))
        self.assertEqual(role.triggers, ["alert", "flag", "group_end", "checkin"])
        self.assertEqual(role.sinks, ["record"])
        self.assertEqual(role.skill, "beamline-assistant")

    def test_roles_table_is_read_with_defaults(self):
        path = write_config(
            '[assistant]\nenabled = true\nworkdir = "/w/assistant"\nmodel = "sonnet"\n'
            '[assistant.roles.manager]\nmode = "chat"\n'
            '[assistant.roles.integrity]\ntriggers = ["alert", "flag"]\nmodel = "opus"\n'
            '[assistant.roles.plotter]\ntriggers = ["run"]\nsinks = ["tile"]\n'
        )
        self.addCleanup(os.unlink, path)
        roles = AssistantConfig.load(path).resolved_roles()
        self.assertEqual(list(roles), ["manager", "integrity", "plotter"])
        # The chat role takes the default triggers no headless role took
        self.assertEqual(roles["manager"].triggers, ["group_end", "checkin"])
        self.assertEqual(roles["integrity"].sinks, ["tile", "record"])
        self.assertEqual(roles["integrity"].workdir, "/w/assistant-integrity")
        self.assertEqual(roles["integrity"].model, "opus")
        self.assertEqual(roles["plotter"].model, "sonnet")
        self.assertEqual(roles["plotter"].sinks, ["tile"])

    def test_overlapping_triggers_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "integrity and health both have trigger 'alert'"):
            read_roles({
                "manager": {"mode": "chat"},
                "integrity": {"triggers": ["alert", "flag"]},
                "health": {"triggers": ["alert"]},
            })
        # The chat role's own triggers count too
        with self.assertRaisesRegex(ValueError, "both have trigger 'run'"):
            read_roles({"manager": {"mode": "chat", "triggers": ["run"]}, "plotter": {"triggers": ["run"]}})

    def test_exactly_one_chat_role(self):
        with self.assertRaisesRegex(ValueError, "exactly one role"):
            read_roles({"a": {"triggers": ["run"]}})
        with self.assertRaisesRegex(ValueError, "exactly one role"):
            read_roles({"a": {"mode": "chat"}, "b": {"mode": "chat"}})

    def test_bad_settings_are_rejected(self):
        bad = [
            ({"Manager": {"mode": "chat"}}, "role names"),
            ({"m": {"mode": "chat", "colour": "red"}}, "Unknown"),
            ({"m": {"mode": "loud"}}, "mode must be"),
            ({"m": {"mode": "chat"}, "h": {"triggers": ["question"]}}, "always go to the chat role"),
            ({"m": {"mode": "chat"}, "h": {"sinks": ["slack"]}}, "unknown sinks"),
            ({"m": {"mode": "chat"}, "h": {"triggers": "run"}}, "list of names"),
        ]
        for table, message in bad:
            with self.subTest(table=table), self.assertRaisesRegex(ValueError, message):
                read_roles(table)

    def test_cli_roles(self):
        for role in ("driver", "assistant", "assistant:integrity"):
            check_cli_role(role)
        for role in ("assistant:", "assistant:Bad", "health"):
            with self.subTest(role=role), self.assertRaises(ValueError):
                check_cli_role(role)
        config = CliConfig(role="assistant:integrity")
        self.assertTrue(config.read_only)
        self.assertTrue(config.is_assistant)
        self.assertEqual(config.assistant_role, "integrity")
        self.assertIsNone(CliConfig(role="assistant").assistant_role)

    def test_runner_refuses_a_role_whose_skill_is_missing(self):
        path = write_config(
            '[assistant]\nenabled = true\n[assistant.roles.manager]\nmode = "chat"\n'
            '[assistant.roles.health]\nskill = "no-such-skill"\n'
        )
        self.addCleanup(os.unlink, path)
        with patch.dict(os.environ, {"NBS_CLI_CONFIG": path}):
            self.assertIsNone(start_assistant(CliConfig(), None, AgentLog(FakeRedis())))

    def test_runner_refuses_overlapping_roles(self):
        path = write_config(
            '[assistant]\nenabled = true\n[assistant.roles.manager]\nmode = "chat"\n'
            '[assistant.roles.a]\ntriggers = ["flag"]\n[assistant.roles.b]\ntriggers = ["flag"]\n'
        )
        self.addCleanup(os.unlink, path)
        with patch.dict(os.environ, {"NBS_CLI_CONFIG": path}):
            self.assertIsNone(start_assistant(CliConfig(), None, AgentLog(FakeRedis())))


class RoutingTests(unittest.TestCase):
    def setUp(self):
        # Today, so nbs-cli (real time) and the runner name the same records
        self.clock = Clock(time.time())
        self.log = AgentLog(FakeRedis(), prefix="bl-")
        self.beamline = MagicMock()
        self.beamline.info.xas_plans.return_value = {}
        self.beamline.beam.return_value = {"expected": True}
        self.beamline.manager.status.return_value = {"manager_state": "executing_queue"}
        self.launchers = {name: FakeLauncher() for name in ROLES}
        workdir = tempfile.mkdtemp()
        config = AssistantConfig(
            enabled=True, workdir=workdir, model="sonnet", alert_model="opus",
            roles=read_roles(ROLES),
        )
        self.agent = AssistantAgent(config, self.beamline, None, self.log, signal="sc", norm="i0",
                                    clock=self.clock, launchers=self.launchers)
        self.agent.open_log()
        self.main = assistant_uid(self.clock.t)

    def calls(self, name):
        return self.launchers[name].calls

    def test_each_role_has_its_own_record(self):
        integrity = self.log.get(assistant_uid(self.clock.t, "integrity"))
        self.assertEqual(integrity["role"], "integrity")
        self.assertEqual(integrity["role_config"]["triggers"], ["alert", "flag"])
        main = self.log.get(self.main)
        self.assertEqual(set(main["roles"]), set(ROLES))
        self.assertEqual(main["roles"]["plotter"]["record"], assistant_uid(self.clock.t, "plotter"))

    def test_events_go_to_exactly_one_role(self):
        self.agent.on_run_stop(xas_run("bad", 3, status="fail"))
        # The plotter takes every run, integrity the alert; the manager neither
        self.assertEqual(len(self.calls("plotter")), 1)
        self.assertEqual(len(self.calls("integrity")), 1)
        self.assertEqual(self.calls("manager"), [])
        call = self.calls("integrity")[0]
        self.assertEqual(call["uid"], assistant_uid(self.clock.t, "integrity"))
        self.assertEqual(call["env"]["NBS_CLI_ROLE"], "assistant:integrity")
        self.assertEqual(call["env"]["NBS_ASSISTANT_SKILL"], "beamline-reduce")
        self.assertEqual(call["args"], ["--role", "assistant", "--model", "opus"])
        prompt = call["env"]["NBS_ASSISTANT_PROMPT"]
        self.assertIn("[alert]", prompt)
        self.assertNotIn("[run]", prompt)
        self.assertIn("integrity role", prompt)
        self.assertIn("post --tile", prompt)
        plot = self.calls("plotter")[0]
        self.assertIn("[run]", plot["env"]["NBS_ASSISTANT_PROMPT"])
        self.assertEqual(plot["args"][-1], "haiku")
        # The run line stays in the main record
        self.assertEqual([e["run_uid"] for e in self.log.get(self.main)["log"] if e["kind"] == "run"], ["bad"])

    def test_question_goes_to_the_chat_role(self):
        self.log.message(self.main, "is s4 ok?", source="gui")
        self.agent.tick()
        self.assertEqual(len(self.calls("manager")), 1)
        self.assertEqual(self.calls("manager")[0]["env"]["NBS_CLI_ROLE"], "assistant")
        self.assertEqual(self.calls("integrity"), [])

    def test_one_call_at_a_time_per_role_with_batching(self):
        self.agent.on_run_stop(xas_run("r1", 1))
        self.agent.on_run_stop(xas_run("r2", 2))
        self.assertEqual(len(self.calls("plotter")), 1, "the second run waits for the first call")
        self.assertEqual([e["kind"] for e in self.agent.roles["plotter"].pending], ["run"])
        self.launchers["plotter"].finish()
        self.assertEqual(len(self.calls("plotter")), 2)
        self.assertIn("r2", self.calls("plotter")[1]["env"]["NBS_ASSISTANT_PROMPT"])

    def test_events_no_role_takes_are_dropped(self):
        roles = {"manager": {"mode": "chat", "triggers": ["checkin"]}}
        config = AssistantConfig(enabled=True, workdir=tempfile.mkdtemp(), roles=read_roles(roles))
        launcher = FakeLauncher()
        agent = AssistantAgent(config, self.beamline, None, self.log, clock=self.clock, launcher=launcher)
        agent.open_log()
        agent.on_run_stop(xas_run("bad", 3, status="fail"))
        self.assertEqual(launcher.calls, [])
        self.assertEqual(agent.pending, [])

    def test_request_reaches_the_role_and_its_answer_comes_back(self):
        bl = Beamline(CliConfig(role="assistant"))
        bl._agent_log = self.log
        bl._assistant = self.agent.config
        out = bl.role_request("integrity", "re-check run 41", runs=["r41"])
        role_uid = assistant_uid(self.clock.t, "integrity")
        self.assertEqual(out["entry"]["kind"], "request")
        self.agent.tick()
        call = self.calls("integrity")[0]
        self.assertIn("[request]", call["env"]["NBS_ASSISTANT_PROMPT"])
        self.assertIn("re-check run 41", call["env"]["NBS_ASSISTANT_PROMPT"])
        self.assertIn("r41", call["env"]["NBS_ASSISTANT_PROMPT"])
        # Taken once
        self.agent.tick()
        self.assertEqual(len(self.calls("integrity")), 1)
        # The role answers, and the runner passes it back to the manager
        self.log.append(role_uid, "answer", "run 41 is charged", source="assistant:integrity", call=1)
        self.launchers["integrity"].finish()
        reply = [e for e in self.log.get(self.main)["log"] if e["kind"] == "reply"][-1]
        self.assertEqual(reply["role"], "integrity")
        self.assertEqual(reply["text"], "run 41 is charged")
        self.assertEqual(reply["request_t"], out["entry"]["t"])
        self.assertEqual(len(self.calls("manager")), 1)
        self.assertIn("[reply]", self.calls("manager")[0]["env"]["NBS_ASSISTANT_PROMPT"])

    def test_request_without_answer_still_replies(self):
        bl = Beamline(CliConfig(role="assistant"))
        bl._agent_log = self.log
        bl._assistant = self.agent.config
        bl.role_request("integrity", "anything odd?")
        self.agent.tick()
        self.launchers["integrity"].finish()
        reply = [e for e in self.log.get(self.main)["log"] if e["kind"] == "reply"][-1]
        self.assertIn("ended without answering", reply["text"])

    def test_role_notebook_is_kept_in_its_record(self):
        notebook = Path(self.agent.roles["integrity"].role.workdir) / "notebook"
        notebook.mkdir(parents=True)
        (notebook / "flags.md").write_text("U6 charges")
        self.agent.on_run_stop(xas_run("bad", 3, status="fail"))
        self.launchers["integrity"].finish()
        record = self.log.get(assistant_uid(self.clock.t, "integrity"))
        self.assertIn("U6 charges", record["notebook"])

    def test_day_rollover_moves_every_role(self):
        self.clock.t += 86400
        self.agent.tick()
        self.assertEqual(self.agent.roles["integrity"].uid, assistant_uid(self.clock.t, "integrity"))
        self.assertIsNotNone(self.log.get(assistant_uid(self.clock.t, "integrity")))


class DriverActiveTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.log = AgentLog(FakeRedis(), prefix="bl-")
        self.beamline = MagicMock()
        self.beamline.info.xas_plans.return_value = {}
        self.beamline.beam.return_value = {"expected": True}
        self.beamline.state.return_value = {}
        config = AssistantConfig(enabled=True, workdir=tempfile.mkdtemp())
        self.agent = AssistantAgent(config, self.beamline, None, self.log, signal="sc", norm="i0", clock=self.clock)
        self.agent.open_log()

    def driver(self, uid="d1"):
        agent = MagicMock(spec=ExternalAgent)
        agent.agent_uid = uid
        agent.parameters = {"goal": "measure U6"}
        return agent

    def test_open_and_close_of_a_driver_block(self):
        self.assertFalse(self.agent.driver_active)
        self.agent.on_agent_opened(MagicMock(agent_uid="r1"))  # not an ExternalAgent
        self.assertFalse(self.agent.driver_active)
        driver = self.driver()
        self.agent.on_agent_opened(driver)
        self.assertTrue(self.agent.driver_active)
        self.assertTrue(self.agent.triage.driver_active)
        state = self.agent.driver_state()
        self.assertEqual(state["drivers"][0]["record"], "bl-AGENTd1")
        self.assertEqual(state["drivers"][0]["goal"], "measure U6")
        self.assertTrue(self.log.get(assistant_uid(self.clock.t))["driver_active"])
        self.assertIn("Driver sessions", "\n".join(self.agent.snapshot()))
        self.agent.on_agent_closed(driver)
        self.assertFalse(self.agent.driver_active)
        self.assertFalse(self.log.get(assistant_uid(self.clock.t))["driver_active"])

    def test_adopted_driver_counts(self):
        self.agent.on_agent_adopted(self.driver("d2"), {})
        self.assertTrue(self.agent.driver_active)

    def test_runs_of_the_driver_are_marked(self):
        self.agent.on_agent_opened(self.driver("d1"))
        run = xas_run("r1", 1)
        run.start["agent_uid"] = "d1"
        self.agent.on_run_stop(run)
        self.agent.on_run_stop(xas_run("r2", 2))
        runs = [e for e in self.log.get(assistant_uid(self.clock.t))["log"] if e["kind"] == "run"]
        self.assertEqual([e.get("driver_run") for e in runs], [True, None])


class TileTests(unittest.TestCase):
    def post(self, severity, message="m", t=1.0):
        return {"role": "health", "severity": severity, "message": message, "ts": t, "run_uids": [], "figure": None,
                "model": None}

    def test_later_ok_clears_but_keeps_the_alert(self):
        tile = next_tile(None, self.post("alert", "beam lost"))
        tile = next_tile(tile, self.post("ok", "beam back", 2.0))
        self.assertEqual((tile["severity"], tile["message"], tile["cleared"]), ("alert", "beam lost", True))
        self.assertEqual(tile["latest"]["message"], "beam back")

    def test_ack_of_a_cleared_alert_shows_the_latest_post(self):
        tile = next_tile(next_tile(None, self.post("alert", "beam lost")), self.post("info", "beam back", 2.0))
        tile = acked_tile(tile, "jamie", 3.0)
        self.assertEqual((tile["severity"], tile["message"], tile["cleared"]), ("info", "beam back", False))
        self.assertEqual(tile["last_ack"], {"by": "jamie", "ts": 3.0, "message": "beam lost"})

    def test_ack_of_an_uncleared_alert_keeps_it_until_the_next_post(self):
        tile = acked_tile(next_tile(None, self.post("alert", "beam lost")), "jamie", 3.0)
        self.assertEqual(tile["severity"], "alert")
        self.assertEqual(tile["ack"], {"by": "jamie", "ts": 3.0})
        tile = next_tile(tile, self.post("ok", "fine", 4.0))
        self.assertEqual((tile["severity"], tile["message"]), ("ok", "fine"))
        self.assertEqual(tile["last_ack"]["by"], "jamie")

    def test_new_alert_replaces_and_nothing_else_needs_ack(self):
        tile = next_tile(None, self.post("alert", "one"))
        tile = next_tile(tile, self.post("alert", "two"))
        self.assertEqual((tile["message"], tile["cleared"]), ("two", False))
        with self.assertRaises(TileError):
            acked_tile(next_tile(None, self.post("warn")), "jamie", 1.0)
        with self.assertRaises(TileError):
            acked_tile(None, "jamie", 1.0)

    def test_store_and_logged_acknowledgement(self):
        log = AgentLog(FakeRedis(), prefix="bl-")
        store = TileStore.for_agent_log(log)
        with self.assertRaises(TileError):
            store.post("health", "fine", "x")
        store.post("health", "alert", "ring dumped", run_uids=["r1"])
        store.post("health", "ok", "beam back")
        self.assertTrue(store.get("health")["cleared"])
        self.assertEqual(set(store.all()), {"health"})
        out = acknowledge(log, "health", "jamie", source="web")
        self.assertEqual(out["tile"]["message"], "beam back")
        entry = log.get(assistant_uid(role="health"))["log"][-1]
        self.assertEqual((entry["kind"], entry["by"], entry["source"]), ("ack", "jamie", "web"))
        self.assertIn("ring dumped", entry["text"])
        self.assertIn("had cleared", entry["text"])
        self.assertEqual(entry["run_uids"], ["r1"])
        # Records and tiles stay apart
        self.assertNotIn("tile:health", "".join(log.records()))


def cli(role="driver", roles=ROLES):
    bl = Beamline(CliConfig(role=role))
    bl._agent_log = AgentLog(FakeRedis())
    bl._assistant = AssistantConfig(enabled=True, workdir="/w/a", roles=read_roles(roles))
    return bl


def run(bl, *argv):
    return run_command(bl, build_parser().parse_args(list(argv)))


class RoleCliTests(unittest.TestCase):
    def test_headless_role_posts_to_its_own_tile_and_record(self):
        bl = cli("assistant:integrity")
        out = run(bl, "post", "--tile", "--severity", "alert", "U6 charging", "--runs", "r6", "--figure", "att:abc")
        uid = assistant_uid(role="integrity")
        self.assertEqual(out["agent_uid"], uid)
        self.assertEqual(out["tile"]["severity"], "alert")
        tile = TileStore.for_agent_log(bl.agent_log).get("integrity")
        self.assertEqual((tile["message"], tile["figure"], tile["run_uids"]), ("U6 charging", "att:abc", ["r6"]))
        entry = bl.agent_log.get(uid)["log"][-1]
        self.assertEqual((entry["kind"], entry["source"], entry["severity"]), ("tile", "assistant:integrity", "alert"))
        out = run(bl, "post", "--kind", "summary", "three runs fine")
        self.assertEqual(out["agent_uid"], uid)
        # Nothing reaches the main record
        self.assertIsNone(bl.agent_log.get(assistant_uid()))
        # log shows its own record
        self.assertEqual(list(run(bl, "log")), [uid])

    def test_posts_only_where_the_sinks_allow(self):
        with self.assertRaisesRegex(CliError, "posts only to its tile"):
            run(cli("assistant:plotter"), "post", "--kind", "summary", "x")
        with self.assertRaisesRegex(CliError, "has no tile"):
            run(cli("assistant"), "post", "--tile", "--severity", "ok", "x")
        with self.assertRaisesRegex(CliError, "not a headless role"):
            run(cli("assistant:nobody"), "post", "--kind", "summary", "x")
        with self.assertRaisesRegex(CliError, "needs --severity"):
            run(cli("assistant:integrity"), "post", "--tile", "x")
        with self.assertRaisesRegex(CliError, "go with --tile"):
            run(cli("assistant:integrity"), "post", "--kind", "summary", "--severity", "ok", "x")
        with self.assertRaisesRegex(CliError, "only for the assistant"):
            run(cli("driver"), "post", "--tile", "--severity", "ok", "x")

    def test_chat_role_posts_to_the_main_record_as_before(self):
        bl = cli("assistant", roles={})
        out = run(bl, "post", "--kind", "advice", "check s4")
        self.assertEqual(out["agent_uid"], assistant_uid())
        self.assertEqual(out["source"], "assistant")

    def test_only_a_person_acknowledges(self):
        bl = cli("driver")
        TileStore.for_agent_log(bl.agent_log).post("integrity", "alert", "U6 charging")
        for role in ("assistant", "assistant:integrity"):
            other = cli(role)
            other._agent_log = bl.agent_log
            with self.subTest(role=role), self.assertRaisesRegex(CliError, "only a person"):
                run(other, "ack", "integrity")
        with patch.dict(os.environ, {"NBS_AGENT_UID": "u1"}), self.assertRaisesRegex(CliError, "only a person"):
            run(bl, "ack", "integrity")
        with self.assertRaisesRegex(CliError, "no role"):
            run(bl, "ack", "nobody")
        out = run(bl, "ack", "integrity")
        self.assertEqual(out["tile"]["ack"]["by"], out["entry"]["by"])
        self.assertEqual(out["agent_uid"], assistant_uid(role="integrity"))
        with self.assertRaisesRegex(CliError, "nothing to acknowledge"):
            run(bl, "ack", "integrity")

    def test_role_verbs_are_for_the_chat_role(self):
        for role in ("driver", "assistant:integrity"):
            with self.subTest(role=role), self.assertRaisesRegex(CliError, "chat role"):
                run(cli(role), "role", "show", "integrity")
            with self.subTest(role=role), self.assertRaisesRegex(CliError, "chat role"):
                run(cli(role), "role", "request", "integrity", "x")

    def test_role_list_and_show(self):
        bl = cli("assistant")
        uid = bl.agent_log.ensure_assistant_record(role="integrity")
        bl.agent_log.append(uid, "summary", "old")
        bl.agent_log.update(uid, notebook="### notebook/flags.md\nU6 charges")
        TileStore.for_agent_log(bl.agent_log).post("integrity", "warn", "noisy")
        listed = run(bl, "role", "list")
        self.assertEqual(list(listed), ["manager", "integrity", "plotter"])
        self.assertEqual(listed["integrity"]["tile"]["message"], "noisy")
        shown = run(bl, "role", "show", "integrity", "--since", "1h")
        self.assertEqual(shown["record"], uid)
        self.assertEqual([e["text"] for e in shown["entries"]], ["old"])
        self.assertIn("U6 charges", shown["notebook"])
        self.assertEqual(run(bl, "role", "show", "integrity", "--since", "0s")["entries"], [])
        with self.assertRaisesRegex(CliError, "no role"):
            run(bl, "role", "show", "nobody")

    def test_requests_go_only_to_headless_roles(self):
        bl = cli("assistant")
        with self.assertRaisesRegex(CliError, "chat role itself"):
            run(bl, "role", "request", "manager", "x")
        out = run(bl, "role", "request", "plotter", "overlay U1 and STD3")
        self.assertEqual(out["entry"]["reply_to"], assistant_uid())
        self.assertEqual(bl.agent_log.take_requests(assistant_uid(role="plotter"))[0]["text"], "overlay U1 and STD3")
        self.assertEqual(bl.agent_log.take_requests(assistant_uid(role="plotter")), [])

    def test_role_flag_cannot_change_a_headless_role(self):
        with patch.dict(os.environ, {"NBS_CLI_ROLE": "assistant:integrity", "NBS_CLI_CONFIG": "/dev/null"}):
            with patch("sys.stdout") as out:
                self.assertEqual(main(["--role", "driver", "log"]), 1)
                self.assertEqual(main(["--role", "assistant", "log"]), 1)
            written = "".join(c.args[0] for c in out.write.call_args_list)
            self.assertIn("cannot", written)
        # assistant:NAME is set by the runner, never on the command line
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            build_parser().parse_args(["--role", "assistant:integrity", "log"])

    def test_parse_since(self):
        self.assertEqual(parse_since("30m", 10_000.0), 8200.0)
        self.assertEqual(parse_since("1700000000", 0), 1_700_000_000.0)
        self.assertIsInstance(parse_since("08:15", 1_800_000_000.0), float)
        with self.assertRaises(CliError):
            parse_since("yesterday", 0)


class RoleScriptTests(unittest.TestCase):
    run_script = base.ScriptRoleTests.run_script

    def test_headless_role_keeps_its_cli_role_and_skill(self):
        out = self.run_script(
            "--headless", "--role", "assistant",
            env={"NBS_ASSISTANT_PROMPT": "events", "NBS_CLI_ROLE": "assistant:integrity",
                 "NBS_ASSISTANT_SKILL": "beamline-reduce"},
        )
        self.assertEqual(out["role"], "assistant:integrity")
        argv = out["argv"]
        prompt = argv[argv.index("--append-system-prompt-file") + 1]
        self.assertTrue(prompt.endswith("beamline-reduce.md"))
        self.assertIn("Bash(nbs-cli role:*)", argv)

    def test_chat_role_is_assistant(self):
        out = self.run_script("--headless", "--role", "assistant", env={"NBS_CLI_ROLE": "driver"})
        self.assertEqual(out["role"], "assistant")


if __name__ == "__main__":
    unittest.main()
