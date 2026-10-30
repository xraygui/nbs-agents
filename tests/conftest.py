import pytest

from nbs_agents.auth import TOKEN_FILE_ENV, ensure_token


@pytest.fixture(autouse=True)
def nbs_agents_token(tmp_path, monkeypatch):
    """A throwaway nbs-agents token, so tests never read or sign with the user's own."""
    path = tmp_path / "nbs-agents" / "token"
    monkeypatch.setenv(TOKEN_FILE_ENV, str(path))
    return ensure_token(str(path))
