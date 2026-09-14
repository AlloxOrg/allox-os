"""Session network policy and HTTP bridge unit tests."""

from __future__ import annotations

import json
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from allox.runtime.networking import (
    SessionNetworkBroker,
    SessionNetworkManager,
    _proxy_client,
    validate_network_mode,
)
from allox.workspace.store import WorkspaceError


def test_network_configuration_is_persistent_but_runtime_is_not_started():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        manager = SessionNetworkManager(
            root, socket_root=root / "sockets", default_mode="inherit"
        )
        result = manager.configure("agent-a", "session-1", "proxy")
        assert result == {
            "agent_id": "agent-a",
            "session_id": "session-1",
            "mode": "proxy",
            "active": False,
            "namespace_pid": None,
            "proxy_url": None,
        }
        manager.close()
        restarted = SessionNetworkManager(
            root, socket_root=root / "sockets", default_mode="inherit"
        )
        assert restarted.mode("agent-a", "session-1") == "proxy"
        restarted.configure("agent-a", "session-1", "inherit")
        assert json.loads((root / "sessions.json").read_text()) == {}
        restarted.close()


def test_network_mode_validation():
    assert validate_network_mode(" PROXY ") == "proxy"
    with pytest.raises(WorkspaceError, match="network mode"):
        validate_network_mode("open")


def test_broker_can_connect_to_a_loopback_tcp_service():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    connected = SessionNetworkBroker._destination("127.0.0.1", listener.getsockname()[1])
    accepted, _ = listener.accept()
    connected.sendall(b"allox")
    assert accepted.recv(5) == b"allox"
    connected.close()
    accepted.close()
    listener.close()


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="requires Unix-domain sockets")
def test_absolute_http_request_is_forwarded_over_unix_broker():
    with tempfile.TemporaryDirectory() as temporary:
        broker_path = str(Path(temporary) / "broker.sock")
        broker = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        broker.bind(broker_path)
        broker.listen(1)
        observed = {}

        def serve():
            stream, _ = broker.accept()
            control = bytearray()
            while not control.endswith(b"\n"):
                control.extend(stream.recv(1))
            observed["control"] = json.loads(control)
            stream.sendall(b'{"ok":true}\n')
            request = bytearray()
            while b"\r\n\r\n" not in request:
                request.extend(stream.recv(4096))
            observed["request"] = bytes(request)
            stream.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            stream.close()

        thread = threading.Thread(target=serve)
        thread.start()
        client, proxy = socket.socketpair()
        proxy_thread = threading.Thread(target=_proxy_client, args=(proxy, broker_path))
        proxy_thread.start()
        client.sendall(
            b"GET http://example.com:8080/a?q=1 HTTP/1.1\r\n"
            b"Host: example.com:8080\r\nProxy-Connection: keep-alive\r\n\r\n"
        )
        client.shutdown(socket.SHUT_WR)
        response = bytearray()
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            response.extend(chunk)
        client.close()
        proxy_thread.join(timeout=2)
        thread.join(timeout=2)
        broker.close()

        assert observed["control"] == {"host": "example.com", "port": 8080}
        assert observed["request"].startswith(b"GET /a?q=1 HTTP/1.1\r\n")
        assert b"Proxy-Connection" not in observed["request"]
        assert response.endswith(b"ok")
