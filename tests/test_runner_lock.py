import json

import pytest

from nbs_agents.runner_lock import RunnerLockedError, acquire

OWNER = {"id": "a", "host": "grettir", "pid": 11, "started": 0}


def test_second_runner_is_refused_and_told_who_holds_it(tmp_path):
    path = tmp_path / "state" / "runner.lock"
    held = acquire(path, OWNER)
    with pytest.raises(RunnerLockedError) as info:
        acquire(path, {"id": "b", "host": "grettir", "pid": 12, "started": 0})
    assert "runner on grettir (pid 11" in str(info.value)
    assert json.loads(path.read_text())["id"] == "a"
    held.close()


def test_lock_is_free_once_the_holder_is_gone(tmp_path):
    path = tmp_path / "runner.lock"
    acquire(path, OWNER).close()
    again = acquire(path, {"id": "b", "host": "h", "pid": 2, "started": 0})
    assert json.loads(path.read_text())["id"] == "b"
    again.close()
