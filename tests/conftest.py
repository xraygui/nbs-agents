import pytest

from nbs_agents.paths import HOME_ENV


@pytest.fixture(autouse=True)
def nbs_agents_home(tmp_path, monkeypatch):
    """A throwaway $NBS_AGENTS_HOME, so tests never read or write the user's own files."""
    home = tmp_path / "nbs-agents-home"
    monkeypatch.setenv(HOME_ENV, str(home))
    monkeypatch.delenv("NBS_CLI_CONFIG", raising=False)
    return home
