"""Tests for the $NBS_AGENTS_HOME layout and `nbs-agents init/check/paths`."""

import stat

from nbs_agents import admin
from nbs_agents.config import AgentLogConfig, AgentsConfig
from nbs_agents.paths import HOME_ENV, paths


def test_every_default_is_under_the_home(nbs_agents_home):
    p = paths()
    assert p.home == nbs_agents_home
    assert p.config_file == nbs_agents_home / "config" / "nbs-cli.toml"
    assert AgentLogConfig().data_dir == str(nbs_agents_home / "state" / "agent-log")
    assert p.tiled_agent_key == nbs_agents_home / "state" / "tiled-agent" / "api-key.json"


def test_home_defaults_to_dot_nbs_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(HOME_ENV)
    assert paths().home == tmp_path / ".nbs-agents"


def test_init_makes_layout_and_keeps_existing_files(nbs_agents_home):
    lines = []
    assert admin.init(out=lines.append) == 0
    p = paths()
    for folder in p.folders.values():
        assert folder.is_dir()
    for name in ("config", "tiled_cache", "tiled_agent"):
        assert stat.S_IMODE(p.folders[name].stat().st_mode) == 0o700
    assert stat.S_IMODE(p.config_file.stat().st_mode) == 0o600
    assert "[agent_log]" in p.config_file.read_text()
    AgentsConfig.load()  # the template parses

    p.config_file.write_text("[nbs_cli]\n")
    admin.init(out=lines.append)
    assert p.config_file.read_text() == "[nbs_cli]\n"


def test_init_lets_packages_add_settings_and_tables(nbs_agents_home):
    admin.init(
        out=lambda line: None,
        extra_settings='claude_command = "claude-beamline"',
        extra_tables="[assistant]\nenabled = false\n",
    )
    import tomllib

    data = tomllib.loads(paths().config_file.read_text())
    assert data["nbs_cli"]["claude_command"] == "claude-beamline"
    assert data["assistant"] == {"enabled": False}
    assert data["agent_log"]["port"] == 60740


def test_check_reports_files_and_unreachable_services(nbs_agents_home, monkeypatch):
    monkeypatch.setattr(admin, "PROBE_TIMEOUT_S", 0.5)
    admin.init(out=lambda line: None)
    p = paths()
    p.config_file.write_text(
        '[nbs_cli]\nqs_addr = "tcp://127.0.0.1:1"\nredis_port = 1\ntiled_uri = "http://127.0.0.1:1"\n'
    )
    lines = []
    assert admin.check(out=lines.append) == 1
    text = "\n".join(lines)
    assert "  ok  config" in text
    assert "FAIL  beamline Redis" in text and "FAIL  Tiled" in text

    # Packages extend the rows before reporting
    rows = admin.check_rows() + [(admin.OK, "extra", "from a package")]
    lines.clear()
    admin.report(rows, out=lines.append)
    assert lines[-1].startswith("  ok  extra")


def test_paths_command_lists_every_location(nbs_agents_home):
    lines = []
    assert admin.show_paths(out=lines.append) == 0
    assert lines[0].split()[1] == str(nbs_agents_home)
    assert len(lines) == 12


def test_init_writes_this_beamlines_tiled_catalog(nbs_agents_home):
    admin.init(out=lambda line: None, tiled_uri="https://tiled.example.org", tiled_path="bl/raw")
    config = AgentsConfig.load()
    assert (config.tiled_uri, config.tiled_path) == ("https://tiled.example.org", "bl/raw")
    lines = []
    admin.init(out=lines.append, tiled_path="other/raw")
    assert any("already exists" in line for line in lines)
    assert AgentsConfig.load().tiled_path == "bl/raw"


def test_check_warns_without_a_catalog(nbs_agents_home, monkeypatch):
    monkeypatch.setattr(admin, "PROBE_TIMEOUT_S", 0.2)
    admin.init(out=lambda line: None)
    lines = []
    admin.check(out=lines.append)
    assert any(line.startswith("warn  tiled_path") for line in lines)
