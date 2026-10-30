"""Fail-closed adapter for one ``deployment-observe`` protocol-v2 run."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import queue
import re
import signal
import stat
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from allox.workspace.store import WorkspaceError
from allox_system_effects.config import MAX_CONFIG_BYTES, SystemEffectsConfig, is_sha256

PROTOCOL_VERSION = 2
RECOVERY_PROTOCOL_VERSION = 3
MAX_PROTOCOL_LINE_BYTES = 64 * 1024
MAX_ARTIFACT_JSON_BYTES = 16 * 1024 * 1024
MAX_VERIFIER_STDOUT_BYTES = 4 * 1024 * 1024
MAX_VERIFIER_STDERR_BYTES = 256 * 1024
_OUTCOMES = frozenset({"completed", "stopped", "launch_failed"})
_EVIDENCE_STATUSES = frozenset({"COMPLETE", "INCOMPLETE", "UNPROVEN_NO_EVENTS"})
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_BOOT_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

_READY_KEYS = frozenset(
    {
        "protocol_version",
        "status",
        "run_id",
        "epoch",
        "finish_nonce",
        "deployment_scope_sha256",
        "registration_cgroup_ids",
        "cgroup_scope_capability_sha256",
        "cgroup_scope_capability",
        "deployment_manifest_sha256",
        "ready_barrier_sha256",
        "ready_validation_sha256",
        "recorder_object_sha256",
        "lifecycle_object_sha256",
        "socket_state_enabled",
        "bpf_state_scope_sha256",
        "bpf_state_s0_sha256",
        "filesystem_state_scope_sha256",
        "filesystem_state_s0_sha256",
        "linux_state_scope_sha256",
        "linux_state_s0_sha256",
    }
)
_FINISHED_KEYS = frozenset(
    {
        "protocol_version",
        "status",
        "run_id",
        "evidence_status",
        "report_sha256",
        "artifact_manifest_sha256",
    }
)
_VERIFICATION_KEYS = frozenset(
    {
        "protocol_version",
        "verified",
        "run_id",
        "epoch",
        "deployment_scope_sha256",
        "artifact_count",
        "committed_artifact_total_bytes",
        "system_effect_wal_event_count",
        "system_effect_jsonl_event_count",
        "process_lifecycle_wal_event_count",
        "process_lifecycle_jsonl_event_count",
        "evidence_status",
        "socket_state_status",
        "socket_state_event_count",
        "bpf_state_scope_sha256",
        "bpf_state_verdict",
        "filesystem_state_scope_sha256",
        "filesystem_state_verdict",
        "linux_state_scope_sha256",
        "linux_state_evidence_status",
    }
)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _json_clone(value: object) -> object:
    return json.loads(_canonical_json(value))


def _fixed_environment() -> dict[str, str]:
    return {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    }


def _valid_nonnegative_integer(value: object) -> bool:
    return type(value) is int and value >= 0


def _validate_relative_cgroup_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise WorkspaceError("system-effects cgroup_relative_path is invalid")
    path = PurePosixPath(value)
    if (
        not path.is_absolute()
        or path == PurePosixPath("/")
        or ".." in path.parts
        or path.as_posix() != value
    ):
        raise WorkspaceError("system-effects cgroup_relative_path must be canonical and absolute")
    return value


def _validate_absolute_path(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value or "\0" in value:
        raise WorkspaceError(f"system-effects {label} is invalid")
    path = Path(value)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError(f"system-effects {label} is unavailable: {exc}") from exc
    if not path.is_absolute() or resolved != path:
        raise WorkspaceError(f"system-effects {label} must be canonical and absolute")
    return path


@dataclass(frozen=True)
class RunBinding:
    agent_id: str
    session_id: str
    run_id: str
    run_epoch: int
    root_pid: int
    cgroup_path: Path
    cgroup_relative_path: str
    cgroup_id: int
    mount_namespace_id: int
    network_namespace_id: int
    audit_directory: Path
    boot_id: str
    on_failure: Callable[[str], None]

    @classmethod
    def from_context(cls, context: object) -> RunBinding:
        values: dict[str, Any] = {}
        for name in (
            "agent_id",
            "session_id",
            "run_id",
            "run_epoch",
            "root_pid",
            "cgroup_path",
            "cgroup_relative_path",
            "cgroup_id",
            "mount_namespace_id",
            "network_namespace_id",
            "audit_directory",
            "boot_id",
            "on_failure",
        ):
            if not hasattr(context, name):
                raise WorkspaceError(f"system-effects execution context lacks {name}")
            values[name] = getattr(context, name)
        for name in ("agent_id", "session_id", "run_id"):
            if not isinstance(values[name], str) or not _IDENTIFIER.fullmatch(values[name]):
                raise WorkspaceError(f"system-effects {name} has an invalid identity")
        for name in (
            "run_epoch",
            "root_pid",
            "cgroup_id",
            "mount_namespace_id",
            "network_namespace_id",
        ):
            if type(values[name]) is not int or values[name] <= 0:
                raise WorkspaceError(f"system-effects {name} must be a positive integer")
        if not isinstance(values["boot_id"], str) or not _BOOT_ID.fullmatch(values["boot_id"]):
            raise WorkspaceError("system-effects boot_id is not a canonical Linux boot UUID")
        if not callable(values["on_failure"]):
            raise WorkspaceError("system-effects on_failure callback is invalid")
        cgroup = _validate_absolute_path(values["cgroup_path"], "cgroup_path")
        if not cgroup.is_dir():
            raise WorkspaceError("system-effects cgroup_path is not a directory")
        audit = _validate_absolute_path(values["audit_directory"], "audit_directory")
        metadata = audit.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
        ):
            raise WorkspaceError(
                "system-effects audit_directory must be a private daemon-owned directory"
            )
        return cls(
            agent_id=values["agent_id"],
            session_id=values["session_id"],
            run_id=values["run_id"],
            run_epoch=values["run_epoch"],
            root_pid=values["root_pid"],
            cgroup_path=cgroup,
            cgroup_relative_path=_validate_relative_cgroup_path(values["cgroup_relative_path"]),
            cgroup_id=values["cgroup_id"],
            mount_namespace_id=values["mount_namespace_id"],
            network_namespace_id=values["network_namespace_id"],
            audit_directory=audit,
            boot_id=values["boot_id"],
            on_failure=values["on_failure"],
        )


@dataclass(frozen=True)
class _ProtocolFailure:
    reason: str


@dataclass(frozen=True)
class _StableArtifact:
    value: dict[str, Any]
    digest: str
    size: int


class _BoundedCapture:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.data = bytearray()
        self.overflow = False

    def drain(self, stream: Any) -> None:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            remaining = self.limit - len(self.data)
            if remaining > 0:
                self.data.extend(chunk[:remaining])
            if len(chunk) > remaining:
                self.overflow = True


class SystemEffectsRun:
    """Own one privileged Observer child and its closed evidence transaction."""

    def __init__(
        self,
        config: SystemEffectsConfig,
        context: object,
        on_close: Callable[[SystemEffectsRun], None],
    ) -> None:
        self._config = config
        self._binding = RunBinding.from_context(context)
        self._on_close = on_close
        self._evidence_directory = self._binding.audit_directory / "system-effects"
        self._config_path = self._binding.audit_directory / "system-effects-config.json"
        self._expectation_path = (
            self._binding.audit_directory / "system-effects-verification-expectation.json"
        )
        self._verification_path = self._binding.audit_directory / "system-effects-verification.json"
        self._receipt_path = self._binding.audit_directory / "system-effects-receipt.json"
        self._messages: queue.Queue[dict[str, Any] | _ProtocolFailure] = queue.Queue(maxsize=4)
        self._diagnostics: deque[bytes] = deque(maxlen=64)
        self._lock = threading.RLock()
        self._phase = "STARTING"
        self._failure_reason: str | None = None
        self._failure_reported = False
        self._expected_shutdown = False
        self._finish_claimed = False
        self._final_result: dict[str, Any] | None = None
        self._ready: dict[str, Any] | None = None
        self._finish_nonce: str | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._verifier_process: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._errors: threading.Thread | None = None
        self._retired = False

        if self._evidence_directory.exists() or self._evidence_directory.is_symlink():
            raise WorkspaceError("system-effects evidence directory already exists")
        self._config_document = self._build_observer_config()
        encoded = _canonical_json(self._config_document) + b"\n"
        if len(encoded) > MAX_CONFIG_BYTES:
            raise WorkspaceError("system-effects Observer config exceeds the 1 MiB bound")
        expectation_probe = {
            "expected_config": self._config_document,
            "expected_artifact_manifest_sha256": "0" * 64,
            "expected_report_sha256": "0" * 64,
        }
        if len(_canonical_json(expectation_probe) + b"\n") > MAX_CONFIG_BYTES:
            raise WorkspaceError(
                "system-effects verifier expectation exceeds the 1 MiB bound"
            )
        self._config_sha256 = hashlib.sha256(encoded).hexdigest()
        _write_new_file(self._config_path, encoded)

        try:
            self._process = subprocess.Popen(
                [str(config.observer_path), "--config", str(self._config_path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                close_fds=True,
                start_new_session=True,
                env=_fixed_environment(),
            )
        except OSError as exc:
            self._set_failed("failed to start deployment-observe")
            self._retire()
            raise WorkspaceError(f"failed to start deployment-observe: {exc}") from exc

        self._reader = threading.Thread(
            target=self._read_stdout,
            name=f"allox-effects-stdout-{self._binding.run_id}",
            daemon=True,
        )
        self._errors = threading.Thread(
            target=self._read_stderr,
            name=f"allox-effects-stderr-{self._binding.run_id}",
            daemon=True,
        )
        self._reader.start()
        self._errors.start()
        try:
            ready = self._receive(time.monotonic() + config.ready_timeout, "READY")
            self._validate_ready(ready)
            with self._lock:
                if self._failure_reason is not None:
                    raise WorkspaceError(self._failure_reason)
                self._ready = ready
                self._finish_nonce = ready["finish_nonce"]
                self._phase = "READY"
            self.assert_healthy()
        except BaseException:
            self.close_unverified()
            raise

    def _build_observer_config(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "deployment": {
                "deployment_kind": "DIRECT",
                "configuration": {
                    "root_pid": self._binding.root_pid,
                    "expected_cgroup_path": self._binding.cgroup_relative_path,
                    "require_dedicated_cgroup": True,
                    "limits": {
                        "max_proc_entries": 131_072,
                        "max_cgroup_nodes": 4096,
                        "max_cgroup_depth": 64,
                        "max_open_fds_per_process": 8192,
                    },
                },
            },
            "recorder_object_path": str(self._config.recorder_object_path),
            "recorder_object_sha256": self._config.recorder_object_sha256,
            "lifecycle_object_path": str(self._config.lifecycle_object_path),
            "lifecycle_object_sha256": self._config.lifecycle_object_sha256,
            "output_directory": str(self._evidence_directory),
            "run_id": self._binding.run_id,
            "epoch": self._binding.run_epoch,
            "profile": self._config.profile,
            "object_resolution_mode": self._config.object_resolution_mode,
            "bpf_state_scope": _json_clone(self._config.bpf_state_scope),
            "filesystem_state_scope": _json_clone(self._config.filesystem_state_scope),
            "linux_state_scope": self._per_run_linux_state_scope(),
            "poll_interval_ms": self._config.poll_interval_ms,
            "drain_timeout_ms": self._config.drain_timeout_ms,
        }

    def _per_run_linux_state_scope(self) -> object:
        scope = _json_clone(self._config.linux_state_scope)
        if scope is None:
            return None
        if not isinstance(scope, dict):  # Defensive: config loading already requires an object.
            raise WorkspaceError("system-effects Linux state scope must be an object")
        for domain, namespace_id in (
            ("mounts", self._binding.mount_namespace_id),
            ("network", self._binding.network_namespace_id),
        ):
            configured = scope.get(domain)
            if configured is None:
                continue
            if not isinstance(configured, dict):
                raise WorkspaceError(
                    f"system-effects Linux state {domain} scope must be an object or null"
                )
            configured["namespace"] = {
                "pid": self._binding.root_pid,
                "expected_namespace_inode": namespace_id,
            }
        return scope

    def _read_stdout(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        while True:
            try:
                line = process.stdout.readline(MAX_PROTOCOL_LINE_BYTES + 1)
            except OSError:
                self._publish_failure("deployment-observe stdout read failed")
                return
            if not line:
                with self._lock:
                    expected = self._expected_shutdown or self._phase == "TERMINAL_RECEIVED"
                if not expected:
                    self._publish_failure("deployment-observe exited unexpectedly")
                return
            if len(line) > MAX_PROTOCOL_LINE_BYTES or not line.endswith(b"\n"):
                self._publish_failure("deployment-observe response exceeded protocol bound")
                return
            try:
                message = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._publish_failure("deployment-observe returned invalid JSON")
                return
            if not isinstance(message, dict):
                self._publish_failure("deployment-observe response must be a JSON object")
                return
            with self._lock:
                phase = self._phase
            expected_status = (
                "READY" if phase == "STARTING" else ("FINISHED" if phase == "FINISHING" else None)
            )
            if message.get("status") != expected_status:
                self._publish_failure("deployment-observe sent an out-of-order message")
                return
            if expected_status == "FINISHED":
                with self._lock:
                    self._phase = "TERMINAL_RECEIVED"
            try:
                self._messages.put_nowait(message)
            except queue.Full:
                self._publish_failure("deployment-observe sent unsolicited messages")
                return

    def _read_stderr(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        while True:
            try:
                chunk = process.stderr.read(4096)
            except OSError:
                return
            if not chunk:
                return
            self._diagnostics.append(chunk[:4096])

    def _publish_failure(self, reason: str) -> None:
        notify = False
        with self._lock:
            if self._failure_reason is None and not self._expected_shutdown:
                self._failure_reason = reason
                self._phase = "FAILED"
                notify = not self._failure_reported
                self._failure_reported = True
        try:
            self._messages.put_nowait(_ProtocolFailure(reason))
        except queue.Full:
            pass
        if notify:
            try:
                self._binding.on_failure(reason)
            except Exception as exc:  # noqa: BLE001 - never stop the drain thread
                self._diagnostics.append(
                    f"on_failure callback raised {type(exc).__name__}".encode()
                )

    def _set_failed(self, reason: str) -> None:
        with self._lock:
            if self._failure_reason is None:
                self._failure_reason = reason
            self._phase = "FAILED"

    def _receive(self, deadline: float, expected: str) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WorkspaceError(f"deployment-observe {expected} response timed out")
        try:
            item = self._messages.get(timeout=remaining)
        except queue.Empty as exc:
            raise WorkspaceError(f"deployment-observe {expected} response timed out") from exc
        if isinstance(item, _ProtocolFailure):
            detail = b"".join(self._diagnostics)[-4000:].decode("utf-8", errors="replace")
            suffix = f": {detail}" if detail else ""
            raise WorkspaceError(f"{item.reason}{suffix}")
        return item

    def _validate_ready(self, ready: dict[str, Any]) -> None:
        if set(ready) != _READY_KEYS:
            raise WorkspaceError("deployment-observe READY schema mismatch")
        if (
            ready.get("protocol_version") != PROTOCOL_VERSION
            or ready.get("status") != "READY"
            or ready.get("run_id") != self._binding.run_id
            or ready.get("epoch") != self._binding.run_epoch
            or not is_sha256(ready.get("finish_nonce"))
            or ready.get("registration_cgroup_ids") != [self._binding.cgroup_id]
            or ready.get("recorder_object_sha256") != self._config.recorder_object_sha256
            or ready.get("lifecycle_object_sha256") != self._config.lifecycle_object_sha256
        ):
            raise WorkspaceError("deployment-observe READY binding mismatch")
        for key in (
            "deployment_scope_sha256",
            "cgroup_scope_capability_sha256",
            "deployment_manifest_sha256",
            "ready_barrier_sha256",
            "ready_validation_sha256",
        ):
            if not is_sha256(ready.get(key)):
                raise WorkspaceError(f"deployment-observe READY {key} is invalid")
        socket_expected = self._config.object_resolution_mode == "KERNEL_OBJECT_EXPANDED_PARTIAL"
        if ready.get("socket_state_enabled") is not socket_expected:
            raise WorkspaceError("deployment-observe READY object mode mismatch")
        self._validate_ready_scope(
            ready,
            "bpf_state_scope_sha256",
            "bpf_state_s0_sha256",
            self._config.bpf_state_scope is not None,
        )
        self._validate_ready_scope(
            ready,
            "filesystem_state_scope_sha256",
            "filesystem_state_s0_sha256",
            self._config.filesystem_state_scope is not None,
        )
        self._validate_ready_scope(
            ready,
            "linux_state_scope_sha256",
            "linux_state_s0_sha256",
            self._config.linux_state_scope is not None,
        )
        self._validate_cgroup_capability(ready)
        deployment = _read_json_artifact(
            self._evidence_directory,
            "deployment-manifest.json",
            MAX_ARTIFACT_JSON_BYTES,
        )
        if not hmac.compare_digest(deployment.digest, ready["deployment_manifest_sha256"]):
            raise WorkspaceError("deployment manifest digest differs from READY")
        self._validate_deployment_manifest(deployment.value, ready)

    @staticmethod
    def _validate_ready_scope(
        ready: dict[str, Any], scope_key: str, snapshot_key: str, enabled: bool
    ) -> None:
        scope = ready.get(scope_key)
        snapshot = ready.get(snapshot_key)
        if enabled:
            if not is_sha256(scope) or not is_sha256(snapshot):
                raise WorkspaceError(f"deployment-observe READY lacks enabled {scope_key} evidence")
        elif scope is not None or snapshot is not None:
            raise WorkspaceError(
                f"deployment-observe READY enabled unconfigured {scope_key} evidence"
            )

    def _validate_cgroup_capability(self, ready: dict[str, Any]) -> None:
        capability = ready.get("cgroup_scope_capability")
        if not isinstance(capability, dict):
            raise WorkspaceError("deployment-observe READY lacks cgroup capability")
        expected = {
            "protocol_version": 1,
            "mode": "CGROUP_V2_ANCESTOR_ROOT",
            "membership_authority": "BPF_CURRENT_TASK_UNDER_CGROUP",
            "root_map_type": "BPF_MAP_TYPE_CGROUP_ARRAY",
            "supported_program_families": [
                "BPF_PROG_TYPE_LSM",
                "BPF_PROG_TYPE_TRACEPOINT",
                "BPF_PROG_TYPE_TRACING_TP_BTF",
            ],
            "maximum_roots": 8,
            "root_cgroup_ids": [self._binding.cgroup_id],
            "event_cgroup_id_semantics": "MATCHED_SCOPE_ROOT",
            "leaf_cgroup_id_retained": False,
            "dynamic_descendants_including_ephemeral": True,
            "readiness_basis": (
                "BOTH_BPF_OBJECTS_LOADED_VERIFIER_ACCEPTED_MAP_ABI_VALIDATED_ROOT_FDS_PINNED"
            ),
        }
        if capability != expected:
            raise WorkspaceError("deployment-observe READY cgroup capability mismatch")
        encoded = json.dumps(capability, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        actual = hashlib.sha256(encoded).hexdigest()
        if not hmac.compare_digest(actual, ready["cgroup_scope_capability_sha256"]):
            raise WorkspaceError("deployment-observe READY cgroup capability digest mismatch")

    def _validate_deployment_manifest(
        self, manifest: dict[str, Any], ready: dict[str, Any]
    ) -> None:
        roots = manifest.get("root_cgroups")
        processes = manifest.get("processes")
        expected_root = {
            "path": self._binding.cgroup_relative_path,
            "cgroup_id": self._binding.cgroup_id,
        }
        if (
            manifest.get("protocol_version") != PROTOCOL_VERSION
            or manifest.get("deployment_kind") != "DIRECT"
            or manifest.get("boot_id") != self._binding.boot_id
            or manifest.get("scope_sha256") != ready["deployment_scope_sha256"]
            or not isinstance(roots, list)
            or len(roots) != 1
            or not isinstance(roots[0], dict)
            or any(roots[0].get(key) != value for key, value in expected_root.items())
            or not isinstance(processes, list)
        ):
            raise WorkspaceError("deployment manifest does not bind the Allox run boundary")
        matching = [
            process
            for process in processes
            if isinstance(process, dict)
            and process.get("pid") == self._binding.root_pid
            and process.get("role") == "DIRECT_ROOT"
            and process.get("cgroup_path") == self._binding.cgroup_relative_path
            and process.get("cgroup_id") == self._binding.cgroup_id
            and process.get("mount_namespace_id") == self._binding.mount_namespace_id
            and process.get("net_namespace_id") == self._binding.network_namespace_id
        ]
        if len(matching) != 1:
            raise WorkspaceError("deployment manifest does not bind the exact Allox root process")

    def status(self) -> dict[str, Any]:
        with self._lock:
            process_pid = self._process.pid if self._process is not None else None
            ready = self._ready
            result = {
                "protocol_version": PROTOCOL_VERSION,
                "status": self._phase,
                "run_id": self._binding.run_id,
                "run_epoch": self._binding.run_epoch,
                "cgroup_id": self._binding.cgroup_id,
                "observer_pid": process_pid,
                "evidence_directory": str(self._evidence_directory),
                "config_sha256": self._config_sha256,
                "profile": self._config.profile,
                "object_resolution_mode": self._config.object_resolution_mode,
                "scopes_enabled": list(self._config.scopes_enabled()),
                "offline_verified": self._final_result is not None,
                "failure": self._failure_reason,
            }
            if ready is not None:
                result["deployment_scope_sha256"] = ready["deployment_scope_sha256"]
                result["registration_cgroup_ids"] = list(ready["registration_cgroup_ids"])
            return result

    def assert_healthy(self) -> None:
        with self._lock:
            process = self._process
            reason = self._failure_reason
            phase = self._phase
            queued = not self._messages.empty()
        if (
            reason is not None
            or phase != "READY"
            or process is None
            or process.poll() is not None
            or queued
        ):
            detail = f": {reason}" if reason else ""
            raise WorkspaceError(f"system-effects Observer is not live and healthy{detail}")

    def agent_started(self) -> None:
        """Protocol v2 has no explicit pre-release transition."""
        self.assert_healthy()

    def finish(self, *, outcome: str, quiescence: object) -> dict[str, Any]:
        if outcome not in _OUTCOMES:
            raise WorkspaceError("invalid system-effects execution outcome")
        barrier = self._validate_quiescence(quiescence)
        with self._lock:
            if self._final_result is not None:
                return dict(self._final_result)
            if self._finish_claimed:
                raise WorkspaceError("system-effects finalization is already in progress")
            if self._phase != "READY" or self._failure_reason is not None:
                raise WorkspaceError("system-effects Observer is not ready for FINISH")
            self._finish_claimed = True
            self._phase = "FINISHING"
            nonce = self._finish_nonce
        assert nonce is not None
        deadline = time.monotonic() + self._config.finalize_timeout
        command = {
            "protocol_version": PROTOCOL_VERSION,
            "command": "FINISH",
            "run_id": self._binding.run_id,
            "finish_nonce": nonce,
        }
        try:
            self._send(command)
            terminal = self._receive(deadline, "FINISHED")
            self._validate_finished(terminal)
            self._require_observer_exit(deadline)
            report, manifest = self._validate_terminal_artifacts(terminal)
            expectation = {
                "expected_config": self._config_document,
                "expected_artifact_manifest_sha256": terminal["artifact_manifest_sha256"],
                "expected_report_sha256": terminal["report_sha256"],
            }
            _write_new_file(self._expectation_path, _canonical_json(expectation) + b"\n")
            verification, verification_bytes = self._run_verifier(deadline)
            self._validate_offline_verification(verification, terminal, report, manifest)
            _write_new_file(self._verification_path, verification_bytes)
            receipt = self._build_receipt(outcome, barrier, terminal, verification)
            _write_new_file(self._receipt_path, _canonical_json(receipt) + b"\n")
            with self._lock:
                self._phase = "FINISHED"
                self._final_result = receipt
            self._shutdown(preserve_phase=True)
            return dict(receipt)
        except BaseException as exc:
            self._set_failed(f"system-effects finalization failed: {type(exc).__name__}")
            self._shutdown(preserve_phase=True)
            raise

    def _validate_quiescence(self, quiescence: object) -> dict[str, Any]:
        if isinstance(quiescence, Mapping):
            barrier = dict(quiescence)
        else:
            as_dict = getattr(quiescence, "as_dict", None)
            if not callable(as_dict):
                raise WorkspaceError("system-effects quiescence must be a Core attestation")
            barrier = as_dict()
        if not isinstance(barrier, dict) or set(barrier) not in (
            {"status", "basis", "cgroup_id"},
            {"status", "basis", "cgroup_path", "cgroup_id"},
        ):
            raise WorkspaceError("invalid system-effects quiescence schema")
        if (
            barrier.get("status") != "QUIESCENT_WITHIN_CGROUP"
            or barrier.get("basis") != "CGROUP_V2_EMPTY_FROZEN_VERIFIED"
            or barrier.get("cgroup_id") != self._binding.cgroup_id
            or (
                "cgroup_path" in barrier
                and barrier["cgroup_path"] != str(self._binding.cgroup_path)
            )
        ):
            raise WorkspaceError("system-effects quiescence does not bind the active cgroup")
        return barrier

    def _send(self, message: Mapping[str, Any]) -> None:
        encoded = _canonical_json(message) + b"\n"
        if len(encoded) > MAX_PROTOCOL_LINE_BYTES:
            raise WorkspaceError("deployment-observe FINISH exceeds the protocol bound")
        process = self._process
        assert process is not None and process.stdin is not None
        try:
            process.stdin.write(encoded)
            process.stdin.flush()
            process.stdin.close()
        except (BrokenPipeError, OSError) as exc:
            raise WorkspaceError("deployment-observe FINISH channel failed") from exc

    def _validate_finished(self, terminal: dict[str, Any]) -> None:
        if set(terminal) != _FINISHED_KEYS:
            raise WorkspaceError("deployment-observe FINISHED schema mismatch")
        if (
            terminal.get("protocol_version") != PROTOCOL_VERSION
            or terminal.get("status") != "FINISHED"
            or terminal.get("run_id") != self._binding.run_id
            or terminal.get("evidence_status") not in _EVIDENCE_STATUSES
            or not is_sha256(terminal.get("report_sha256"))
            or not is_sha256(terminal.get("artifact_manifest_sha256"))
        ):
            raise WorkspaceError("deployment-observe FINISHED binding mismatch")

    def _require_observer_exit(self, deadline: float) -> None:
        process = self._process
        assert process is not None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WorkspaceError("deployment-observe exit timed out")
        try:
            return_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise WorkspaceError("deployment-observe did not exit after FINISHED") from exc
        if return_code != 0:
            detail = b"".join(self._diagnostics)[-4000:].decode("utf-8", errors="replace")
            raise WorkspaceError(f"deployment-observe exited with status {return_code}: {detail}")
        if self._reader is not None:
            self._reader.join(timeout=1)
        if self._errors is not None:
            self._errors.join(timeout=1)

    def _validate_terminal_artifacts(
        self, terminal: dict[str, Any]
    ) -> tuple[_StableArtifact, _StableArtifact]:
        report = _read_json_artifact(
            self._evidence_directory,
            "deployment-observation-report.json",
            MAX_ARTIFACT_JSON_BYTES,
        )
        manifest = _read_json_artifact(
            self._evidence_directory,
            "artifact-manifest.json",
            MAX_ARTIFACT_JSON_BYTES,
        )
        if not hmac.compare_digest(report.digest, terminal["report_sha256"]):
            raise WorkspaceError("deployment observation report digest mismatch")
        if not hmac.compare_digest(manifest.digest, terminal["artifact_manifest_sha256"]):
            raise WorkspaceError("deployment artifact manifest digest mismatch")
        ready = self._ready
        assert ready is not None
        report_deployment = report.value.get("deployment")
        if (
            report.value.get("protocol_version") != PROTOCOL_VERSION
            or report.value.get("run_id") != self._binding.run_id
            or report.value.get("epoch") != self._binding.run_epoch
            or not isinstance(report_deployment, dict)
            or report_deployment.get("scope_sha256") != ready["deployment_scope_sha256"]
            or report.value.get("cgroup_scope_capability_sha256")
            != ready["cgroup_scope_capability_sha256"]
        ):
            raise WorkspaceError("deployment observation report binding mismatch")
        if (
            manifest.value.get("protocol_version") != PROTOCOL_VERSION
            or manifest.value.get("run_id") != self._binding.run_id
            or manifest.value.get("epoch") != self._binding.run_epoch
            or manifest.value.get("deployment_scope_sha256")
            != ready["deployment_scope_sha256"]
            or manifest.value.get("cgroup_scope_capability_sha256")
            != ready["cgroup_scope_capability_sha256"]
        ):
            raise WorkspaceError("deployment observation manifest binding mismatch")
        if (
            report.value.get("recorder_object_sha256") != self._config.recorder_object_sha256
            or report.value.get("lifecycle_object_sha256") != self._config.lifecycle_object_sha256
            or report.value.get("registration_cgroup_ids") != [self._binding.cgroup_id]
            or report.value.get("overall_evidence_status") != terminal["evidence_status"]
            or manifest.value.get("evidence_status") != terminal["evidence_status"]
        ):
            raise WorkspaceError("deployment observation report does not bind configured inputs")
        entries = manifest.value.get("artifacts")
        if not isinstance(entries, list):
            raise WorkspaceError("deployment artifact manifest has no artifact list")
        report_entries = [
            item
            for item in entries
            if isinstance(item, dict)
            and item.get("relative_path") == "deployment-observation-report.json"
        ]
        if (
            len(report_entries) != 1
            or report_entries[0].get("sha256") != terminal["report_sha256"]
            or report_entries[0].get("size_bytes") != report.size
        ):
            raise WorkspaceError("artifact manifest does not commit the observation report")
        return report, manifest

    def _run_verifier(self, deadline: float) -> tuple[dict[str, Any], bytes]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WorkspaceError("system-effects offline verification timed out")
        try:
            process = subprocess.Popen(
                [
                    str(self._config.verifier_path),
                    "--artifacts",
                    str(self._evidence_directory),
                    "--expectation",
                    str(self._expectation_path),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                close_fds=True,
                start_new_session=True,
                env=_fixed_environment(),
            )
        except OSError as exc:
            raise WorkspaceError(f"failed to start deployment-observe-verify: {exc}") from exc
        with self._lock:
            self._verifier_process = process
        assert process.stdout is not None and process.stderr is not None
        stdout = _BoundedCapture(MAX_VERIFIER_STDOUT_BYTES)
        stderr = _BoundedCapture(MAX_VERIFIER_STDERR_BYTES)
        stdout_thread = threading.Thread(target=stdout.drain, args=(process.stdout,), daemon=True)
        stderr_thread = threading.Thread(target=stderr.drain, args=(process.stderr,), daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            _terminate_process_group(process)
            raise WorkspaceError("deployment-observe-verify timed out") from exc
        finally:
            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=5)
            with self._lock:
                self._verifier_process = None
        if stdout_thread.is_alive() or stderr_thread.is_alive():
            raise WorkspaceError("deployment-observe-verify output drain did not terminate")
        if stdout.overflow or stderr.overflow:
            raise WorkspaceError("deployment-observe-verify output exceeded its bound")
        if process.returncode != 0:
            detail = bytes(stderr.data).decode("utf-8", errors="replace")
            raise WorkspaceError(
                f"deployment-observe-verify exited with status {process.returncode}: {detail}"
            )
        try:
            verification = json.loads(stdout.data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkspaceError("deployment-observe-verify returned invalid JSON") from exc
        if not isinstance(verification, dict):
            raise WorkspaceError("deployment-observe-verify result must be a JSON object")
        result_bytes = bytes(stdout.data)
        if not result_bytes.endswith(b"\n"):
            result_bytes += b"\n"
        return verification, result_bytes

    def _validate_offline_verification(
        self,
        verification: dict[str, Any],
        terminal: dict[str, Any],
        report: _StableArtifact,
        manifest: _StableArtifact,
    ) -> None:
        del report, manifest  # Terminal hashes and the verifier bind both exact bytes.
        if set(verification) != _VERIFICATION_KEYS:
            raise WorkspaceError("deployment-observe-verify result schema mismatch")
        ready = self._ready
        assert ready is not None
        if (
            verification.get("protocol_version") != PROTOCOL_VERSION
            or verification.get("verified") is not True
            or verification.get("run_id") != self._binding.run_id
            or verification.get("epoch") != self._binding.run_epoch
            or verification.get("deployment_scope_sha256") != ready["deployment_scope_sha256"]
            or verification.get("evidence_status") != terminal["evidence_status"]
        ):
            raise WorkspaceError("deployment-observe-verify did not verify the exact run")
        for key in (
            "artifact_count",
            "committed_artifact_total_bytes",
            "system_effect_wal_event_count",
            "system_effect_jsonl_event_count",
            "process_lifecycle_wal_event_count",
            "process_lifecycle_jsonl_event_count",
        ):
            if not _valid_nonnegative_integer(verification.get(key)):
                raise WorkspaceError(f"deployment-observe-verify returned invalid {key}")
        if (
            verification["system_effect_wal_event_count"]
            != verification["system_effect_jsonl_event_count"]
            or verification["process_lifecycle_wal_event_count"]
            != verification["process_lifecycle_jsonl_event_count"]
        ):
            raise WorkspaceError("deployment-observe-verify event projections disagree")
        for prefix in ("bpf", "filesystem", "linux"):
            scope_key = f"{prefix}_state_scope_sha256"
            if verification.get(scope_key) != ready.get(scope_key):
                raise WorkspaceError(f"deployment-observe-verify {prefix} scope binding mismatch")

    def _build_receipt(
        self,
        outcome: str,
        quiescence: dict[str, Any],
        terminal: dict[str, Any],
        verification: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "status": "FINISHED",
            "run_id": self._binding.run_id,
            "run_epoch": self._binding.run_epoch,
            "agent_id": self._binding.agent_id,
            "session_id": self._binding.session_id,
            "cgroup_id": self._binding.cgroup_id,
            "outcome": outcome,
            "quiescence": quiescence,
            "observer_evidence_status": terminal["evidence_status"],
            "offline_verification": "VERIFIED",
            "artifact_count": verification["artifact_count"],
            "committed_artifact_total_bytes": verification["committed_artifact_total_bytes"],
            "evidence_directory": str(self._evidence_directory),
            "config_path": str(self._config_path),
            "config_sha256": self._config_sha256,
            "expectation_path": str(self._expectation_path),
            "verification_result_path": str(self._verification_path),
            "report_path": str(self._evidence_directory / "deployment-observation-report.json"),
            "report_sha256": terminal["report_sha256"],
            "artifact_manifest_path": str(self._evidence_directory / "artifact-manifest.json"),
            "artifact_manifest_sha256": terminal["artifact_manifest_sha256"],
        }

    def close_unverified(self) -> None:
        with self._lock:
            if self._expected_shutdown:
                self._retire()
                return
            if self._phase != "FINISHED":
                self._phase = "CLOSED_UNVERIFIED"
            try:
                self._messages.put_nowait(
                    _ProtocolFailure("system-effects observation closed without verification")
                )
            except queue.Full:
                pass
        self._shutdown(preserve_phase=True)

    def _shutdown(self, *, preserve_phase: bool) -> None:
        with self._lock:
            if self._expected_shutdown:
                self._retire()
                return
            self._expected_shutdown = True
            if not preserve_phase:
                self._phase = "CLOSED_UNVERIFIED"
            process = self._process
            verifier = self._verifier_process
        if verifier is not None and verifier.poll() is None:
            _terminate_process_group(verifier)
        if process is not None:
            if process.stdin is not None and not process.stdin.closed:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            if process.poll() is None:
                _terminate_process_group(process)
            if self._reader is not None and self._reader is not threading.current_thread():
                self._reader.join(timeout=5)
            if self._errors is not None and self._errors is not threading.current_thread():
                self._errors.join(timeout=5)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        self._retire()

    def _retire(self) -> None:
        with self._lock:
            if self._retired:
                return
            self._retired = True
        self._on_close(self)


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _open_private_directory(path: Path) -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise WorkspaceError(f"failed to open private audit directory: {exc}") from exc
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
    ):
        os.close(descriptor)
        raise WorkspaceError("audit directory is not private and daemon-owned")
    return descriptor


def _write_new_file(path: Path, encoded: bytes) -> None:
    directory_fd = _open_private_directory(path.parent)
    descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            descriptor = os.open(path.name, flags, 0o600, dir_fd=directory_fd)
        except OSError as exc:
            raise WorkspaceError(
                f"refusing to replace system-effects file {path.name}: {exc}"
            ) from exc
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise WorkspaceError(f"short write while creating {path.name}")
            view = view[written:]
        os.fsync(descriptor)
        os.fsync(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory_fd)


def _read_json_artifact(directory: Path, name: str, limit: int) -> _StableArtifact:
    if PurePosixPath(name).name != name or name in {"", ".", ".."}:
        raise WorkspaceError("invalid system-effects artifact basename")
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        directory_fd = os.open(directory, directory_flags)
    except OSError as exc:
        raise WorkspaceError(f"system-effects evidence directory is unavailable: {exc}") from exc
    try:
        directory_metadata = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(directory_metadata.st_mode)
            or directory_metadata.st_uid != os.geteuid()
            or directory_metadata.st_mode & 0o077
        ):
            raise WorkspaceError("system-effects evidence directory is not private")
        try:
            descriptor = os.open(name, file_flags, dir_fd=directory_fd)
        except OSError as exc:
            raise WorkspaceError(f"system-effects artifact {name} is unavailable: {exc}") from exc
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or before.st_nlink != 1
                or before.st_size < 0
                or before.st_size > limit
            ):
                raise WorkspaceError(
                    f"system-effects artifact {name} is not a bounded daemon-owned file"
                )
            chunks: list[bytes] = []
            digest = hashlib.sha256()
            consumed = 0
            while True:
                chunk = os.read(descriptor, min(64 * 1024, limit + 1 - consumed))
                if not chunk:
                    break
                consumed += len(chunk)
                if consumed > limit:
                    raise WorkspaceError(f"system-effects artifact {name} exceeds its bound")
                chunks.append(chunk)
                digest.update(chunk)
            after = os.fstat(descriptor)
            stable_fields = (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_uid",
                "st_nlink",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
            if consumed != before.st_size or any(
                getattr(before, field) != getattr(after, field) for field in stable_fields
            ):
                raise WorkspaceError(f"system-effects artifact {name} changed while read")
        finally:
            os.close(descriptor)
    finally:
        os.close(directory_fd)
    try:
        value = json.loads(b"".join(chunks))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError(f"system-effects artifact {name} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise WorkspaceError(f"system-effects artifact {name} must be a JSON object")
    return _StableArtifact(value=value, digest=digest.hexdigest(), size=consumed)
