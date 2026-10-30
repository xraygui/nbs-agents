"""Tests for the agent log's own Redis: settings, published location and the served redis-server."""

import shutil
import socket

import pytest

from fakes import FakeRedis

from nbs_agents.agentlog import AgentLog
from nbs_agents.agentlog_store import AgentLogServer, location_key, publish_location, read_location
from nbs_agents.config import AgentLogConfig, AgentsConfig


def _write(tmp_path, text):
    path = tmp_path / "nbs-cli.toml"
    path.write_text(text)
    return str(path)


def test_agent_log_shares_beamline_redis_by_default(tmp_path):
    config = AgentsConfig.load(_write(tmp_path, '[nbs_cli]\nredis_port = 60737\nredis_db = 4\nredis_prefix = "claude-"\n'))
    assert not config.agent_log.dedicated
    assert config.agent_log_settings == config.redis_settings


def test_agent_log_table(tmp_path):
    config = AgentsConfig.load(_write(tmp_path, (
        '[nbs_cli]\nredis_port = 60737\nredis_prefix = "claude-"\n'
        '[agent_log]\nport = 60740\nserve = true\ndata_dir = "/x"\n'
    )))
    assert config.agent_log.dedicated and config.agent_log.serve
    assert config.agent_log_settings == {"host": "localhost", "port": 60740, "db": 0, "prefix": ""}
    assert config.redis_port == 60737


def test_agent_log_table_errors(tmp_path):
    with pytest.raises(ValueError, match="needs a port"):
        AgentsConfig.load(_write(tmp_path, "[agent_log]\nserve = true\n"))
    with pytest.raises(ValueError, match="Unknown \\[agent_log\\]"):
        AgentsConfig.load(_write(tmp_path, "[agent_log]\nprot = 1\n"))


def test_location_round_trip():
    redis = FakeRedis()
    redis.delete = lambda key: redis.set(key, "")
    assert read_location(redis, "claude-") is None
    settings = {"host": "localhost", "port": 60740, "db": 0, "prefix": ""}
    publish_location(redis, "claude-", settings)
    assert redis.get(location_key("claude-")) is not None
    assert read_location(redis, "claude-") == settings
    assert read_location(redis, "other-") is None
    publish_location(redis, "claude-", None)
    assert read_location(redis, "claude-") is None
    redis.set(location_key("claude-"), "not json")
    assert read_location(redis, "claude-") is None


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(shutil.which("redis-server") is None, reason="needs redis-server")
def test_server_starts_keeps_a_snapshot_and_is_reused(tmp_path):
    port = _free_port()
    config = AgentLogConfig(port=port, serve=True, data_dir=str(tmp_path / "agent-log"))
    server = AgentLogServer(config)
    assert server.start()
    try:
        log = AgentLog.from_settings({"host": "localhost", "port": port, "db": 0, "prefix": ""})
        log.open("u1", "claude", goal="map C")
        assert log.redis.config_get("notify-keyspace-events")["notify-keyspace-events"]
        # Another runner finds it running and leaves it alone
        assert not AgentLogServer(config).start()
    finally:
        server.stop()
    assert (tmp_path / "agent-log" / "agent-log.rdb").exists()

    # The snapshot brings the log back after a restart
    again = AgentLogServer(config)
    assert again.start()
    try:
        log = AgentLog.from_settings({"host": "localhost", "port": port, "db": 0, "prefix": ""})
        assert log.get("u1")["goal"] == "map C"
    finally:
        again.stop()
