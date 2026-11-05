"""Tests for the $NBS_AGENTS_HOME layout and `nbs-agents init/check/paths`."""

import stat

from nbs_agents import admin
from nbs_agents.config import AgentLogConfig, AgentsConfig
import pytest

from nbs_agents.paths import BEAMLINE_ENV, HOME_ENV, BeamlineError, beamlines, paths, use_beamline


def test_every_default_is_under_the_beamline_folder(nbs_agents_home, beamline_dir):
    p = paths()
    assert p.home == nbs_agents_home and p.beamline == "testbl" and p.root == beamline_dir
    assert p.config_file == beamline_dir / "config" / "nbs-cli.toml"
    assert AgentLogConfig().data_dir == str(beamline_dir / "state" / "agent-log")
    assert p.tiled_agent_key == beamline_dir / "state" / "tiled-agent" / "api-key.json"
    assert p.tiled_cache == nbs_agents_home / "tiled"  # one login for every beamline


def test_home_defaults_to_dot_nbs_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(HOME_ENV)
    assert paths().home == tmp_path / ".nbs-agents"


def test_beamline_choice(nbs_agents_home, monkeypatch):
    monkeypatch.delenv(BEAMLINE_ENV)
    with pytest.raises(BeamlineError, match="no beamline set up"):
        paths()
    with pytest.raises(BeamlineError, match="not set up"):
        use_beamline("sst1")
    use_beamline("sst1", must_exist=False)
    admin.init(out=lambda line: None)
    monkeypatch.delenv(BEAMLINE_ENV)
    assert beamlines() == ["sst1"] and paths().beamline == "sst1"  # the only one
    use_beamline("sst2", must_exist=False)
    admin.init(out=lambda line: None)
    monkeypatch.delenv(BEAMLINE_ENV)
    with pytest.raises(BeamlineError, match="several beamlines.*sst1, sst2"):
        paths()
    assert paths("sst2").root == nbs_agents_home / "sst2"
    assert use_beamline("sst1") == "sst1" and paths().beamline == "sst1"
    with pytest.raises(BeamlineError, match="bad beamline name"):
        paths("../etc")


def test_main_takes_beamline(nbs_agents_home, monkeypatch, capsys):
    monkeypatch.delenv(BEAMLINE_ENV)
    assert admin.main(["check"]) == 2
    assert "no beamline set up" in capsys.readouterr().err
    assert admin.main(["init", "--beamline", "sst1"]) == 0
    monkeypatch.delenv(BEAMLINE_ENV)
    assert admin.main(["paths"]) == 0
    assert "sst1" in capsys.readouterr().out
    assert admin.main(["beamlines"]) == 0
    assert "* sst1" in capsys.readouterr().out


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
    assert data["agent_log"] == {}  # its own Redis by default


def test_check_reports_files_and_unreachable_services(nbs_agents_home, monkeypatch):
    monkeypatch.setattr(admin, "PROBE_TIMEOUT_S", 0.5)
    admin.init(out=lambda line: None)
    p = paths()
    p.config_file.write_text(
        '[nbs_cli]\nqs_addr = "tcp://127.0.0.1:1"\ntiled_uri = "http://127.0.0.1:1"\n'
    )
    lines = []
    assert admin.check(out=lines.append) == 1
    text = "\n".join(lines)
    assert "  ok  config" in text
    assert "FAIL  QueueServer" in text and "FAIL  Tiled" in text and "beamline Redis" not in text

    # Packages extend the rows before reporting
    rows = admin.check_rows() + [(admin.OK, "extra", "from a package")]
    lines.clear()
    admin.report(rows, out=lines.append)
    assert lines[-1].startswith("  ok  extra")


def test_paths_command_lists_every_location(nbs_agents_home):
    lines = []
    assert admin.show_paths(out=lines.append) == 0
    assert lines[0].split()[1] == "testbl" and lines[1].split()[1] == str(nbs_agents_home)
    assert len(lines) == 14


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
