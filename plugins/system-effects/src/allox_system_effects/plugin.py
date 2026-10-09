"""Allox workspace feature adapter for Linux system-effects observation."""

from __future__ import annotations

import errno
import fcntl
import os
import stat
import threading
import time
from pathlib import Path
from typing import Any

from allox.runtime.extensions import ExecutionContribution
from allox.workspace.store import WorkspaceError
from allox_system_effects.config import SystemEffectsConfig
from allox_system_effects.recovery_runner import (
    RECOVERY_PROTOCOL_VERSION,
    RecoverySystemEffectsRun,
)
from allox_system_effects.runner import PROTOCOL_VERSION, SystemEffectsRun


class _HostRunLock:
    """One crash-released kernel lock shared by every daemon on this Linux host."""

    def __init__(self, descriptor: int) -> None:
        self._descriptor = descriptor
        self._lock = threading.Lock()

    @classmethod
    def acquire(cls, path: Path, timeout: float) -> _HostRunLock:
        directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
        file_flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            directory_fd = os.open(path.parent, directory_flags)
        except OSError as exc:
            raise WorkspaceError(f"failed to open system-effects host lock parent: {exc}") from exc
        descriptor: int | None = None
        try:
            parent = os.fstat(directory_fd)
            if (
                not stat.S_ISDIR(parent.st_mode)
                or parent.st_uid not in {0, os.geteuid()}
                or parent.st_mode & 0o022
            ):
                raise WorkspaceError(
                    "system-effects host lock parent is not a trusted private directory"
                )
            try:
                descriptor = os.open(path.name, file_flags, 0o600, dir_fd=directory_fd)
            except OSError as exc:
                raise WorkspaceError(f"failed to open system-effects host lock: {exc}") from exc
            opened = os.fstat(descriptor)
            linked = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_uid not in {0, os.geteuid()}
                or opened.st_mode & 0o077
                or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
            ):
                raise WorkspaceError(
                    "system-effects host lock is not a private singly-linked "
                    "root/daemon-owned regular file"
                )
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            raise
        finally:
            os.close(directory_fd)

        assert descriptor is not None
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return cls(descriptor)
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    os.close(descriptor)
                    raise WorkspaceError(
                        f"failed to acquire system-effects host lock: {exc}"
                    ) from exc
                if time.monotonic() >= deadline:
                    os.close(descriptor)
                    raise WorkspaceError(
                        "system-effects host lock timed out; overlapping global snapshots "
                        "are forbidden"
                    ) from exc
                time.sleep(min(0.02, max(0.001, deadline - time.monotonic())))

    def release(self) -> None:
        with self._lock:
            descriptor = self._descriptor
            if descriptor < 0:
                return
            self._descriptor = -1
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(descriptor)


class SystemEffectsFeature:
    """Activate one fail-closed ``deployment-observe`` child per execution."""

    name = "system-effects"
    abi = 1
    execution_lifecycle_abi = 2
    rpc_prefixes: tuple[str, ...] = ()

    def __init__(self, *, context: object, config: dict[str, Any]) -> None:
        self._context = context
        self._config = SystemEffectsConfig.from_mapping(config)
        self._lock = threading.Lock()
        self._run_slot = threading.BoundedSemaphore(1)
        self._slot_busy = False
        self._closed = False
        self._active: set[object] = set()

    @property
    def supports_recovery(self) -> bool:
        return self._config.recovery_enabled

    def prepare_execution(self, agent_id: str, session_id: str) -> ExecutionContribution:
        del agent_id, session_id
        return ExecutionContribution()

    def start_execution(self, context: object) -> object:
        with self._lock:
            if self._closed:
                raise WorkspaceError("system-effects feature is closed")
        if not self._run_slot.acquire(timeout=self._config.ready_timeout):
            raise WorkspaceError(
                "system-effects observation slot timed out; overlapping global snapshots "
                "are forbidden"
            )
        with self._lock:
            self._slot_busy = True
        try:
            host_lock = _HostRunLock.acquire(
                self._config.host_lock_path, self._config.ready_timeout
            )
        except BaseException:
            with self._lock:
                self._slot_busy = False
            self._run_slot.release()
            raise
        release_lock = threading.Lock()
        released = False

        def retire(handle: object) -> None:
            nonlocal released
            with release_lock:
                if released:
                    release_slot = False
                else:
                    released = True
                    release_slot = True
            with self._lock:
                self._active.discard(handle)
                if release_slot:
                    self._slot_busy = False
            if release_slot:
                host_lock.release()
                self._run_slot.release()

        try:
            recovery = getattr(context, "recovery", None)
            if recovery is None:
                handle = SystemEffectsRun(self._config, context, retire)
            else:
                if not self._config.recovery_enabled:
                    raise WorkspaceError(
                        "system-effects recovery was requested but its trusted helper/verifier "
                        "is not configured"
                    )
                store = getattr(self._context, "store", None)
                if store is None:
                    raise WorkspaceError(
                        "system-effects recovery requires the trusted Allox workspace store"
                    )
                handle = RecoverySystemEffectsRun(
                    self._config, context, retire, store
                )
        except BaseException:
            with release_lock:
                if not released:
                    released = True
                    with self._lock:
                        self._slot_busy = False
                    host_lock.release()
                    self._run_slot.release()
            raise
        with self._lock:
            closed = self._closed
            if not closed:
                self._active.add(handle)
        if closed:
            handle.close_unverified()
            raise WorkspaceError("system-effects feature closed during execution startup")
        return handle

    def status(self) -> dict[str, Any]:
        with self._lock:
            active = len(self._active)
            closed = self._closed
            slot_busy = self._slot_busy
        return {
            "enabled": not closed,
            "protocol_version": PROTOCOL_VERSION,
            "protocol_versions": [
                PROTOCOL_VERSION,
                *([RECOVERY_PROTOCOL_VERSION] if self._config.recovery_enabled else []),
            ],
            "recovery_enabled": self._config.recovery_enabled,
            "profile": self._config.profile,
            "object_resolution_mode": self._config.object_resolution_mode,
            "scopes_enabled": list(self._config.scopes_enabled()),
            "active_runs": active,
            "global_run_serialization": "HOST_WIDE_FLOCK",
            "host_lock_path": str(self._config.host_lock_path),
            "observation_slot_busy": slot_busy,
            "offline_verifier_required": True,
        }

    def dispatch(self, action: str, params: dict[str, Any]) -> Any:
        del action, params
        raise WorkspaceError("system-effects plugin has no workload/RPC control surface")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            handles = list(self._active)
        for handle in handles:
            handle.close_unverified()

    def _retire(self, handle: SystemEffectsRun) -> None:
        with self._lock:
            self._active.discard(handle)
