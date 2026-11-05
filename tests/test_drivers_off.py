"""Tests for the assistant-only switch: [nbs_cli] drivers = false."""

from unittest.mock import MagicMock, patch

import pytest

from fakes import FakeRedis
from test_assistant_cli import make, run
from test_launcher import _open_run, _sentinel_manager
from test_web import FakeBeamline, client_for, ORIGIN

from nbs_agents.agentlog import AgentLog
from nbs_agents.supervisor import AgentSupervisor
from nbs_agents.assistant.claude_runner import DRIVERS_OFF, DriverRefusal
from nbs_agents.cli.config import CliConfig
from nbs_agents.cli.core import CliError
from nbs_agents.assistant.launcher import claude_launch_parameters
from nbs_agents.paths import paths


def test_setting_and_environment(monkeypatch):
    assert CliConfig().drivers and not CliConfig().read_only
    assert CliConfig(drivers=False).read_only
    monkeypatch.setenv("NBS_DRIVERS", "false")
    assert CliConfig.load().drivers is False
    monkeypatch.setenv("NBS_DRIVERS", "yes")
    assert CliConfig.load().drivers is True


def test_runner_closes_claude_sessions_and_releases_the_queue():
    log = AgentLog(FakeRedis(), "bl-")
    manager = _sentinel_manager()
    supervisor = AgentSupervisor(manager, MagicMock(), agent_log=log, observers=[DriverRefusal()])
    with patch("nbs_agents.assistant.claude_runner.release_queue_toward_close") as release:
        supervisor._activate_agent(_open_run({"goal": "map C", "launch": True}))
    release.assert_called_once_with(manager)
    record = log.get("uid-12345678")
    assert record["status"] == "closed" and record["close_reason"] == DRIVERS_OFF
    assert any(e["kind"] == "error" and e["text"] == DRIVERS_OFF for e in record["log"])
    assert "uid-12345678" not in supervisor.active_agents
    supervisor.shutdown()


def test_nbs_cli_refuses_queueing_verbs():
    bl, manager = make("driver")
    bl.config.drivers = False
    with pytest.raises(CliError, match="drivers = false"):
        run(bl, "add", "c_xas", "--reason", "x")
    manager.item_add.assert_not_called()


def test_new_agent_refused(monkeypatch):
    paths().config_file.parent.mkdir(parents=True, exist_ok=True)
    paths().config_file.write_text("[nbs_cli]\ndrivers = false\n")
    with pytest.raises(ValueError, match="Claude agents are off"):
        claude_launch_parameters("map C")

    bl = FakeBeamline()
    bl.config.drivers = False
    bl.agent_log.heartbeat()
    client = client_for(bl)
    assert client.get("/api/snapshot").json()["drivers"] is False
    res = client.post("/api/agents", json={"goal": "map C"}, headers=ORIGIN)
    assert res.status_code == 403 and "drivers = false" in res.json()["detail"]


def test_assistant_only_flag_turns_drivers_off(monkeypatch):
    from nbs_agents.assistant import admin

    seen = {}

    def fake_main(argv):
        seen["argv"], seen["drivers"] = argv, CliConfig.load().drivers
        return 0

    # Set it here so monkeypatch removes what the runner command sets
    monkeypatch.setenv("NBS_DRIVERS", "true")
    paths().config_file.parent.mkdir(parents=True, exist_ok=True)
    paths().config_file.write_text("[nbs_cli]\n")
    with patch("nbs_agents.runner.main", fake_main):
        assert admin.main(["runner", "--assistant-only", "--beamline", "testbl"]) == 0
    assert seen == {"argv": ["--beamline", "testbl"], "drivers": False}
