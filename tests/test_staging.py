"""The shared staging queue: the store, applying ops, and the nbs-cli staging verbs."""

import io
import itertools
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

from fakes import FakeRedis, StaticInfo
from nbs_agents.agentlog import AgentLog
from nbs_agents.cli.config import CliConfig
from nbs_agents.cli.core import Beamline, CliError
from nbs_agents.cli.main import build_parser, run_command, staging_ops
from nbs_agents.staging import (
    PROPOSAL_TTL_S, StagingError, StagingStore, StaleProposal, apply_ops, check_ops, check_proposal,
)

PLANS = {
    "nbs_count": {"name": "nbs_count", "parameters": [
        {"name": "num", "kind": {"name": "KEYWORD_ONLY", "value": 3}, "default": "1", "annotation": {"type": "int"}},
        {"name": "sample", "kind": {"name": "KEYWORD_ONLY", "value": 3}, "default": "None"},
    ]},
}


def staged(*uids):
    return [{"item_type": "plan", "name": "nbs_count", "args": [], "kwargs": {"num": n}, "item_uid": uid}
            for n, uid in enumerate(uids, 1)]


def write_queue(redis, items, version=3, prefix="bl-"):
    redis.set(f"{prefix}staging:queue", json.dumps(
        {"version": version, "items": items, "updated": 100.0, "updated_by": "nbs-gui:ws1:jamie"}))


def uids(items):
    return [i["item_uid"] for i in items]


class ApplyOpsTests(unittest.TestCase):
    def setUp(self):
        counter = itertools.count(1)
        self.new_uid = lambda: f"n{next(counter)}"

    def add(self, name="nbs_count", after=None, **kwargs):
        return {"op": "add", "item": {"name": name, "kwargs": kwargs}, "after_uid": after}

    def test_adds_keep_their_order_at_any_position(self):
        items = staged("a", "b")
        ops = [self.add(after="a", num=1), self.add(after="a", num=2)]
        self.assertEqual(uids(apply_ops(items, ops, self.new_uid)), ["a", "n1", "n2", "b"])
        ops = [self.add(after="top"), self.add(after="top")]
        self.assertEqual(uids(apply_ops(items, ops, self.new_uid)), ["n3", "n4", "a", "b"])
        out = apply_ops(items, [self.add(), self.add()], self.new_uid)
        self.assertEqual(uids(out), ["a", "b", "n5", "n6"])
        self.assertEqual(out[-1]["item_type"], "plan")
        self.assertEqual(uids(items), ["a", "b"])  # the input is not changed

    def test_remove_move_update(self):
        items = staged("a", "b", "c")
        ops = [{"op": "move", "item_uid": "c", "after_uid": "top"},
               {"op": "remove", "item_uid": "b"},
               {"op": "update", "item_uid": "a", "item": {"name": "nbs_count", "kwargs": {"num": 9}}},
               {"op": "move", "item_uid": "c", "after_uid": None}]
        out = apply_ops(items, ops, self.new_uid)
        self.assertEqual(uids(out), ["a", "c"])
        self.assertEqual(out[0]["kwargs"], {"num": 9})

    def test_missing_items_make_it_stale(self):
        with self.assertRaises(StaleProposal):
            apply_ops(staged("a"), [{"op": "remove", "item_uid": "gone"}])
        with self.assertRaises(StaleProposal):
            apply_ops(staged("a"), [self.add(after="gone")])
        proposal = {"base_version": 2, "ops": [{"op": "remove", "item_uid": "a"}]}
        self.assertEqual(check_proposal(proposal, {"version": 2, "items": staged("a")}),
                         {"stale": False, "changed": False, "missing": []})
        self.assertEqual(check_proposal(proposal, {"version": 3, "items": staged("b")}),
                         {"stale": True, "changed": True, "missing": ["a"]})

    def test_malformed_ops(self):
        for ops in ([], [{"op": "drop"}], [{"op": "add", "item": {}}], [{"op": "remove"}],
                    [{"op": "add", "item": {"name": "x", "args": {}}}], [{"op": "move", "item_uid": "a", "after_uid": 3}]):
            with self.assertRaises(StagingError, msg=ops):
                check_ops(ops)


class StoreTests(unittest.TestCase):
    def test_missing_queue_is_empty(self):
        store = StagingStore(FakeRedis(), "bl-")
        self.assertEqual(store.queue(), {"version": 0, "items": [], "updated": None, "updated_by": None})

    def test_propose_withdraw_and_expiry(self):
        redis = FakeRedis()
        write_queue(redis, staged("a"), version=5)
        store = StagingStore(redis, "bl-")
        p = store.propose([{"op": "remove", "item_uid": "a"}], "duplicate", "assistant:manager", t=1000.0)
        key = f"bl-staging:proposal:{p['id']}"
        self.assertEqual((p["status"], p["base_version"]), ("pending", 5))
        self.assertEqual(redis.ttl[key], PROPOSAL_TTL_S)
        self.assertEqual([x["id"] for x in store.proposals()], [p["id"]])
        with self.assertRaises(StagingError):
            store.withdraw(p["id"], "jamie")
        out = store.withdraw(p["id"], "assistant:manager", t=1000.0 + 86400)
        self.assertEqual((out["status"], out["decided_by"]), ("withdrawn", "assistant:manager"))
        self.assertEqual(redis.ttl[key], PROPOSAL_TTL_S - 86400)
        self.assertEqual(store.proposals(), [])
        with self.assertRaises(StagingError):
            store.withdraw(p["id"], "assistant:manager")
        self.assertNotIn("bl-staging:queue", [k for k in redis.ttl])

    def test_accept_applies_to_the_current_list_and_bumps_the_version(self):
        redis = FakeRedis()
        write_queue(redis, staged("a", "b"), version=5)
        store = StagingStore(redis, "bl-")
        p = store.propose([{"op": "add", "item": {"name": "nbs_count", "kwargs": {"num": 9}}, "after_uid": "a"},
                           {"op": "remove", "item_uid": "b"}], "swap b for a 9-count", "assistant:manager")
        write_queue(redis, staged("a", "b", "c"), version=6)  # someone staged c meanwhile
        out = store.accept(p["id"], "web:jamie", t=2000.0)
        queue = store.queue()
        self.assertEqual(queue["version"], 7)
        self.assertEqual(queue["updated_by"], "web:jamie")
        self.assertEqual([i["kwargs"]["num"] for i in queue["items"]], [1, 9, 3])
        self.assertEqual(out["queue"]["items"], queue["items"])
        self.assertEqual((out["proposal"]["status"], out["proposal"]["decided_by"]), ("accepted", "web:jamie"))
        with self.assertRaises(StagingError):
            store.accept(p["id"], "web:jamie")

    def test_accept_marks_stale_and_reject_leaves_the_queue(self):
        redis = FakeRedis()
        write_queue(redis, staged("a", "b"), version=5)
        store = StagingStore(redis, "bl-")
        p = store.propose([{"op": "remove", "item_uid": "b"}], "duplicate", "assistant:manager")
        write_queue(redis, staged("a"), version=6)
        with self.assertRaises(StaleProposal):
            store.accept(p["id"], "web:jamie")
        self.assertEqual(store.get(p["id"])["status"], "stale")
        self.assertEqual(store.queue()["version"], 6)
        q = store.propose([{"op": "remove", "item_uid": "a"}], "drop a", "assistant:manager")
        self.assertEqual(store.reject(q["id"], "web:jamie")["status"], "rejected")
        self.assertEqual(uids(store.queue()["items"]), ["a"])


class VerbTests(unittest.TestCase):
    def make(self, items=(), role=None, estimate=lambda item: 60.0, warning=None):
        config = CliConfig()
        if role:
            config.role = role
        bl = Beamline(config)
        bl._agent_log = AgentLog(FakeRedis())
        self.redis = FakeRedis()
        write_queue(self.redis, list(items))
        bl._info = StaticInfo(estimate=estimate, warning=warning, staging=(self.redis, "bl-"))
        manager = MagicMock()
        manager.plans_allowed.return_value = {"success": True, "plans_allowed": PLANS}
        manager.devices_allowed.return_value = {"success": True, "devices_allowed": {}}
        bl._manager = manager
        return bl

    def queue_before(self):
        return self.redis.store["bl-staging:queue"]

    def test_unavailable_without_a_provider(self):
        bl = Beamline(CliConfig())
        bl._info = StaticInfo()
        with self.assertRaises(CliError) as ctx:
            bl.staging_show()
        self.assertIn("no shared staging queue", str(ctx.exception))

    def test_show(self):
        bl = self.make(staged("a", "b"))
        bl.staging.propose([{"op": "remove", "item_uid": "a"}], "dup", "jamie")
        out = bl.staging_show()
        self.assertEqual((out["version"], out["n_items"], out["total_est_s"]), (3, 2, 120.0))
        self.assertEqual(out["items"][0], {"item_uid": "a", "name": "nbs_count", "kwargs": {"num": 1}, "est_s": 60.0})
        self.assertEqual(out["proposals"][0]["ops"], ["remove"])
        self.assertFalse(out["proposals"][0]["stale"])

    def test_validate(self):
        bl = self.make(warning="sample s9 is not loaded")
        ok = bl.check_plan("nbs_count", [], {"num": 3, "sample": "s9"})
        self.assertEqual((ok["valid"], ok["estimate_s"], ok["warnings"]), (True, 60.0, ["sample s9 is not loaded"]))
        bad = bl.check_plan("nbs_count", [], {"num": "x"})
        self.assertFalse(bad["valid"])
        self.assertIn("valid integer", bad["errors"][0])
        self.assertNotIn("Plan:", bad["errors"][0])
        self.assertEqual(bl.check_plan("nbs_count", [], {"bogus": 1})["errors"][0][:22], "unknown parameters for")
        self.assertFalse(bl.check_plan("mv", [], {})["valid"])

    def test_propose_stores_checks_author_and_thread_and_never_writes_the_queue(self):
        bl = self.make(staged("a"), role="assistant")
        before = self.queue_before()
        ops = [{"op": "add", "item": {"name": "nbs_count", "kwargs": {"num": 2}}, "after_uid": "a"},
               {"op": "add", "item": {"name": "nbs_count", "kwargs": {"num": 3}}, "after_uid": "a"}]
        with patch.dict(os.environ, {"NBS_ASSISTANT_THREAD": "t42"}):
            out = bl.staging_propose(ops, "repeat counts on S1")
        p = out["proposal"]
        self.assertEqual((p["author"], p["thread"], p["base_version"]), ("assistant:manager", "t42", 3))
        self.assertEqual(p["checks"], {"estimate_s": 120.0, "warnings": [], "errors": []})
        self.assertEqual(self.queue_before(), before)

    def test_propose_refuses_invalid_plans_and_missing_items(self):
        bl = self.make(staged("a"))
        with self.assertRaises(CliError) as ctx:
            bl.staging_propose([{"op": "add", "item": {"name": "nbs_count", "kwargs": {"num": "x"}}}], "why")
        self.assertIn("op 1 (nbs_count)", str(ctx.exception))
        with self.assertRaises(CliError):
            bl.staging_propose([{"op": "remove", "item_uid": "gone"}], "why")
        with self.assertRaises(CliError):
            bl.staging_propose([{"op": "remove", "item_uid": "a"}], " ")
        self.assertEqual(bl.staging.proposals(), [])

    def test_who_may_propose(self):
        bl = self.make(staged("a"), role="assistant:plotter")
        with self.assertRaises(CliError):
            bl.staging_propose([{"op": "remove", "item_uid": "a"}], "why")
        bl = self.make(staged("a"))
        with patch.dict(os.environ, {"NBS_AGENT_UID": "u1"}):
            with self.assertRaises(CliError):
                bl.staging_propose([{"op": "remove", "item_uid": "a"}], "why")
        with patch("nbs_agents.cli.core.login_name", return_value="jamie"):
            p = bl.staging_propose([{"op": "remove", "item_uid": "a"}], "why")["proposal"]
            self.assertEqual(p["author"], "jamie")
            self.assertEqual(bl.staging_withdraw(p["id"])["proposal"]["status"], "withdrawn")


class ParserTests(unittest.TestCase):
    def ops(self, *argv):
        return staging_ops(build_parser().parse_args(["staging", "propose", "--reason", "r", *argv]))

    def test_flags_become_ops_in_order(self):
        ops = self.ops("--add", "nbs_count", "num=2", "--add", "nbs_count", "num=3", "--after", "top",
                       "--remove", "x", "--move", "y", "end")
        self.assertEqual([o["op"] for o in ops], ["remove", "move", "add", "add"])
        self.assertIsNone(ops[1]["after_uid"])
        self.assertEqual(ops[2], {"op": "add", "item": {"item_type": "plan", "name": "nbs_count", "args": [],
                                                        "kwargs": {"num": 2}}, "after_uid": "top"})

    def test_needs_something(self):
        with self.assertRaises(CliError):
            self.ops()

    def test_file(self):
        cwd = os.getcwd()
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            try:
                with open("ops.json", "w") as f:
                    json.dump([{"op": "remove", "item_uid": "a"}], f)
                self.assertEqual(self.ops("--file", "ops.json"), [{"op": "remove", "item_uid": "a"}])
                with self.assertRaises(CliError):
                    self.ops("--file", "ops.json", "--remove", "b")
            finally:
                os.chdir(cwd)

    def test_run_command_show(self):
        bl = VerbTests().make(staged("a"))
        out = run_command(bl, build_parser().parse_args(["staging", "show"]))
        self.assertEqual(out["n_items"], 1)

    def test_run_command_show_one_proposal_whatever_its_status(self):
        bl = VerbTests().make(staged("a"))
        p = bl.staging.propose([{"op": "remove", "item_uid": "a"}], "drop a", "assistant:manager")
        out = run_command(bl, build_parser().parse_args(["staging", "show", "--proposal", p["id"]]))
        self.assertEqual(out["proposal"]["ops"], p["ops"])
        self.assertFalse(out["fit"]["stale"])
        bl.staging.request_changes(p["id"], "web:jamie", "keep a, drop nothing")
        out = run_command(bl, build_parser().parse_args(["staging", "show", "--proposal", p["id"]]))
        self.assertEqual(out["proposal"]["status"], "changes_requested")
        self.assertEqual(out["proposal"]["decision_reason"], "keep a, drop nothing")
        self.assertNotIn("fit", out)
        with self.assertRaises(CliError):
            run_command(bl, build_parser().parse_args(["staging", "show", "--proposal", "nope"]))


if __name__ == "__main__":
    unittest.main()
