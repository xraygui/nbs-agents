"""Tests for the $NBS_AGENTS_HOME layout and `nbs-agents init/check/paths`."""

import os
import stat

from nbs_agents import admin
from nbs_agents.attachments import default_root
from nbs_agents.auth import token_path
from nbs_agents.cli.config import AgentLogConfig, AssistantConfig, CliConfig
from nbs_agents.cli.kb import KnowledgeBase
from nbs_agents.paths import HOME_ENV, paths


def test_every_default_is_under_the_home(nbs_agents_home, monkeypatch):
    monkeypatch.delenv("NBS_AGENTS_TOKEN_FILE")
    p = paths()
    assert p.home == nbs_agents_home
    assert p.config_file == nbs_agents_home / "config" / "nbs-cli.toml"
    config = CliConfig.load()
    assert config.claude_workdir == str(nbs_agents_home / "work" / "sessions" / "{uid8}")
    assert config.claude_log_dir == str(nbs_agents_home / "work" / "logs")
    assert AssistantConfig.load().workdir == str(nbs_agents_home / "work" / "assistant")
    assert AgentLogConfig().data_dir == str(nbs_agents_home / "state" / "agent-log")
    assert default_root() == nbs_agents_home / "data" / "attachments"
    assert KnowledgeBase().agent_dir == nbs_agents_home / "data" / "kb"
    assert token_path() == nbs_agents_home / "config" / "token"


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
    assert stat.S_IMODE(p.token.parent.stat().st_mode) == 0o700
    assert not p.gateway_env.exists()  # the gateway key normally comes from the environment
    for f in (p.config_file, p.token):
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert "[agent_log]" in p.config_file.read_text()
    assert (p.assistant / ".claude" / "settings.json").exists()
    CliConfig.load()  # the template parses

    p.config_file.write_text("[nbs_cli]\n")
    settings = p.assistant / ".claude" / "settings.json"
    settings.write_text("{}")
    admin.init(out=lines.append)
    assert p.config_file.read_text() == "[nbs_cli]\n"
    assert settings.read_text() == "{}"
    admin.init(refresh_assistant=True, out=lines.append)
    assert settings.read_text() != "{}" and (settings.parent / "settings.json.bak").read_text() == "{}"


def test_check_reports_files_and_unreachable_services(nbs_agents_home, monkeypatch):
    monkeypatch.setattr(admin, "PROBE_TIMEOUT_S", 0.5)
    monkeypatch.delenv("AIFAPIM_HOST", raising=False)
    monkeypatch.delenv("AIFAPIM_API_KEY", raising=False)
    admin.init(out=lambda line: None)
    p = paths()
    p.config_file.write_text(
        '[nbs_cli]\nqs_addr = "tcp://127.0.0.1:1"\nredis_port = 1\ntiled_uri = "http://127.0.0.1:1"\n'
    )
    lines = []
    assert admin.check(out=lines.append) == 1
    text = "\n".join(lines)
    assert "warn  gateway" in text and "not in the environment" in text
    assert " ok  web token" in text
    assert "FAIL  beamline Redis" in text and "FAIL  Tiled" in text
    assert "AIFAPIM_API_KEY=" not in text

    # A key file is the fallback, and must be private
    p.gateway_env.write_text("AIFAPIM_HOST=h\nAIFAPIM_API_KEY=k\n")
    os.chmod(p.gateway_env, 0o644)
    lines.clear()
    admin.check(out=lines.append)
    assert any(line.startswith("FAIL  gateway") and "mode 644" in line for line in lines)
    os.chmod(p.gateway_env, 0o600)
    lines.clear()
    admin.check(out=lines.append)
    assert any(line.startswith("  ok  gateway") and "from " in line for line in lines)

    # The environment wins, and its values are never printed
    monkeypatch.setenv("AIFAPIM_HOST", "gw.example")
    monkeypatch.setenv("AIFAPIM_API_KEY", "s3cret-value")
    lines.clear()
    admin.check(out=lines.append)
    assert any(line.startswith("  ok  gateway") and "environment" in line for line in lines)
    assert "s3cret-value" not in "\n".join(lines)


def test_paths_command_lists_every_location(nbs_agents_home):
    lines = []
    assert admin.show_paths(out=lines.append) == 0
    assert lines[0].split()[1] == str(nbs_agents_home)
    assert len(lines) == 12


def test_init_writes_this_beamlines_tiled_catalog(nbs_agents_home):
    admin.init(out=lambda line: None, tiled_uri="https://tiled.example.org", tiled_path="bl/raw")
    config = CliConfig.load()
    assert (config.tiled_uri, config.tiled_path) == ("https://tiled.example.org", "bl/raw")
    lines = []
    admin.init(out=lines.append, tiled_path="other/raw")
    assert any("already exists" in line for line in lines)
    assert CliConfig.load().tiled_path == "bl/raw"


def test_check_warns_without_a_catalog(nbs_agents_home, monkeypatch):
    monkeypatch.setattr(admin, "PROBE_TIMEOUT_S", 0.2)
    admin.init(out=lambda line: None)
    lines = []
    admin.check(out=lines.append)
    assert any(line.startswith("warn  tiled_path") for line in lines)
