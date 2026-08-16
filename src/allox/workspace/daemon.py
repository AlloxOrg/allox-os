"""Trusted in-VM control service for Agent/Session workspace operations."""

from __future__ import annotations

import argparse
import json
import secrets
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from allox.workspace.store import WorkspaceError, WorkspaceStore, validate_id


class ExecutionRegistry:
    """Prevent checkpoint/rollback while a Session command is active."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._leases: dict[str, tuple[str, str]] = {}
        self._mutating: set[tuple[str, str]] = set()
        self._background: dict[tuple[str, str], dict[str, dict[str, str]]] = {}
        self._resets: dict[str, tuple[str, str]] = {}

    def acquire(self, agent_id: str, session_id: str) -> dict[str, Any]:
        key = (validate_id("agent", agent_id), validate_id("session", session_id))
        with self._lock:
            if key in self._mutating:
                raise WorkspaceError(f"session is being checkpointed or restored: {agent_id}/{session_id}")
            if key in self._leases.values():
                raise WorkspaceError(f"session already has an active execution: {agent_id}/{session_id}")
            token = uuid.uuid4().hex
            self._leases[token] = key
        return {"lease_token": token}

    def release(self, token: str) -> dict[str, Any]:
        with self._lock:
            released = self._leases.pop(token, None) is not None
        return {"released": released}

    def register_background(
        self, agent_id: str, session_id: str, sandbox_id: str, execution_id: str
    ) -> dict[str, Any]:
        key = (validate_id("agent", agent_id), validate_id("session", session_id))
        if not sandbox_id or not execution_id:
            raise WorkspaceError("background execution requires sandbox_id and execution_id")
        with self._lock:
            if key in self._mutating:
                raise WorkspaceError(f"session is being restored: {agent_id}/{session_id}")
            records = self._background.setdefault(key, {})
            records[execution_id] = {"sandbox_id": sandbox_id, "execution_id": execution_id}
        return {"registered": True, "execution_id": execution_id}

    def begin_runtime_reset(self, agent_id: str, session_id: str) -> dict[str, Any]:
        """Fence a Session and return its persistent executions for termination."""
        key = (validate_id("agent", agent_id), validate_id("session", session_id))
        with self._lock:
            if key in self._mutating:
                raise WorkspaceError(f"session mutation already in progress: {agent_id}/{session_id}")
            if key in self._leases.values():
                raise WorkspaceError(f"session has active executions: {agent_id}/{session_id}")
            self._mutating.add(key)
            token = uuid.uuid4().hex
            self._resets[token] = key
            executions = list(self._background.get(key, {}).values())
        return {"reset_token": token, "executions": executions}

    def complete_runtime_reset(self, token: str, *, success: bool) -> dict[str, Any]:
        with self._lock:
            key = self._resets.pop(token, None)
            if key is None:
                raise WorkspaceError("unknown runtime reset token")
            if success:
                self._background.pop(key, None)
            self._mutating.discard(key)
        return {"completed": True, "success": success}

    @contextmanager
    def reset_mutation(self, token: str, agent_id: str, session_id: str) -> Iterator[None]:
        key = (validate_id("agent", agent_id), validate_id("session", session_id))
        with self._lock:
            if self._resets.get(token) != key or key not in self._mutating:
                raise WorkspaceError("invalid runtime reset token for session")
        yield

    @contextmanager
    def mutation(self, agent_id: str, session_id: str) -> Iterator[None]:
        key = (validate_id("agent", agent_id), validate_id("session", session_id))
        with self._lock:
            if key in self._mutating:
                raise WorkspaceError(f"session mutation already in progress: {agent_id}/{session_id}")
            if key in self._leases.values():
                raise WorkspaceError(f"session has active executions: {agent_id}/{session_id}")
            if self._background.get(key):
                raise WorkspaceError(
                    f"session has persistent runtime executions: {agent_id}/{session_id}; "
                    "use runtime reset before rollback"
                )
            self._mutating.add(key)
        try:
            yield
        finally:
            with self._lock:
                self._mutating.discard(key)


class WorkspaceService:
    def __init__(self, store: WorkspaceStore):
        self.store = store
        self.executions = ExecutionRegistry()

    @staticmethod
    def _required(params: dict[str, Any], key: str) -> str:
        value = params.get(key)
        if not isinstance(value, str) or not value:
            raise WorkspaceError(f"missing string parameter: {key}")
        return value

    @staticmethod
    def _boolean(params: dict[str, Any], key: str, default: bool) -> bool:
        value = params.get(key, default)
        if not isinstance(value, bool):
            raise WorkspaceError(f"{key} must be a boolean")
        return value

    def dispatch(self, action: str, params: dict[str, Any]) -> Any:
        agent_id = params.get("agent_id")
        session_id = params.get("session_id")
        origin = str(params.get("origin", "allox-cli"))

        if action == "store.initialize":
            return self.store.initialize()
        if action == "agent.create":
            return self.store.create_agent(self._required(params, "agent_id"), origin=origin)
        if action == "session.create":
            return self.store.create_session(
                self._required(params, "agent_id"),
                self._required(params, "session_id"),
                origin=origin,
            )
        if action == "session.describe":
            return self.store.describe(
                self._required(params, "agent_id"),
                self._required(params, "session_id"),
            )
        if action == "checkpoint.list":
            agent_id = self._required(params, "agent_id")
            session_id = self._required(params, "session_id")
            return self.store.checkpoint_status(agent_id, session_id)
        if action == "execution.acquire":
            return self.executions.acquire(
                self._required(params, "agent_id"),
                self._required(params, "session_id"),
            )
        if action == "execution.release":
            return self.executions.release(self._required(params, "lease_token"))
        if action == "runtime.register_background":
            return self.executions.register_background(
                self._required(params, "agent_id"),
                self._required(params, "session_id"),
                self._required(params, "sandbox_id"),
                self._required(params, "execution_id"),
            )
        if action == "runtime.begin_reset":
            return self.executions.begin_runtime_reset(
                self._required(params, "agent_id"), self._required(params, "session_id")
            )
        if action == "runtime.complete_reset":
            return self.executions.complete_runtime_reset(
                self._required(params, "reset_token"),
                success=self._boolean(params, "success", False),
            )

        if action in {
            "checkpoint.create",
            "checkpoint.delete",
            "session.rollback",
            "session.rollback_after_runtime_reset",
        }:
            agent_id = self._required(params, "agent_id")
            session_id = self._required(params, "session_id")
            reset_token = params.get("reset_token")
            if action == "session.rollback_after_runtime_reset":
                if not isinstance(reset_token, str):
                    raise WorkspaceError("rollback after runtime reset requires reset_token")
                mutation = self.executions.reset_mutation(reset_token, agent_id, session_id)
            else:
                mutation = self.executions.mutation(agent_id, session_id)
            with mutation:
                if action == "checkpoint.create":
                    checkpoint_id = params.get("checkpoint_id")
                    if checkpoint_id is not None and not isinstance(checkpoint_id, str):
                        raise WorkspaceError("checkpoint_id must be a string")
                    message = params.get("message")
                    if message is not None and not isinstance(message, str):
                        raise WorkspaceError("message must be a string")
                    metadata = params.get("metadata")
                    if metadata is not None and not isinstance(metadata, dict):
                        raise WorkspaceError("metadata must be an object")
                    return self.store.create_checkpoint(
                        agent_id,
                        session_id,
                        checkpoint_id,
                        origin=origin,
                        message=message,
                        pinned=self._boolean(params, "pinned", False),
                        metadata=metadata,
                    )
                if action == "checkpoint.delete":
                    checkpoint_id = self._required(params, "checkpoint_id")
                    return self.store.delete_checkpoint(
                        agent_id,
                        session_id,
                        checkpoint_id,
                        origin=origin,
                        force=self._boolean(params, "force", False),
                    )
                checkpoint_id = params.get("checkpoint_id")
                if checkpoint_id is not None and not isinstance(checkpoint_id, str):
                    raise WorkspaceError("checkpoint_id must be a string")
                num_ancestors = params.get("num_ancestors")
                if num_ancestors is not None and (
                    isinstance(num_ancestors, bool) or not isinstance(num_ancestors, int)
                ):
                    raise WorkspaceError("num_ancestors must be an integer")
                return self.store.rollback(
                    agent_id,
                    session_id,
                    checkpoint_id,
                    num_ancestors=num_ancestors,
                    scrub_runtime=self._boolean(params, "scrub_runtime", True),
                    origin=origin,
                )

        raise WorkspaceError(f"unknown action: {action}")


class WorkspaceHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, service: WorkspaceService, token: str):
        super().__init__(address, WorkspaceRequestHandler)
        self.service = service
        self.token = token


class WorkspaceRequestHandler(BaseHTTPRequestHandler):
    server: WorkspaceHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _write_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not self.server.token:
            return True
        supplied = self.headers.get("Authorization", "")
        prefix = "Bearer "
        return supplied.startswith(prefix) and secrets.compare_digest(
            supplied[len(prefix) :], self.server.token
        )

    def do_GET(self) -> None:
        if self.path != "/health":
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self._write_json(HTTPStatus.OK, {"status": "healthy"})

    def do_POST(self) -> None:
        if self.path != "/v1/rpc":
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self._authorized():
            self._write_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1024 * 1024:
                raise WorkspaceError("invalid request size")
            request = json.loads(self.rfile.read(length))
            action = request.get("action")
            params = request.get("params", {})
            if not isinstance(action, str) or not isinstance(params, dict):
                raise WorkspaceError("request requires string action and object params")
            result = self.server.service.dispatch(action, params)
            self._write_json(HTTPStatus.OK, {"ok": True, "result": result})
        except (WorkspaceError, ValueError, json.JSONDecodeError) as exc:
            self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        except Exception:  # noqa: BLE001 - do not expose daemon internals over RPC
            self._write_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": "internal workspace daemon error"},
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="Btrfs workspace store root")
    parser.add_argument("--listen", default="127.0.0.1:8092")
    parser.add_argument("--token", default="", help="Bearer token (prefer ALLOX_WORKSPACE_TOKEN)")
    return parser


def main(argv: list[str] | None = None) -> int:
    import os

    args = build_parser().parse_args(argv)
    host, port_raw = args.listen.rsplit(":", 1)
    if host not in {"127.0.0.1", "::1", "localhost"} and not (
        args.token or os.environ.get("ALLOX_WORKSPACE_TOKEN")
    ):
        raise SystemExit("a bearer token is required when listening beyond loopback")
    token = args.token or os.environ.get("ALLOX_WORKSPACE_TOKEN", "")
    store = WorkspaceStore(args.root)
    store.initialize()
    server = WorkspaceHTTPServer((host, int(port_raw)), WorkspaceService(store), token)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
