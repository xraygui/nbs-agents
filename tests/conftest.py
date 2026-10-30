import pytest

from nbs_agents.paths import BEAMLINE_ENV, HOME_ENV


@pytest.fixture(autouse=True)
def nbs_agents_home(tmp_path, monkeypatch):
    """A throwaway $NBS_AGENTS_HOME, so tests never read or write the user's own files."""
    home = tmp_path / "nbs-agents-home"
    monkeypatch.setenv(HOME_ENV, str(home))
    monkeypatch.setenv(BEAMLINE_ENV, "testbl")
    monkeypatch.delenv("NBS_CLI_CONFIG", raising=False)
    monkeypatch.delenv("TILED_CACHE_DIR", raising=False)
    return home


@pytest.fixture
def beamline_dir(nbs_agents_home):
    """The test beamline's folder."""
    return nbs_agents_home / "testbl"
