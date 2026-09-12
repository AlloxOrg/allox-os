"""Per-Session Unix endpoints outside Bubblewrap; no Agent-facing admin token."""

from __future__ import annotations

import json
import os
import socket
import socketserver
import struct
import tempfile
import threading
from pathlib import Path

from allox.workspace.store import WorkspaceError

MAX_REQUEST = 512 * 1024


class ShareHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(10)
        try:
            pid, _, _ = struct.unpack("3i", self.request.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            ))
            if not self.server.cgroups.owns(*self.server.identity, pid):
                raise WorkspaceError("caller does not belong to this Session")
            line = self.rfile.readline(MAX_REQUEST + 1)
            if len(line) > MAX_REQUEST or not line.endswith(b"\n"):
                raise WorkspaceError("invalid share request size")
            request = json.loads(line)
            if not isinstance(request, dict) or not isinstance(request.get("params", {}), dict):
                raise WorkspaceError("invalid share request")
            params = request.get("params", {})
            if any(key in params for key in ("agent_id", "session_id", "caller")):
                raise WorkspaceError("caller identity cannot be supplied by the Agent")
            action = request.get("action")
            if not isinstance(action, str):
                raise WorkspaceError("invalid share action")
            result = self.server.shares.dispatch(self.server.identity, action, params)
            response = {"ok": True, "result": result}
        except (WorkspaceError, ValueError) as exc:
            response = {"ok": False, "error": str(exc)}
        except Exception:  # noqa: BLE001 - never expose trusted paths or implementation details
            response = {"ok": False, "error": "share request failed"}
        try:
            self.wfile.write(json.dumps(response).encode() + b"\n")
        except OSError:
            pass


class ShareServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


class SessionShareEndpoints:
    def __init__(self, shares, cgroups, root: Path):
        self.shares = shares
        self.cgroups = cgroups
        self.root = root
        self._directory = None
        self._servers = {}
        self._lock = threading.Lock()

    def endpoint(self, agent_id, session_id):
        identity = (agent_id, session_id)
        with self._lock:
            if identity in self._servers:
                return self._servers[identity].server_address
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self._directory is None:
                self._directory = tempfile.TemporaryDirectory(prefix="share-", dir=self.root)
            path = str(Path(self._directory.name) / (str(len(self._servers)) + ".sock"))
            server = ShareServer(path, ShareHandler)
            server.identity = identity
            server.shares = self.shares
            server.cgroups = self.cgroups
            os.chmod(path, 0o600)
            self._servers[identity] = server
            threading.Thread(target=server.serve_forever, daemon=True).start()
            return path

    def close(self):
        with self._lock:
            for server in self._servers.values():
                server.shutdown()
                server.server_close()
            self._servers.clear()
            if self._directory:
                self._directory.cleanup()
                self._directory = None
