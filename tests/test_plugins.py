"""Tests for the entry-point hooks other packages plug into: agents, runner plugins, launchers."""

from importlib.metadata import EntryPoint
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nbs_agents import launchers, runner, supervisor
from nbs_agents.agents.external import ExternalAgent
from nbs_agents.config import AgentsConfig
from nbs_agents.cli.config import CliConfig


def fake_entry_points(groups):
    def entry_points(group):
        return [EntryPoint(name, value, group) for name, value in groups.get(group, [])]
    return entry_points


def test_registry_adds_installed_agents_and_skips_broken_ones(monkeypatch):
    import importlib.metadata

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points({
        supervisor.AGENTS_ENTRY_POINT: [
            ("gp", "nbs_agents.agents.external:ExternalAgent"),
            ("broken", "no_such_module:Agent"),
        ],
    }))
    registry = supervisor.default_registry()
    assert registry.get("gp") is ExternalAgent
    assert registry.get("repeat") and registry.get("external")
    with pytest.raises(KeyError):
        registry.get("broken")
    with pytest.raises(KeyError):
        registry.get("claude")  # only when a package declares it


def make_observers(runtime):
    return [SimpleNamespace(runtime=runtime)]


def test_runner_loads_plugins_and_survives_a_failing_one(monkeypatch):
    import importlib.metadata

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points({
        runner.RUNNER_ENTRY_POINT: [
            ("mine", "test_plugins:make_observers"),
            ("bad", "no_such_module:plugin"),
        ],
    }))
    runtime = runner.Runtime(AgentsConfig(), MagicMock(), MagicMock(), None)
    observers = runner.load_plugins(runtime)
    assert len(observers) == 1 and observers[0].runtime is runtime


def test_supervisor_tells_observers_about_agents():
    seen = []

    class Watcher:
        def on_agent_opened(self, agent):
            seen.append(("opened", agent))

        def on_agent_closed(self, agent):
            seen.append(("closed", agent))

    sup = supervisor.AgentSupervisor(MagicMock(), MagicMock(), observers=[Watcher(), object()])
    agent = SimpleNamespace(agent_uid="u1")
    sup._notify("on_agent_opened", agent)
    sup._remove_agent(agent)
    assert seen == [("opened", agent), ("closed", agent)]
    sup.shutdown()


def test_launchers_come_from_entry_points(monkeypatch):
    import importlib.metadata

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points({
        launchers.LAUNCHERS_ENTRY_POINT: [("claude", "nbs_agents.launcher:claude_launch_parameters")],
    }))
    launchers._launchers.cache_clear()
    try:
        assert launchers.launcher_names() == ["claude"]
        assert launchers.launch_parameters("claude", "map C") == {"goal": "map C", "launch": True}
        with pytest.raises(ValueError, match="no launcher"):
            launchers.launch_parameters("gp", "x")
    finally:
        launchers._launchers.cache_clear()


def test_core_config_ignores_other_packages_settings(tmp_path):
    path = tmp_path / "nbs-cli.toml"
    path.write_text('[nbs_cli]\nqs_addr = "tcp://qs:1"\nclaude_command = "x"\n[agent_log]\nport = 60740\n')
    core = AgentsConfig.load(str(path))
    assert core.qs_addr == "tcp://qs:1" and core.agent_log.port == 60740
    assert not hasattr(core, "claude_command")
    assert CliConfig.load(str(path)).claude_command == "x"
    path.write_text('[nbs_cli]\nno_such_setting = 1\n')
    AgentsConfig.load(str(path))
    with pytest.raises(ValueError, match="Unknown nbs-cli settings"):
        CliConfig.load(str(path))
