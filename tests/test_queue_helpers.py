import unittest
from unittest.mock import MagicMock

from bluesky_queueserver_api import BInst, BPlan

from nbs_agents.constants import (
    AGENT_NAME_KEY,
    AGENT_PARAMETERS_KEY,
    AGENT_UID_KEY,
    CLOSE_AGENT_PLAN,
    OPEN_AGENT_PLAN,
    QUEUE_STOP_INSTRUCTION,
)
from nbs_agents.plans.agent_control import close_agent, open_agent
from nbs_agents.queue import (
    ensure_queue_stop_before_close,
    find_sentinel_pair,
    insert_before_queue_stop,
    make_open_agent_block,
    make_queue_stop,
    tag_agent_metadata,
)


class QueueHelperTests(unittest.TestCase):
    def test_make_open_agent_block_order(self):
        block = make_open_agent_block("repeat", {"repeat_count": 2})
        self.assertEqual(len(block), 3)
        self.assertEqual(block[0].name, OPEN_AGENT_PLAN)
        self.assertEqual(block[1].name, QUEUE_STOP_INSTRUCTION)
        self.assertEqual(block[2].name, CLOSE_AGENT_PLAN)

    def test_find_sentinel_pair(self):
        items = [
            {"item_type": "plan", "name": OPEN_AGENT_PLAN, "item_uid": "a"},
            {"item_type": "instruction", "name": QUEUE_STOP_INSTRUCTION, "item_uid": "b"},
            {"item_type": "plan", "name": CLOSE_AGENT_PLAN, "item_uid": "c", "args": ["uid-1"]},
        ]
        pair = find_sentinel_pair(items, agent_uid="uid-1")
        self.assertIsNotNone(pair)
        self.assertEqual(pair.queue_stop_uid, "b")
        self.assertEqual(pair.close_agent_uid, "c")

    def test_ensure_queue_stop_inserts_missing_instruction(self):
        manager = MagicMock()
        manager.queue_get.side_effect = [
            {
                "success": True,
                "items": [
                    {"item_type": "plan", "name": OPEN_AGENT_PLAN, "item_uid": "a"},
                    {"item_type": "plan", "name": CLOSE_AGENT_PLAN, "item_uid": "c", "args": ["uid-1"]},
                ],
            },
            {
                "success": True,
                "items": [
                    {"item_type": "plan", "name": OPEN_AGENT_PLAN, "item_uid": "a"},
                    {"item_type": "instruction", "name": QUEUE_STOP_INSTRUCTION, "item_uid": "b"},
                    {"item_type": "plan", "name": CLOSE_AGENT_PLAN, "item_uid": "c", "args": ["uid-1"]},
                ],
            },
        ]
        manager.item_add.return_value = {"success": True}

        pair = ensure_queue_stop_before_close(manager, agent_uid="uid-1")
        self.assertIsNotNone(pair)
        manager.item_add.assert_called_once()
        added = manager.item_add.call_args.args[0]
        self.assertIsInstance(added, BInst)
        self.assertEqual(added.name, QUEUE_STOP_INSTRUCTION)

    def test_insert_before_queue_stop(self):
        manager = MagicMock()
        manager.queue_get.return_value = {
            "success": True,
            "items": [
                {"item_type": "instruction", "name": QUEUE_STOP_INSTRUCTION, "item_uid": "b"},
                {"item_type": "plan", "name": CLOSE_AGENT_PLAN, "item_uid": "c", "args": ["uid-1"]},
            ],
        }
        manager.item_add.return_value = {"success": True}

        item = BPlan("count", [[]], num=1)
        insert_before_queue_stop(manager, item, "uid-1")
        manager.item_add.assert_called_once()
        self.assertEqual(manager.item_add.call_args.kwargs["before_uid"], "b")

    def test_tag_agent_metadata(self):
        item = BPlan("count", [[]], num=1)
        tagged = tag_agent_metadata(item, "uid-1", "repeat", 2)
        self.assertEqual(tagged.meta[AGENT_UID_KEY], "uid-1")
        self.assertEqual(tagged.meta[AGENT_NAME_KEY], "repeat")


    def test_release_queue_toward_close_waits_for_idle(self):
        manager = MagicMock()
        manager.queue_stop_cancel.return_value = {"success": True}
        manager.queue_start.return_value = {"success": True}

        from nbs_agents.queue import release_queue_toward_close

        release_queue_toward_close(manager)
        manager.wait_for_idle.assert_called_once_with(timeout=60)
        manager.queue_start.assert_called_once()


class ControlPlanTests(unittest.TestCase):
    def test_open_agent_metadata(self):
        msgs = list(open_agent("repeat", {"repeat_count": 2}))
        open_msg = msgs[0]
        self.assertEqual(open_msg.kwargs[AGENT_NAME_KEY], "repeat")
        self.assertEqual(open_msg.kwargs[AGENT_PARAMETERS_KEY], {"repeat_count": 2})
        self.assertIn(AGENT_UID_KEY, open_msg.kwargs)

    def test_close_agent_metadata(self):
        msgs = list(close_agent("uid-1"))
        open_msg = msgs[0]
        self.assertEqual(open_msg.kwargs[AGENT_UID_KEY], "uid-1")


if __name__ == "__main__":
    unittest.main()
