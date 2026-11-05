import pytest

from nbs_agents.assistant.auth import TOKEN_FILE_ENV, ensure_token
from nbs_agents.paths import BEAMLINE_ENV, HOME_ENV


@pytest.fixture(autouse=True)
def nbs_agents_home(tmp_path, monkeypatch):
    """A throwaway $NBS_AGENTS_HOME, so tests never read or write the user's own files."""
    home = tmp_path / "nbs-agents-home"
    monkeypatch.setenv(HOME_ENV, str(home))
    monkeypatch.setenv(BEAMLINE_ENV, "testbl")
    monkeypatch.delenv("TILED_CACHE_DIR", raising=False)
    monkeypatch.delenv("NBS_CLAUDE_SANDBOX", raising=False)
    for name in ("NBS_CLI_CONFIG", "NBS_ATTACHMENTS_DIR", "NBS_KB_AGENT_DIR", "NBS_GATEWAY_ENV", "NBS_ENV_DIR"):
        monkeypatch.delenv(name, raising=False)
    return home


@pytest.fixture(autouse=True)
def nbs_agents_token(nbs_agents_home, monkeypatch):
    """A throwaway web login token."""
    path = nbs_agents_home / "testbl" / "config" / "token"
    monkeypatch.setenv(TOKEN_FILE_ENV, str(path))
    return ensure_token(str(path))


@pytest.fixture
def beamline_dir(nbs_agents_home):
    """The test beamline's folder."""
    return nbs_agents_home / "testbl"
