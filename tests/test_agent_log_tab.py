"""Tests for the nbs-gui agent log tab."""

import os

import pytest

pytest.importorskip("qtpy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtCore import QObject, Signal  # noqa: E402
from qtpy.QtWidgets import QApplication  # noqa: E402

from nbs_agents.qt.tabs.agent_log import (  # noqa: E402
    ALL_AGENTS,
    AgentLogTab,
    collect_entries,
    entry_details,
    sorted_sessions,
)

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


def test_tab_follows_redis_changes():
    app = QApplication.instance() or QApplication([])
    data = {k: dict(v, log=list(v["log"])) for k, v in RECORDS.items()}
    model = FakeModel(data)
    tab = AgentLogTab(model)
    assert tab.sessions.rowCount() == 3
    assert tab.entries.rowCount() == 3

    tab.sessions.selectRow(2)
    assert tab._selected == "bbbbbbbb-2"
    assert tab.entries.rowCount() == 1

    data["bbbbbbbb-2"]["log"].append({"t": 400.0, "kind": "close", "text": "done"})
    model.user_status.redis_dict.changed.emit()
    assert tab.entries.rowCount() == 2
    assert tab.entries.item(1, 3).text() == "done"

    del data["bbbbbbbb-2"]
    model.user_status.redis_dict.changed.emit()
    assert tab._selected == ALL_AGENTS
    assert tab.entries.rowCount() == 2
    app.processEvents()


def test_tab_without_redis():
    QApplication.instance() or QApplication([])

    class NoRedis:
        def get_redis_dict(self, topic):
            return None

    model = FakeModel({})
    model.user_status = NoRedis()
    tab = AgentLogTab(model)
    assert tab.entries.rowCount() == 0
    assert "unavailable" in tab.summary.text()
