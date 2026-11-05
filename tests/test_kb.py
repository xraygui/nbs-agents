import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from nbs_agents.cli.kb import KBError, KnowledgeBase
from nbs_agents.cli.main import add_note_refs, main, trim_index

SLIT = """---
title: Exit slit and resolution
topic: hardware
devices: [eslit]
nodes: [mono]
---
A wider exit slit gives more flux and coarser energy resolution.
"""

DUMP = """---
title: Beam dumps
topic: problem
nodes: [front_end]
---
After a dump the ring current reads 0 and i0 drops to its dark level.
"""


class KBTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.human = self.root / "knowledge"
        (self.human / "mono").mkdir(parents=True)
        (self.human / "mono" / "exit-slit.md").write_text(SLIT)
        (self.human / "beam-dumps.md").write_text(DUMP)
        (self.human / "README.md").write_text("How to write notes.\n")
        self.agent = self.root / "agent"
        self.kb = KnowledgeBase(self.human, self.agent, clock=lambda: 0)


class TestKnowledgeBase(KBTestCase):
    def test_notes_are_labelled_by_folder(self):
        self.kb.add("Slit drift", "eslit reads 2 um wide after a restart.", "driver abcd1234", devices=["eslit"])
        by_id = {n.id: n.written_by for n in self.kb.notes()}
        self.assertEqual(by_id, {
            "beam-dumps": "human", "mono/exit-slit": "human", "agent/slit-drift": "agent",
        })

    def test_search_ranks_and_filters(self):
        self.kb.add("Slit drift", "The slit drifts.", "driver", devices=["eslit"])
        hits = {h["id"]: h for h in self.kb.search("slit")}
        self.assertEqual(set(hits), {"mono/exit-slit", "agent/slit-drift"})
        self.assertIn("wider exit slit", hits["mono/exit-slit"]["snippet"])
        self.assertEqual(self.kb.search("slit flux")[0]["id"], "mono/exit-slit")
        self.assertEqual(self.kb.search("nonsenseword"), [])
        self.assertEqual([h["id"] for h in self.kb.search("", written_by="agent")], ["agent/slit-drift"])
        self.assertEqual([h["id"] for h in self.kb.search("", node="front_end")], ["beam-dumps"])

    def test_search_lists_partial_matches_after_full_ones(self):
        hits = self.kb.search("slit nonsenseword")
        self.assertEqual([h["id"] for h in hits], ["mono/exit-slit"])
        self.assertEqual(hits[0]["missing"], ["nonsenseword"])
        hits = self.kb.search("ring flux")
        self.assertEqual({h["id"] for h in hits}, {"beam-dumps", "mono/exit-slit"})
        self.assertNotIn("missing", self.kb.search("ring current")[0])

    def test_search_ignores_common_words(self):
        hits = self.kb.search("why is the ring current zero")
        self.assertEqual(hits[0]["id"], "beam-dumps")
        self.assertEqual(hits[0]["missing"], ["zero"])
        self.assertEqual(self.kb.search("to")[0]["id"], "beam-dumps")  # only common words: search them

    def test_search_weights_rare_words_and_the_head(self):
        self.kb.add("Mirror cooling", "The mirror flux and ring current drift as the mirror warms.", "driver",
                    tags=["thermal"])
        self.assertEqual(self.kb.search("thermal")[0]["id"], "agent/mirror-cooling")
        self.assertEqual(self.kb.search("dark level")[0]["id"], "beam-dumps")

    def test_search_reads_aliases_and_odd_input(self):
        (self.human / "beam-dumps.md").write_text(DUMP.replace("nodes:", "aliases: [beam loss, no beam]\nnodes:"))
        self.assertEqual(self.kb.search("loss")[0]["id"], "beam-dumps")
        for query in ('"', "slit AND", "SR:C03-BI{DCCT:1}", "slit-flux", "NEAR(", "*"):
            self.kb.search(query)  # never an FTS5 syntax error

    def test_search_matches_plurals_and_endings(self):
        self.kb.add("L1 info", "L1 mirror has three stripes: Si, Rh and Pt.\nReflectivity drops above 8 keV.",
                    "driver")
        for query in ("stripe", "stripes", "striped", "reflect", "reflecting", "mirrors"):
            self.assertEqual([h["id"] for h in self.kb.search(query)], ["agent/l1-info"], query)
        self.assertIn("three stripes", self.kb.search("stripe")[0]["snippet"])
        self.assertEqual(self.kb.search("str"), [])

    def test_index_lists_the_vocabulary(self):
        self.kb.add("Slit drift", "The slit drifts.", "driver", devices=["eslit"], tags=["drift"])
        index = self.kb.index()
        self.assertEqual(index["counts"], {"human": 2, "agent": 1})
        self.assertEqual(index["devices"], {"eslit": ["mono/exit-slit", "agent/slit-drift"]})
        self.assertEqual(index["topics"], {"hardware": ["mono/exit-slit"], "problem": ["beam-dumps"]})
        self.assertEqual(index["nodes"], {"front_end": ["beam-dumps"], "mono": ["mono/exit-slit"]})
        self.assertEqual(index["tags"], {"drift": ["agent/slit-drift"]})
        self.assertEqual(self.kb.index(written_by="agent")["counts"], {"agent": 1})

    def test_index_can_be_narrowed(self):
        index = self.kb.index()
        self.assertEqual(trim_index(index, ["devices"], False), {"devices": {"eslit": ["mono/exit-slit"]}})
        self.assertEqual(trim_index(index, ["tags", "titles"], True),
                         {"titles": {"beam-dumps": "Beam dumps", "mono/exit-slit": "Exit slit and resolution"},
                          "tags": []})
        self.assertEqual(trim_index(index, None, True)["topics"], ["hardware", "problem"])
        self.assertIn("counts", trim_index(index, None, False))

    def test_human_notes_cannot_be_edited(self):
        with self.assertRaises(KBError):
            self.kb.edit("mono/exit-slit", "driver", body="replaced")
        self.assertEqual((self.human / "mono" / "exit-slit.md").read_text(), SLIT)

    def test_append_goes_to_the_agent_folder(self):
        self.kb.append("mono/exit-slit", "At 30 um, i0 is ~70.", "assistant")
        self.kb.append("mono/exit-slit", "At 50 um, i0 is ~120.", "driver abcd1234")
        self.assertEqual((self.human / "mono" / "exit-slit.md").read_text(), SLIT)
        note = self.kb.get("mono/exit-slit")
        self.assertIn("## Appended by assistant at", note.appendix)
        self.assertIn("## Appended by driver abcd1234 at", note.appendix)
        self.assertEqual(note.summary()["appended"], 2)
        self.assertEqual(self.kb.search("120")[0]["id"], "mono/exit-slit")

    def test_agent_notes_can_be_edited(self):
        note = self.kb.add("Slit drift", "old", "driver", tags=["slit"])
        self.kb.edit(note["id"], "assistant", body="new", tags=["slit", "drift"])
        full = self.kb.get(note["id"]).full()
        self.assertEqual(full["body"].strip(), "new")
        self.assertEqual(full["meta"]["author"], "driver")
        self.assertEqual(full["meta"]["edited_by"], "assistant")
        self.assertEqual(full["tags"], ["slit", "drift"])

    def test_ids_are_unique_and_safe(self):
        a = self.kb.add("Same title", "one", "driver")["id"]
        b = self.kb.add("Same title", "two", "driver")["id"]
        self.assertNotEqual(a, b)
        for bad in ("../knowledge/beam-dumps", "/etc/passwd", "agent/../x"):
            with self.assertRaises(KBError):
                self.kb.get(bad)

    def test_broken_front_matter_stays_listed(self):
        (self.human / "broken.md").write_text("---\ntitle: [unclosed\n---\nbody\n")
        broken = [n for n in self.kb.notes() if n.id == "broken"][0]
        self.assertIn("bad front matter", broken.meta["error"])

    def test_note_refs_on_device_and_diagnose(self):
        device = add_note_refs(self.kb, "device", {"eslit": {"node": "mono"}, "i0": {}})
        self.assertEqual(device["eslit"]["notes"][0]["id"], "mono/exit-slit")
        self.assertNotIn("notes", device["i0"])
        diag = add_note_refs(self.kb, "diagnose", {"path": [
            {"node": "mono", "devices": [{"name": "eslit"}]}, {"node": "front_end", "devices": []},
        ]})
        self.assertEqual(diag["path"][0]["notes"][0]["id"], "mono/exit-slit")
        self.assertEqual(diag["path"][0]["devices"][0]["notes"][0]["id"], "mono/exit-slit")
        self.assertEqual(diag["path"][1]["notes"][0]["id"], "beam-dumps")


class TestStaffReview(KBTestCase):
    def setUp(self):
        super().setUp()
        self.review = self.root / "review"
        self.kb = KnowledgeBase(self.human, self.agent, self.review, clock=lambda: 0)

    def test_drafts_stay_out_of_search_until_approved(self):
        draft = self.kb.add("Slit backlash", "Approach eslit from wide.", "assistant", draft=True,
                            devices=["eslit"])
        self.assertEqual(draft["id"], "draft/slit-backlash")
        self.assertTrue(draft["draft"])
        self.assertEqual(self.kb.search("backlash"), [])
        self.assertEqual(self.kb.refs(device="eslit"), [
            {"id": "mono/exit-slit", "title": "Exit slit and resolution", "written_by": "human"}])
        queue = self.kb.review_queue()
        self.assertEqual([n["id"] for n in queue["notes"]], ["draft/slit-backlash"])
        self.assertEqual(queue["count"], 1)

        note = self.kb.approve("draft/slit-backlash", "Ana", body="Always approach eslit from wide.")
        self.assertEqual(note["id"], "slit-backlash")
        self.assertEqual(note["written_by"], "human")
        self.assertEqual(note["author"], "assistant")
        self.assertEqual(note["approved_by"], "Ana")
        self.assertEqual(note["devices"], ["eslit"])
        full = self.kb.get("slit-backlash")
        self.assertEqual(full.meta["drafted_from"], "draft/slit-backlash")
        self.assertIn("Always approach", full.body)
        self.assertEqual(self.kb.search("backlash")[0]["id"], "slit-backlash")
        self.assertEqual(self.kb.drafts(), [])
        self.assertEqual(len(list((self.review / "archive" / "draft").iterdir())), 1)
        self.assertEqual(self.kb.review_queue()["count"], 0)

    def test_approved_notes_go_into_the_profile(self):
        self.kb.add("Slit backlash", "b", "assistant", draft=True)
        self.kb.approve("draft/slit-backlash", "Ana")
        self.assertTrue((self.human / "slit-backlash.md").is_file())
        self.kb.write_note("Ana", "Beam dumps", "a second one")
        self.assertTrue((self.human / "beam-dumps-2.md").is_file())
        self.assertEqual((self.human / "beam-dumps.md").read_text(), DUMP)
        nohuman = KnowledgeBase(None, self.agent, self.review)
        nohuman.add("Other", "b", "assistant", draft=True)
        with self.assertRaisesRegex(KBError, "kb_dir"):
            nohuman.approve("draft/other", "Ana")

    def test_agents_cannot_edit_notes_staff_wrote(self):
        self.kb.write_note("Ana", "Gate valves", "Open gv_es after the chamber is below 1e-7.")
        with self.assertRaises(KBError):
            self.kb.edit("gate-valves", "assistant", body="x")
        self.kb.append("gate-valves", "Took 20 min today.", "assistant")
        self.assertEqual(self.kb.get("gate-valves").appendix.count("## Appended by"), 1)

    def test_web_note_edits_and_names(self):
        with self.assertRaises(KBError):
            self.kb.write_note("  ", "t", "b")
        self.kb.write_note("Ana", "Gate valves", "Old.", topic="procedure", tags=["vacuum"])
        note = self.kb.write_note("Ben", "Gate valves", "New.", note_id="gate-valves")
        full = self.kb.get(note["id"])
        self.assertEqual(full.body.strip(), "New.")
        self.assertEqual((full.meta["author"], full.meta["edited_by"]), ("Ana", "Ben"))
        self.assertNotIn("tags", full.meta)
        self.kb.add("Note", "agent text", "assistant")
        with self.assertRaises(KBError):
            self.kb.write_note("Ana", "t", "b", note_id="agent/note")
        # Staff can edit any profile note from the web page
        self.kb.write_note("Ana", "Beam dumps", "Edited.", note_id="beam-dumps", topic="problem")
        self.assertEqual(self.kb.get("beam-dumps").meta["edited_by"], "Ana")

    def test_keep_and_reject_agent_notes(self):
        self.kb.add("Slit drift", "eslit drifts.", "assistant")
        self.kb.add("Wrong idea", "nonsense", "assistant")
        self.kb.append("agent/wrong-idea", "more", "assistant")
        self.kb.keep("agent/slit-drift", "Ana")
        self.assertEqual([n["id"] for n in self.kb.review_queue()["notes"]], ["agent/wrong-idea"])
        out = self.kb.reject("agent/wrong-idea", "Ana", "not true")
        rejected = Path(out["file"]).read_text()
        self.assertIn("reason: not true", rejected)
        self.assertIn("more", rejected)
        self.assertNotIn("agent/wrong-idea", [n.id for n in self.kb.notes()])
        self.assertEqual(self.kb.review_queue()["count"], 0)
        # An agent changing a kept note puts it back on the list
        os.utime(self.kb.get("agent/slit-drift").path, ns=(1, 1))
        self.assertEqual([n["id"] for n in self.kb.review_queue()["notes"]], ["agent/slit-drift"])
        with self.assertRaises(KBError):
            self.kb.reject("beam-dumps", "Ana")

    def test_appendix_entries_are_reviewed_or_removed(self):
        for text in ("first", "second", "third"):
            self.kb.append("beam-dumps", text, "assistant")
        self.assertEqual([e["text"] for e in self.kb.review_queue()["appendices"][0]["entries"]],
                         ["first", "second", "third"])
        self.kb.review_appendix("beam-dumps", "Ana")
        self.assertEqual(self.kb.review_queue()["appendices"], [])
        self.kb.remove_entry("beam-dumps", 1, "Ana", "wrong")
        self.assertEqual([e["text"] for e in self.kb.entries("beam-dumps")], ["first", "third"])
        self.kb.append("beam-dumps", "fourth", "assistant")
        waiting = self.kb.review_queue()["appendices"][0]["entries"]
        self.assertEqual([(e["index"], e["text"]) for e in waiting], [(2, "fourth")])
        self.assertIn("second", (self.review / "rejected" / "appendix" / "beam-dumps.md").read_text())
        self.assertEqual((self.human / "beam-dumps.md").read_text(), DUMP)

    def test_approval_moves_the_appendix(self):
        self.kb.add("Slit drift", "eslit drifts.", "assistant")
        self.kb.append("agent/slit-drift", "again", "driver")
        self.kb.review_appendix("agent/slit-drift", "Ana")
        self.kb.approve("agent/slit-drift", "Ana")
        self.assertIn("again", self.kb.get("slit-drift").appendix)
        self.assertEqual(self.kb.review_queue()["count"], 0)


class TestKBCommand(KBTestCase):
    def cli(self, *argv, role=None):
        env = {"NBS_KB_DIR": str(self.human), "NBS_KB_AGENT_DIR": str(self.agent)}
        if role:
            env["NBS_CLI_ROLE"] = role
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), redirect_stdout(out):
            status = main(["--config", "/dev/null", *argv])
        return status, json.loads(out.getvalue())

    def test_missing_kb_dir_is_reported_not_silent(self):
        from contextlib import redirect_stderr

        err = io.StringIO()
        with mock.patch.object(self, "human", self.human / "missing"), redirect_stderr(err):
            status, notes = self.cli("kb", "list")
        self.assertEqual(status, 0)
        self.assertIn("is not a folder here", err.getvalue())

    def test_assistant_can_add_and_append_but_not_edit_human_notes(self):
        status, note = self.cli("kb", "add", "--title", "U7 is weak", "--text", "3% signal.",
                                "--devices", "sc", role="assistant")
        self.assertEqual(status, 0)
        self.assertEqual(note["author"], "assistant")
        self.assertEqual(note["written_by"], "agent")
        status, _ = self.cli("kb", "append", "beam-dumps", "--text", "Seen 19:40.", role="assistant")
        self.assertEqual(status, 0)
        status, err = self.cli("kb", "edit", "beam-dumps", "--text", "gone", role="assistant")
        self.assertEqual(status, 1)
        self.assertIn("append", err["error"])
        self.assertEqual((self.human / "beam-dumps.md").read_text(), DUMP)

    def test_driver_attribution_names_the_session(self):
        _, note = self.cli("kb", "add", "--title", "t", "--text", "b", "--agent-uid", "0123456789abcdef")
        self.assertEqual(note["author"], "driver 01234567")

    def test_search_and_show(self):
        _, hits = self.cli("kb", "search", "ring", "current")
        self.assertEqual(hits[0]["id"], "beam-dumps")
        _, full = self.cli("kb", "show", "mono/exit-slit")
        self.assertIn("coarser energy resolution", full["body"])
        status, err = self.cli("kb", "show", "nope")
        self.assertEqual(status, 1)

    def test_assistant_can_write_drafts(self):
        status, note = self.cli("kb", "add", "--draft", "--title", "Slit backlash", "--text", "b",
                                role="assistant")
        self.assertEqual((status, note["id"]), (0, "draft/slit-backlash"))
        _, drafts = self.cli("kb", "list", "--drafts")
        self.assertEqual([d["id"] for d in drafts], ["draft/slit-backlash"])
        _, listed = self.cli("kb", "list")
        self.assertNotIn("draft/slit-backlash", [n["id"] for n in listed])

    def test_file_must_be_in_the_current_folder(self):
        status, err = self.cli("kb", "add", "--title", "t", "--file", "/etc/hostname")
        self.assertEqual(status, 1)


if __name__ == "__main__":
    unittest.main()
