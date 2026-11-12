"""Trusted in-VM control service for Agent/Session workspace operations."""

from __future__ import annotations

import argparse
import json
import secrets
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from allox.runtime.extensions import FeatureManager, WorkspaceFeatureContext
from allox.workspace.store import WorkspaceError, WorkspaceStore, validate_id

if TYPE_CHECKING:
    from allox.runtime.process_tracking import ProcessTrackingService


class ExecutionRegistry:
    """Prevent checkpoint/rollback while a Session command is active."""

    def __init__(self, admission_guard: Callable[[str, str], None] | None = None) -> None:
        self._lock = threading.Lock()
        self._leases: dict[str, tuple[str, str]] = {}
        self._recovery_leases: set[str] = set()
        self._mutating: set[tuple[str, str]] = set()
        self._background: dict[tuple[str, str], dict[str, dict[str, str]]] = {}
        self._resets: dict[str, tuple[str, str]] = {}
        self._active_resets: set[str] = set()
        self._share_readers: dict[tuple[str, str], int] = {}
        self._admission_guard = admission_guard

    def _require_ready(self, key: tuple[str, str]) -> None:
        if self._admission_guard is not None:
            self._admission_guard(*key)

    def acquire(
        self, agent_id: str, session_id: str, *, recovery: bool = False
    ) -> dict[str, Any]:
        key = (validate_id("agent", agent_id), validate_id("session", session_id))
        if not isinstance(recovery, bool):
            raise WorkspaceError("execution recovery reservation must be a boolean")
        with self._lock:
            self._require_ready(key)
            if key in self._mutating:
                raise WorkspaceError(f"session is being checkpointed or restored: {agent_id}/{session_id}")
            if key in self._leases.values():
                raise WorkspaceError(f"session already has an active execution: {agent_id}/{session_id}")
            if recovery and self._share_readers.get(key):
                raise WorkspaceError("recovery execution cannot start with active share reads")
            if recovery and self._background.get(key):
                raise WorkspaceError(
                    "recovery execution requires no persistent runtime executions"
                )
            token = uuid.uuid4().hex
            self._leases[token] = key
            if recovery:
                self._recovery_leases.add(token)
        result: dict[str, Any] = {"lease_token": token}
        if recovery:
            result["recovery_reserved"] = True
        return result

    def release(self, token: str) -> dict[str, Any]:
        with self._lock:
            self._recovery_leases.discard(token)
            released = self._leases.pop(token, None) is not None
        return {"released": released}

    def register_background(
        self, agent_id: str, session_id: str, sandbox_id: str, execution_id: str
    ) -> dict[str, Any]:
        key = (validate_id("agent", agent_id), validate_id("session", session_id))
        if not sandbox_id or not execution_id:
            raise WorkspaceError("background execution requires sandbox_id and execution_id")
        with self._lock:
            self._require_ready(key)
            if key in self._mutating:
                raise WorkspaceError(f"session is being restored: {agent_id}/{session_id}")
            if any(
                token in self._recovery_leases and lease_key == key
                for token, lease_key in self._leases.items()
            ):
                raise WorkspaceError(
                    "session has an active recovery execution reservation"
                )
            records = self._background.setdefault(key, {})
            records[execution_id] = {"sandbox_id": sandbox_id, "execution_id": execution_id}
        return {"registered": True, "execution_id": execution_id}

    def begin_runtime_reset(self, agent_id: str, session_id: str) -> dict[str, Any]:
        """Fence a Session and return its persistent executions for termination."""
        key = (validate_id("agent", agent_id), validate_id("session", session_id))
        with self._lock:
            self._require_ready(key)
            if self._share_readers.get(key):
                raise WorkspaceError("session has active share reads")
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
            if token in self._active_resets:
                raise WorkspaceError("runtime reset is in use by an active rollback")
            key = self._resets.pop(token, None)
            if key is None:
                raise WorkspaceError("unknown runtime reset token")
            if success:
                self._background.pop(key, None)
            self._mutating.discard(key)
        return {"completed": True, "success": success}

    def validate_termination(self, agent_id, session_id, *, reset_token=None):
        key = (validate_id("agent", agent_id), validate_id("session", session_id))
        with self._lock:
            self._require_ready(key)
            if self._share_readers.get(key):
                raise WorkspaceError("session has active share reads")
            active_reset = reset_token in self._active_resets and self._resets.get(reset_token) == key
            if key in self._mutating and not active_reset:
                raise WorkspaceError("session mutation already in progress")
            if self._background.get(key) and not active_reset:
                raise WorkspaceError("session has persistent runtime executions; use runtime reset first")

    @contextmanager
    def reset_mutation(self, token: str, agent_id: str, session_id: str) -> Iterator[None]:
        key = (validate_id("agent", agent_id), validate_id("session", session_id))
        with self._lock:
            self._require_ready(key)
            if self._resets.get(token) != key or key not in self._mutating:
                raise WorkspaceError("invalid runtime reset token for session")
            if token in self._active_resets:
                raise WorkspaceError("runtime reset already has an active rollback")
            self._active_resets.add(token)
        try:
            yield
        finally:
            with self._lock:
                self._active_resets.discard(token)

    @contextmanager
    def mutation(self, agent_id: str, session_id: str) -> Iterator[None]:
        key = (validate_id("agent", agent_id), validate_id("session", session_id))
        with self._lock:
            self._require_ready(key)
            if self._share_readers.get(key):
                raise WorkspaceError("session has active share reads")
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

    @contextmanager
    def share_access(self, agent_id: str, session_id: str, *, write: bool):
        if write:
            with self.mutation(agent_id, session_id):
                yield
            return
        key = (validate_id("agent", agent_id), validate_id("session", session_id))
        with self._lock:
            self._require_ready(key)
            if key in self._mutating:
                raise WorkspaceError("session is being checkpointed or restored")
            if any(
                token in self._recovery_leases and lease_key == key
                for token, lease_key in self._leases.items()
            ):
                raise WorkspaceError(
                    "session has an active recovery execution reservation"
                )
            self._share_readers[key] = self._share_readers.get(key, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                self._share_readers[key] -= 1
                if not self._share_readers[key]:
                    del self._share_readers[key]


class WorkspaceService:
    def __init__(
        self,
        store: WorkspaceStore,
        *,
        process_tracking: str = "disabled",
        process_audit_root: Path | None = None,
        process_tracker_command: str = "/usr/libexec/allox/allox-process-tracker",
        process_cgroup_root: Path = Path("/sys/fs/cgroup/allox"),
        kill_session_processes_on_rollback: bool = False,
        share_tools: bool = False,
        share_socket_root: Path = Path("/run/allox-share"),
        session_network: str = "disabled",
        network_root: Path = Path("/run/allox-network"),
        network_socket_root: Path = Path("/dev/shm/allox-network"),
        monitor_root: Path | None = None,
        monitor_async_write: bool = False,
        monitor_socket_root: Path = Path("/dev/shm/allox-monitor"),
        session_memory: bool = False,
        correlation_config: dict[str, Any] | None = None,
        recovery_checks_config: dict[str, Any] | None = None,
        system_effects_config: dict[str, Any] | None = None,
        process_service_factory=None,
        feature_factories=None,
    ):
        self.store = store
        self.features: FeatureManager | None = None
        self.executions = ExecutionRegistry(self._require_session_ready)
        if type(session_memory) is not bool:
            raise WorkspaceError("session_memory must be a boolean")
        self.session_memory = session_memory
        self.processes: ProcessTrackingService | None = None
        self.process_tracking = process_tracking.strip().lower()
        session_network = session_network.strip().lower()
        if session_network not in {"disabled", "inherit", "isolated", "proxy"}:
            raise WorkspaceError(
                "session_network must be disabled, inherit, isolated, or proxy"
            )
        if not isinstance(kill_session_processes_on_rollback, bool):
            raise WorkspaceError("kill_session_processes_on_rollback must be a boolean")
        self.kill_session_processes_on_rollback = kill_session_processes_on_rollback
        feature_specs = []
        if monitor_async_write and monitor_root is None:
            raise WorkspaceError('monitor_async_write requires monitor_root')
        if monitor_root is not None:
            feature_specs.append(("agent-monitor", {
                "root": monitor_root, "socket_root": monitor_socket_root,
                "async_write": monitor_async_write,
            }))
        if share_tools:
            feature_specs.append(("session-share", {"socket_root": share_socket_root}))
        if session_network != "disabled":
            feature_specs.append(
                (
                    "session-network",
                    {
                        "root": network_root,
                        "socket_root": network_socket_root,
                        "default_mode": session_network,
                    },
                )
            )
        if system_effects_config is not None:
            if not isinstance(system_effects_config, dict):
                raise WorkspaceError("system effects configuration must be an object")
            if self.process_tracking != "disabled":
                raise WorkspaceError(
                    "system-effects already records process lifecycle; disable the separate "
                    "process tracker to avoid duplicate kernel hooks"
                )
            feature_specs.append(("system-effects", dict(system_effects_config)))
        if correlation_config is not None:
            if not isinstance(correlation_config, dict):
                raise WorkspaceError("correlation configuration must be an object")
            feature_specs.append(("trace-correlation", dict(correlation_config)))
        if recovery_checks_config is not None:
            if not isinstance(recovery_checks_config, dict):
                raise WorkspaceError("recovery checks configuration must be an object")
            if system_effects_config is not None:
                raise WorkspaceError("recovery-checks v1 cannot wrap system-effects internal rollback")
            feature_specs.append(("recovery-checks", dict(recovery_checks_config)))
        execution_required = bool(
            self.process_tracking != "disabled"
            or any(name not in {"trace-correlation", "recovery-checks"} for name, _ in feature_specs)
            or self.kill_session_processes_on_rollback
        )
        if execution_required:
            if process_audit_root is None:
                raise WorkspaceError(
                    "process audit root is required when Session execution features are enabled"
                )
            if process_service_factory is None:
                from allox.runtime.process_tracking import ProcessTrackingService

                process_service_factory = ProcessTrackingService
            self.processes = process_service_factory(
                store,
                self.executions,
                process_audit_root,
                provider=self.process_tracking,
                provider_command=process_tracker_command,
                cgroup_root=process_cgroup_root,
            )
        if feature_specs:
            try:
                self.features = FeatureManager(
                    WorkspaceFeatureContext(
                        store=self.store,
                        executions=self.executions,
                        cgroups=self.processes.cgroups if self.processes is not None else None,
                    ),
                    tuple(feature_specs),
                    factories=feature_factories,
                )
                if self.processes is not None:
                    self.processes.features = self.features
            except BaseException:
                if self.processes is not None:
                    self.processes.close()
                self.processes = None
                raise

    @property
    def feature_names(self) -> tuple[str, ...]:
        return self.features.names if self.features is not None else ()

    def _require_session_ready(self, agent_id: str, session_id: str) -> None:
        self.store.require_memory_ready(agent_id, session_id)
        if self.features is not None:
            self.features.assert_session_ready(agent_id, session_id)

    def close(self) -> None:
        if self.processes is not None:
            self.processes.close()
        if self.features is not None:
            self.features.close()

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
        if self.features is not None and action in {
            "checkpoint.create", "checkpoint.delete", "session.rollback",
            "session.rollback_after_runtime_reset", "memory.restore",
        }:
            return self.features.management_mutation(
                action, params, lambda: self._dispatch(action, params),
            )
        return self._dispatch(action, params)

    def _dispatch(self, action: str, params: dict[str, Any]) -> Any:
        agent_id = params.get("agent_id")
        session_id = params.get("session_id")
        origin = str(params.get("origin", "allox-control-plane"))

        if self.features is not None and self.features.handles(action):
            return self.features.dispatch(action, params)

        if action in {"memory.read", "memory.write", "memory.restore"}:
            if not self.session_memory:
                raise WorkspaceError("Session memory is not enabled")
            agent_id = self._required(params, "agent_id")
            session_id = self._required(params, "session_id")
            if action == "memory.read":
                with self.executions.share_access(agent_id, session_id, write=False):
                    return self.store.memory_read(agent_id, session_id)
            with self.executions.mutation(agent_id, session_id):
                revision = params.get("expected_revision")
                if action == "memory.write":
                    return self.store.memory_write(
                        agent_id, session_id, params.get("state"), expected_revision=revision,
                    )
                return self.store.memory_restore(
                    agent_id, session_id, self._required(params, "checkpoint_id"),
                    expected_revision=revision,
                )

        if action.startswith("process."):
            if action == "process.status":
                if self.processes is None:
                    return {"enabled": False, "backend": "disabled"}
                return self.processes.status()
            if self.processes is None:
                raise WorkspaceError("process tracking is not enabled on this daemon")
            agent_id = self._required(params, "agent_id")
            session_id = self._required(params, "session_id")
            if action == "process.start":
                trace_options = (
                    {"trace_context": params["trace_context"]} if "trace_context" in params else {}
                )
                argv = params.get("argv")
                if not isinstance(argv, list):
                    raise WorkspaceError("argv must be a nonempty array of nonempty strings")
                recovery_options = {"recovery": params["recovery"]} if "recovery" in params else {}
                return self.processes.start(
                    agent_id, session_id, argv,
                    env=params.get("env"),
                    timeout=params.get("timeout", 300),
                    **recovery_options,
                    **trace_options,
                )
            if action == "process.list":
                return self.processes.list_runs(agent_id, session_id)
            run_id = self._required(params, "run_id")
            if action == "process.get":
                return self.processes.get(agent_id, session_id, run_id)
            if action == "process.events":
                return self.processes.events(
                    agent_id, session_id, run_id,
                    after=params.get("after", 0), limit=params.get("limit", 100),
                )
            if action == "process.stop":
                return self.processes.stop(agent_id, session_id, run_id)
            raise WorkspaceError(f"unknown action: {action}")

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
            agent_id = self._required(params, "agent_id")
            session_id = self._required(params, "session_id")
            terminate = self._boolean(
                params, "kill_processes", self.kill_session_processes_on_rollback
            )
            if terminate:
                if self.processes is None:
                    raise WorkspaceError("kill_processes requires process tracking")
                launch_fence = self.processes.session_launch_fence(
                    agent_id, session_id, terminate=True, reason="rollback"
                )
            else:
                launch_fence = nullcontext([])
            with launch_fence as stopped:
                result = self.executions.begin_runtime_reset(agent_id, session_id)
            if terminate:
                result["terminated_process_runs"] = stopped
            return result
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
            mutation: AbstractContextManager[Any]
            if action == "session.rollback_after_runtime_reset":
                if not isinstance(reset_token, str):
                    raise WorkspaceError("rollback after runtime reset requires reset_token")
                mutation = self.executions.reset_mutation(reset_token, agent_id, session_id)
            else:
                mutation = self.executions.mutation(agent_id, session_id)
            terminate = action.startswith("session.rollback") and self._boolean(
                params, "kill_processes", self.kill_session_processes_on_rollback
            )
            # Enter a token-bound reservation BEFORE any kill. It stays pinned
            # until rollback completes, so another request cannot complete/reset it.
            reset_scope = mutation if action == "session.rollback_after_runtime_reset" else nullcontext()
            if action == "session.rollback_after_runtime_reset":
                mutation = nullcontext()
            def before_change():
                if self.features is not None:
                    self.features.mutation_started(action, params)

            mutation_options: dict[str, Any] = (
                {"before_change": before_change} if self.features is not None else {}
            )

            def before_terminate():
                self.executions.validate_termination(
                    agent_id, session_id,
                    reset_token=reset_token if action == "session.rollback_after_runtime_reset" else None,
                )
                checkpoint = params.get("checkpoint_id")
                if checkpoint is not None and not isinstance(checkpoint, str):
                    raise WorkspaceError("checkpoint_id must be a string")
                ancestors = params.get("num_ancestors")
                if ancestors is not None and type(ancestors) is not int:
                    raise WorkspaceError("num_ancestors must be an integer")
                self._boolean(params, "scrub_runtime", True)
                self.store.validate_rollback_target(
                    agent_id, session_id, checkpoint,
                    num_ancestors=ancestors, restore_memory=self._boolean(params, "restore_memory", True),
                )
                before_change()
            if terminate:
                if self.processes is None:
                    raise WorkspaceError("kill_processes requires process tracking")
                launch_fence = self.processes.session_launch_fence(
                    agent_id, session_id, terminate=True, reason="rollback",
                    before_terminate=before_terminate,
                )
            else:
                launch_fence = nullcontext([])
            with reset_scope, launch_fence as stopped, mutation:
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
                    checkpoint_options: dict[str, Any] = {}
                    if "memory_state" in params or "expected_memory_revision" in params:
                        if not self.session_memory:
                            raise WorkspaceError("Session memory is not enabled")
                        checkpoint_options["memory_update"] = (
                            params.get("memory_state"), params.get("expected_memory_revision"),
                        )
                    return self.store.create_checkpoint(
                        agent_id,
                        session_id,
                        checkpoint_id,
                        origin=origin,
                        message=message,
                        pinned=self._boolean(params, "pinned", False),
                        metadata=metadata,
                        **checkpoint_options,
                        **mutation_options,
                    )
                if action == "checkpoint.delete":
                    checkpoint_id = self._required(params, "checkpoint_id")
                    return self.store.delete_checkpoint(
                        agent_id,
                        session_id,
                        checkpoint_id,
                        origin=origin,
                        force=self._boolean(params, "force", False),
                        **mutation_options,
                    )
                checkpoint_id = params.get("checkpoint_id")
                if checkpoint_id is not None and not isinstance(checkpoint_id, str):
                    raise WorkspaceError("checkpoint_id must be a string")
                num_ancestors = params.get("num_ancestors")
                if num_ancestors is not None and (
                    isinstance(num_ancestors, bool) or not isinstance(num_ancestors, int)
                ):
                    raise WorkspaceError("num_ancestors must be an integer")
                memory_options: dict[str, Any] = (
                    {"restore_memory": self._boolean(params, "restore_memory", True)}
                    if "restore_memory" in params else {}
                )
                result = self.store.rollback(
                    agent_id,
                    session_id,
                    checkpoint_id,
                    num_ancestors=num_ancestors,
                    scrub_runtime=self._boolean(params, "scrub_runtime", True),
                    origin=origin,
                    **memory_options,
                    **mutation_options,
                )
                if terminate:
                    result["terminated_process_runs"] = stopped
                return result

        raise WorkspaceError(f"unknown action: {action}")


class WorkspaceHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, service: WorkspaceService, token: str):
        if service.session_memory and not token:
            raise WorkspaceError("Session memory requires an authenticated management endpoint")
        if {"session-share", "agent-monitor", "trace-correlation", "recovery-checks"}.intersection(service.feature_names) and not token:
            raise WorkspaceError("share/monitor tools require an authenticated management endpoint")
        super().__init__(address, WorkspaceRequestHandler)
        self.service = service
        self.token = token


class WorkspaceRequestHandler(BaseHTTPRequestHandler):
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
        server = cast(WorkspaceHTTPServer, self.server)
        if not server.token:
            return True
        supplied = self.headers.get("Authorization", "")
        prefix = "Bearer "
        return supplied.startswith(prefix) and secrets.compare_digest(
            supplied[len(prefix) :], server.token
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
            server = cast(WorkspaceHTTPServer, self.server)
            result = server.service.dispatch(action, params)
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
    parser.add_argument("--session-memory", action="store_true",
                        help="Enable authenticated Session logical memory RPCs")
    parser.add_argument("--correlation-trace-root", type=Path, default=None,
                        help="Enable read-only post-hoc correlation from existing trace storage")
    parser.add_argument("--monitor-root", type=Path, default=None,
                        help="Enable agent-monitor; durable traces outside workspace store")
    parser.add_argument('--monitor-async-write', action='store_true',
                        help='Opt-in bounded monitoring queue; only flush-confirmed events are durable')
    parser.add_argument("--recovery-checks-root", type=Path, default=None,
                        help="Enable bounded restore verification with durable external fences")
    parser.add_argument("--monitor-socket-root", type=Path, default=Path("/dev/shm/allox-monitor"),
                        help="Trusted tmpfs for Session-scoped append-only telemetry sockets")
    parser.add_argument(
        "--process-tracking", default="disabled", metavar="PROVIDER",
        help="Process tracker provider: disabled, ebpf, or an installed plugin name",
    )
    parser.add_argument(
        "--process-audit-root", type=Path, default=None,
        help="Audit path outside the workspace store; required when tracking is enabled",
    )
    parser.add_argument(
        "--process-tracker-command", default="/usr/libexec/allox/allox-process-tracker",
        help="Native provider command; independently replaceable from alloxd",
    )
    parser.add_argument(
        "--process-cgroup-root", type=Path, default=Path("/sys/fs/cgroup/allox"),
        help="Allox-owned cgroup v2 subtree for Agent/Session processes",
    )
    parser.add_argument(
        "--kill-session-processes-on-rollback",
        action="store_true",
        help="Kill the tracked Session cgroup before workspace rollback",
    )
    parser.add_argument("--share-tools", action="store_true",
                        help="Mount a Session-bound share tool in tracked Bubblewrap runs")
    parser.add_argument("--share-socket-root", type=Path, default=Path("/run/allox-share"),
                        help="Trusted local filesystem for share sockets (not a host share)")
    parser.add_argument(
        "--session-network",
        choices=("disabled", "inherit", "isolated", "proxy"),
        default="disabled",
        help="Enable persistent per-Session network namespaces; default disabled",
    )
    parser.add_argument(
        "--network-root",
        type=Path,
        default=Path("/run/allox-network"),
        help="Trusted Session network configuration and audit directory",
    )
    parser.add_argument(
        "--network-socket-root",
        type=Path,
        default=Path("/dev/shm/allox-network"),
        help="Short tmpfs path for trusted per-Session network broker sockets",
    )
    parser.add_argument(
        "--system-effects-observer",
        type=Path,
        default=None,
        help="Absolute deployment-observe binary; enables the system-effects plugin",
    )
    parser.add_argument(
        "--system-effects-verifier",
        type=Path,
        default=None,
        help="Absolute deployment-observe-verify binary (defaults beside observer)",
    )
    parser.add_argument(
        "--system-effects-recovery-verifier",
        type=Path,
        default=None,
        help="Absolute deployment-observe-recovery-verify binary",
    )
    parser.add_argument(
        "--system-effects-recovery-helper",
        type=Path,
        default=None,
        help="Content-bound Allox recovery adapter executable",
    )
    parser.add_argument("--system-effects-recovery-helper-sha256", default=None)
    parser.add_argument(
        "--system-effects-recovery-timeout-ms", type=int, default=None
    )
    parser.add_argument(
        "--system-effects-recovery-max-output-bytes", type=int, default=None
    )
    parser.add_argument("--system-effects-recorder-object", type=Path, default=None)
    parser.add_argument("--system-effects-recorder-sha256", default=None)
    parser.add_argument("--system-effects-lifecycle-object", type=Path, default=None)
    parser.add_argument("--system-effects-lifecycle-sha256", default=None)
    parser.add_argument(
        "--system-effects-profile",
        choices=("control", "balanced", "full"),
        default=None,
    )
    parser.add_argument(
        "--system-effects-object-resolution",
        choices=(
            "USER_INPUT",
            "KERNEL_FILE_OPEN_PARTIAL",
            "KERNEL_FILE_OPEN_WRITE_PARTIAL",
            "KERNEL_OBJECT_EXPANDED_PARTIAL",
        ),
        default=None,
    )
    parser.add_argument("--system-effects-bpf-scope", type=Path, default=None)
    parser.add_argument("--system-effects-filesystem-scope", type=Path, default=None)
    parser.add_argument("--system-effects-linux-scope", type=Path, default=None)
    parser.add_argument("--system-effects-poll-interval-ms", type=int, default=None)
    parser.add_argument("--system-effects-drain-timeout-ms", type=int, default=None)
    parser.add_argument("--system-effects-ready-timeout", type=float, default=None)
    parser.add_argument("--system-effects-finalize-timeout", type=float, default=None)
    parser.add_argument(
        "--system-effects-host-lock",
        type=Path,
        default=None,
        help="Shared host lock serializing global S0/S1 snapshots",
    )
    return parser


def _system_effects_config_from_args(args) -> dict[str, Any] | None:
    required = {
        "observer_path": args.system_effects_observer,
        "recorder_object_path": args.system_effects_recorder_object,
        "recorder_object_sha256": args.system_effects_recorder_sha256,
        "lifecycle_object_path": args.system_effects_lifecycle_object,
        "lifecycle_object_sha256": args.system_effects_lifecycle_sha256,
    }
    if not any(value is not None for value in required.values()):
        if any(
            value is not None
            for value in (
                args.system_effects_verifier,
                args.system_effects_recovery_verifier,
                args.system_effects_recovery_helper,
                args.system_effects_recovery_helper_sha256,
                args.system_effects_recovery_timeout_ms,
                args.system_effects_recovery_max_output_bytes,
                args.system_effects_bpf_scope,
                args.system_effects_filesystem_scope,
                args.system_effects_linux_scope,
                args.system_effects_profile,
                args.system_effects_object_resolution,
                args.system_effects_poll_interval_ms,
                args.system_effects_drain_timeout_ms,
                args.system_effects_ready_timeout,
                args.system_effects_finalize_timeout,
                args.system_effects_host_lock,
            )
        ):
            raise WorkspaceError(
                "system-effects scope/verifier options require the complete observer configuration"
            )
        return None
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise WorkspaceError(
            "incomplete system-effects configuration; missing " + ", ".join(sorted(missing))
        )
    observer = Path(args.system_effects_observer)
    verifier = args.system_effects_verifier or observer.with_name("deployment-observe-verify")
    recovery_values = (
        args.system_effects_recovery_helper,
        args.system_effects_recovery_helper_sha256,
    )
    if any(value is not None for value in recovery_values) and not all(
        value is not None for value in recovery_values
    ):
        raise WorkspaceError(
            "system-effects recovery helper path and SHA-256 must be configured together"
        )
    if (
        args.system_effects_recovery_verifier is not None
        and args.system_effects_recovery_helper is None
    ):
        raise WorkspaceError(
            "system-effects recovery verifier requires a recovery helper"
        )
    return {
        **{
            key: str(value) if isinstance(value, Path) else value
            for key, value in required.items()
        },
        "verifier_path": str(verifier),
        "recovery_verifier_path": (
            str(
                args.system_effects_recovery_verifier
                or observer.with_name("deployment-observe-recovery-verify")
            )
            if args.system_effects_recovery_helper is not None
            else None
        ),
        "recovery_helper_path": (
            str(args.system_effects_recovery_helper)
            if args.system_effects_recovery_helper is not None
            else None
        ),
        "recovery_helper_sha256": args.system_effects_recovery_helper_sha256,
        "recovery_timeout_ms": (
            args.system_effects_recovery_timeout_ms
            if args.system_effects_recovery_timeout_ms is not None
            else 120_000
        ),
        "recovery_max_output_bytes": (
            args.system_effects_recovery_max_output_bytes
            if args.system_effects_recovery_max_output_bytes is not None
            else 65_536
        ),
        "profile": args.system_effects_profile or "balanced",
        "object_resolution_mode": (
            args.system_effects_object_resolution or "KERNEL_OBJECT_EXPANDED_PARTIAL"
        ),
        "bpf_state_scope_file": (
            str(args.system_effects_bpf_scope) if args.system_effects_bpf_scope else None
        ),
        "filesystem_state_scope_file": (
            str(args.system_effects_filesystem_scope)
            if args.system_effects_filesystem_scope
            else None
        ),
        "linux_state_scope_file": (
            str(args.system_effects_linux_scope) if args.system_effects_linux_scope else None
        ),
        "poll_interval_ms": (
            args.system_effects_poll_interval_ms
            if args.system_effects_poll_interval_ms is not None
            else 20
        ),
        "drain_timeout_ms": (
            args.system_effects_drain_timeout_ms
            if args.system_effects_drain_timeout_ms is not None
            else 5000
        ),
        "ready_timeout": (
            args.system_effects_ready_timeout
            if args.system_effects_ready_timeout is not None
            else 30.0
        ),
        "finalize_timeout": (
            args.system_effects_finalize_timeout
            if args.system_effects_finalize_timeout is not None
            else 60.0
        ),
        "host_lock_path": str(
            args.system_effects_host_lock or Path("/run/allox-system-effects.lock")
        ),
    }


def main(argv: list[str] | None = None) -> int:
    import os

    args = build_parser().parse_args(argv)
    host, port_raw = args.listen.rsplit(":", 1)
    if host not in {"127.0.0.1", "::1", "localhost"} and not (
        args.token or os.environ.get("ALLOX_WORKSPACE_TOKEN")
    ):
        raise SystemExit("a bearer token is required when listening beyond loopback")
    token = args.token or os.environ.get("ALLOX_WORKSPACE_TOKEN", "")
    try:
        system_effects_config = _system_effects_config_from_args(args)
    except WorkspaceError as exc:
        raise SystemExit(str(exc)) from exc
    if args.share_tools and not token:
        raise SystemExit("--share-tools requires ALLOX_WORKSPACE_TOKEN for the management API")
    if args.monitor_root is not None and not token:
        raise SystemExit("--monitor-root requires ALLOX_WORKSPACE_TOKEN for the management API")
    if args.session_memory and not token:
        raise SystemExit("--session-memory requires ALLOX_WORKSPACE_TOKEN for the management API")
    correlation_config = None
    if args.correlation_trace_root is not None:
        if not token or args.process_audit_root is None:
            raise SystemExit("--correlation-trace-root requires a token and --process-audit-root")
        correlation_config = {
            "trace_root": args.correlation_trace_root, "audit_root": args.process_audit_root,
        }
    if system_effects_config is not None and not token:
        raise SystemExit(
            "--system-effects-observer requires ALLOX_WORKSPACE_TOKEN for the management API"
        )
    store = WorkspaceStore(args.root)
    store.initialize()
    service = WorkspaceService(
        store,
        process_tracking=args.process_tracking,
        process_audit_root=args.process_audit_root,
        process_tracker_command=args.process_tracker_command,
        process_cgroup_root=args.process_cgroup_root,
        kill_session_processes_on_rollback=args.kill_session_processes_on_rollback,
        share_tools=args.share_tools,
        share_socket_root=args.share_socket_root,
        session_network=args.session_network,
        network_root=args.network_root,
        network_socket_root=args.network_socket_root,
        monitor_root=args.monitor_root,
        monitor_async_write=args.monitor_async_write,
        monitor_socket_root=args.monitor_socket_root,
        session_memory=args.session_memory,
        correlation_config=correlation_config,
        recovery_checks_config=({"root": args.recovery_checks_root}
                                if args.recovery_checks_root is not None else None),
        system_effects_config=system_effects_config,
    )
    server = WorkspaceHTTPServer((host, int(port_raw)), service, token)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.close()
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
