"""Tests for Tiled logins and the agents' read-only key."""

import os
import stat
import time
from types import SimpleNamespace

import pytest

from fakes import FakeRedis

from nbs_agents import tiled_auth
from nbs_agents.agentlog import AgentLog
from nbs_agents.config import AgentsConfig
from nbs_agents.paths import paths


def test_key_file_is_private_and_expires(nbs_agents_home):
    path = tiled_auth.write_key_file({"secret": "s1", "first_eight": "abcd1234", "expires": time.time() + 60})
    assert path == paths().tiled_agent_key
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert tiled_auth.read_key_file() == "s1"
    assert tiled_auth.read_key_file(now=time.time() + 120) is None
    path.write_text("not json")
    assert tiled_auth.read_key_file() is None


class FakeContext:
    """Stand-in for tiled.client.context.Context."""

    def __init__(self, providers=("entra",), required=True, cached=False, login_text=""):
        auth = SimpleNamespace(providers=list(providers), required=required)
        self.server_info = SimpleNamespace(authentication=auth)
        self.authenticated = False
        self.cached = cached
        self.login_text = login_text
        self.logins = 0
        self.keys, self.revoked = [], []
        self.fail_next_create = None

    def use_cached_tokens(self):
        self.authenticated = self.cached
        return self.cached

    def authenticate(self, remember_me=True):
        print(self.login_text, end="")
        self.logins += 1
        self.authenticated = True

    def create_api_key(self, scopes=None, expires_in=None, note=None):
        if self.fail_next_create:
            exc, self.fail_next_create = self.fail_next_create, None
            raise exc
        n = len(self.keys)
        self.keys.append({"scopes": scopes, "expires_in": expires_in, "note": note})
        return {"secret": f"secret-{n}", "first_eight": f"key{n:05d}"}

    def revoke_api_key(self, first_eight):
        self.revoked.append(first_eight)


@pytest.fixture
def server(monkeypatch):
    from tiled.client.context import Context

    holder = {"context": FakeContext()}
    monkeypatch.setattr(Context, "from_any_uri", classmethod(lambda cls, uri: (holder["context"], [])))
    return holder


LOGIN_TEXT = "\nYou have 15 minutes to visit this URL\n\nhttps://login.example/device\n\nand enter the code:\n\nABCD-EFGH\n\n"


def test_login_context_uses_cache_then_logs_in_and_reports_link(server, nbs_agents_home, capsys):
    server["context"] = FakeContext(cached=True)
    assert tiled_auth.login_context("https://tiled", interactive=False).logins == 0
    assert os.environ["TILED_CACHE_DIR"] == str(paths().tiled_cache)

    server["context"] = FakeContext(login_text=LOGIN_TEXT)
    with pytest.raises(tiled_auth.TiledLoginRequired, match="nbs-agents tiled-login"):
        tiled_auth.login_context("https://tiled", interactive=False)
    links = []
    context = tiled_auth.login_context("https://tiled", interactive=True, on_link=lambda u, c: links.append((u, c)))
    assert context.logins == 1
    assert links == [("https://login.example/device", "ABCD-EFGH")]
    assert "ABCD-EFGH" in capsys.readouterr().out  # still shown in the terminal

    server["context"] = FakeContext(providers=())
    assert tiled_auth.login_context("https://tiled", interactive=False).logins == 0


def test_connect_prefers_fixed_key_then_agent_key(monkeypatch, server, nbs_agents_home):
    import tiled.client

    calls = []
    monkeypatch.setattr(tiled.client, "from_uri", lambda uri, api_key=None: calls.append(("uri", api_key)) or {"raw": "node"})
    monkeypatch.setattr(tiled.client, "from_context", lambda context: calls.append(("context", None)) or {"raw": "node"})
    monkeypatch.delenv("TILED_API_KEY", raising=False)
    config = AgentsConfig(tiled_uri="https://tiled", tiled_path="raw")

    assert tiled_auth.connect(AgentsConfig(tiled_uri="https://tiled", tiled_api_key="fixed")) == {"raw": "node"}
    tiled_auth.write_key_file({"secret": "agent", "first_eight": "x", "expires": time.time() + 60})
    assert tiled_auth.connect(config) == "node"
    assert calls == [("uri", "fixed"), ("uri", "agent")]

    paths().tiled_agent_key.unlink()
    with pytest.raises(tiled_auth.TiledLoginRequired):
        tiled_auth.connect(config, interactive=False)


def test_keeper_makes_rotates_and_revokes_read_only_keys(server, nbs_agents_home, monkeypatch):
    from tiled.client.auth import CannotRefreshAuthentication

    server["context"] = FakeContext(login_text=LOGIN_TEXT)
    log = AgentLog(FakeRedis(), "bl-")
    seen = []

    def on_link(url, code):
        seen.append(log.tiled_login() and log.tiled_login()["url"])
        log.set_tiled_login(url, code)

    monkeypatch.delenv("TILED_API_KEY", raising=False)
    keeper = tiled_auth.AgentKeyKeeper(AgentsConfig(tiled_uri="https://tiled", tiled_key_lifetime_s=7200), on_link=on_link)
    assert keeper.needed
    assert keeper.renew()
    context = server["context"]
    assert context.keys[0]["scopes"] == ["read:metadata", "read:data"]
    assert context.keys[0]["expires_in"] == 7200
    assert tiled_auth.read_key_file() == "secret-0"
    assert log.tiled_login() is None  # the link was shown, then cleared after login

    keeper.renew()
    assert tiled_auth.read_key_file() == "secret-1" and context.revoked == ["key00000"]

    # An expired login: log in again (new link) and carry on
    context.fail_next_create = CannotRefreshAuthentication("expired")
    context.authenticated = False
    keeper.renew()
    assert context.logins == 2 and tiled_auth.read_key_file() == "secret-2"

    keeper.stop()
    assert "key00002" in context.revoked
    assert not paths().tiled_agent_key.exists()


def test_keeper_not_needed_with_fixed_key():
    keeper = tiled_auth.AgentKeyKeeper(AgentsConfig(tiled_api_key="fixed"))
    assert not keeper.needed
    keeper.start()
    keeper.stop()
