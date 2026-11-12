"""
One nbs-agents-runner per beamline folder.

The runner holds an exclusive ``flock`` on ``state/runner.lock`` under the
beamline's folder for as long as it runs. The kernel drops the lock when the
process exits, however it exits, so a crashed runner never leaves a stale lock
behind; the file itself stays and only says who held it last. Python opens
files close-on-exec, so the agents a runner launches do not hold the lock.

A runner on another machine with its own ``$NBS_AGENTS_HOME`` is not stopped
by this; the owner stamped on each agent block (``queue.claim_close_agent``)
still keeps it from driving a block this runner owns.
"""

import fcntl
import json
import os
from pathlib import Path
from typing import Any, IO


class RunnerLockedError(RuntimeError):
    """Another runner holds the lock."""

    def __init__(self, path: Path, holder: dict[str, Any]) -> None:
        self.path = path
        self.holder = holder
        from .queue import describe_owner

        who = describe_owner(holder) if holder else "another runner"
        super().__init__(f"the {who} already serves this beamline (lock {path})")


def acquire(path: Path, owner: dict[str, Any]) -> IO:
    """
    Take the runner lock, and record ``owner`` in it.

    Parameters
    ----------
    path : Path
        The lock file (``paths().runner_lock``).
    owner : dict
        This runner's owner stamp (``supervisor.runner_identity``).

    Returns
    -------
    file
        The open lock file. Keep it open for as long as the runner runs.

    Raises
    ------
    RunnerLockedError
        If another live process holds the lock.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.seek(0)
        try:
            holder = json.loads(handle.read() or "{}")
        except ValueError:
            holder = {}
        handle.close()
        raise RunnerLockedError(path, holder) from None
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps(owner))
    handle.flush()
    os.fsync(handle.fileno())
    return handle
