"""Per-Session append-only telemetry, authenticated by kernel peer cgroup identity."""

from __future__ import annotations

import json
import os
import socket
import socketserver
import struct
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from allox.workspace.store import WorkspaceError


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        server = cast(Server, self.server)
        self.request.settimeout(5)
        try:
            pid, _, _ = struct.unpack("3i", self.request.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
            if not server.cgroups.owns(*server.identity, pid):
                raise WorkspaceError("caller does not belong to this Session")
            bound_run = server.plugin.active_run_id(server.identity)
            line = self.rfile.readline(131073)
            if len(line) > 131072 or not line.endswith(b"\n"):
                raise WorkspaceError("invalid telemetry request size")
            request = json.loads(line)
            if not isinstance(request, dict) or set(request) != {"event"}:
                raise WorkspaceError("only event ingestion is allowed")
            event = request["event"]
            if not isinstance(event, dict) or any(
                key in event for key in ("agent_id", "session_id", "source", "process_run_id")
            ):
                raise WorkspaceError("Agent cannot supply caller identity or source")
            with server.executions.share_access(*server.identity, write=False):
                if not server.cgroups.owns(*server.identity, pid):
                    raise WorkspaceError("caller left this Session")
                result = server.plugin.ingest_session(server.identity, event, bound_run)
            response = {"ok": True, "result": result}
        except (WorkspaceError, ValueError) as exc:
            response = {"ok": False, "error": str(exc)}
        except Exception:  # noqa: BLE001 - do not expose collector internals to an Agent
            response = {"ok": False, "error": "monitor delivery failed"}
        try:
            self.wfile.write(json.dumps(response).encode() + b"\n")
        except OSError:
            pass


if TYPE_CHECKING or hasattr(socketserver, "UnixStreamServer"):
    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True
        cgroups: Any
        identity: tuple[str, str]
        executions: Any
        plugin: Any
else:
    class Server:
        def __init__(self, *args, **kwargs):
            raise WorkspaceError("monitor endpoints require Linux Unix sockets")


class SessionEndpoints:
    def __init__(self, context, plugin, root: Path) -> None:
        self.context = context
        self.plugin = plugin
        self.root = root
        self._lock = threading.Lock()
        self._directory = None
        self._servers: dict[tuple[str, str], Server] = {}

    def endpoint(self, agent: str, session: str) -> str:
        with self._lock:
            key = (agent, session)
            if key not in self._servers:
                self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
                if self._directory is None:
                    self._directory = tempfile.TemporaryDirectory(prefix="mon-", dir=self.root)
                path = str(Path(self._directory.name) / f"{len(self._servers)}.sock")
                server = Server(path, Handler)
                server.identity = key
                server.cgroups = self.context.cgroups
                server.executions = self.context.executions
                server.plugin = self.plugin
                os.chmod(path, 0o600)
                self._servers[key] = server
                threading.Thread(target=server.serve_forever, daemon=True).start()
            address = self._servers[key].server_address
            assert isinstance(address, str)
            return address

    def close(self):
        with self._lock:
            for server in self._servers.values():
                # Wake serve_forever's selector instead of waiting its default 500 ms.
                # Keep the normal authenticated handler; the wake request has no event.
                stopper = threading.Thread(target=server.shutdown, daemon=True)
                stopper.start()
                deadline = time.monotonic()+2
                while stopper.is_alive():
                    try:
                        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as wake:
                            wake.settimeout(.1)
                            wake.connect(server.server_address)
                            wake.sendall(b'{}\n')
                    except OSError:
                        pass
                    stopper.join(.01)
                    if time.monotonic() >= deadline and stopper.is_alive():
                        raise WorkspaceError('monitor endpoint did not shut down')
                server.server_close()
            self._servers.clear()
            if self._directory:
                self._directory.cleanup()
                self._directory = None
