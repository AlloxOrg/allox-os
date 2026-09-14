"""Plugin-owned per-Session network namespaces and an HTTP egress bridge.

The trusted daemon owns the outer end of the bridge. Sandboxed processes see a
loopback HTTP proxy and a Session-bound AF_UNIX byte-stream connector.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from allox.workspace.store import WorkspaceError, validate_id

NETWORK_MODES = {"inherit", "isolated", "proxy"}
_MAX_HEADER = 64 * 1024
_MAX_CONTROL = 4096
_PROXY_PORT = 3128


def validate_network_mode(mode: str) -> str:
    if not isinstance(mode, str) or mode.strip().lower() not in NETWORK_MODES:
        raise WorkspaceError("network mode must be inherit, isolated, or proxy")
    return mode.strip().lower()


@dataclass(frozen=True)
class NetworkLaunch:
    mode: str
    argv_prefix: tuple[str, ...] = ()
    environment: tuple[tuple[str, str], ...] = ()
    namespace_pid: int | None = None
    broker_socket: str | None = None


class SessionNetworkBroker:
    """Root-namespace TCP connector for one Session's network tools."""

    def __init__(self, socket_path: Path, audit_path: Path) -> None:
        self.socket_path = socket_path
        self.audit_path = audit_path
        self._closed = threading.Event()
        self._connections: set[socket.socket] = set()
        self._connections_lock = threading.Lock()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
        self._server.listen(32)
        self._server.settimeout(0.2)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._closed.is_set():
            try:
                client, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with self._connections_lock:
                self._connections.add(client)
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    @staticmethod
    def _destination(host: str, port: int) -> socket.socket:
        if not isinstance(host, str) or not host or len(host) > 253:
            raise ValueError("invalid host")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("invalid port")
        last_error: OSError | None = None
        for family, socktype, proto, _, sockaddr in socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        ):
            candidate = socket.socket(family, socktype, proto)
            candidate.settimeout(15)
            try:
                candidate.connect(sockaddr)
                candidate.settimeout(None)
                return candidate
            except OSError as exc:
                last_error = exc
                candidate.close()
        if last_error is not None:
            raise last_error
        raise ConnectionError("destination has no usable address")

    def _audit(self, host: object, port: object, allowed: bool, error: str | None = None) -> None:
        record = {
            "timestamp_ns": time.time_ns(),
            "host": host,
            "port": port,
            "allowed": allowed,
        }
        if error:
            record["error"] = error
        with self.audit_path.open("a") as stream:
            stream.write(json.dumps(record, separators=(",", ":")) + "\n")

    def _handle(self, client: socket.socket) -> None:
        remote = None
        try:
            control = _receive_until(client, b"\n", _MAX_CONTROL)
            request = json.loads(control)
            host, port = request.get("host"), request.get("port")
            remote = self._destination(host, port)
            self._audit(host, port, True)
            client.sendall(b'{"ok":true}\n')
            _relay(client, remote)
        except Exception as exc:  # noqa: BLE001 - protocol errors become a generic denial
            try:
                host = locals().get("host")
                port = locals().get("port")
                self._audit(host, port, False, type(exc).__name__)
                client.sendall(b'{"ok":false}\n')
            except OSError:
                pass
        finally:
            with self._connections_lock:
                self._connections.discard(client)
            client.close()
            if remote is not None:
                remote.close()

    def close(self) -> None:
        self._closed.set()
        self._server.close()
        with self._connections_lock:
            connections = list(self._connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        self._thread.join(timeout=2)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass


class SessionNetworkManager:
    """Own persistent network namespaces keyed by Agent and Session IDs."""

    def __init__(
        self,
        root: Path,
        *,
        socket_root: Path = Path("/dev/shm/allox-network"),
        default_mode: str = "inherit",
        python: str = sys.executable,
        unshare: str = "unshare",
        nsenter: str = "nsenter",
    ) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.socket_root = socket_root.resolve()
        self.socket_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.socket_root, 0o700)
        self.default_mode = validate_network_mode(default_mode)
        self.python = python
        self.unshare = unshare
        self.nsenter = nsenter
        self._lock = threading.RLock()
        self._runtime: dict[tuple[str, str], dict] = {}
        self._configuration = self._load_configuration()

    def _load_configuration(self) -> dict[str, str]:
        try:
            data = json.loads((self.root / "sessions.json").read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise WorkspaceError("invalid Session network configuration") from exc
        if not isinstance(data, dict):
            raise WorkspaceError("invalid Session network configuration")
        return {str(key): validate_network_mode(value) for key, value in data.items()}

    @staticmethod
    def _key(agent_id: str, session_id: str) -> tuple[str, str]:
        return validate_id("agent", agent_id), validate_id("session", session_id)

    @staticmethod
    def _storage_key(key: tuple[str, str]) -> str:
        return key[0] + "/" + key[1]

    def mode(self, agent_id: str, session_id: str) -> str:
        key = self._key(agent_id, session_id)
        with self._lock:
            return self._configuration.get(self._storage_key(key), self.default_mode)

    def configure(self, agent_id: str, session_id: str, mode: str) -> dict:
        key = self._key(agent_id, session_id)
        mode = validate_network_mode(mode)
        with self._lock:
            current = self._configuration.get(self._storage_key(key), self.default_mode)
            if current == mode:
                return self.status(*key)
            self._stop_locked(key)
            storage_key = self._storage_key(key)
            if mode == self.default_mode:
                self._configuration.pop(storage_key, None)
            else:
                self._configuration[storage_key] = mode
            temporary = self.root / "sessions.json.tmp"
            temporary.write_text(json.dumps(self._configuration, indent=2, sort_keys=True))
            os.replace(temporary, self.root / "sessions.json")
        return self.status(agent_id, session_id)

    def prepare(self, agent_id: str, session_id: str) -> NetworkLaunch:
        key = self._key(agent_id, session_id)
        with self._lock:
            mode = self._configuration.get(self._storage_key(key), self.default_mode)
            if mode == "inherit":
                return NetworkLaunch(mode=mode)
            runtime = self._runtime.get(key)
            if runtime is None or runtime["process"].poll() is not None:
                if runtime is not None:
                    self._stop_locked(key)
                runtime = self._start_locked(key, mode)
            environment = ()
            if mode == "proxy":
                proxy = f"http://127.0.0.1:{_PROXY_PORT}"
                environment = (
                    ("HTTP_PROXY", proxy),
                    ("HTTPS_PROXY", proxy),
                    ("http_proxy", proxy),
                    ("https_proxy", proxy),
                    ("NO_PROXY", "127.0.0.1,localhost,::1"),
                    ("no_proxy", "127.0.0.1,localhost,::1"),
                )
            return NetworkLaunch(
                mode=mode,
                argv_prefix=(
                    self.nsenter,
                    "--target",
                    str(runtime["process"].pid),
                    "--net",
                    "--",
                ),
                environment=environment,
                namespace_pid=runtime["process"].pid,
                broker_socket=(
                    str(runtime["broker_socket"]) if runtime["broker"] is not None else None
                ),
            )

    def _start_locked(self, key: tuple[str, str], mode: str) -> dict:
        for command in (self.unshare, self.nsenter):
            if shutil.which(command) is None:
                raise WorkspaceError(f"Session network isolation requires {command}")
        # AF_UNIX paths are short on Linux; a digest also prevents ID-dependent
        # socket path lengths while the full identity remains in the audit row.
        digest = hashlib.sha256((key[0] + "\0" + key[1]).encode()).hexdigest()[:24]
        directory = self.root / digest
        directory.mkdir(mode=0o700, exist_ok=True)
        (directory / "session.json").write_text(
            json.dumps({"agent_id": key[0], "session_id": key[1], "mode": mode})
        )
        broker = None
        socket_directory = self.socket_root / digest
        socket_directory.mkdir(mode=0o700, exist_ok=True)
        broker_socket = socket_directory / "broker.sock"
        if mode == "proxy":
            if len(os.fsencode(broker_socket)) >= 108:
                raise WorkspaceError(
                    "network socket root is too long; use a short tmpfs path such as /dev/shm"
                )
            try:
                broker_socket.unlink()
            except FileNotFoundError:
                pass
            broker = SessionNetworkBroker(broker_socket, directory / "egress.jsonl")
        stderr = (directory / "namespace.log").open("ab", buffering=0)
        command = [
            self.unshare,
            "--net",
            "--",
            self.python,
            "-m",
            "allox_session_network.networking",
            "_namespace_helper",
            "--mode",
            mode,
        ]
        if broker is not None:
            command.extend(["--broker-socket", str(broker_socket), "--proxy-port", str(_PROXY_PORT)])
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            env={
                key: value
                for key, value in os.environ.items()
                if key in {"PATH", "PYTHONPATH", "LANG", "LD_LIBRARY_PATH"}
            },
        )
        try:
            assert process.stdout is not None
            ready = _readline_with_timeout(process.stdout, process, 10)
            message = json.loads(ready)
            if message.get("ready") is not True:
                raise WorkspaceError("Session network helper did not become ready")
        except Exception as exc:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            stderr.close()
            if broker is not None:
                broker.close()
            try:
                detail = (directory / "namespace.log").read_text(errors="replace").strip()
            except OSError:
                detail = ""
            if detail:
                raise WorkspaceError(
                    "Session network helper failed: " + detail[-500:]
                ) from exc
            raise
        runtime = {
            "mode": mode,
            "process": process,
            "broker": broker,
            "broker_socket": broker_socket if broker is not None else None,
            "stderr": stderr,
        }
        self._runtime[key] = runtime
        return runtime

    def status(self, agent_id: str, session_id: str) -> dict:
        key = self._key(agent_id, session_id)
        with self._lock:
            runtime = self._runtime.get(key)
            active = runtime is not None and runtime["process"].poll() is None
            return {
                "agent_id": key[0],
                "session_id": key[1],
                "mode": self._configuration.get(self._storage_key(key), self.default_mode),
                "active": active,
                "namespace_pid": runtime["process"].pid if active else None,
                "proxy_url": f"http://127.0.0.1:{_PROXY_PORT}" if active and runtime["mode"] == "proxy" else None,
            }

    def _stop_locked(self, key: tuple[str, str]) -> None:
        runtime = self._runtime.pop(key, None)
        if runtime is None:
            return
        process = runtime["process"]
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        if runtime["broker"] is not None:
            runtime["broker"].close()
        if process.stdout is not None:
            process.stdout.close()
        runtime["stderr"].close()

    def close(self) -> None:
        with self._lock:
            for key in list(self._runtime):
                self._stop_locked(key)


def _readline_with_timeout(stream, process: subprocess.Popen, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    fd = stream.fileno()
    while time.monotonic() < deadline:
        readable, _, _ = select.select([fd], [], [], min(0.1, deadline - time.monotonic()))
        if readable:
            line = stream.readline()
            if line:
                return line
            break
        if process.poll() is not None:
            break
    raise WorkspaceError("Session network helper failed to start")


def _bring_loopback_up() -> None:
    import fcntl

    interface = b"lo" + b"\0" * 14
    control = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        flags = struct.unpack("16sh", fcntl.ioctl(control, 0x8913, struct.pack("16sh", interface, 0)))[1]
        fcntl.ioctl(control, 0x8914, struct.pack("16sh", interface, flags | 0x1 | 0x40))
    finally:
        control.close()


def _receive_until(stream: socket.socket, marker: bytes, limit: int) -> bytes:
    data = bytearray()
    while marker not in data:
        # Control frames precede an opaque tunnel on the same stream. Read one
        # byte at a time so application bytes can never be consumed here.
        chunk = stream.recv(1)
        if not chunk:
            raise ConnectionError("unexpected end of stream")
        data.extend(chunk)
        if len(data) >= limit and marker not in data:
            raise ValueError("request header too large")
    position = data.index(marker) + len(marker)
    if position != len(data):
        raise ValueError("control request has trailing bytes")
    return bytes(data[:position])


def _receive_header(stream: socket.socket) -> bytes:
    data = bytearray()
    marker = b"\r\n\r\n"
    while marker not in data:
        chunk = stream.recv(min(4096, _MAX_HEADER - len(data)))
        if not chunk:
            raise ConnectionError("unexpected end of HTTP headers")
        data.extend(chunk)
        if len(data) >= _MAX_HEADER and marker not in data:
            raise ValueError("HTTP header too large")
    return bytes(data)


def _broker_connect(path: str, host: str, port: int) -> socket.socket:
    broker = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        broker.connect(path)
        broker.sendall(json.dumps({"host": host, "port": port}).encode() + b"\n")
        response = _receive_until(broker, b"\n", _MAX_CONTROL)
        if not json.loads(response).get("ok"):
            raise PermissionError("egress broker denied destination")
        return broker
    except Exception:
        broker.close()
        raise


def _relay(left: socket.socket, right: socket.socket) -> None:
    peers = {left: right, right: left}
    readable = set(peers)
    while readable:
        ready, _, _ = select.select(list(readable), [], [], 30)
        if not ready:
            continue
        for source in ready:
            try:
                chunk = source.recv(65536)
            except OSError:
                chunk = b""
            target = peers[source]
            if chunk:
                try:
                    target.sendall(chunk)
                except OSError:
                    return
            else:
                readable.discard(source)
                try:
                    target.shutdown(socket.SHUT_WR)
                except OSError:
                    pass


def _parse_authority(authority: str, default_port: int) -> tuple[str, int]:
    parsed = urlsplit("//" + authority)
    if not parsed.hostname:
        raise ValueError("HTTP proxy request has no destination")
    return parsed.hostname, parsed.port or default_port


def _proxy_client(client: socket.socket, broker_path: str) -> None:
    remote = None
    try:
        request = _receive_header(client)
        boundary = request.index(b"\r\n\r\n") + 4
        header, body = request[:boundary], request[boundary:]
        lines = header.split(b"\r\n")
        method, target, version = lines[0].decode("ascii").split(" ", 2)
        if method.upper() == "CONNECT":
            host, port = _parse_authority(target, 443)
            remote = _broker_connect(broker_path, host, port)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        else:
            parsed = urlsplit(target)
            if parsed.scheme.lower() != "http" or not parsed.hostname:
                raise ValueError("only absolute-form http requests or CONNECT are supported")
            host, port = parsed.hostname, parsed.port or 80
            remote = _broker_connect(broker_path, host, port)
            origin = parsed.path or "/"
            if parsed.query:
                origin += "?" + parsed.query
            kept = [line for line in lines[1:] if not line.lower().startswith(b"proxy-connection:")]
            rewritten = f"{method} {origin} {version}\r\n".encode() + b"\r\n".join(kept)
            remote.sendall(rewritten + body)
        _relay(client, remote)
    except Exception:  # noqa: BLE001 - do not expose broker internals to the sandbox
        try:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
        except OSError:
            pass
    finally:
        client.close()
        if remote is not None:
            remote.close()


def _namespace_helper(mode: str, broker_socket: str | None, proxy_port: int) -> int:
    _bring_loopback_up()
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    server = None
    if mode == "proxy":
        if not broker_socket:
            return 125
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", proxy_port))
        server.listen(32)
        server.settimeout(0.2)
    print(json.dumps({"ready": True, "mode": mode}), flush=True)
    while not stopping.is_set():
        if server is None:
            stopping.wait(0.2)
            continue
        try:
            client, _ = server.accept()
        except TimeoutError:
            continue
        threading.Thread(target=_proxy_client, args=(client, broker_socket), daemon=True).start()
    if server is not None:
        server.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    helper = subparsers.add_parser("_namespace_helper")
    helper.add_argument("--mode", choices=("isolated", "proxy"), required=True)
    helper.add_argument("--broker-socket")
    helper.add_argument("--proxy-port", type=int, default=_PROXY_PORT)
    args = parser.parse_args(argv)
    if args.command == "_namespace_helper":
        return _namespace_helper(args.mode, args.broker_socket, args.proxy_port)
    return errno.EINVAL


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
