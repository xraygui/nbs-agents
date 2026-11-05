"""Tests for the nbs-agents-web login token file."""

import os
import stat

import pytest

from nbs_agents.assistant.auth import TokenError, check_token, ensure_token, read_token, token_path


def test_token_is_private_and_stable(nbs_agents_token):
    path = token_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert ensure_token() == read_token() == nbs_agents_token
    assert len(nbs_agents_token) >= 40


def test_unsafe_or_missing_token_is_refused(tmp_path):
    open_file = tmp_path / "token"
    open_file.write_text("abc\n")
    os.chmod(open_file, 0o644)
    with pytest.raises(TokenError, match="chmod 600"):
        read_token(str(open_file))
    with pytest.raises(TokenError, match="no nbs-agents token"):
        read_token(str(tmp_path / "missing"))


def test_check_token():
    assert check_token("abc", "abc")
    assert not check_token("abc", "abd")
    assert not check_token("abc", "")
    assert not check_token("abc", None)
    assert not check_token("", "")
