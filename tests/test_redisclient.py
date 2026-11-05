"""Redis clients must not send HELLO, which servers older than 6 refuse."""

import shutil
import socket
import subprocess
import time

import pytest

from nbs_agents.redisclient import connect


def test_clients_use_resp2():
    client = connect("localhost", 1)
    assert client.connection_pool.connection_kwargs["protocol"] == 2


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(shutil.which("redis-server") is None, reason="needs redis-server")
def test_works_with_a_server_that_refuses_hello():
    # Rename HELLO away, as on a Redis older than 6
    port = _free_port()
    server = subprocess.Popen(
        ["redis-server", "--port", str(port), "--bind", "127.0.0.1", "--save", "", "--appendonly", "no",
         "--enable-debug-command", "no", "--rename-command", "HELLO", ""],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        client = connect("127.0.0.1", port, socket_timeout=2)
        for _ in range(50):
            try:
                client.ping()
                break
            except Exception:
                time.sleep(0.1)
        client.set("k", "v")
        assert client.get("k") == b"v"
    finally:
        server.terminate()
        server.wait()


def test_encrypted_redis_uses_tls_and_the_password_file(tmp_path, monkeypatch):
    from nbs_agents.redisclient import connect_settings, redis_password

    secret = tmp_path / "redis.secret"
    secret.write_text("pw-from-file\n")
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)
    monkeypatch.delenv("REDIS_SECRET_FILE", raising=False)
    settings = {"host": "info.example", "port": 6380, "db": 0, "prefix": "", "ssl": True, "password_file": str(secret)}
    kw = connect_settings(settings).connection_pool.connection_kwargs
    assert kw["password"] == "pw-from-file" and kw["protocol"] == 2
    assert connect_settings(settings).connection_pool.connection_class.__name__ == "SSLConnection"

    monkeypatch.setenv("REDIS_SECRET_FILE", str(secret))
    assert redis_password() == "pw-from-file"
    monkeypatch.setenv("REDIS_PASSWORD", "pw-from-env")
    assert redis_password(str(secret)) == "pw-from-env"

    plain = connect_settings({"host": "localhost", "port": 6379, "db": 0, "prefix": ""})
    assert plain.connection_pool.connection_class.__name__ == "Connection"
    assert "password" not in plain.connection_pool.connection_kwargs or not plain.connection_pool.connection_kwargs["password"]
