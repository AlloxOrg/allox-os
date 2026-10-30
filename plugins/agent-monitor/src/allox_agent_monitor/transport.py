"""Agent-side append-only telemetry transport: no management credentials."""

from __future__ import annotations

import json
import os
import socket


def emit_session(event: dict, endpoint: str | None = None) -> dict:
    endpoint = endpoint or os.environ.get("ALLOX_MONITOR_SOCK", "/run/allox/monitor.sock")
    message = json.dumps({"event": event}, allow_nan=False).encode() + b"\n"
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
