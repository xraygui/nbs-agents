"""Tests for the nbs-gui Assistant tab and the Agents Log tab's firewall."""

import os
import time

import pytest

pytest.importorskip("qtpy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtCore import QUrl  # noqa: E402
from qtpy.QtWidgets import QApplication  # noqa: E402

from fakes import FakeRedis  # noqa: E402
from qt_fakes import FakeModel, qapp  # noqa: E402

from nbs_agents.agentlog import AgentLog, assistant_uid  # noqa: E402
from nbs_agents.qt.tabs.agent_log import AgentLogTab  # noqa: E402
from nbs_agents.qt.tabs.assistant import (  # noqa: E402
    AssistantTab,
    assistant_state,
    call_summary,
    entry_html,
    feed_html,
)


def today_record(**extra):
    now = time.time()
    record = {
        "agent_uid": assistant_uid(),
        "agent_name": "assistant",
        "kind": "assistant",
        "status": "watching",
        "heartbeat": now,
        "calls": 2,
        "last_call": {"t": now, "reasons": ["alert"], "model": "opus", "returncode": 0},
        "log": [
            {"t": now, "kind": "run", "text": "scan 5 c_xas success on s4, J/N 2.1", "run_uid": "r4uid-long",
             "flags": ["low jump to noise 2.1 < 10"]},
            {"t": now, "kind": "run", "text": "scan 6 c_xas fail on s6", "run_uid": "r6", "alerts": ["run fail"]},
            {"t": now, "kind": "alert", "text": "s6 failed <check>", "run_uids": ["r6"]},
            {"t": now, "kind": "advice", "text": "s4 looks displaced", "run_uids": ["r4uid-long"]},
            {"t": now, "kind": "summary", "text": "3 runs done"},
            {"t": now, "kind": "operator", "text": "is s4 ok?", "source": "gui"},
            {"t": now, "kind": "answer", "text": "probably displaced"},
        ],
    }
    record.update(extra)
    return record


def test_state_from_heartbeat_and_status():
    now = 1000.0
    assert assistant_state(None)[0].startswith("Assistant: no record")
    assert assistant_state({"status": "watching", "heartbeat": now}, now) == ("Assistant: watching", "#2e7d32")
    assert assistant_state({"status": "calling", "heartbeat": now}, now)[0] == "Assistant: thinking…"
    assert assistant_state({"status": "watching", "heartbeat": now - 500}, now)[0] == "Assistant: not running"
    assert assistant_state({"status": "stopped", "heartbeat": now}, now)[0] == "Assistant: not running"


def test_call_summary():
    text = call_summary(today_record(pending=1))
    assert text.startswith("2 call(s) today")
    assert "for alert (opus)" in text
    assert "1 event(s) waiting" in text
    assert call_summary(None) == ""


def test_entries_are_styled_by_kind_and_escaped():
    record = today_record()
    html = feed_html(record)
    for tag in ("Alert", "Advice", "Summary", "Answer", "Operator"):
        assert tag in html
    assert "s6 failed &lt;check&gt;" in html
    assert "href='run:r4uid-long'" in html
    assert "flag:</b> low jump to noise" in html
    assert "alert:</b> run fail" in html
    assert "#f9dcdb" in entry_html(record["log"][2])  # alert colour
    assert "#dbe8fb" in entry_html(record["log"][3])  # advice colour
    without_runs = feed_html(record, show_runs=False)
    assert "scan 5" not in without_runs
    assert "Nothing from the assistant" in feed_html(None)


def test_tab_shows_today_and_asks_questions():
    app = qapp()
    redis = FakeRedis()
    writer = AgentLog(redis, "bl-")
    data = {assistant_uid(): today_record(), "u1": {"agent_name": "claude", "status": "active", "log": []}}
    model = FakeModel(data)
    model.user_status.redis_dict._redis = redis
    tab = AssistantTab(model)
    assert tab.name == "Assistant"
    assert tab.state_label.text() == "Assistant: watching"
    assert "probably displaced" in tab.feed.toPlainText()
    tab.show_runs.setChecked(False)
    assert "scan 5" not in tab.feed.toPlainText()

    tab.question_edit.setText("is s6 worth repeating?")
    tab.ask()
    record = writer.get(assistant_uid())
    assert record["log"][-1]["kind"] == "operator"
    assert record["log"][-1]["text"] == "is s6 worth repeating?"
    assert record["log"][-1]["source"] == "gui"
    assert record["kind"] == "assistant"
    assert tab.question_edit.text() == ""
    assert writer.get("u1") is None  # nothing reaches a driver session

    seen = []
    tab.run_selected.connect(seen.append)
    tab._on_link(QUrl("run:r6"))
    assert seen == ["r6"]
    assert QApplication.clipboard().text() == "r6"
    tab.close()


def test_tab_without_record_or_redis():
    qapp()
    tab = AssistantTab(FakeModel({}))
    assert tab.state_label.text().startswith("Assistant: no record")

    class Broken:
        def get_redis_dict(self, topic):
            raise RuntimeError("no redis")

    model = FakeModel({})
    model.user_status = Broken()
    tab = AssistantTab(model)
    assert "no redis" in tab.state_label.text()
    assert not tab.ask_button.isEnabled()


def agents_log_data():
    return {
        assistant_uid(): today_record(),
        "assistant-20200101": dict(today_record(), status="closed"),
        "u1": {"agent_name": "claude", "status": "active", "opened": 1.0, "updated": 1.0,
               "log": [{"t": 1.0, "kind": "note", "text": "driver note"}, {"t": 2.0, "kind": "question", "text": "go?"}]},
    }


def test_agents_log_chat_shows_assistant_posts():
    qapp()
    redis = FakeRedis()
    model = FakeModel(agents_log_data())
    model.user_status.redis_dict._redis = redis
    tab = AgentLogTab(model)
    # Today's assistant record is listed; older days are not
    uids = [tab.agent_combo.itemData(i) for i in range(tab.agent_combo.count())]
    assert assistant_uid() in uids and "assistant-20200101" not in uids
    tab.select_agent(assistant_uid())
    chat = tab.conversation.toPlainText()
    for text in ("s6 failed <check>", "s4 looks displaced", "3 runs done", "is s4 ok?", "probably displaced"):
        assert text in chat, text
    for tag in ("Alert", "Advice", "Summary", "Answer"):
        assert tag in chat, tag
    # Run lines stay in the activity table
    assert "scan 5 c_xas" not in chat
    activity = [tab.entries.item(r, 3).text() for r in range(tab.entries.rowCount())]
    assert any("scan 5 c_xas" in t for t in activity)
    assert not any("probably displaced" in t for t in activity)
    html = tab._conversation_html
    assert "href='run:r6'" in html
    assert "color:#c62828'>Alert" in html


def test_agents_log_asks_the_assistant_without_a_stop_button():
    qapp()
    redis = FakeRedis()
    writer = AgentLog(redis, "bl-")
    model = FakeModel(agents_log_data())
    model.user_status.redis_dict._redis = redis
    tab = AgentLogTab(model)
    tab.show()
    tab.select_agent(assistant_uid())
    assert tab.message_edit.isEnabled()
    assert tab.message_edit.placeholderText() == "Ask the assistant…"
    assert tab.stop_button.isHidden()
    tab.message_edit.setText("Is U7 worth another scan?")
    tab.send_message()
    entry = writer.get(assistant_uid())["log"][-1]
    assert (entry["kind"], entry["text"], entry["source"]) == ("operator", "Is U7 worth another scan?", "gui")
    tab.request_stop(confirm=False)
    assert "stop_requested" not in writer.get(assistant_uid())
    # Driver sessions keep the stop button and their notes and questions show as bubbles
    tab.select_agent("u1")
    assert not tab.stop_button.isHidden()
    assert "driver note" in tab.conversation.toPlainText()
    assert "go?" in tab.conversation.toPlainText()
    tab.close()
