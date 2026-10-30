"""Tests for the nbs-gui agent log tab."""

import os

import pytest

pytest.importorskip("qtpy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtCore import QObject, Signal  # noqa: E402
from qtpy.QtWidgets import QApplication  # noqa: E402

from fakes import FakeRedis  # noqa: E402

from nbs_agents.qt.tabs.agent_log import (  # noqa: E402
    ALL_AGENTS,
    AgentLogTab,
    collect_entries,
    conversation_html,
    entry_details,
    is_awaiting_input,
    sorted_sessions,
    split_entries,
)

_APP = None


def qapp():
    """Return the QApplication, keeping a reference so it is not garbage collected."""
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


RECORDS = {
    "aaaaaaaa-1": {
        "agent_name": "claude",
        "status": "active",
        "goal": "map C K-edge",
        "opened": 100.0,
        "updated": 300.0,
        "log": [
            {"t": 100.0, "kind": "open", "text": "map C K-edge", "source": "claude"},
            {"t": 250.0, "kind": "run", "text": "xas finished", "run_uid": "r1"},
        ],
    },
    "bbbbbbbb-2": {
        "agent_name": "repeat",
        "status": "closed",
        "opened": 50.0,
        "updated": 200.0,
        "log": [{"t": 150.0, "kind": "note", "text": "hi"}],
    },
}


class FakeRedisDict(QObject):
    changed = Signal()

    def __init__(self, data):
        super().__init__()
        self.data = data
        self._redis = None
        self._prefix = "bl-"

    def items(self):
        return self.data.items()


class FakeUserStatus:
    def __init__(self, data):
        self.redis_dict = FakeRedisDict(data)

    def get_redis_dict(self, topic):
        assert topic == "AGENT"
        return self.redis_dict


class FakeModel:
    def __init__(self, data):
        self.user_status = FakeUserStatus(data)


def test_sorted_sessions_newest_first():
    assert [uid for uid, _ in sorted_sessions(RECORDS)] == ["aaaaaaaa-1", "bbbbbbbb-2"]


def test_collect_entries_merges_by_time():
    texts = [entry["text"] for _, entry in collect_entries(RECORDS)]
    assert texts == ["map C K-edge", "hi", "xas finished"]
    assert len(collect_entries(RECORDS, "bbbbbbbb-2")) == 1


def test_entry_details_shows_refs_only():
    assert entry_details({"t": 1, "kind": "run", "text": "x", "source": "s", "run_uid": "r1"}) == "run_uid=r1"


def test_split_entries_separates_conversation():
    conversation, activity = split_entries(collect_entries(RECORDS))
    assert [e["kind"] for _, e in conversation] == ["note"]
    assert [e["kind"] for _, e in activity] == ["open", "run"]


def test_awaiting_input_only_while_running():
    record = {"status": "active", "awaiting_input": {"t": 1.0, "text": "go?"}}
    assert is_awaiting_input(record)
    assert not is_awaiting_input(dict(record, status="closed"))
    assert not is_awaiting_input({"status": "active"})


def test_conversation_html_marks_open_question_and_escapes():
    records = {
        "u1": {
            "agent_name": "claude",
            "status": "active",
            "awaiting_input": {"t": 2.0, "text": "x <or> y?"},
            "log": [
                {"t": 1.0, "kind": "question", "text": "old one"},
                {"t": 2.0, "kind": "question", "text": "x <or> y?"},
            ],
        }
    }
    conversation, _ = split_entries(collect_entries(records, "u1"))
    text = conversation_html(records, conversation)
    assert text.count("Waiting for your answer") == 1
    assert "x &lt;or&gt; y?" in text


def test_tab_follows_redis_changes():
    app = qapp()
    data = {k: dict(v, log=list(v["log"])) for k, v in RECORDS.items()}
    model = FakeModel(data)
    tab = AgentLogTab(model)
    assert tab._selected == "aaaaaaaa-1"  # newest running session
    tab.select_agent(ALL_AGENTS)
    assert tab.agent_combo.count() == 3
    assert tab.entries.rowCount() == 2
    assert "hi" in tab.conversation.toPlainText()

    tab.select_agent("bbbbbbbb-2")
    assert tab._selected == "bbbbbbbb-2"
    assert tab.entries.rowCount() == 0

    data["bbbbbbbb-2"]["log"].append({"t": 400.0, "kind": "close", "text": "done"})
    model.user_status.redis_dict.changed.emit()
    assert tab.entries.rowCount() == 1
    assert tab.entries.item(0, 3).text() == "done"

    del data["bbbbbbbb-2"]
    model.user_status.redis_dict.changed.emit()
    assert tab._selected == ALL_AGENTS
    assert tab.agent_combo.count() == 2
    assert tab.entries.rowCount() == 2
    app.processEvents()


def test_tab_shows_waiting_banner_and_title():
    qapp()
    from qtpy.QtWidgets import QTabWidget

    from nbs_agents.agentlog import AgentLog

    redis = FakeRedis()
    log = AgentLog(redis, "bl-")
    log.open("u1", "claude", goal="g")
    data = {"u1": log.get("u1")}
    model = FakeModel(data)
    model.user_status.redis_dict._redis = redis
    tabs = QTabWidget()
    tab = AgentLogTab(model)
    tabs.addTab(tab, tab.name)
    tab.select_agent(ALL_AGENTS)
    assert tab.banner.isHidden()

    log.ask("u1", "which motors?")
    data["u1"] = log.get("u1")
    tab.refresh()
    assert not tab.banner.isHidden()
    assert "which motors?" in tab.banner.text()
    assert "Reply" in tab.banner.text()
    assert "waiting for input" in tab.agent_combo.itemText(0)
    assert tabs.tabText(0) == "Agents Log (1 waiting)"

    tab.banner.linkActivated.emit("u1")
    assert tab._selected == "u1"
    tab.message_edit.setText("x and y")
    tab.send_message()
    data["u1"] = log.get("u1")
    tab.refresh()
    assert tab.banner.isHidden()
    assert tabs.tabText(0) == "Agents Log"
    assert "x and y" in tab.conversation.toPlainText()


def test_tab_without_redis():
    qapp()

    class NoRedis:
        def get_redis_dict(self, topic):
            return None

    model = FakeModel({})
    model.user_status = NoRedis()
    tab = AgentLogTab(model)
    assert tab.entries.rowCount() == 0
    assert "unavailable" in tab.summary.text()


def test_tab_sends_messages_and_stop():
    qapp()
    from nbs_agents.agentlog import AgentLog

    redis = FakeRedis()
    log = AgentLog(redis, "bl-")
    log.open("u1", "claude", goal="g")
    model = FakeModel({"u1": log.get("u1")})
    model.user_status.redis_dict._redis = redis
    tab = AgentLogTab(model)
    tab.select_agent(ALL_AGENTS)
    assert not tab.send_button.isEnabled()

    tab.select_agent("u1")
    assert tab.send_button.isEnabled() and tab.stop_button.isEnabled()
    tab.message_edit.setText("skip s3")
    tab.send_message()
    assert tab.message_edit.text() == ""
    tab.message_edit.setText("beam dump")
    tab.request_stop(confirm=False)

    record = log.get("u1")
    assert [(e["kind"], e["text"], e["source"]) for e in record["log"][-2:]] == [
        ("operator", "skip s3", "gui"),
        ("stop", "beam dump", "gui"),
    ]
    model.user_status.redis_dict.data["u1"] = record
    tab.refresh()
    assert not tab.stop_button.isEnabled()
    assert "stop requested" in tab.summary.text()


class FakeRunEngine:
    def __init__(self, queue=()):
        from unittest.mock import MagicMock

        self._user_name = "gui-user"
        self._user_group = "primary"
        self._client = MagicMock()
        added = [{"item_uid": "op"}, {"item_uid": "qs"}, {"item_uid": "ca"}]
        self._client.item_add_batch.return_value = {"success": True, "items": added}
        self._client.queue_get.return_value = {"success": True, "items": list(queue) + added}
        self._client.status.return_value = {"manager_state": "idle"}
        self._client.queue_start.return_value = {"success": True}
        self.status_loads = 0

    def load_re_manager_status(self, unbuffered=False):
        self.status_loads += 1


def test_new_agent_dialog_builds_launch_parameters():
    qapp()
    from qtpy.QtWidgets import QDialogButtonBox

    from nbs_agents.qt.tabs.agent_log import NewAgentDialog

    dialog = NewAgentDialog(runner_alive=True)
    ok = dialog.buttons.button(QDialogButtonBox.Ok)
    assert not ok.isEnabled()
    dialog.goal_edit.setPlainText("  Map the C K-edge\non samples 1-3 ")
    assert ok.isEnabled()
    assert dialog.start_queue()
    params = dialog.parameters()
    assert set(params.pop("auth")) == {"t", "nonce", "sig"}
    assert params == {"goal": "Map the C K-edge\non samples 1-3", "launch": True}
    dialog.budget_edit.setText("2h")
    assert dialog.parameters()["budget"] == {"time_s": 7200.0}
    dialog.budget_edit.setText("soon")
    assert not ok.isEnabled()
    assert "bad duration" in dialog.error_label.text()

    down = NewAgentDialog(runner_alive=False)
    down.goal_edit.setPlainText("g")
    assert not down.buttons.button(QDialogButtonBox.Ok).isEnabled()
    assert "not running" in down.runner_label.text()
    unknown = NewAgentDialog(runner_alive=None)
    unknown.goal_edit.setPlainText("g")
    assert unknown.buttons.button(QDialogButtonBox.Ok).isEnabled()


def test_tab_submits_claude_block_and_starts_queue():
    qapp()
    from nbs_agents.agentlog import AgentLog

    redis = FakeRedis()
    model = FakeModel({})
    model.user_status.redis_dict._redis = redis
    model.run_engine = FakeRunEngine()
    tab = AgentLogTab(model)
    assert "unknown" not in tab.runner_label.text()
    assert not tab.new_agent_button.isEnabled()  # no heartbeat yet
    AgentLog(redis, "bl-").heartbeat()
    tab._update_runner_state()
    assert tab.new_agent_button.isEnabled()
    assert "running" in tab.runner_label.text()

    out = tab.submit_new_agent({"goal": "g", "launch": True})
    client = model.run_engine._client
    items = client.item_add_batch.call_args.args[0]
    assert [i.to_dict()["name"] for i in items] == ["open_agent", "queue_stop", "close_agent"]
    assert items[0].to_dict()["args"] == ["claude", {"goal": "g", "launch": True}]
    assert client.item_add_batch.call_args.kwargs == {"user": "gui-user", "user_group": "primary"}
    client.queue_start.assert_called_once()
    assert out["started"] and out["position"] == 0
    assert model.run_engine.status_loads == 1


def test_tab_does_not_start_queue_behind_other_items():
    qapp()
    model = FakeModel({})
    model.run_engine = FakeRunEngine(queue=[{"item_uid": "other"}])
    tab = AgentLogTab(model)
    out = tab.submit_new_agent({"goal": "g", "launch": True}, start=True)
    assert not out["started"] and out["position"] == 1
    model.run_engine._client.queue_start.assert_not_called()
    out = AgentLogTab(FakeModel({})).runner_alive()
    assert out is None
