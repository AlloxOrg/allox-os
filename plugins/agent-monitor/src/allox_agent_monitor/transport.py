"""Agent-side append-only telemetry transport: no management credentials."""

from __future__ import annotations

import json
import os
import socket


def emit_session(event: dict, endpoint: str | None = None) -> dict:
    return _request({"event": event}, endpoint)


def _request(request: dict, endpoint: str | None = None) -> dict:
    endpoint = endpoint or os.environ.get("ALLOX_MONITOR_SOCK", "/run/allox/monitor.sock")
    message = json.dumps(request, allow_nan=False).encode() + b"\n"
    if len(message) > 131072:
        raise ValueError("monitor request too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(endpoint)
        connection.sendall(message)
        with connection.makefile("rb") as stream:
            line = stream.readline(8193)
    if len(line) > 8192 or not line.endswith(b"\n"):
        raise ValueError("invalid monitor response")
    result = json.loads(line)
    if not result.get("ok"):
        raise ValueError(result.get("error", "monitor delivery failed"))
    return result["result"]


class SessionQueueTransport:
    """Explicit opt-in; requires the collector's async_write option."""
    def __init__(self, endpoint: str | None = None):
        self.endpoint = endpoint

    def __call__(self, event):
        return _request({"event": event, "delivery": "queued"}, self.endpoint)

    def flush(self, receipt):
        return _request({"flush": receipt}, self.endpoint)
