"""Tests for the assistant's init/check (nbs_agents.assistant.admin), which wrap nbs-agents' own."""

import os
import stat
import tomllib

import pytest

from nbs_agents import admin as core
from nbs_agents.paths import paths
from nbs_agents.assistant import admin
from nbs_agents.cli.config import AssistantConfig, CliConfig


def test_init_runs_core_then_adds_the_assistant(nbs_agents_home, monkeypatch):
    monkeypatch.delenv("NBS_AGENTS_TOKEN_FILE")
    lines = []
    assert admin.init(out=lines.append, tiled_uri="https://tiled.example.org", tiled_path="bl/raw") == 0
    p = paths()
    data = tomllib.loads(p.config_file.read_text())
    assert data["nbs_cli"]["tiled_path"] == "bl/raw"
    assert data["assistant"] == {"enabled": False}
    assert data["agent_log"]["port"] == 60740
    config = CliConfig.load()
    assert config.claude_command == "claude-beamline" and not AssistantConfig.load().enabled
    assert stat.S_IMODE(p.token.stat().st_mode) == 0o600
    assert (p.assistant / ".claude" / "settings.json").exists()

    settings = p.assistant / ".claude" / "settings.json"
    settings.write_text("{}")
    admin.init(out=lines.append)
    assert settings.read_text() == "{}"
    admin.init(out=lines.append, refresh_assistant=True)
    assert settings.read_text() != "{}" and (settings.parent / "settings.json.bak").read_text() == "{}"


def test_check_adds_gateway_and_claude_rows(nbs_agents_home, monkeypatch):
    monkeypatch.setattr(core, "PROBE_TIMEOUT_S", 0.3)
    monkeypatch.delenv("AIFAPIM_HOST", raising=False)
    monkeypatch.delenv("AIFAPIM_API_KEY", raising=False)
    monkeypatch.setenv("NBS_CLAUDE_SANDBOX", "bwclaude")
    admin.init(out=lambda line: None)
    p = paths()
    p.config_file.write_text(
        '[nbs_cli]\nqs_addr = "tcp://127.0.0.1:1"\nredis_port = 1\ntiled_uri = "http://127.0.0.1:1"\n'
    )
    lines = []
    assert admin.check(out=lines.append) == 1
    text = "\n".join(lines)
    assert "FAIL  beamline Redis" in text  # from nbs-agents
    assert "FAIL  gateway" in text and "not in the environment" in text  # bwclaude needs it
    assert " ok  web token" in text and "claude_command" in text

    # A key file is the fallback, and must be private
    p.gateway_env.write_text("AIFAPIM_HOST=h\nAIFAPIM_API_KEY=k\n")
    os.chmod(p.gateway_env, 0o644)
    lines.clear()
    admin.check(out=lines.append)
    assert any(line.startswith("FAIL  gateway") and "mode 644" in line for line in lines)

    # The environment wins, and its values are never printed
    monkeypatch.setenv("AIFAPIM_HOST", "gw.example")
    monkeypatch.setenv("AIFAPIM_API_KEY", "s3cret-value")
    lines.clear()
    admin.check(out=lines.append)
    assert any(line.startswith("  ok  gateway") and "environment" in line for line in lines)
    assert "s3cret-value" not in "\n".join(lines)


def test_init_drops_the_old_skill_link(nbs_agents_home, tmp_path):
    from nbs_agents.assistant.agent import SKILL_DIR

    link = paths().assistant / ".claude" / "skills" / SKILL_DIR.name
    link.parent.mkdir(parents=True)
    link.symlink_to(SKILL_DIR)
    admin.init(out=lambda line: None)
    assert not link.is_symlink() and not link.exists()


def test_check_fails_without_bwclaude_unless_sandbox_is_off(nbs_agents_home, monkeypatch):
    monkeypatch.setattr(core, "PROBE_TIMEOUT_S", 0.2)
    monkeypatch.setenv("NBS_BWCLAUDE", "")
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.delenv("AIFAPIM_HOST", raising=False)
    admin.init(out=lambda line: None)
    rows = {name: state for state, name, _ in admin.check_rows()}
    assert rows["bwclaude"] == core.WARN and rows["gateway"] == core.WARN  # no sandbox by default
    monkeypatch.setenv("NBS_CLAUDE_SANDBOX", "bwclaude")
    rows = {name: state for state, name, _ in admin.check_rows()}
    assert rows["bwclaude"] == core.FAIL and rows["gateway"] == core.FAIL
    monkeypatch.setenv("NBS_CLAUDE_SANDBOX", "none")
    rows = {name: state for state, name, _ in admin.check_rows()}
    assert rows["bwclaude"] == core.WARN and rows["gateway"] == core.WARN
    paths().config_file.write_text('[nbs_cli]\nclaude_sandbox = "bwclaude"\n')
    monkeypatch.delenv("NBS_CLAUDE_SANDBOX")
    rows = {name: state for state, name, _ in admin.check_rows()}
    assert rows["bwclaude"] == core.FAIL


def test_commands_take_a_beamline(nbs_agents_home, monkeypatch, capsys):
    from nbs_agents.paths import BEAMLINE_ENV

    monkeypatch.delenv(BEAMLINE_ENV)
    assert admin.main(["init", "--beamline", "sst1"]) == 0
    monkeypatch.delenv(BEAMLINE_ENV)
    assert admin.main(["init", "--beamline", "sst2"]) == 0
    monkeypatch.delenv(BEAMLINE_ENV)
    capsys.readouterr()
    assert admin.main(["paths"]) == 2
    assert "several beamlines" in capsys.readouterr().err
    assert admin.main(["paths", "--beamline", "sst2"]) == 0
    assert "sst2" in capsys.readouterr().out
