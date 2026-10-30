"""Tests for how the Qt tabs find the agent log."""

import pytest

import sys
import types

from fakes import FakeRedis  # noqa: E402

from nbs_agents.agentlog_store import publish_location  # noqa: E402
from nbs_agents.qt import agent_log_source  # noqa: E402


class FakeRecords:
    def __init__(self, redis, prefix):
        self._redis, self._prefix = redis, prefix


class FakeUserStatus:
    def __init__(self, records):
        self.records = records

    def get_redis_dict(self, topic):
        return self.records


def test_uses_beamline_redis_without_a_published_location():
    records = FakeRecords(FakeRedis(), "claude-")
    assert agent_log_source.agent_log_dict(FakeUserStatus(records)) is records
    assert agent_log_source.agent_log_dict(FakeUserStatus(None)) is None


def test_connects_to_the_published_agent_log_redis(monkeypatch):
    made = []

    class QtRedisJSONDict:
        @classmethod
        def from_settings(cls, settings, topic="", parent=None):
            made.append((settings, topic, parent))
            return "dedicated"

    module = types.ModuleType("nbs_gui.models.QtRedisJSONDict")
    module.QtRedisJSONDict = QtRedisJSONDict
    monkeypatch.setitem(sys.modules, "nbs_gui.models.QtRedisJSONDict", module)
    records = FakeRecords(FakeRedis(), "claude-")
    settings = {"host": "localhost", "port": 60740, "db": 0, "prefix": ""}
    publish_location(records._redis, "claude-", settings)
    assert agent_log_source.agent_log_dict(FakeUserStatus(records), parent="tab") == "dedicated"
    assert made == [(settings, "AGENT", "tab")]
