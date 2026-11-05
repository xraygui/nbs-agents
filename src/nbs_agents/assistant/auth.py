"""
Per-user login token for nbs-agents-web.

Programs on the beamline machine (the runner, nbs-cli, nbs-gui) are trusted
and reach the agent log directly. The web page is the boundary: it asks for
this token, Jupyter style, so another user on the machine cannot open it and
act as you. It is the first step towards fuller web login (Entra ID) when the
page is served beyond localhost.

The token lives in ``config/token`` under ``$NBS_AGENTS_HOME`` (``$NBS_AGENTS_TOKEN_FILE``),
mode 600 in a mode 700 folder.
"""

import hmac
import os
import secrets
import stat
from pathlib import Path
from typing import Optional

from nbs_agents.paths import paths

TOKEN_FILE_ENV = "NBS_AGENTS_TOKEN_FILE"


class TokenError(RuntimeError):
    """The token file is missing, unsafe or unreadable."""


def token_path(path: Optional[str] = None) -> Path:
    """
    Where the token lives.

    Parameters
    ----------
    path : str, optional
        Explicit path; otherwise ``$NBS_AGENTS_TOKEN_FILE`` or
        ``paths().token``.

    Returns
    -------
    Path
        The token file path.
    """
    return Path(path or os.environ.get(TOKEN_FILE_ENV) or paths().token).expanduser()


def _check_private(path: Path) -> None:
    info = path.stat()
    if info.st_uid != os.getuid():
        raise TokenError(f"{path} is not owned by you")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise TokenError(f"{path} must not be readable by group or others (chmod 600)")


def read_token(path: Optional[str] = None) -> str:
    """
    Read the token, refusing a file that others could read.

    Parameters
    ----------
    path : str, optional
        Token file; see ``token_path``.

    Returns
    -------
    str
        The token.

    Raises
    ------
    TokenError
        If the file is missing, not yours, readable by others or empty.
    """
    target = token_path(path)
    try:
        _check_private(target)
        text = target.read_text().strip()
    except FileNotFoundError:
        raise TokenError(f"no nbs-agents token at {target}; start nbs-agents-web first")
    except OSError as exc:
        raise TokenError(f"cannot read {target}: {exc}")
    if not text:
        raise TokenError(f"{target} is empty")
    return text


def ensure_token(path: Optional[str] = None) -> str:
    """
    Read the token, creating it (mode 600, folder mode 700) if it is missing.

    Parameters
    ----------
    path : str, optional
        Token file; see ``token_path``.

    Returns
    -------
    str
        The token.
    """
    target = token_path(path)
    if not target.exists():
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass  # another process made it first
        else:
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_urlsafe(32) + "\n")
    return read_token(str(target))


def check_token(token: str, candidate: Optional[str]) -> bool:
    """
    Whether ``candidate`` is the token, compared in constant time.

    Parameters
    ----------
    token : str
        The user's token.
    candidate : str or None
        What a client sent.

    Returns
    -------
    bool
        True if they match.
    """
    return bool(token) and bool(candidate) and hmac.compare_digest(token.encode(), candidate.encode())
