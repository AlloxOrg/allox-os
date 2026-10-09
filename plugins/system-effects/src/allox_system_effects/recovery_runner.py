"""Fail-closed Allox adapter for ``deployment-observe`` protocol v3 recovery."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import queue
import stat
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from allox.workspace.store import WorkspaceError
from allox_system_effects.config import MAX_CONFIG_BYTES, SystemEffectsConfig, is_sha256
from allox_system_effects.runner import (
    MAX_ARTIFACT_JSON_BYTES,
    MAX_PROTOCOL_LINE_BYTES,
    MAX_VERIFIER_STDERR_BYTES,
    MAX_VERIFIER_STDOUT_BYTES,
    RunBinding,
    SystemEffectsRun,
    _BoundedCapture,
    _canonical_json,
    _fixed_environment,
    _json_clone,
    _ProtocolFailure,
    _read_json_artifact,
    _terminate_process_group,
    _valid_nonnegative_integer,
    _write_new_file,
)

RECOVERY_PROTOCOL_VERSION = 3
_LEGACY_READY_PROTOCOL_VERSION = 2
_OUTCOMES = frozenset({"completed", "stopped", "launch_failed"})
_VERDICTS = frozenset({"CONSISTENT_WITHIN_SCOPE", "INCONSISTENT_WITHIN_SCOPE", "INCONCLUSIVE"})

_READY_KEYS = frozenset(
    {
        "protocol_version",
        "status",
        "run_id",
        "epoch",
        "checkpoint_id",
        "recovery_id",
        "command_seq",
        "nonce",
        "snapshot_a_sha256",
    }
)
_AGENT_STARTED_KEYS = frozenset(
    {
        "protocol_version",
        "status",
        "run_id",
        "epoch",
        "checkpoint_id",
        "recovery_id",
        "command_seq",
        "nonce",
        "agent_started_sha256",
    }
)
_SNAPSHOT_B_KEYS = frozenset(
    {
        "protocol_version",
        "status",
        "run_id",
        "epoch",
        "checkpoint_id",
        "recovery_id",
        "command_seq",
        "nonce",
        "pre_restore_sha256",
        "snapshot_b_sha256",
    }
)
_RESTORE_COMPLETE_KEYS = frozenset(
    {
        "protocol_version",
        "status",
        "run_id",
        "epoch",
        "checkpoint_id",
        "recovery_id",
        "command_seq",
        "nonce",
        "recovery_sha256",
    }
)
_SNAPSHOT_C_KEYS = frozenset(
    {
        "protocol_version",
        "status",
        "run_id",
        "epoch",
        "checkpoint_id",
        "recovery_id",
        "command_seq",
        "nonce",
        "snapshot_c_sha256",
    }
)
_FINISHED_KEYS = frozenset(
    {
        "protocol_version",
        "status",
        "run_id",
        "epoch",
        "checkpoint_id",
        "recovery_id",
        "command_seq",
        "nonce",
        "verdict_sha256",
        "artifact_manifest_sha256",
    }
)
_SNAPSHOT_KEYS = frozenset(
    {
        "schema_version",
        "protocol_version",
        "phase",
        "run_id",
        "epoch",
        "checkpoint_id",
        "recovery_id",
        "snapshot_id",
        "capture_started_unix_ns",
        "capture_completed_unix_ns",
        "boot_id",
        "deployment_scope_sha256",
        "cgroup_scope_capability",
        "cgroup_scope_capability_sha256",
        "root_cgroups",
        "mount_namespace_ids",
        "pid_namespace_ids",
        "net_namespace_ids",
        "uts_namespace_ids",
        "ipc_namespace_ids",
        "barrier_sha256",
        "identity_assessment_sha256",
        "bpf",
        "filesystem",
        "linux",
    }
)
_MANIFEST_KEYS = frozenset(
    {
        "protocol_version",
        "run_id",
        "epoch",
        "checkpoint_id",
        "recovery_id",
        "deployment_scope_sha256",
        "recovery_config_sha256",
        "recovery_verdict_sha256",
        "snapshot_a_sha256",
        "snapshot_b_sha256",
        "snapshot_c_sha256",
        "committed_artifact_total_bytes",
        "artifacts",
    }
)
_VERDICT_KEYS = frozenset(
    {
        "schema_version",
        "protocol_version",
        "run_id",
        "epoch",
        "checkpoint_id",
        "recovery_id",
        "snapshot_a_id",
        "snapshot_b_id",
        "snapshot_c_id",
        "snapshot_a_sha256",
        "snapshot_b_sha256",
        "snapshot_c_sha256",
        "agent_delta",
        "recovery_delta",
        "residual_delta",
        "classifications",
        "raw_endpoint_proof_complete",
        "state_complete",
        "external_incompleteness_reasons",
        "incompleteness_reasons",
        "verdict",
        "limitations",
    }
)
_VERIFICATION_KEYS = frozenset(
    {
        "schema_version",
        "protocol_version",
        "verified",
        "run_id",
        "epoch",
        "checkpoint_id",
        "recovery_id",
        "artifact_count",
        "committed_artifact_total_bytes",
        "verdict",
        "endpoint_replay_verified",
        "recorder_wal_replay_verified",
    }
)


def _wire_json(value: object) -> bytes:
    """Match serde_json's compact struct encoding, including insertion order."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _strict_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise WorkspaceError(f"system-effects recovery {label} must be a boolean")
    return value


def _identifier(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or not all(
            character.isascii() and (character.isalnum() or character in "-_.:")
            for character in value
        )
    ):
        raise WorkspaceError(f"system-effects recovery {label} has an invalid identity")
    return value


class RecoverySystemEffectsRun(SystemEffectsRun):
    """Own one protocol-v3 A/B/C transaction and its deferred Allox rollback."""

    def __init__(
        self,
        config: SystemEffectsConfig,
        context: object,
        on_close: Callable[[RecoverySystemEffectsRun], None],
        store: object,
    ) -> None:
        if not config.recovery_enabled:
            raise WorkspaceError("system-effects recovery is not configured")
        self._config = config
        self._binding = RunBinding.from_context(context)
        self._on_close = on_close
        self._store = store
        self._recovery = self._bind_recovery(context)
        self._store_root = self._bind_store_root(store)
        self._evidence_directory = self._binding.audit_directory / "system-effects"
        self._config_path = self._binding.audit_directory / "system-effects-config.json"
        self._expectation_path = (
            self._binding.audit_directory / "system-effects-recovery-verification-expectation.json"
        )
        self._verification_path = (
            self._binding.audit_directory / "system-effects-recovery-verification.json"
        )
        self._receipt_path = self._binding.audit_directory / "system-effects-recovery-receipt.json"
        self._messages: queue.Queue[dict[str, Any] | _ProtocolFailure] = queue.Queue(maxsize=8)
        self._diagnostics: deque[bytes] = deque(maxlen=64)
        self._lock = threading.RLock()
        self._phase = "STARTING"
        self._failure_reason: str | None = None
        self._failure_reported = False
        self._expected_shutdown = False
        self._terminal_seen = False
        self._agent_start_claimed = False
        self._finish_claimed = False
        self._final_result: dict[str, Any] | None = None
        self._ready: dict[str, Any] | None = None
        self._legacy_ready: dict[str, Any] | None = None
        self._agent_started_message: dict[str, Any] | None = None
        self._snapshots: dict[str, Any] = {}
        self._recovery_helper_succeeded: bool | None = None
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
            "expected_recovery_verdict_sha256": "0" * 64,
        }
        if len(_canonical_json(expectation_probe) + b"\n") > MAX_CONFIG_BYTES:
            raise WorkspaceError(
                "system-effects recovery verifier expectation exceeds the 1 MiB bound"
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
            self._set_failed("failed to start deployment-observe recovery")
            self._retire()
            raise WorkspaceError(f"failed to start deployment-observe recovery: {exc}") from exc

        self._reader = threading.Thread(
            target=self._read_stdout,
            name=f"allox-effects-recovery-stdout-{self._binding.run_id}",
            daemon=True,
        )
        self._errors = threading.Thread(
            target=self._read_stderr,
            name=f"allox-effects-recovery-stderr-{self._binding.run_id}",
            daemon=True,
        )
        self._reader.start()
        self._errors.start()
        try:
            ready = self._receive_status(time.monotonic() + config.ready_timeout, "READY")
            self._validate_recovery_ready(ready)
            with self._lock:
                if self._failure_reason is not None:
                    raise WorkspaceError(self._failure_reason)
                self._ready = ready
                self._phase = "READY"
            self.assert_healthy()
        except BaseException:
            self.close_unverified()
            raise

    def _bind_recovery(self, context: object) -> dict[str, Any]:
        if not hasattr(context, "recovery") or context.recovery is None:
            raise WorkspaceError("system-effects recovery context lacks a recovery plan")
        plan = context.recovery
        required = (
            "checkpoint_id",
            "recovery_id",
            "scrub_runtime",
            "require_consistent",
            "baseline_created",
        )
        missing = [name for name in required if not hasattr(plan, name)]
        if missing:
            raise WorkspaceError("system-effects recovery plan lacks " + ", ".join(missing))
        return {
            "checkpoint_id": _identifier(plan.checkpoint_id, "checkpoint_id"),
            "recovery_id": _identifier(plan.recovery_id, "recovery_id"),
            "scrub_runtime": _strict_bool(plan.scrub_runtime, "scrub_runtime"),
            "require_consistent": _strict_bool(plan.require_consistent, "require_consistent"),
            "baseline_created": _strict_bool(plan.baseline_created, "baseline_created"),
        }

    @staticmethod
    def _bind_store_root(store: object) -> Path:
        if not hasattr(store, "root"):
            raise WorkspaceError("system-effects recovery requires a WorkspaceStore root")
        root = Path(store.root)
        try:
            resolved = root.resolve(strict=True)
            metadata = root.lstat()
        except OSError as exc:
            raise WorkspaceError(f"system-effects store root is unavailable: {exc}") from exc
        if not root.is_absolute() or resolved != root or not stat.S_ISDIR(metadata.st_mode):
            raise WorkspaceError("system-effects store root must be a canonical existing directory")
        return root

    def _trusted_store_directory(self, path: object, label: str) -> Path:
        candidate = Path(path)
        try:
            resolved = candidate.resolve(strict=True)
            metadata = candidate.lstat()
            resolved.relative_to(self._store_root)
        except (OSError, ValueError) as exc:
            raise WorkspaceError(
                f"system-effects {label} is not a trusted store directory: {exc}"
            ) from exc
        if resolved != candidate or not stat.S_ISDIR(metadata.st_mode):
            raise WorkspaceError(f"system-effects {label} must be a canonical store directory")
        return candidate

    def _mapped_filesystem_scope(self) -> dict[str, Any] | None:
        scope = _json_clone(self._config.filesystem_state_scope)
        if scope is None:
            return None
        if not isinstance(scope, dict):
            raise WorkspaceError(
                "system-effects recovery requires an explicit filesystem state scope"
            )
        roots = scope.get("roots")
        if not isinstance(roots, list) or not roots:
            raise WorkspaceError("system-effects filesystem scope has no roots")
        try:
            workspace = self._trusted_store_directory(
                self._store.current(self._binding.agent_id, self._binding.session_id),
                "Session workspace",
            )
            shared = self._trusted_store_directory(
                self._store.agent_shared(self._binding.agent_id), "Agent shared workspace"
            )
        except AttributeError as exc:
            raise WorkspaceError(
                "system-effects recovery store lacks workspace path methods"
            ) from exc
        mapping = {
            b"/workspace": os.fsencode(workspace),
            b"/agent/shared": os.fsencode(shared),
        }
        rewritten: list[dict[str, str]] = []
        for root in roots:
            if not isinstance(root, dict) or set(root) != {"path_bytes_hex"}:
                raise WorkspaceError("system-effects filesystem root schema is invalid")
            encoded = root.get("path_bytes_hex")
            if not isinstance(encoded, str) or len(encoded) % 2 or encoded.lower() != encoded:
                raise WorkspaceError("system-effects filesystem root encoding is invalid")
            try:
                path_bytes = bytes.fromhex(encoded)
            except ValueError as exc:
                raise WorkspaceError("system-effects filesystem root encoding is invalid") from exc
            if not path_bytes.startswith(b"/") or b"\0" in path_bytes:
                raise WorkspaceError("system-effects filesystem root must be absolute")
            rewritten.append({"path_bytes_hex": mapping.get(path_bytes, path_bytes).hex()})
        rewritten.sort(key=lambda value: value["path_bytes_hex"])
        if len({value["path_bytes_hex"] for value in rewritten}) != len(rewritten):
            raise WorkspaceError("system-effects mapped filesystem roots are not unique")
        scope["roots"] = rewritten
        return scope

    def _build_observer_config(self) -> dict[str, Any]:
        assert self._config.recovery_helper_path is not None
        assert self._config.recovery_helper_sha256 is not None
        scrub = "true" if self._recovery["scrub_runtime"] else "false"
        recovery = {
            "checkpoint_id": self._recovery["checkpoint_id"],
            "recovery_id": self._recovery["recovery_id"],
            "executable_path": str(self._config.recovery_helper_path),
            "expected_sha256": self._config.recovery_helper_sha256,
            "argv": [
                "--store-root",
                str(self._store_root),
                "--agent-id",
                self._binding.agent_id,
                "--session-id",
                self._binding.session_id,
                "--checkpoint-id",
                self._recovery["checkpoint_id"],
                "--recovery-id",
                self._recovery["recovery_id"],
                "--scrub-runtime",
                scrub,
            ],
            "timeout_ms": self._config.recovery_timeout_ms,
            "max_output_bytes": self._config.recovery_max_output_bytes,
        }
        filesystem_scope = self._mapped_filesystem_scope()
        return {
            "protocol_version": RECOVERY_PROTOCOL_VERSION,
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
            "filesystem_state_scope": filesystem_scope,
            "filesystem_namespace_mode": (
                "OBSERVER" if filesystem_scope is not None else "DEPLOYMENT"
            ),
            "linux_state_scope": self._per_run_linux_state_scope(),
            "recovery": recovery,
            "poll_interval_ms": self._config.poll_interval_ms,
            "drain_timeout_ms": self._config.drain_timeout_ms,
        }

    def _read_stdout(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        while True:
            try:
                line = process.stdout.readline(MAX_PROTOCOL_LINE_BYTES + 1)
            except OSError:
                self._publish_failure("deployment-observe recovery stdout read failed")
                return
            if not line:
                with self._lock:
                    expected = self._expected_shutdown or self._terminal_seen
                if not expected:
                    self._publish_failure("deployment-observe recovery exited unexpectedly")
                return
            if len(line) > MAX_PROTOCOL_LINE_BYTES or not line.endswith(b"\n"):
                self._publish_failure(
                    "deployment-observe recovery response exceeded protocol bound"
                )
                return
            try:
                message = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._publish_failure("deployment-observe recovery returned invalid JSON")
                return
            if not isinstance(message, dict):
                self._publish_failure("deployment-observe recovery response must be a JSON object")
                return
            if message.get("status") == "FINISHED":
                with self._lock:
                    self._terminal_seen = True
            try:
                self._messages.put_nowait(message)
            except queue.Full:
                self._publish_failure("deployment-observe recovery sent unsolicited messages")
                return

    def _receive_status(self, deadline: float, expected: str) -> dict[str, Any]:
        message = self._receive(deadline, expected)
        if message.get("status") != expected:
            raise WorkspaceError(
                f"deployment-observe recovery sent {message.get('status')!r}; expected {expected}"
            )
        return message

    def _validate_common(
        self,
        message: dict[str, Any],
        keys: frozenset[str],
        status: str,
        sequence: int,
        nonce: str | None = None,
    ) -> None:
        if set(message) != keys:
            raise WorkspaceError(f"deployment-observe recovery {status} schema mismatch")
        if (
            message.get("protocol_version") != RECOVERY_PROTOCOL_VERSION
            or message.get("status") != status
            or message.get("run_id") != self._binding.run_id
            or message.get("epoch") != self._binding.run_epoch
            or message.get("checkpoint_id") != self._recovery["checkpoint_id"]
            or message.get("recovery_id") != self._recovery["recovery_id"]
            or message.get("command_seq") != sequence
            or not is_sha256(message.get("nonce"))
            or (nonce is not None and not hmac.compare_digest(message["nonce"], nonce))
        ):
            raise WorkspaceError(f"deployment-observe recovery {status} binding mismatch")

    def _validate_recovery_ready(self, ready: dict[str, Any]) -> None:
        self._validate_common(ready, _READY_KEYS, "READY", 1)
        if not is_sha256(ready.get("snapshot_a_sha256")):
            raise WorkspaceError("deployment-observe recovery READY A digest is invalid")
        committed_ready = _read_json_artifact(
            self._evidence_directory,
            "recovery-ready-message.json",
            MAX_ARTIFACT_JSON_BYTES,
        )
        if committed_ready.value != ready:
            raise WorkspaceError(
                "deployment-observe recovery READY differs from committed evidence"
            )
        legacy = _read_json_artifact(
            self._evidence_directory, "ready-message.json", MAX_ARTIFACT_JSON_BYTES
        )
        self._validate_ready(legacy.value)
        self._legacy_ready = legacy.value
        snapshot = _read_json_artifact(
            self._evidence_directory, "snapshot-a.json", MAX_ARTIFACT_JSON_BYTES
        )
        if not hmac.compare_digest(snapshot.digest, ready["snapshot_a_sha256"]):
            raise WorkspaceError("deployment-observe recovery snapshot A digest mismatch")
        self._validate_snapshot(snapshot.value, "A", legacy.value)
        self._validate_snapshot_barrier(
            snapshot.value, "ready-barrier.json", "ready-validation.json"
        )
        self._snapshots["A"] = snapshot

    def _validate_snapshot(
        self, snapshot: dict[str, Any], phase: str, legacy: dict[str, Any]
    ) -> None:
        if set(snapshot) != _SNAPSHOT_KEYS:
            raise WorkspaceError(f"deployment-observe recovery snapshot {phase} schema mismatch")
        roots = snapshot.get("root_cgroups")
        if (
            snapshot.get("schema_version") != 1
            or snapshot.get("protocol_version") != RECOVERY_PROTOCOL_VERSION
            or snapshot.get("phase") != phase
            or snapshot.get("run_id") != self._binding.run_id
            or snapshot.get("epoch") != self._binding.run_epoch
            or snapshot.get("checkpoint_id") != self._recovery["checkpoint_id"]
            or snapshot.get("recovery_id") != self._recovery["recovery_id"]
            or not isinstance(snapshot.get("snapshot_id"), str)
            or not snapshot["snapshot_id"]
            or not _valid_nonnegative_integer(snapshot.get("capture_started_unix_ns"))
            or not _valid_nonnegative_integer(snapshot.get("capture_completed_unix_ns"))
            or snapshot["capture_completed_unix_ns"] < snapshot["capture_started_unix_ns"]
            or snapshot.get("boot_id") != self._binding.boot_id
            or snapshot.get("deployment_scope_sha256") != legacy["deployment_scope_sha256"]
            or snapshot.get("cgroup_scope_capability") != legacy["cgroup_scope_capability"]
            or snapshot.get("cgroup_scope_capability_sha256")
            != legacy["cgroup_scope_capability_sha256"]
            or roots
            != [
                {
                    "cgroup_id": self._binding.cgroup_id,
                    "path": self._binding.cgroup_relative_path,
                }
            ]
        ):
            raise WorkspaceError(f"deployment-observe recovery snapshot {phase} binding mismatch")
        namespace_bindings = (
            ("mount_namespace_ids", self._binding.mount_namespace_id),
            ("net_namespace_ids", self._binding.network_namespace_id),
            ("pid_namespace_ids", None),
            ("uts_namespace_ids", None),
            ("ipc_namespace_ids", None),
        )
        for key, required_identity in namespace_bindings:
            values = snapshot.get(key)
            if (
                not isinstance(values, list)
                or not values
                or any(type(value) is not int or value <= 0 for value in values)
                or values != sorted(set(values))
                or (
                    required_identity is not None
                    and required_identity not in values
                )
            ):
                raise WorkspaceError(
                    f"deployment-observe recovery snapshot {phase} {key} is invalid"
                )
        for key in ("barrier_sha256", "identity_assessment_sha256"):
            if not is_sha256(snapshot.get(key)):
                raise WorkspaceError(
                    f"deployment-observe recovery snapshot {phase} {key} is invalid"
                )
        for key, enabled in (
            ("bpf", self._config.bpf_state_scope is not None),
            ("filesystem", self._config.filesystem_state_scope is not None),
            ("linux", self._config.linux_state_scope is not None),
        ):
            if (snapshot.get(key) is not None) is not enabled:
                raise WorkspaceError(
                    f"deployment-observe recovery snapshot {phase} {key} scope mismatch"
                )

    def status(self) -> dict[str, Any]:
        with self._lock:
            process_pid = self._process.pid if self._process is not None else None
            result = {
                "protocol_version": RECOVERY_PROTOCOL_VERSION,
                "status": self._phase,
                "run_id": self._binding.run_id,
                "run_epoch": self._binding.run_epoch,
                "checkpoint_id": self._recovery["checkpoint_id"],
                "recovery_id": self._recovery["recovery_id"],
                "cgroup_id": self._binding.cgroup_id,
                "observer_pid": process_pid,
                "evidence_directory": str(self._evidence_directory),
                "config_sha256": self._config_sha256,
                "profile": self._config.profile,
                "object_resolution_mode": self._config.object_resolution_mode,
                "scopes_enabled": list(self._config.scopes_enabled()),
                "filesystem_namespace_mode": self._config_document[
                    "filesystem_namespace_mode"
                ],
                "offline_verified": self._final_result is not None,
                "failure": self._failure_reason,
            }
            if self._final_result is not None:
                result["recovery_verdict"] = self._final_result["recovery_verdict"]
                result["release_allowed"] = self._final_result["release_allowed"]
            return result

    def assert_healthy(self) -> None:
        with self._lock:
            process = self._process
            reason = self._failure_reason
            phase = self._phase
            queued = not self._messages.empty()
        if (
            reason is not None
            or phase not in {"READY", "AGENT_RUNNING"}
            or process is None
            or process.poll() is not None
            or queued
        ):
            detail = f": {reason}" if reason else ""
            raise WorkspaceError(
                f"system-effects recovery Observer is not live and healthy{detail}"
            )

    def _command(self, kind: str, sequence: int, nonce: str) -> dict[str, Any]:
        return {
            "command": kind,
            "protocol_version": RECOVERY_PROTOCOL_VERSION,
            "run_id": self._binding.run_id,
            "epoch": self._binding.run_epoch,
            "checkpoint_id": self._recovery["checkpoint_id"],
            "recovery_id": self._recovery["recovery_id"],
            "command_seq": sequence,
            "nonce": nonce,
        }

    def _send_recovery_command(self, command: Mapping[str, Any], *, close: bool) -> None:
        encoded = _wire_json(command) + b"\n"
        if len(encoded) > MAX_PROTOCOL_LINE_BYTES:
            raise WorkspaceError("deployment-observe recovery command exceeds protocol bound")
        process = self._process
        assert process is not None and process.stdin is not None
        try:
            process.stdin.write(encoded)
            process.stdin.flush()
            if close:
                process.stdin.close()
        except (BrokenPipeError, OSError) as exc:
            raise WorkspaceError("deployment-observe recovery control channel failed") from exc

    def agent_started(self) -> None:
        with self._lock:
            if self._phase == "AGENT_RUNNING" and self._agent_started_message is not None:
                return
            if self._phase != "READY" or self._failure_reason is not None:
                raise WorkspaceError(
                    "system-effects recovery Observer is not ready for AGENT_STARTED"
                )
            if self._agent_start_claimed:
                raise WorkspaceError("system-effects recovery AGENT_STARTED is already in progress")
            self._agent_start_claimed = True
            self._phase = "AGENT_STARTING"
            ready = self._ready
        assert ready is not None
        command = self._command("AGENT_STARTED", 1, ready["nonce"])
        deadline = time.monotonic() + self._config.ready_timeout
        try:
            self._send_recovery_command(command, close=False)
            response = self._receive_status(deadline, "AGENT_STARTED")
            self._validate_common(response, _AGENT_STARTED_KEYS, "AGENT_STARTED", 2)
            if not is_sha256(response.get("agent_started_sha256")):
                raise WorkspaceError("deployment-observe recovery AGENT_STARTED digest is invalid")
            expected = hashlib.sha256(_wire_json(command)).hexdigest()
            if not hmac.compare_digest(response["agent_started_sha256"], expected):
                raise WorkspaceError(
                    "deployment-observe recovery AGENT_STARTED command digest mismatch"
                )
            committed = _read_json_artifact(
                self._evidence_directory, "agent-started.json", MAX_ARTIFACT_JSON_BYTES
            )
            if committed.value != response:
                raise WorkspaceError(
                    "deployment-observe recovery AGENT_STARTED differs from evidence"
                )
            with self._lock:
                if self._failure_reason is not None:
                    raise WorkspaceError(self._failure_reason)
                self._agent_started_message = response
                self._phase = "AGENT_RUNNING"
        except BaseException as exc:
            self._set_failed(f"system-effects recovery AGENT_STARTED failed: {type(exc).__name__}")
            self._shutdown(preserve_phase=True)
            raise

    def finish(self, *, outcome: str, quiescence: object) -> dict[str, Any]:
        if outcome not in _OUTCOMES:
            raise WorkspaceError("invalid system-effects recovery execution outcome")
        barrier = self._validate_quiescence(quiescence)
        with self._lock:
            if self._final_result is not None:
                return dict(self._final_result)
            if self._finish_claimed:
                raise WorkspaceError("system-effects recovery finalization is already in progress")
            if self._phase != "AGENT_RUNNING" or self._failure_reason is not None:
                raise WorkspaceError(
                    "system-effects recovery Observer is not ready for PRE_RESTORE"
                )
            self._finish_claimed = True
            self._phase = "RECOVERING"
            started = self._agent_started_message
        assert started is not None
        deadline = (
            time.monotonic()
            + self._config.finalize_timeout
            + self._config.recovery_timeout_ms / 1000.0
        )
        try:
            pre_restore = self._command("PRE_RESTORE", 2, started["nonce"])
            self._send_recovery_command(pre_restore, close=False)

            snapshot_b_message = self._receive_status(deadline, "SNAPSHOT_B")
            self._validate_common(
                snapshot_b_message,
                _SNAPSHOT_B_KEYS,
                "SNAPSHOT_B",
                2,
                started["nonce"],
            )
            for key in ("pre_restore_sha256", "snapshot_b_sha256"):
                if not is_sha256(snapshot_b_message.get(key)):
                    raise WorkspaceError(f"deployment-observe recovery SNAPSHOT_B {key} is invalid")
            pre_validation = _read_json_artifact(
                self._evidence_directory,
                "pre-restore-validation.json",
                MAX_ARTIFACT_JSON_BYTES,
            )
            if not hmac.compare_digest(
                pre_validation.digest, snapshot_b_message["pre_restore_sha256"]
            ):
                raise WorkspaceError(
                    "deployment-observe recovery PRE_RESTORE validation digest mismatch"
                )
            snapshot_b = _read_json_artifact(
                self._evidence_directory, "snapshot-b.json", MAX_ARTIFACT_JSON_BYTES
            )
            if not hmac.compare_digest(snapshot_b.digest, snapshot_b_message["snapshot_b_sha256"]):
                raise WorkspaceError("deployment-observe recovery snapshot B digest mismatch")
            assert self._legacy_ready is not None
            self._validate_snapshot(snapshot_b.value, "B", self._legacy_ready)
            self._validate_snapshot_barrier(
                snapshot_b.value,
                "pre-restore-barrier.json",
                "pre-restore-validation.json",
            )
            self._snapshots["B"] = snapshot_b

            restored = self._receive_status(deadline, "RESTORE_COMPLETE")
            self._validate_common(
                restored,
                _RESTORE_COMPLETE_KEYS,
                "RESTORE_COMPLETE",
                2,
                started["nonce"],
            )
            if not is_sha256(restored.get("recovery_sha256")):
                raise WorkspaceError(
                    "deployment-observe recovery RESTORE_COMPLETE digest is invalid"
                )
            recovery_evidence = _read_json_artifact(
                self._evidence_directory,
                "recovery.json",
                # stdout and stderr each carry a max_output_bytes prefix as
                # hexadecimal (2x expansion). Keep the fixed artifact bound
                # as headroom for identities, barriers, argv, and metadata.
                MAX_ARTIFACT_JSON_BYTES
                + 4 * self._config.recovery_max_output_bytes,
            )
            if not hmac.compare_digest(recovery_evidence.digest, restored["recovery_sha256"]):
                raise WorkspaceError("deployment-observe recovery evidence digest mismatch")
            self._validate_recovery_evidence(recovery_evidence.value)
            self._recovery_helper_succeeded = recovery_evidence.value["success"]

            snapshot_c_message = self._receive_status(deadline, "SNAPSHOT_C")
            self._validate_common(snapshot_c_message, _SNAPSHOT_C_KEYS, "SNAPSHOT_C", 3)
            if not is_sha256(snapshot_c_message.get("snapshot_c_sha256")):
                raise WorkspaceError("deployment-observe recovery SNAPSHOT_C digest is invalid")
            snapshot_c = _read_json_artifact(
                self._evidence_directory, "snapshot-c.json", MAX_ARTIFACT_JSON_BYTES
            )
            if not hmac.compare_digest(snapshot_c.digest, snapshot_c_message["snapshot_c_sha256"]):
                raise WorkspaceError("deployment-observe recovery snapshot C digest mismatch")
            self._validate_snapshot(snapshot_c.value, "C", self._legacy_ready)
            self._validate_snapshot_barrier(
                snapshot_c.value,
                "post-restore-barrier.json",
                "post-restore-validation.json",
            )
            self._snapshots["C"] = snapshot_c

            finish = self._command("FINISH", 3, snapshot_c_message["nonce"])
            self._send_recovery_command(finish, close=True)
            terminal = self._receive_status(deadline, "FINISHED")
            self._validate_common(
                terminal,
                _FINISHED_KEYS,
                "FINISHED",
                3,
                snapshot_c_message["nonce"],
            )
            for key in ("verdict_sha256", "artifact_manifest_sha256"):
                if not is_sha256(terminal.get(key)):
                    raise WorkspaceError(f"deployment-observe recovery FINISHED {key} is invalid")
            with self._lock:
                self._phase = "TERMINAL_RECEIVED"
            self._require_observer_exit(deadline)
            verdict, manifest = self._validate_terminal_artifacts(terminal)
            expectation = {
                "expected_config": self._config_document,
                "expected_artifact_manifest_sha256": terminal["artifact_manifest_sha256"],
                "expected_recovery_verdict_sha256": terminal["verdict_sha256"],
            }
            _write_new_file(self._expectation_path, _canonical_json(expectation) + b"\n")
            verification, verification_bytes = self._run_recovery_verifier(deadline)
            self._validate_recovery_verification(
                verification, terminal, verdict.value, manifest.value
            )
            _write_new_file(self._verification_path, verification_bytes)

            if self._recovery_helper_succeeded:
                cleanup = self._store.complete_deferred_rollback(
                    self._binding.agent_id,
                    self._binding.session_id,
                    self._recovery["recovery_id"],
                )
                if (
                    not isinstance(cleanup, Mapping)
                    or cleanup.get("cleanup_completed") is not True
                ):
                    raise WorkspaceError(
                        "system-effects deferred rollback cleanup did not complete"
                    )
                cleanup_result = dict(cleanup)
            else:
                # A failed helper can still produce a complete, independently
                # verified INCONCLUSIVE transcript. Never authorize deletion
                # of a deferred pre-rollback tree in that case.
                cleanup_result = {
                    "cleanup_status": "NOT_APPLICABLE",
                    "cleanup_completed": False,
                    # The helper may have failed before or after committing a
                    # deferred swap; do not invent transaction state here.
                    "cleanup_pending": None,
                    "reason": "RECOVERY_HELPER_FAILED",
                }
            receipt = self._build_receipt(
                outcome, barrier, terminal, verification, cleanup_result
            )
            _write_new_file(self._receipt_path, _canonical_json(receipt) + b"\n")
            with self._lock:
                self._phase = "FINISHED"
                self._final_result = receipt
            self._shutdown(preserve_phase=True)
            return dict(receipt)
        except BaseException as exc:
            self._set_failed(f"system-effects recovery finalization failed: {type(exc).__name__}")
            self._shutdown(preserve_phase=True)
            raise

    def _validate_snapshot_barrier(
        self, snapshot: dict[str, Any], barrier_name: str, validation_name: str
    ) -> None:
        barrier = _read_json_artifact(
            self._evidence_directory, barrier_name, MAX_ARTIFACT_JSON_BYTES
        )
        validation = _read_json_artifact(
            self._evidence_directory, validation_name, MAX_ARTIFACT_JSON_BYTES
        )
        if not hmac.compare_digest(
            snapshot["barrier_sha256"], barrier.digest
        ) or not hmac.compare_digest(snapshot["identity_assessment_sha256"], validation.digest):
            raise WorkspaceError("deployment-observe recovery snapshot boundary digest mismatch")

    def _validate_recovery_evidence(self, evidence: dict[str, Any]) -> None:
        configured = self._config_document["recovery"]
        if (
            evidence.get("schema_version") != 1
            or evidence.get("mechanism") != "DIRECT_EXEC_NO_SHELL"
            or evidence.get("checkpoint_id") != self._recovery["checkpoint_id"]
            or evidence.get("recovery_id") != self._recovery["recovery_id"]
            or evidence.get("executable_path") != configured["executable_path"]
            or evidence.get("argv") != configured["argv"]
            or evidence.get("timeout_ms") != configured["timeout_ms"]
            or evidence.get("max_output_bytes") != configured["max_output_bytes"]
            or evidence.get("expected_executable_sha256") != configured["expected_sha256"]
            or type(evidence.get("success")) is not bool
        ):
            raise WorkspaceError(
                "deployment-observe recovery evidence does not bind the configured helper"
            )

    def _validate_terminal_artifacts(self, terminal: dict[str, Any]) -> tuple[Any, Any]:
        verdict = _read_json_artifact(
            self._evidence_directory, "recovery-verdict.json", MAX_ARTIFACT_JSON_BYTES
        )
        manifest = _read_json_artifact(
            self._evidence_directory,
            "recovery-artifact-manifest.json",
            MAX_ARTIFACT_JSON_BYTES,
        )
        if not hmac.compare_digest(verdict.digest, terminal["verdict_sha256"]):
            raise WorkspaceError("deployment-observe recovery verdict digest mismatch")
        if not hmac.compare_digest(manifest.digest, terminal["artifact_manifest_sha256"]):
            raise WorkspaceError("deployment-observe recovery manifest digest mismatch")
        if set(verdict.value) != _VERDICT_KEYS:
            raise WorkspaceError("deployment-observe recovery verdict schema mismatch")
        if set(manifest.value) != _MANIFEST_KEYS:
            raise WorkspaceError("deployment-observe recovery manifest schema mismatch")
        common = (
            verdict.value.get("protocol_version") == RECOVERY_PROTOCOL_VERSION
            and verdict.value.get("schema_version") == 1
            and verdict.value.get("run_id") == self._binding.run_id
            and verdict.value.get("epoch") == self._binding.run_epoch
            and verdict.value.get("checkpoint_id") == self._recovery["checkpoint_id"]
            and verdict.value.get("recovery_id") == self._recovery["recovery_id"]
            and verdict.value.get("verdict") in _VERDICTS
        )
        if not common:
            raise WorkspaceError("deployment-observe recovery verdict binding mismatch")
        a = self._snapshots["A"]
        b = self._snapshots["B"]
        c = self._snapshots["C"]
        if (
            verdict.value.get("snapshot_a_sha256") != a.digest
            or verdict.value.get("snapshot_b_sha256") != b.digest
            or verdict.value.get("snapshot_c_sha256") != c.digest
            or verdict.value.get("snapshot_a_id") != a.value["snapshot_id"]
            or verdict.value.get("snapshot_b_id") != b.value["snapshot_id"]
            or verdict.value.get("snapshot_c_id") != c.value["snapshot_id"]
        ):
            raise WorkspaceError("deployment-observe recovery verdict snapshot mismatch")
        recovery_config_digest = hashlib.sha256(
            _wire_json(self._config_document["recovery"])
        ).hexdigest()
        if (
            manifest.value.get("protocol_version") != RECOVERY_PROTOCOL_VERSION
            or manifest.value.get("run_id") != self._binding.run_id
            or manifest.value.get("epoch") != self._binding.run_epoch
            or manifest.value.get("checkpoint_id") != self._recovery["checkpoint_id"]
            or manifest.value.get("recovery_id") != self._recovery["recovery_id"]
            or manifest.value.get("deployment_scope_sha256")
            != self._legacy_ready["deployment_scope_sha256"]
            or manifest.value.get("recovery_config_sha256") != recovery_config_digest
            or manifest.value.get("recovery_verdict_sha256") != verdict.digest
            or manifest.value.get("snapshot_a_sha256") != a.digest
            or manifest.value.get("snapshot_b_sha256") != b.digest
            or manifest.value.get("snapshot_c_sha256") != c.digest
            or not _valid_nonnegative_integer(manifest.value.get("committed_artifact_total_bytes"))
        ):
            raise WorkspaceError("deployment-observe recovery manifest binding mismatch")
        entries = manifest.value.get("artifacts")
        if not isinstance(entries, list):
            raise WorkspaceError("deployment-observe recovery manifest has no artifact list")
        expected = {
            "recovery-verdict.json": (verdict.digest, verdict.size),
            "snapshot-a.json": (a.digest, a.size),
            "snapshot-b.json": (b.digest, b.size),
            "snapshot-c.json": (c.digest, c.size),
        }
        found: dict[str, tuple[object, object]] = {}
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {
                "relative_path",
                "size_bytes",
                "sha256",
            }:
                raise WorkspaceError(
                    "deployment-observe recovery manifest artifact schema mismatch"
                )
            name = entry["relative_path"]
            if (
                not isinstance(name, str)
                or PurePosixPath(name).name != name
                or name in found
                or not _valid_nonnegative_integer(entry["size_bytes"])
                or not is_sha256(entry["sha256"])
            ):
                raise WorkspaceError("deployment-observe recovery manifest artifact is invalid")
            found[name] = (entry["sha256"], entry["size_bytes"])
        if any(found.get(name) != binding for name, binding in expected.items()):
            raise WorkspaceError(
                "deployment-observe recovery manifest lacks required endpoint evidence"
            )
        return verdict, manifest

    def _run_recovery_verifier(self, deadline: float) -> tuple[dict[str, Any], bytes]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WorkspaceError("system-effects recovery offline verification timed out")
        verifier = self._config.recovery_verifier_path
        assert verifier is not None
        try:
            process = subprocess.Popen(
                [
                    str(verifier),
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
            raise WorkspaceError(
                f"failed to start deployment-observe-recovery-verify: {exc}"
            ) from exc
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
            raise WorkspaceError("deployment-observe-recovery-verify timed out") from exc
        finally:
            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=5)
            with self._lock:
                self._verifier_process = None
        if stdout_thread.is_alive() or stderr_thread.is_alive():
            raise WorkspaceError(
                "deployment-observe-recovery-verify output drain did not terminate"
            )
        if stdout.overflow or stderr.overflow:
            raise WorkspaceError("deployment-observe-recovery-verify output exceeded its bound")
        if process.returncode != 0:
            detail = bytes(stderr.data).decode("utf-8", errors="replace")
            raise WorkspaceError(
                "deployment-observe-recovery-verify exited with status "
                f"{process.returncode}: {detail}"
            )
        try:
            verification = json.loads(stdout.data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkspaceError(
                "deployment-observe-recovery-verify returned invalid JSON"
            ) from exc
        if not isinstance(verification, dict):
            raise WorkspaceError("deployment-observe-recovery-verify result must be a JSON object")
        encoded = bytes(stdout.data)
        if not encoded.endswith(b"\n"):
            encoded += b"\n"
        return verification, encoded

    def _validate_recovery_verification(
        self,
        verification: dict[str, Any],
        terminal: dict[str, Any],
        verdict: dict[str, Any],
        manifest: dict[str, Any],
    ) -> None:
        if set(verification) != _VERIFICATION_KEYS:
            raise WorkspaceError("deployment-observe-recovery-verify result schema mismatch")
        if (
            verification.get("schema_version") != 1
            or verification.get("protocol_version") != RECOVERY_PROTOCOL_VERSION
            or verification.get("verified") is not True
            or verification.get("run_id") != self._binding.run_id
            or verification.get("epoch") != self._binding.run_epoch
            or verification.get("checkpoint_id") != self._recovery["checkpoint_id"]
            or verification.get("recovery_id") != self._recovery["recovery_id"]
            or verification.get("verdict") != verdict["verdict"]
            or verification.get("endpoint_replay_verified") is not True
            or verification.get("recorder_wal_replay_verified") is not True
            or not _valid_nonnegative_integer(verification.get("artifact_count"))
            or not _valid_nonnegative_integer(verification.get("committed_artifact_total_bytes"))
            or verification["artifact_count"] != len(manifest["artifacts"])
            or verification["committed_artifact_total_bytes"]
            != manifest["committed_artifact_total_bytes"]
            or terminal["verdict_sha256"] != manifest["recovery_verdict_sha256"]
        ):
            raise WorkspaceError("deployment-observe-recovery-verify did not verify the exact run")

    def _build_receipt(
        self,
        outcome: str,
        quiescence: dict[str, Any],
        terminal: dict[str, Any],
        verification: dict[str, Any],
        cleanup: dict[str, Any],
    ) -> dict[str, Any]:
        verdict = verification["verdict"]
        consistent = verdict == "CONSISTENT_WITHIN_SCOPE"
        release_allowed = consistent or not self._recovery["require_consistent"]
        return {
            "protocol_version": RECOVERY_PROTOCOL_VERSION,
            "status": "FINISHED",
            "run_id": self._binding.run_id,
            "run_epoch": self._binding.run_epoch,
            "agent_id": self._binding.agent_id,
            "session_id": self._binding.session_id,
            "checkpoint_id": self._recovery["checkpoint_id"],
            "recovery_id": self._recovery["recovery_id"],
            "baseline_created": self._recovery["baseline_created"],
            "cgroup_id": self._binding.cgroup_id,
            "outcome": outcome,
            "quiescence": quiescence,
            "offline_verified": True,
            "offline_verification": "VERIFIED",
            "recovery_verdict": verdict,
            "endpoint_replay_verified": True,
            "recorder_wal_replay_verified": True,
            "recovery_command_succeeded": self._recovery_helper_succeeded is True,
            "require_consistent": self._recovery["require_consistent"],
            "release_allowed": release_allowed,
            "fence_required": not release_allowed,
            "artifact_count": verification["artifact_count"],
            "committed_artifact_total_bytes": verification["committed_artifact_total_bytes"],
            "deferred_rollback_cleanup": cleanup,
            "evidence_directory": str(self._evidence_directory),
            "config_path": str(self._config_path),
            "config_sha256": self._config_sha256,
            "expectation_path": str(self._expectation_path),
            "verification_result_path": str(self._verification_path),
            "verdict_path": str(self._evidence_directory / "recovery-verdict.json"),
            "verdict_sha256": terminal["verdict_sha256"],
            "artifact_manifest_path": str(
                self._evidence_directory / "recovery-artifact-manifest.json"
            ),
            "artifact_manifest_sha256": terminal["artifact_manifest_sha256"],
        }
