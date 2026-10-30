"""
Connect to Tiled, logging in when the server needs it.

NSLS-II's Tiled uses Entra ID through OAuth2 device-code login: Tiled prints a
link and a code and opens a browser. Logins are cached (refreshable) in
``state/tiled`` under ``$NBS_AGENTS_HOME``, so after the first one a
connection is usually silent.

Agents never log in themselves. nbs-agents-runner, logged in as the user,
makes a read-only (``read:metadata``, ``read:data``), expiring API key for
them in ``state/tiled-agent/api-key.json``, replaces it before it expires and
revokes it on exit. That folder is mounted read-only into bwclaude, so the
agents hold only this key, never the user's login.

How a client connects, first match wins:

1. ``tiled_api_key`` in the config or ``$TILED_API_KEY``;
2. the agent key file, if it is there and not expired;
3. the cached login, refreshed if needed;
4. a new login, if ``interactive`` (a terminal, or the runner); otherwise
   ``TiledLoginRequired`` explains how to get one.
"""

import contextlib
import io
import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .paths import paths

LOGGER = logging.getLogger(__name__)

AGENT_SCOPES = ["read:metadata", "read:data"]
KEY_LIFETIME_S = 24 * 3600.0
# Replace the agent key when less than this fraction of its life is left
RENEW_FRACTION = 1 / 3
LINK_RE = re.compile(r"(https?://\S+)")
CODE_RE = re.compile(r"enter the code:\s*(\S+)")


class TiledLoginRequired(RuntimeError):
    """Tiled needs a login and this process may not ask for one."""


def use_cache_dir() -> Path:
    """Keep Tiled's login cache under ``$NBS_AGENTS_HOME`` unless ``TILED_CACHE_DIR`` is set."""
    folder = Path(os.environ.setdefault("TILED_CACHE_DIR", str(paths().tiled_cache)))
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    return folder


# ----------------------------------------------------------- agent key file


def read_key_file(path: Optional[str] = None, now: Optional[float] = None) -> Optional[str]:
    """
    The agent key, if the file exists and the key has not expired.

    Parameters
    ----------
    path : str, optional
        Key file; ``paths().tiled_agent_key`` by default.
    now : float, optional
        Current time, for tests.

    Returns
    -------
    str or None
        The secret.
    """
    target = Path(path).expanduser() if path else paths().tiled_agent_key
    try:
        data = json.loads(target.read_text())
    except (OSError, ValueError):
        return None
    expires = data.get("expires")
    if isinstance(expires, (int, float)) and expires <= (time.time() if now is None else now):
        return None
    return data.get("secret") or None


def write_key_file(info: dict[str, Any], path: Optional[str] = None) -> Path:
    """
    Store a new agent key (mode 600), replacing the old one atomically.

    Parameters
    ----------
    info : dict
        ``secret``, ``first_eight`` and ``expires`` (epoch seconds).
    path : str, optional
        Key file; ``paths().tiled_agent_key`` by default.
    """
    target = Path(path).expanduser() if path else paths().tiled_agent_key
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({k: info[k] for k in ("secret", "first_eight", "expires")}, f)
    # Replace inside the folder: a bind mount of the folder sees the new file
    os.replace(tmp, target)
    return target


# ---------------------------------------------------------------- connect


def _navigate(client, tiled_path: str):
    for part in filter(None, (tiled_path or "").split("/")):
        client = client[part]
    return client


def login_context(uri: str, interactive: bool, on_link: Optional[Callable[[str, str], None]] = None):
    """
    A Tiled ``Context`` logged in with the cached login or, if allowed, a new one.

    Parameters
    ----------
    uri : str
        Tiled server.
    interactive : bool
        Whether a new login may be started (Tiled prints the link and opens a
        browser, then waits for it).
    on_link : callable, optional
        Called as ``on_link(url, code)`` when a new login starts, so the
        runner can show it on the web page too.

    Returns
    -------
    tiled.client.context.Context
        Authenticated if the server offers logins; anonymous otherwise.

    Raises
    ------
    TiledLoginRequired
        If the server requires a login, none is cached, and ``interactive`` is false.
    """
    from tiled.client.context import Context

    use_cache_dir()
    context, _ = Context.from_any_uri(uri)
    auth = context.server_info.authentication
    if context.authenticated or not auth.providers:
        return context
    if context.use_cached_tokens():
        return context
    if not interactive:
        if auth.required:
            raise TiledLoginRequired(
                f"Tiled at {uri} needs a login. Ask the operator to run `nbs-agents tiled-login` "
                "or restart nbs-agents-runner, which makes a read-only key for agents."
            )
        return context
    _login(context, on_link)
    return context


def _login(context, on_link: Optional[Callable[[str, str], None]]) -> None:
    """Run Tiled's login, copying its link and code to ``on_link`` as well as the terminal."""

    class Tee(io.TextIOBase):
        def __init__(self, out):
            self.out, self.text, self.sent = out, "", False

        def write(self, s):
            self.out.write(s)
            self.out.flush()
            self.text += s
            link, code = LINK_RE.search(self.text), CODE_RE.search(self.text)
            if link and code and not self.sent and on_link is not None:
                self.sent = True
                try:
                    on_link(link.group(1), code.group(1))
                except Exception:
                    LOGGER.exception("Could not publish the Tiled login link")
            return len(s)

    with contextlib.redirect_stdout(Tee(sys.stdout)):
        context.authenticate(remember_me=True)


def connect(config: Any, interactive: Optional[bool] = None, on_link=None):
    """
    The Tiled run catalog for nbs-cli settings.

    Parameters
    ----------
    config : nbs_agents.cli.config.CliConfig
        ``tiled_uri``, ``tiled_path``, ``tiled_api_key`` and ``tiled_api_key_file``.
    interactive : bool, optional
        Whether a new login may be started; by default when stdin is a terminal.
    on_link : callable, optional
        See ``login_context``.

    Returns
    -------
    tiled.client.container.Container
        The catalog at ``tiled_path``.
    """
    from tiled.client import from_context, from_uri

    key = config.tiled_api_key or os.environ.get("TILED_API_KEY") or read_key_file(config.tiled_api_key_file or None)
    if key:
        return _navigate(from_uri(config.tiled_uri, api_key=key), config.tiled_path)
    if interactive is None:
        interactive = sys.stdin.isatty()
    context = login_context(config.tiled_uri, interactive, on_link)
    return _navigate(from_context(context), config.tiled_path)


# ------------------------------------------------------------ runner side


class AgentKeyKeeper:
    """
    Keep a read-only, expiring Tiled API key for agents, made from the user's login.

    Parameters
    ----------
    config : nbs_agents.cli.config.CliConfig
        Tiled settings; ``tiled_key_lifetime_s`` sets each key's life.
    on_link : callable, optional
        Called as ``on_link(url, code)`` when a login is needed, and as
        ``on_link(None, None)`` once it is done.
    """

    def __init__(self, config: Any, on_link: Optional[Callable] = None) -> None:
        self.config = config
        self.on_link = on_link
        self.lifetime_s = float(getattr(config, "tiled_key_lifetime_s", KEY_LIFETIME_S))
        self.path = Path(config.tiled_api_key_file).expanduser() if config.tiled_api_key_file else paths().tiled_agent_key
        self.context = None
        self.current: Optional[dict[str, Any]] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def needed(self) -> bool:
        """False when a fixed key (``tiled_api_key`` or ``$TILED_API_KEY``) already covers everyone."""
        return not (self.config.tiled_api_key or os.environ.get("TILED_API_KEY"))

    def _context(self):
        if self.context is None or not self.context.authenticated:
            self.context = login_context(self.config.tiled_uri, True, self.on_link)
            if self.on_link is not None:
                self.on_link(None, None)
        return self.context

    def renew(self) -> bool:
        """
        Make a new agent key, store it, and revoke the previous one.

        Returns
        -------
        bool
            False if the server has no logins (anonymous access; no key needed).
        """
        from tiled.client.auth import CannotRefreshAuthentication

        context = self._context()
        if not context.server_info.authentication.providers:
            return False
        try:
            made = self._create(context)
        except CannotRefreshAuthentication:
            # The login itself expired: log in again
            self.context = None
            made = self._create(self._context())
        old, self.current = self.current, made
        write_key_file(made, str(self.path))
        LOGGER.info("New read-only Tiled key for agents (%s...), valid %.0f h", made["first_eight"], self.lifetime_s / 3600)
        if old:
            self._revoke(old)
        return True

    def _create(self, context) -> dict[str, Any]:
        host = os.uname().nodename
        info = context.create_api_key(
            scopes=AGENT_SCOPES, expires_in=int(self.lifetime_s), note=f"nbs-agents agents on {host}"
        )
        return {"secret": info["secret"], "first_eight": info["first_eight"], "expires": time.time() + self.lifetime_s}

    def _revoke(self, key: dict[str, Any]) -> None:
        try:
            self.context.revoke_api_key(key["first_eight"])
        except Exception:
            LOGGER.warning("Could not revoke Tiled key %s...; it expires on its own", key["first_eight"], exc_info=True)

    def start(self) -> None:
        """
        Make the first key and keep replacing it before it expires, in the background.

        A login that is needed shows its link through ``on_link`` (and the
        runner's terminal); until it is done, agents are told to ask for one.
        """
        if not self.needed:
            return

        def loop():
            delay = 0.0
            while not self._stop.wait(delay):
                try:
                    if not self.renew():
                        return  # anonymous server: nothing to keep
                    delay = self.lifetime_s * (1 - RENEW_FRACTION)
                except Exception:
                    LOGGER.exception("Could not make the agents' Tiled key; trying again in 5 min")
                    delay = 300.0

        self._thread = threading.Thread(target=loop, name="tiled-agent-key", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop renewing, revoke the key and remove its file."""
        self._stop.set()
        if self.current:
            self._revoke(self.current)
            self.current = None
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
