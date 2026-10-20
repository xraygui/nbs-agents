import unittest
from unittest.mock import MagicMock, patch

from nbs_agents.agents.repeat import RepeatAgent
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
