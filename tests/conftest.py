import pytest

from nbs_agents.auth import TOKEN_FILE_ENV, ensure_token
from nbs_agents.paths import HOME_ENV


@pytest.fixture(autouse=True)
def nbs_agents_home(tmp_path, monkeypatch):
    """A throwaway $NBS_AGENTS_HOME, so tests never read or write the user's own files."""
    home = tmp_path / "nbs-agents-home"
    monkeypatch.setenv(HOME_ENV, str(home))
    for name in ("NBS_CLI_CONFIG", "NBS_ATTACHMENTS_DIR", "NBS_KB_AGENT_DIR", "NBS_GATEWAY_ENV"):
        monkeypatch.delenv(name, raising=False)
    return home


@pytest.fixture(autouse=True)
def nbs_agents_token(nbs_agents_home, monkeypatch):
    """A throwaway web login token."""
    path = nbs_agents_home / "config" / "token"
    monkeypatch.setenv(TOKEN_FILE_ENV, str(path))
    return ensure_token(str(path))
