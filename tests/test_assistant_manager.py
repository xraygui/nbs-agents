"""The manager: driver-aware digests, a thread per question, directives a person confirms."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fakes import FakeRedis
from nbs_agents.agentlog import AgentLog, assistant_uid
from nbs_agents.agents.external import ExternalAgent
from nbs_agents.assistant.agent import AssistantAgent, entry_line
from nbs_agents.cli.config import AssistantConfig, CliConfig, read_roles
from nbs_agents.cli.core import Beamline, CliError
from nbs_agents.cli.main import build_parser, run_command
from nbs_agents.directives import (
    DirectiveError, DirectiveStore, decide_directive, directives_text, expired, parse_expiry,
)
from nbs_agents.threads import ThreadError, ThreadStore, open_thread, unseen
from nbs_agents.tiles import TileStore

from test_assistant_agent import Clock, FakeLauncher, xas_run

ROLES = {
    "manager": {"mode": "chat"},
    "integrity": {"triggers": ["alert", "flag"], "model": "opus"},
}
# Not an agent, so nbs-cli treats the caller as a person
PERSON_ENV = {"NBS_AGENT_UID": "", "NBS_DRIVER": ""}


class Launcher(FakeLauncher):
    """FakeLauncher whose calls end with a reply in their log, as claude -p writes it."""

    def __init__(self):
        super().__init__()
        self.folder = tempfile.mkdtemp()

    def launch(self, agent_uid, goal, driver, extra_args=None, env=None):
        out = super().launch(agent_uid, goal, driver, extra_args, env)
        out.log_path = os.path.join(self.folder, f"call{len(self.calls)}.log")
        self.calls[-1]["log_path"] = out.log_path
        return out

    def finish(self, returncode=0, reply=None, uid=None, error=None):
        call = next(c for c in reversed(self.calls) if uid is None or c["uid"] == uid)
        with open(call["log_path"], "w") as f:
            f.write('{"type": "system", "subtype": "init"}\n')
            if reply is not None:
                f.write(json.dumps({"type": "result", "result": reply}) + "\n")
            if error is not None:
                f.write(json.dumps({"type": "result", "is_error": True, "result": error}) + "\n")
        self.on_exit(SimpleNamespace(agent_uid=call["uid"], log_path=call["log_path"], pid=1), returncode)


def driver(uid="d1", goal="measure U6"):
    agent = MagicMock(spec=ExternalAgent)
    agent.agent_uid = uid
    agent.parameters = {"goal": goal}
    return agent


def driver_run(uid, scan_id, agent_uid="d1", **kw):
    run = xas_run(uid, scan_id, **kw)
    run.start["agent_uid"] = agent_uid
    return run


class ManagerCase(unittest.TestCase):
    roles = None

    def setUp(self):
        self.clock = Clock(time.time())
        self.log = AgentLog(FakeRedis(), prefix="bl-")
        self.beamline = MagicMock()
        self.beamline.info.xas_plans.return_value = {}
        self.beamline.info.metadata.return_value = {"proposal": {"proposal_id": "P1"}}
        self.beamline.beam.return_value = {"expected": True}
        self.beamline.state.return_value = {}
        self.beamline.manager.status.return_value = {"manager_state": "executing_queue"}
        self.beamline.queue.return_value = {
            "running_item": {"name": "c_xas", "kwargs": {"sample": "U6"}, "meta": {"agent_uid": "d1"}},
            "items": [
                {"name": "c_xas", "kwargs": {"sample": "U7"}, "item_uid": "i7",
                 "meta": {"agent_uid": "d1", "agent_reason": "U7 next, same edge"}},
                {"name": "other", "item_uid": "x", "meta": {}},
            ],
        }
        self.workdir = tempfile.mkdtemp()
        self.config = AssistantConfig(
            enabled=True, workdir=self.workdir, model="sonnet", alert_model="opus",
            roles=read_roles(self.roles) if self.roles else {},
        )
        self.launcher = Launcher()
        self.launchers = {"integrity": Launcher()} if self.roles else {}
        self.agent = AssistantAgent(self.config, self.beamline, None, self.log, signal="sc", norm="i0",
                                    clock=self.clock, launcher=self.launcher, launchers=self.launchers)
        self.agent.open_log()
        self.main = assistant_uid(self.clock.t)

    def prompt(self, n=-1):
        return self.launcher.calls[n]["env"]["NBS_ASSISTANT_PROMPT"]

    def cli(self, role="driver"):
        bl = Beamline(CliConfig(role=role))
        bl._agent_log = self.log
        bl._assistant = self.config
        bl._info = self.beamline.info
        return bl

    def run_cli(self, bl, *argv):
        with patch.dict(os.environ, PERSON_ENV):
            return run_command(bl, build_parser().parse_args(list(argv)))


class DigestTests(ManagerCase):
    def open_driver(self):
        self.agent.on_agent_opened(driver())
        self.log.append("d1", "note", "plan: U6 then U7 at the C edge", source="nbs-cli")
        self.log.append("d1", "add", "U6 looks charged; repeat at lower flux", plan="c_xas", item_uid="i6")

    def test_driver_groups_get_a_digest_with_the_full_record(self):
        self.open_driver()
        self.clock.t += self.config.digest_min_s
        self.agent.on_run_stop(driver_run("r1", 1, sample="U6"))
        self.agent.on_run_stop(driver_run("r2", 2, sample="U7"))
        self.assertEqual(len(self.launcher.calls), 1)
        prompt = self.prompt()
        self.assertIn("[digest]", prompt)
        self.assertNotIn("[group_end]", prompt)
        # Every driver entry, with its refs, and the queue with its reasons
        self.assertIn("plan: U6 then U7 at the C edge", prompt)
        self.assertIn("U6 looks charged; repeat at lower flux (plan=c_xas, item_uid=i6)", prompt)
        self.assertIn("U7 next, same edge", prompt)
        self.assertIn('"running":true', prompt)
        self.assertNotIn('"other"', prompt)
        self.assertIn("This call writes the digest", prompt)
        full = Path(self.workdir, "digest", "driver-d1.md").read_text()
        self.assertIn("U6 looks charged", full)
        # The digest is posted: its post is tagged and the mark moves on
        self.log.append(self.main, "summary", "U6 charged; driver repeating", source="assistant", call=1)
        self.launcher.finish(0)
        record = self.log.get(self.main)
        self.assertTrue([e for e in record["log"] if e["kind"] == "summary"][-1]["digest"])
        self.assertIn("d1", record["digest_since"])
        # The next digest has only what came after
        self.log.append("d1", "note", "U7 fine")
        self.agent.on_agent_closed(driver())
        prompt = self.prompt()
        self.assertIn("U7 fine", prompt)
        self.assertNotIn("U6 looks charged", prompt)
        self.assertIn("(ended)", prompt)
        self.assertIn("driver session d1 ended", prompt)

    def test_failed_digest_keeps_the_entries_for_the_next(self):
        self.open_driver()
        self.agent.on_agent_closed(driver())
        self.launcher.finish(1)
        self.assertEqual(self.agent.digest_since.get("d1", 0), 0)
        self.assertEqual([d["agent_uid"] for d in self.agent.ended_drivers], ["d1"])

    def test_group_ends_wait_for_digest_min_s(self):
        self.open_driver()
        self.agent.on_run_stop(driver_run("r1", 1, sample="U6"))
        self.agent.on_run_stop(driver_run("r2", 2, sample="U7"))
        self.assertEqual(self.launcher.calls, [], "too soon after the driver opened")
        self.clock.t += self.config.digest_min_s
        self.agent.tick()
        self.assertEqual(len(self.launcher.calls), 1)
        self.assertIn("[digest]", self.prompt())
        self.assertIn("U6", self.prompt())

    def test_other_runs_still_get_group_summaries(self):
        self.open_driver()
        self.agent.on_run_stop(xas_run("r1", 1, sample="s1"))
        self.agent.on_run_stop(xas_run("r2", 2, sample="s2"))
        self.assertIn("[group_end]", self.prompt())

    def test_no_driver_is_as_before(self):
        self.agent.on_run_stop(xas_run("r1", 1, sample="s1"))
        self.agent.on_run_stop(xas_run("r2", 2, sample="s2"))
        self.assertIn("[group_end]", self.prompt())
        self.assertNotIn("Driver sessions since the last digest", self.prompt())

    def test_hourly_digest_replaces_the_checkin(self):
        self.open_driver()
        self.agent.on_run_stop(driver_run("r1", 1, sample="U6"))
        self.clock.t += self.config.checkin_s + 1
        self.agent.tick()
        self.assertEqual(len(self.launcher.calls), 1)
        self.assertIn("[digest]", self.prompt())
        self.assertIn("hourly digest", self.prompt())
        self.assertNotIn("[checkin]", self.prompt())
        self.log.append(self.main, "summary", "all fine", source="assistant", call=1)
        self.launcher.finish(0)
        # Nothing new from the driver in the next hour: no digest
        self.clock.t += self.config.checkin_s + 1
        self.agent.tick()
        self.assertEqual(len(self.launcher.calls), 1)

    def test_without_a_digest_role_driver_groups_are_summarized(self):
        config = AssistantConfig(enabled=True, workdir=tempfile.mkdtemp(),
                                 roles=read_roles({"manager": {"mode": "chat", "triggers": ["group_end"]}}))
        launcher = Launcher()
        agent = AssistantAgent(config, self.beamline, None, self.log, clock=self.clock, launcher=launcher)
        agent.open_log()
        agent.on_agent_opened(driver())
        agent.on_run_stop(driver_run("r1", 1, sample="U6"))
        agent.on_run_stop(driver_run("r2", 2, sample="U7"))
        self.assertIn("[group_end]", launcher.calls[0]["env"]["NBS_ASSISTANT_PROMPT"])

    def test_entry_line(self):
        line = entry_line({"t": 0, "kind": "note", "text": "one\ntwo", "plan": "c_xas"})
        self.assertTrue(line.endswith("[note] one\n    two (plan=c_xas)"))


class ThreadTests(ManagerCase):
    def ask(self, *argv):
        return self.run_cli(self.cli(), "ask-assistant", *argv)

    def test_question_opens_a_thread_with_one_line_in_the_record(self):
        out = self.ask("why is U6 noisy?")
        thread_id = out["thread"]
        marker = self.log.get(self.main)["log"][-1]
        self.assertEqual((marker["kind"], marker["thread"], marker["text"]), ("operator", thread_id, "why is U6 noisy?"))
        self.agent.tick()
        call = self.launcher.calls[0]
        self.assertEqual(call["uid"], thread_id)
        self.assertEqual(call["env"]["NBS_ASSISTANT_THREAD"], thread_id)
        session = ThreadStore.for_agent_log(self.log).get(thread_id)["session_id"]
        self.assertEqual(call["args"], ["--role", "assistant", "--model", "opus", "--session-id", session])
        self.assertIn("why is U6 noisy?", self.prompt())
        self.assertIn("top-level box", self.prompt())
        self.assertIn("Your final message is your answer", self.prompt())
        # Not answered twice, and the main record gets no question call
        self.agent.tick()
        self.assertEqual(len(self.launcher.calls), 1)
        self.launcher.finish(0, reply="U6 is charging: the pre-edge rises.")
        thread = ThreadStore.for_agent_log(self.log).get(thread_id)
        self.assertEqual([m["kind"] for m in thread["messages"]], ["question", "answer"])
        self.assertEqual(thread["messages"][-1]["model"], "opus")
        self.assertFalse(thread["calling"])
        # The answer stays out of the main record
        self.assertNotIn("charging", json.dumps(self.log.get(self.main)["log"]))
        # A follow-up resumes the session
        self.ask("and U7?", "--thread", thread_id)
        self.agent.tick()
        call = self.launcher.calls[1]
        self.assertEqual(call["args"][-2:], ["--resume", session])
        self.assertIn("and U7?", self.prompt())
        self.assertNotIn("why is U6 noisy?", self.prompt())
        # The main record still has the one line for the thread
        self.assertEqual([e["text"] for e in self.log.get(self.main)["log"] if e.get("thread")], ["why is U6 noisy?"])
        self.assertNotIn("and U7?", json.dumps(self.log.get(self.main)["log"]))

    def test_threads_run_beside_the_digest_call(self):
        self.agent.on_agent_opened(driver())
        self.agent.on_agent_closed(driver())
        self.assertIn("[digest]", self.prompt())
        thread_id = self.ask("anything?")["thread"]
        self.agent.tick()
        self.assertEqual(self.launcher.calls[-1]["uid"], thread_id)

    def test_quiet_thread_closes_with_a_summary(self):
        thread_id = self.ask("why is U6 noisy?")["thread"]
        self.agent.tick()
        self.launcher.finish(0, reply="charging")
        self.clock.t += self.config.thread_idle_s + 1
        self.agent.tick()
        self.assertIn("is closing", self.prompt())
        self.assertEqual(self.launcher.calls[-1]["args"][-2], "--resume")
        self.assertEqual(self.launcher.calls[-1]["args"][3], "sonnet")
        self.launcher.finish(0, reply="Asked why U6 is noisy: it charges.")
        thread = ThreadStore.for_agent_log(self.log).get(thread_id)
        self.assertEqual((thread["status"], thread["summary"]), ("closed", "Asked why U6 is noisy: it charges."))
        notes = Path(self.workdir, "notebook", "threads.md").read_text()
        self.assertIn(f"thread {thread_id}", notes)
        self.assertIn("it charges", notes)
        marker = [e for e in self.log.get(self.main)["log"] if e.get("thread") == thread_id][0]
        self.assertEqual(marker["thread_status"], "closed")
        # Later calls read it in the notebook
        self.ask("next question")
        self.agent.tick()
        self.assertIn("it charges", self.prompt())
        # A follow-up reopens a closed thread
        self.ask("one more", "--thread", thread_id)
        self.assertEqual(ThreadStore.for_agent_log(self.log).get(thread_id)["status"], "open")

    def test_threads_md_keeps_the_newest(self):
        for i in range(45):
            thread = ThreadStore.for_agent_log(self.log).create(f"q{i}", "jamie")
            self.agent._close_thread(thread, f"summary {i}")
        notes = Path(self.workdir, "notebook", "threads.md").read_text()
        self.assertEqual(notes.count("\n## "), 40)
        self.assertIn("summary 44", notes)
        self.assertNotIn("summary 4\n", notes)

    def test_resume_that_fails_retries_in_a_new_session(self):
        thread_id = self.ask("q1")["thread"]
        self.agent.tick()
        self.launcher.finish(0, reply="a1")
        self.ask("q2", "--thread", thread_id)
        self.agent.tick()
        old = ThreadStore.for_agent_log(self.log).get(thread_id)["session_id"]
        # The session is gone (claude -p --resume says so as an error result)
        self.launcher.finish(1, error="No conversation found with session ID: x")
        call = self.launcher.calls[-1]
        new = ThreadStore.for_agent_log(self.log).get(thread_id)["session_id"]
        self.assertNotEqual(new, old)
        self.assertEqual(call["args"][-2:], ["--session-id", new])
        self.assertIn("Earlier in this thread", self.prompt())
        self.assertIn("q1", self.prompt())
        self.assertIn("q2", self.prompt())
        self.launcher.finish(1, error="Failed to authenticate")
        last = ThreadStore.for_agent_log(self.log).get(thread_id)["messages"][-1]
        self.assertEqual(last["kind"], "error")
        self.assertIn("Failed to authenticate", last["text"])
        self.assertEqual([m["kind"] for m in ThreadStore.for_agent_log(self.log).get(thread_id)["messages"]],
                         ["question", "answer", "question", "error"])

    def test_question_written_into_the_record_opens_a_thread(self):
        self.log.message(self.main, "is s4 ok?", source="gui")
        self.agent.tick()
        thread_id = self.launcher.calls[0]["uid"]
        self.assertNotEqual(thread_id, self.main)
        entry = self.log.get(self.main)["log"][-1]
        self.assertEqual(entry["thread"], thread_id)
        self.assertEqual(ThreadStore.for_agent_log(self.log).get(thread_id)["marker"]["t"], entry["t"])

    def test_seeds(self):
        self.log.append(self.main, "alert", "U6 pre-edge above post-edge", source="assistant", run_uids=["r6"])
        TileStore.for_agent_log(self.log).post("integrity", "warn", "i0 dropped 40%")
        out = self.ask("is this charging?", "--entry", "last")
        seed = ThreadStore.for_agent_log(self.log).get(out["thread"])["seed"]
        self.assertEqual((seed["kind"], seed["entry"]["text"]), ("entry", "U6 pre-edge above post-edge"))
        self.agent.tick()
        self.assertIn("U6 pre-edge above post-edge", self.prompt())
        self.assertIn("r6", self.prompt())
        out = self.ask("why?", "--tile", "integrity")
        self.launcher.finish(0, reply="x")
        self.agent.tick()
        self.assertIn("integrity role's tile, warn", self.prompt())
        with self.assertRaisesRegex(CliError, "no tile"):
            self.ask("why?", "--tile", "plotter")
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            self.ask("x", "--tile", "integrity", "--thread", "t")
        with self.assertRaisesRegex(CliError, "no thread"):
            self.ask("x", "--thread", "nope")

    def test_threads_off_keeps_questions_in_the_record(self):
        self.config.threads = False
        out = self.ask("is s4 ok?")
        self.assertNotIn("thread", out)
        self.agent.tick()
        self.assertEqual(self.launcher.calls[0]["uid"], self.main)
        with self.assertRaisesRegex(CliError, "threads is off"):
            self.ask("x", "--entry", "last")

    def test_thread_calls_are_limited(self):
        for i in range(3):
            self.ask(f"q{i}")
        self.agent.tick()
        self.assertEqual(len(self.launcher.calls), self.config.thread_calls)
        self.launcher.finish(0, reply="a")
        self.assertEqual(len(self.launcher.calls), 3)

    def test_thread_list_and_show(self):
        thread_id = self.ask("why is U6 noisy?")["thread"]
        listed = self.run_cli(self.cli(), "thread", "list")
        self.assertEqual((listed[0]["id"], listed[0]["question"]), (thread_id, "why is U6 noisy?"))
        self.assertEqual(self.run_cli(self.cli("assistant"), "thread", "show", thread_id)["id"], thread_id)
        with patch.dict(os.environ, {"NBS_AGENT_UID": "d1"}), self.assertRaisesRegex(CliError, "not for driving"):
            run_command(self.cli(), build_parser().parse_args(["thread", "list"]))

    def test_store(self):
        store = ThreadStore(FakeRedis(), "bl-")
        with self.assertRaises(ThreadError):
            store.create(" ", "jamie")
        with self.assertRaises(ThreadError):
            store.add("nope", "question", "x")
        thread = store.create("q", "jamie", t=100.0)
        store.add(thread["id"], "note", "proposed d-1")
        self.assertEqual(len(unseen(store.get(thread["id"]))), 1)
        store.mark_seen(thread["id"], 2)
        self.assertEqual(unseen(store.get(thread["id"])), [])
        store.update(thread["id"], status="closed")
        self.assertEqual(store.open_threads(), [])
        self.assertEqual(store.prune(older_than=50.0), 0)
        self.assertEqual(store.prune(older_than=time.time() + 10), 1)
        self.assertIsNone(store.get(thread["id"]))


class RoleThreadTests(ManagerCase):
    roles = ROLES

    def test_role_request_from_a_thread_replies_into_it(self):
        thread_id = self.run_cli(self.cli(), "ask-assistant", "is U6 charging?")["thread"]
        self.agent.tick()
        self.assertIn("role show", self.prompt())
        bl = self.cli("assistant")
        with patch.dict(os.environ, {"NBS_ASSISTANT_THREAD": thread_id}):
            out = run_command(bl, build_parser().parse_args(["role", "request", "integrity", "re-check run 41"]))
        self.assertIn("into this thread", out["note"])
        self.launcher.finish(0, reply="asked integrity")
        self.agent.tick()
        integrity = self.launchers["integrity"]
        self.assertIn("re-check run 41", integrity.calls[0]["env"]["NBS_ASSISTANT_PROMPT"])
        role_uid = assistant_uid(self.clock.t, "integrity")
        self.log.append(role_uid, "answer", "run 41 is charged", source="assistant:integrity", call=1)
        integrity.finish(0)
        thread = ThreadStore.for_agent_log(self.log).get(thread_id)
        reply = thread["messages"][-1]
        self.assertEqual((reply["kind"], reply["role"], reply["text"]), ("reply", "integrity", "run 41 is charged"))
        # It goes to the thread, not the main record, and resumes the thread
        self.assertEqual([e for e in self.log.get(self.main)["log"] if e["kind"] == "reply"], [])
        self.assertEqual(self.launcher.calls[-1]["uid"], thread_id)
        self.assertIn("--resume", self.launcher.calls[-1]["args"])
        self.assertIn("the integrity role replied: run 41 is charged", self.prompt())


class DirectiveTests(ManagerCase):
    roles = ROLES

    def propose(self, *argv, thread=""):
        with patch.dict(os.environ, {"NBS_ASSISTANT_THREAD": thread}):
            return run_command(self.cli("assistant"), build_parser().parse_args(["directive", "propose", *argv]))

    def test_propose_confirm_and_prompt(self):
        thread_id = self.run_cli(self.cli(), "ask-assistant", "U6 charges, stop alerting")["thread"]
        out = self.propose("integrity", "Do not alert on U6 charging", thread=thread_id)
        directive = out["directive"]
        self.assertEqual((directive["status"], directive["thread"]), ("proposed", thread_id))
        self.assertEqual(directive["expires"]["kind"], "beamtime")
        self.assertEqual(directive["expires"]["proposal"], {"proposal_id": "P1"})
        entry = self.log.get(self.main)["log"][-1]
        self.assertEqual((entry["kind"], entry["directive"], entry["status"]), ("directive", directive["id"], "proposed"))
        self.assertEqual(ThreadStore.for_agent_log(self.log).get(thread_id)["messages"][-1]["kind"], "note")
        # Not in the role's prompt until a person confirms it
        self.agent.on_run_stop(xas_run("bad", 3, status="fail"))
        self.assertNotIn("Do not alert on U6", self.launchers["integrity"].calls[0]["env"]["NBS_ASSISTANT_PROMPT"])
        self.launchers["integrity"].finish(0)
        # Only a person confirms
        for role in ("assistant", "assistant:integrity"):
            with self.subTest(role=role), self.assertRaisesRegex(CliError, "only a person"):
                self.run_cli(self.cli(role), "directive", "confirm", directive["id"])
        with patch.dict(os.environ, {"NBS_AGENT_UID": "d1"}), self.assertRaisesRegex(CliError, "only a person"):
            run_command(self.cli(), build_parser().parse_args(["directive", "confirm", directive["id"]]))
        out = self.run_cli(self.cli(), "directive", "confirm", directive["id"])
        self.assertEqual(out["directive"]["status"], "active")
        role_entry = self.log.get(assistant_uid(self.clock.t, "integrity"))["log"][-1]
        self.assertEqual((role_entry["kind"], role_entry["status"]), ("directive", "active"))
        self.assertIn("confirmed", ThreadStore.for_agent_log(self.log).get(thread_id)["messages"][-1]["text"])
        with self.assertRaisesRegex(CliError, "not proposed"):
            self.run_cli(self.cli(), "directive", "confirm", directive["id"])
        # Now the role follows it
        self.agent.on_run_stop(xas_run("bad2", 4, status="fail"))
        prompt = self.launchers["integrity"].calls[1]["env"]["NBS_ASSISTANT_PROMPT"]
        self.assertIn("## Directives", prompt)
        self.assertIn("Do not alert on U6 charging", prompt)
        text = Path(self.agent.roles["integrity"].role.workdir, "directives.md").read_text()
        self.assertIn("Do not alert on U6 charging", text)
        # Listed until revoked
        listed = self.run_cli(self.cli(), "directive", "list", "--role", "integrity")
        self.assertEqual([d["id"] for d in listed], [directive["id"]])
        self.run_cli(self.cli(), "directive", "revoke", directive["id"], "--reason", "U6 done")
        self.assertEqual(self.run_cli(self.cli(), "directive", "list"), [])
        self.assertEqual(self.run_cli(self.cli(), "directive", "list", "--all")[0]["status"], "revoked")

    def test_reject(self):
        directive = self.propose("integrity", "x", "--expires", "12h")["directive"]
        out = self.run_cli(self.cli(), "directive", "reject", directive["id"], "--reason", "no")
        self.assertEqual(out["directive"]["status"], "rejected")
        self.assertEqual(DirectiveStore.for_agent_log(self.log).active("integrity"), [])

    def test_only_the_chat_role_proposes(self):
        with self.assertRaisesRegex(CliError, "chat role"):
            run_command(self.cli("assistant:integrity"),
                        build_parser().parse_args(["directive", "propose", "integrity", "x"]))
        with self.assertRaisesRegex(CliError, "chat role"):
            self.run_cli(self.cli(), "directive", "propose", "integrity", "x")
        with self.assertRaisesRegex(CliError, "no role"):
            self.propose("nobody", "x")
        with self.assertRaisesRegex(CliError, "--expires"):
            self.propose("integrity", "x", "--expires", "soon")

    def test_expiry(self):
        store = DirectiveStore.for_agent_log(self.log)
        timed = store.propose("integrity", "timed", parse_expiry("1h", self.clock.t))
        beamtime = store.propose("integrity", "beamtime", parse_expiry("beamtime", self.clock.t, {"proposal_id": "P1"}))
        for d in (timed, beamtime):
            decide_directive(self.log, d["id"], True, "jamie")
        self.agent.tick()
        self.assertEqual(len(store.active("integrity")), 2)
        self.clock.t += 3601
        self.agent.tick()
        self.assertEqual([d["text"] for d in store.active("integrity")], ["beamtime"])
        self.assertEqual(store.get(timed["id"])["status"], "expired")
        # The proposal changes: the beamtime is over
        self.beamline.info.metadata.return_value = {"proposal": {"proposal_id": "P2"}}
        self.clock.t += 601
        self.agent.tick()
        self.assertEqual(store.active("integrity"), [])
        entry = self.log.get(assistant_uid(self.clock.t, "integrity"))["log"][-1]
        self.assertIn("proposal changed", entry["text"])

    def test_parse_expiry(self):
        now = 1_800_000_000.0
        self.assertEqual(parse_expiry("3d", now)["until"], now + 3 * 86400)
        self.assertEqual(parse_expiry("beamtime", now, beamtime_s=10)["until"], now + 10)
        self.assertGreater(parse_expiry("2030-01-02", now)["until"], now)
        for bad in ("soon", "2020-01-01"):
            with self.subTest(bad=bad), self.assertRaises(DirectiveError):
                parse_expiry(bad, now)
        held = {"expires": {"kind": "beamtime", "until": now + 10, "proposal": None}}
        self.assertIsNone(expired(held, now, {"proposal_id": "P2"}), "unknown proposal at propose time: time only")
        self.assertEqual(directives_text([]), "No directives.")


if __name__ == "__main__":
    unittest.main()
