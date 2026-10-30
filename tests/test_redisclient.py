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
