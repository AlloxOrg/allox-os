"""Trusted configuration for the system-effects workspace feature."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from allox.workspace.store import WorkspaceError

MAX_SCOPE_BYTES = 4 * 1024 * 1024
MAX_CONFIG_BYTES = 1024 * 1024
MAX_RECOVERY_OUTPUT_BYTES = 8 * 1024 * 1024
DEFAULT_HOST_LOCK_PATH = Path("/run/allox-system-effects.lock")

_HEX = frozenset("0123456789abcdef")
_PROFILES = frozenset({"control", "balanced", "full"})
_OBJECT_MODES = frozenset(
    {
        "USER_INPUT",
        "KERNEL_FILE_OPEN_PARTIAL",
        "KERNEL_FILE_OPEN_WRITE_PARTIAL",
        "KERNEL_OBJECT_EXPANDED_PARTIAL",
    }
)
_CONFIG_KEYS = frozenset(
    {
        "observer_path",
        "verifier_path",
        "recorder_object_path",
        "recorder_object_sha256",
        "lifecycle_object_path",
        "lifecycle_object_sha256",
        "profile",
        "object_resolution_mode",
        "bpf_state_scope_file",
        "filesystem_state_scope_file",
        "linux_state_scope_file",
        "poll_interval_ms",
        "drain_timeout_ms",
        "ready_timeout",
        "finalize_timeout",
        "host_lock_path",
        "recovery_verifier_path",
        "recovery_helper_path",
        "recovery_helper_sha256",
        "recovery_timeout_ms",
        "recovery_max_output_bytes",
    }
)
_REQUIRED_KEYS = frozenset(
    {
        "observer_path",
        "recorder_object_path",
        "recorder_object_sha256",
        "lifecycle_object_path",
        "lifecycle_object_sha256",
    }
)


def is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and not (set(value) - _HEX)


def _bounded_number(
    config: dict[str, Any],
    key: str,
    default: float,
    lower: float,
    upper: float,
    *,
    integer: bool = False,
) -> int | float:
    value = config.get(key, default)
    expected = int if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, expected) or not lower <= value <= upper:
        raise WorkspaceError(f"system-effects {key} must be in [{lower}, {upper}]")
    return int(value) if integer else float(value)


def _trusted_regular_path(raw: object, label: str, *, executable: bool = False) -> Path:
    if not isinstance(raw, str) or not raw or "\0" in raw:
        raise WorkspaceError(f"system-effects {label} must be a nonempty path")
    path = Path(raw)
    if not path.is_absolute():
        raise WorkspaceError(f"system-effects {label} must be absolute")
    try:
        resolved = path.resolve(strict=True)
        metadata = path.lstat()
    except OSError as exc:
        raise WorkspaceError(f"system-effects {label} is unavailable: {exc}") from exc
    if resolved != path or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise WorkspaceError(
            f"system-effects {label} must be a canonical singly-linked regular file"
        )
    if metadata.st_uid not in {0, os.geteuid()} or metadata.st_mode & 0o022:
        raise WorkspaceError(
            f"system-effects {label} must be root/daemon-owned and not group/world writable"
        )
    if executable and not os.access(path, os.X_OK):
        raise WorkspaceError(f"system-effects {label} is not executable")
    return path


def _trusted_host_lock_path(raw: object) -> Path:
    """Validate a stable path whose parent prevents lock-inode replacement."""
    if not isinstance(raw, str) or not raw or "\0" in raw:
        raise WorkspaceError("system-effects host_lock_path must be a nonempty path")
    path = Path(raw)
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise WorkspaceError("system-effects host_lock_path must be absolute")
    try:
        parent = path.parent.resolve(strict=True)
        parent_metadata = path.parent.lstat()
    except OSError as exc:
        raise WorkspaceError(f"system-effects host lock parent is unavailable: {exc}") from exc
    if parent != path.parent or not stat.S_ISDIR(parent_metadata.st_mode):
        raise WorkspaceError(
            "system-effects host_lock_path parent must be a canonical real directory"
        )
    if parent_metadata.st_uid not in {0, os.geteuid()} or parent_metadata.st_mode & 0o022:
        raise WorkspaceError(
            "system-effects host lock parent must be root/daemon-owned and not group/world writable"
        )
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return path
    except OSError as exc:
        raise WorkspaceError(f"system-effects host lock is unavailable: {exc}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid not in {0, os.geteuid()}
        or metadata.st_mode & 0o077
    ):
        raise WorkspaceError(
            "system-effects host lock must be a private singly-linked root/daemon-owned regular file"
        )
    return path


def _read_stable_json(path: Path, label: str) -> dict[str, Any]:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise WorkspaceError(f"failed to open system-effects {label}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size < 0
            or before.st_size > MAX_SCOPE_BYTES
        ):
            raise WorkspaceError(
                f"system-effects {label} must be a bounded singly-linked regular file"
            )
        chunks: list[bytes] = []
        consumed = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, MAX_SCOPE_BYTES + 1 - consumed))
            if not chunk:
                break
            consumed += len(chunk)
            if consumed > MAX_SCOPE_BYTES:
                raise WorkspaceError(f"system-effects {label} exceeds the 4 MiB bound")
            chunks.append(chunk)
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
            raise WorkspaceError(f"system-effects {label} changed while it was read")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(b"".join(chunks))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError(f"system-effects {label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise WorkspaceError(f"system-effects {label} must contain one JSON object")
    return value


def _scope(config: dict[str, Any], key: str) -> dict[str, Any] | None:
    raw = config.get(key)
    if raw is None:
        return None
    path = _trusted_regular_path(raw, key)
    return _read_stable_json(path, key)


@dataclass(frozen=True)
class SystemEffectsConfig:
    """Immutable daemon-owned configuration shared by per-run handles."""

    observer_path: Path
    verifier_path: Path
    recorder_object_path: Path
    recorder_object_sha256: str
    lifecycle_object_path: Path
    lifecycle_object_sha256: str
    profile: str
    object_resolution_mode: str
    bpf_state_scope: dict[str, Any] | None
    filesystem_state_scope: dict[str, Any] | None
    linux_state_scope: dict[str, Any] | None
    poll_interval_ms: int
    drain_timeout_ms: int
    ready_timeout: float
    finalize_timeout: float
    host_lock_path: Path
    recovery_verifier_path: Path | None
    recovery_helper_path: Path | None
    recovery_helper_sha256: str | None
    recovery_timeout_ms: int
    recovery_max_output_bytes: int

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> SystemEffectsConfig:
        if not isinstance(raw, dict):
            raise WorkspaceError("system-effects plugin configuration must be an object")
        unknown = sorted(set(raw) - _CONFIG_KEYS)
        missing = sorted(_REQUIRED_KEYS - set(raw))
        if unknown:
            raise WorkspaceError("unknown system-effects configuration keys: " + ", ".join(unknown))
        if missing:
            raise WorkspaceError("missing system-effects configuration keys: " + ", ".join(missing))

        observer = _trusted_regular_path(raw["observer_path"], "observer_path", executable=True)
        verifier_raw = raw.get("verifier_path")
        verifier = _trusted_regular_path(
            verifier_raw
            if verifier_raw is not None
            else str(observer.with_name("deployment-observe-verify")),
            "verifier_path",
            executable=True,
        )
        recorder = _trusted_regular_path(raw["recorder_object_path"], "recorder_object_path")
        lifecycle = _trusted_regular_path(raw["lifecycle_object_path"], "lifecycle_object_path")
        recorder_sha = raw["recorder_object_sha256"]
        lifecycle_sha = raw["lifecycle_object_sha256"]
        if not is_sha256(recorder_sha):
            raise WorkspaceError(
                "system-effects recorder_object_sha256 must be 64 lowercase hexadecimal bytes"
            )
        if not is_sha256(lifecycle_sha):
            raise WorkspaceError(
                "system-effects lifecycle_object_sha256 must be 64 lowercase hexadecimal bytes"
            )
        profile = raw.get("profile", "balanced")
        if profile not in _PROFILES:
            raise WorkspaceError("system-effects profile must be control, balanced, or full")
        object_mode = raw.get("object_resolution_mode", "KERNEL_OBJECT_EXPANDED_PARTIAL")
        if object_mode not in _OBJECT_MODES:
            raise WorkspaceError("unsupported system-effects object_resolution_mode")
        recovery_helper_raw = raw.get("recovery_helper_path")
        recovery_helper_sha = raw.get("recovery_helper_sha256")
        recovery_verifier_raw = raw.get("recovery_verifier_path")
        recovery_values = (recovery_helper_raw, recovery_helper_sha)
        if any(value is not None for value in recovery_values) and not all(
            value is not None for value in recovery_values
        ):
            raise WorkspaceError(
                "system-effects recovery_helper_path and recovery_helper_sha256 "
                "must be configured together"
            )
        if recovery_verifier_raw is not None and recovery_helper_raw is None:
            raise WorkspaceError(
                "system-effects recovery_verifier_path requires a recovery helper"
            )
        recovery_helper = (
            _trusted_regular_path(
                recovery_helper_raw, "recovery_helper_path", executable=True
            )
            if recovery_helper_raw is not None
            else None
        )
        if recovery_helper_sha is not None and not is_sha256(recovery_helper_sha):
            raise WorkspaceError(
                "system-effects recovery_helper_sha256 must be 64 lowercase hexadecimal bytes"
            )
        recovery_verifier = (
            _trusted_regular_path(
                recovery_verifier_raw
                if recovery_verifier_raw is not None
                else str(observer.with_name("deployment-observe-recovery-verify")),
                "recovery_verifier_path",
                executable=True,
            )
            if recovery_helper is not None
            else None
        )

        return cls(
            observer_path=observer,
            verifier_path=verifier,
            recorder_object_path=recorder,
            recorder_object_sha256=recorder_sha,
            lifecycle_object_path=lifecycle,
            lifecycle_object_sha256=lifecycle_sha,
            profile=profile,
            object_resolution_mode=object_mode,
            bpf_state_scope=_scope(raw, "bpf_state_scope_file"),
            filesystem_state_scope=_scope(raw, "filesystem_state_scope_file"),
            linux_state_scope=_scope(raw, "linux_state_scope_file"),
            poll_interval_ms=_bounded_number(raw, "poll_interval_ms", 20, 1, 1000, integer=True),
            drain_timeout_ms=_bounded_number(
                raw, "drain_timeout_ms", 5000, 1, 60_000, integer=True
            ),
            ready_timeout=_bounded_number(raw, "ready_timeout", 30.0, 0.1, 60.0),
            finalize_timeout=_bounded_number(raw, "finalize_timeout", 60.0, 0.1, 300.0),
            host_lock_path=_trusted_host_lock_path(
                raw.get("host_lock_path", str(DEFAULT_HOST_LOCK_PATH))
            ),
            recovery_verifier_path=recovery_verifier,
            recovery_helper_path=recovery_helper,
            recovery_helper_sha256=recovery_helper_sha,
            recovery_timeout_ms=_bounded_number(
                raw, "recovery_timeout_ms", 120_000, 1, 3_600_000, integer=True
            ),
            recovery_max_output_bytes=_bounded_number(
                raw, "recovery_max_output_bytes", 65_536, 1, MAX_RECOVERY_OUTPUT_BYTES,
                integer=True,
            ),
        )

    @property
    def recovery_enabled(self) -> bool:
        return (
            self.recovery_verifier_path is not None
            and self.recovery_helper_path is not None
            and self.recovery_helper_sha256 is not None
        )

    def scopes_enabled(self) -> tuple[str, ...]:
        values = (
            ("bpf", self.bpf_state_scope),
            ("filesystem", self.filesystem_state_scope),
            ("linux", self.linux_state_scope),
        )
        return tuple(name for name, scope in values if scope is not None)
