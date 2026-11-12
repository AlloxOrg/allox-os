"""Trusted two-level workspace storage inside one Allox Kata VM.

The daemon and store live inside the Kata VM but outside untrusted Agent
processes. An Agent workspace is the first-level ownership boundary and contains
shared Agent files plus child Session workspaces. Each Session workspace is a
second-level execution and rollback boundary whose ``current`` directory is a
writable Btrfs subvolume.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

from allox.workspace import btrfs_ioctl
from allox.workspace.checkpoint_index import CheckpointIndex, CheckpointIndexError
from allox.workspace.session_memory import (
    MAX_BYTES,
    MemoryStateError,
    empty_state,
    is_version,
    snapshot,
)

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class WorkspaceError(RuntimeError):
    """A safe, user-facing workspace operation failure."""


def validate_id(kind: str, value: str) -> str:
    if not ID_RE.fullmatch(value):
        raise WorkspaceError(f"invalid {kind} id: {value!r}")
    return value


class SnapshotBackend(Protocol):
    def assert_root(self, root: Path) -> None: ...

    def create_subvolume(self, path: Path) -> None: ...

    def snapshot(self, source: Path, target: Path, *, readonly: bool) -> None: ...

    def delete_subvolume(self, path: Path) -> None: ...


class BtrfsBackend:
    """Btrfs implementation used by the trusted host daemon."""

    def __init__(self, *, native_ioctls: bool = True) -> None:
        self._native_ioctls = native_ioctls and btrfs_ioctl.supported()

    @staticmethod
    def _run(argv: list[str], *, capture: bool = False) -> str:
        if capture:
            return subprocess.check_output(argv, text=True).strip()
        subprocess.run(argv, check=True)
        return ""

    def assert_root(self, root: Path) -> None:
        if not root.exists():
            raise WorkspaceError(f"workspace root does not exist: {root}")
        fs_type = self._run(["findmnt", "-T", str(root), "-no", "FSTYPE"], capture=True)
        if fs_type != "btrfs":
            raise WorkspaceError(f"workspace root must be Btrfs, got: {fs_type}")

    def create_subvolume(self, path: Path) -> None:
        self._run(["btrfs", "subvolume", "create", str(path)])

    def snapshot(self, source: Path, target: Path, *, readonly: bool) -> None:
        if self._native_ioctls:
            try:
                btrfs_ioctl.snapshot(source, target, readonly=readonly)
            except (OSError, ValueError) as exc:
                # Do not retry a mutating ioctl via CLI after an error.
                raise WorkspaceError(f"Btrfs snapshot failed: {exc}") from exc
            return
        argv = ["btrfs", "subvolume", "snapshot"]
        if readonly:
            argv.append("-r")
        argv.extend([str(source), str(target)])
        self._run(argv)

    def delete_subvolume(self, path: Path) -> None:
        if self._native_ioctls:
            try:
                btrfs_ioctl.delete_subvolume(path)
            except (OSError, ValueError) as exc:
                raise WorkspaceError(f"Btrfs subvolume deletion failed: {exc}") from exc
            return
        self._run(["btrfs", "subvolume", "delete", str(path)])


class WorkspaceStore:
    """Hierarchical ``Kata VM -> Agent workspace -> Session workspace`` manager."""

    def __init__(self, root: Path | str, backend: SnapshotBackend | None = None):
        self.root = Path(root).resolve()
        self.backend = backend or BtrfsBackend()
        self._thread_locks: dict[tuple[str, str], threading.RLock] = {}
        self._thread_locks_guard = threading.Lock()

    def initialize(self) -> dict[str, Any]:
        self.backend.assert_root(self.root)
        (self.root / ".allox").mkdir(parents=True, exist_ok=True)
        with self._initialization_guard():
            return self._initialize_unowned()

    @contextmanager
    def _initialization_guard(self):
        # Journal recovery must not race an already running recovery owner,
        # even before a second daemon reaches plugin activation.
        if os.name != "posix":
            yield
            return
        import fcntl

        fd = os.open(self.root / ".allox" / "recovery-checks.owner",
                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise WorkspaceError("recovery owner lock must be a regular file")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WorkspaceError("store already has an active recovery owner") from exc
            yield
        finally:
            os.close(fd)

    def _initialize_unowned(self) -> dict[str, Any]:
        (self.root / "agents").mkdir(parents=True, exist_ok=True)
        (self.root / ".allox" / "events").mkdir(parents=True, exist_ok=True)
        (self.root / ".allox" / "indexes").mkdir(parents=True, exist_ok=True)
        (self.root / ".allox" / "locks").mkdir(parents=True, exist_ok=True)
        (self.root / ".allox" / "transactions").mkdir(parents=True, exist_ok=True)
        recovered = self._recover_sessions()
        self._append_event({"op": "store.initialize"})
        return {"root": str(self.root), "recovered_sessions": recovered}

    def agent_dir(self, agent_id: str) -> Path:
        return self.root / "agents" / validate_id("agent", agent_id)

    def agent_workspace(self, agent_id: str) -> Path:
        return self.agent_dir(agent_id) / "workspace"

    def agent_shared(self, agent_id: str) -> Path:
        return self.agent_workspace(agent_id) / "shared"

    def sessions_dir(self, agent_id: str) -> Path:
        return self.agent_workspace(agent_id) / "sessions"

    def session_dir(self, agent_id: str, session_id: str) -> Path:
        validate_id("session", session_id)
        return self.sessions_dir(agent_id) / session_id

    def current(self, agent_id: str, session_id: str) -> Path:
        return self.session_dir(agent_id, session_id) / "current"

    def checkpoints(self, agent_id: str, session_id: str) -> Path:
        return self.session_dir(agent_id, session_id) / "checkpoints"

    def _index_path(self, agent_id: str, session_id: str) -> Path:
        validate_id("agent", agent_id)
        validate_id("session", session_id)
        return self.root / ".allox" / "indexes" / agent_id / f"{session_id}.json"

    def _transaction_path(self, agent_id: str, session_id: str) -> Path:
        validate_id("agent", agent_id)
        validate_id("session", session_id)
        return (
            self.root / ".allox" / "transactions" / agent_id / f"{session_id}.json"
        )

    @staticmethod
    def _atomic_write_json(
        target: Path,
        value: dict[str, Any],
        *,
        file_mode: int | None = None,
        compact: bool = False,
    ) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            if file_mode is None:
                stream = temporary.open("w", encoding="utf-8")
            else:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    file_mode,
                )
                stream = os.fdopen(descriptor, "w", encoding="utf-8")
            with stream:
                json.dump(value, stream, indent=None if compact else 2, sort_keys=True,
                          separators=(",", ":") if compact else None, allow_nan=not compact)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            WorkspaceStore._sync_directory(target.parent)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _sync_directory(path: Path) -> None:
        if os.name == "posix":
            descriptor = os.open(path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def require_memory_ready(self, agent_id: str, session_id: str) -> None:
        transaction = self._load_transaction(agent_id, session_id)
        if transaction is not None and transaction.get("version") == 3:
            raise WorkspaceError("Session has a pending memory rollback; recover before use")

    def _read_memory_version(self, agent_id: str, session_id: str, version: str) -> dict[str, Any]:
        if not is_version(version):
            raise WorkspaceError("invalid memory version")
        root = self.root / ".allox" / "memory"
        self._require_managed_directory(root, "memory storage")
        path = root / f"{version}.json"
        if path.is_symlink() or not path.is_file():
            raise WorkspaceError("missing or unsafe memory snapshot")
        try:
            with path.open("rb") as stream:
                data = stream.read(MAX_BYTES + 2)
            if len(data) > MAX_BYTES + 1:
                raise MemoryStateError("memory snapshot exceeds size limit")
            document = json.loads(data)
            if not isinstance(document, dict):
                raise MemoryStateError("invalid memory snapshot")
            normalized, digest = snapshot(agent_id, session_id, document.get("state"))
            if normalized != document or digest != version:
                raise MemoryStateError("memory snapshot identity or digest mismatch")
        except (OSError, ValueError, RecursionError) as exc:
            raise WorkspaceError("invalid memory snapshot or integrity check failed") from exc
        return normalized["state"]

    @staticmethod
    def _check_memory_revision(index: CheckpointIndex, expected_revision: Any) -> None:
        if type(expected_revision) is not int or expected_revision != index.memory_revision:
            raise WorkspaceError("memory revision conflict; reload the current Session state")

    def memory_read(self, agent_id: str, session_id: str) -> dict[str, Any]:
        with self._session_lock(agent_id, session_id):
            self.require_memory_ready(agent_id, session_id)
            self._require_managed_directory(self.current(agent_id, session_id), "session workspace")
            index = self._load_index(agent_id, session_id)
            version = index.memory_version
            state = self._read_memory_version(agent_id, session_id, version) if version else empty_state()
            return {"memory_version": version, "memory_revision": index.memory_revision, "state": state}

    def _stage_memory(
        self, agent_id: str, session_id: str, index: CheckpointIndex,
        state: Any, expected_revision: Any,
    ) -> None:
        """Persist an immutable blob but leave head publication to the caller."""
        self._check_memory_revision(index, expected_revision)
        try:
            document, version = snapshot(agent_id, session_id, state)
        except MemoryStateError as exc:
            raise WorkspaceError(str(exc)) from exc
        root = self.root / ".allox" / "memory"
        root.mkdir(mode=0o700, exist_ok=True)
        self._require_managed_directory(root, "memory storage")
        self._sync_directory(root.parent)
        target = root / f"{version}.json"
        if target.exists() or target.is_symlink():
            self._read_memory_version(agent_id, session_id, version)
        else:
            self._atomic_write_json(target, document, file_mode=0o600, compact=True)
        index.memory_version = version
        index.memory_revision += 1

    def memory_write(
        self, agent_id: str, session_id: str, state: Any, *, expected_revision: Any
    ) -> dict[str, Any]:
        with self._session_lock(agent_id, session_id):
            self.require_memory_ready(agent_id, session_id)
            self._require_managed_directory(self.current(agent_id, session_id), "session workspace")
            index = self._load_index(agent_id, session_id)
            self._stage_memory(agent_id, session_id, index, state, expected_revision)
            self._save_index(agent_id, session_id, index)
            result = {"memory_version": index.memory_version, "memory_revision": index.memory_revision}
            self._append_event({"op": "memory.write", **result}, agent_id, session_id)
            return result

    def memory_restore(
        self, agent_id: str, session_id: str, checkpoint_id: str, *, expected_revision: Any
    ) -> dict[str, Any]:
        validate_id("checkpoint", checkpoint_id)
        with self._session_lock(agent_id, session_id):
            self.require_memory_ready(agent_id, session_id)
            self._require_managed_directory(self.current(agent_id, session_id), "session workspace")
            index = self._load_index(agent_id, session_id)
            self._check_memory_revision(index, expected_revision)
            entry = index.checkpoints.get(checkpoint_id, {})
            version = entry.get("memory_version")
            if not isinstance(version, str) or entry.get("missing"):
                raise WorkspaceError("exact checkpoint with a bound memory version is required")
            self._read_memory_version(agent_id, session_id, version)
            index.memory_version = version
            index.memory_revision += 1
            self._save_index(agent_id, session_id, index)
            result = {"checkpoint_id": checkpoint_id, "memory_version": version,
                      "memory_revision": index.memory_revision, "scope": "memory-only"}
            self._append_event({"op": "memory.restore", **result}, agent_id, session_id)
            return result

    def _apply_memory_target(
        self, agent_id: str, session_id: str, index: CheckpointIndex,
        checkpoint_id: str, target: Any,
    ) -> None:
        if not isinstance(target, dict) or set(target) != {"version", "revision", "restore"}:
            raise WorkspaceError("invalid rollback memory target")
        version, revision, restore = target["version"], target["revision"], target["restore"]
        if (not is_version(version) or type(revision) is not int or revision < 1
                or type(restore) is not bool or index.memory_revision not in (revision-1, revision)
                or (index.memory_revision == revision and index.memory_version != version)):
            raise WorkspaceError("invalid rollback memory revision")
        expected = index.checkpoints[checkpoint_id].get("memory_version") if restore else index.memory_version
        if version != expected:
            raise WorkspaceError("rollback memory binding mismatch")
        self._read_memory_version(agent_id, session_id, version)
        index.memory_version = version
        index.memory_revision = revision

    def _actual_checkpoint_ids(self, agent_id: str, session_id: str) -> set[str]:
        target = self.checkpoints(agent_id, session_id)
        if not target.exists():
            return set()
        self._require_managed_directory(target, "checkpoint directory")
        return {
            path.name
            for path in target.iterdir()
            if path.is_dir() and not path.is_symlink()
        }

    @staticmethod
    def _require_managed_directory(path: Path, label: str) -> None:
        if path.is_symlink() or not path.is_dir():
            raise WorkspaceError(f"{label} is not a managed directory: {path}")

    def _load_index(self, agent_id: str, session_id: str) -> CheckpointIndex:
        target = self._index_path(agent_id, session_id)
        try:
            if target.exists():
                value = json.loads(target.read_text(encoding="utf-8"))
                if not isinstance(value, dict):
                    raise CheckpointIndexError("checkpoint index root must be an object")
                index = CheckpointIndex.from_dict(value)
            else:
                index = CheckpointIndex()
            changed = index.reconcile(
                self._actual_checkpoint_ids(agent_id, session_id)
            )
        except (OSError, json.JSONDecodeError, CheckpointIndexError) as exc:
            raise WorkspaceError(f"invalid checkpoint index: {exc}") from exc
        if changed or not target.exists():
            self._save_index(agent_id, session_id, index)
        return index

    def _save_index(
        self, agent_id: str, session_id: str, index: CheckpointIndex
    ) -> None:
        self._atomic_write_json(self._index_path(agent_id, session_id), index.to_dict())

    def _write_transaction(
        self, agent_id: str, session_id: str, transaction: dict[str, Any]
    ) -> None:
        path = self._transaction_path(agent_id, session_id)
        self._atomic_write_json(path, transaction)
        if transaction.get("version") == 3 and transaction.get("phase") == "preparing":
            self._sync_directory(path.parent.parent)

    def _clear_transaction(self, agent_id: str, session_id: str) -> None:
        path = self._transaction_path(agent_id, session_id)
        path.unlink(missing_ok=True)
        if path.parent.is_dir():
            self._sync_directory(path.parent)

    def _load_transaction(
        self, agent_id: str, session_id: str
    ) -> dict[str, Any] | None:
        target = self._transaction_path(agent_id, session_id)
        if not target.exists():
            return None
        if target.is_symlink() or not target.is_file():
            raise WorkspaceError("rollback transaction is not a managed file")
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceError(f"invalid rollback transaction: {exc}") from exc
        if not isinstance(loaded, dict):
            raise WorkspaceError("rollback transaction root must be an object")
        return loaded

    def _recovery_receipt_path(
        self, agent_id: str, session_id: str, recovery_id: str
    ) -> Path:
        validate_id("agent", agent_id)
        validate_id("session", session_id)
        validate_id("recovery", recovery_id)
        return (
            self.root
            / ".allox"
            / "recovery-receipts"
            / agent_id
            / session_id
            / f"{recovery_id}.json"
        )

    def _write_recovery_receipt(
        self,
        agent_id: str,
        session_id: str,
        recovery_id: str,
        receipt: dict[str, Any],
    ) -> None:
        target = self._recovery_receipt_path(agent_id, session_id, recovery_id)
        private_directories = [
            self.root / ".allox" / "recovery-receipts",
            self.root / ".allox" / "recovery-receipts" / agent_id,
            target.parent,
        ]
        for directory in private_directories:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
        self._atomic_write_json(target, receipt, file_mode=0o600)

    def _load_recovery_receipt(
        self, agent_id: str, session_id: str, recovery_id: str
    ) -> dict[str, Any] | None:
        target = self._recovery_receipt_path(agent_id, session_id, recovery_id)
        if not target.exists():
            return None
        if target.is_symlink() or not target.is_file():
            raise WorkspaceError("deferred rollback receipt is not a managed file")
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceError(f"invalid deferred rollback receipt: {exc}") from exc
        if not isinstance(loaded, dict):
            raise WorkspaceError("deferred rollback receipt root must be an object")
        if (
            loaded.get("version") != 1
            or loaded.get("agent_id") != agent_id
            or loaded.get("session_id") != session_id
            or loaded.get("recovery_id") != recovery_id
            or loaded.get("status") not in {"cleanup_started", "completed"}
        ):
            raise WorkspaceError("deferred rollback receipt does not match the request")
        checkpoint_value = loaded.get("checkpoint_id")
        if not isinstance(checkpoint_value, str):
            raise WorkspaceError("deferred rollback receipt checkpoint_id must be a string")
        validate_id("checkpoint", checkpoint_value)
        self._safe_transaction_name(loaded.get("discard_name"), ".discard-")
        return loaded

    @staticmethod
    def _safe_transaction_name(value: Any, prefix: str) -> str:
        if (
            not isinstance(value, str)
            or Path(value).name != value
            or not value.startswith(prefix)
        ):
            raise WorkspaceError("rollback transaction contains an unsafe path")
        return value

    def _transaction_subvolume_path(
        self,
        session_dir: Path,
        value: Any,
        prefix: str,
    ) -> Path:
        name = self._safe_transaction_name(value, prefix)
        candidate = session_dir / name
        try:
            candidate.resolve(strict=False).relative_to(session_dir.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise WorkspaceError("rollback transaction path escapes the session") from exc
        if candidate.parent != session_dir or candidate.is_symlink():
            raise WorkspaceError("rollback transaction path is not confined to the session")
        return candidate

    def _recover_sessions(self) -> list[str]:
        recovered: list[str] = []
        agents = self.root / "agents"
        for agent_dir in agents.iterdir():
            if (
                not agent_dir.is_dir()
                or agent_dir.is_symlink()
                or not ID_RE.fullmatch(agent_dir.name)
            ):
                continue
            sessions = agent_dir / "workspace" / "sessions"
            if not sessions.is_dir():
                continue
            for session_dir in sessions.iterdir():
                if (
                    not session_dir.is_dir()
                    or session_dir.is_symlink()
                    or not ID_RE.fullmatch(session_dir.name)
                ):
                    continue
                with self._session_lock(agent_dir.name, session_dir.name):
                    if self._recover_session(agent_dir.name, session_dir.name):
                        recovered.append(f"{agent_dir.name}/{session_dir.name}")
        return recovered

    def _recover_session(self, agent_id: str, session_id: str) -> bool:
        session_dir = self.session_dir(agent_id, session_id)
        current = self.current(agent_id, session_id)
        transaction = self._load_transaction(agent_id, session_id)

        restores = sorted(session_dir.glob(".restore-*"))
        discards = sorted(session_dir.glob(".discard-*"))
        if transaction is None:
            if not restores and not discards:
                return False
            if not current.exists():
                if len(discards) != 1:
                    raise WorkspaceError(
                        f"cannot recover orphan rollback for {agent_id}/{session_id}"
                    )
                discards[0].rename(current)
                discards = []
            for path in [*restores, *discards]:
                if path.exists():
                    self.backend.delete_subvolume(path)
            self._append_event(
                {"op": "session.rollback.recovered", "outcome": "aborted-orphan"},
                agent_id,
                session_id,
            )
            return True

        phase = transaction.get("phase")
        if phase not in {"preparing", "prepared", "old_moved", "committed"}:
            raise WorkspaceError(f"invalid rollback transaction phase: {phase!r}")
        version = transaction.get("version")
        if version not in {1, 2, 3}:
            raise WorkspaceError("unsupported rollback transaction version")
        memory_rollback = version == 3
        memory_target = transaction.get("memory_target")
        if memory_rollback and (not isinstance(memory_target, dict)
                                or not isinstance(transaction.get("defer_cleanup"), bool)
                                or not isinstance(transaction.get("scrub_runtime"), bool)):
            raise WorkspaceError("invalid Session memory rollback journal")
        deferred = version == 2 or (memory_rollback and transaction["defer_cleanup"])
        recovery_id: str | None = None
        if deferred:
            if transaction.get("defer_cleanup") is not True:
                raise WorkspaceError("deferred rollback transaction is not marked deferred")
            recovery_value = transaction.get("recovery_id")
            if not isinstance(recovery_value, str):
                raise WorkspaceError(
                    "deferred rollback transaction recovery_id must be a string"
                )
            recovery_id = validate_id("recovery", recovery_value)
            if not isinstance(transaction.get("scrub_runtime"), bool):
                raise WorkspaceError(
                    "deferred rollback transaction scrub_runtime must be a boolean"
                )
        checkpoint_value = transaction.get("checkpoint_id")
        if not isinstance(checkpoint_value, str):
            raise WorkspaceError("rollback transaction checkpoint_id must be a string")
        checkpoint_id = validate_id("checkpoint", checkpoint_value)
        restored = self._transaction_subvolume_path(
            session_dir, transaction.get("restore_name"), ".restore-"
        )
        discarded = self._transaction_subvolume_path(
            session_dir, transaction.get("discard_name"), ".discard-"
        )
        if memory_rollback and (
            (phase == "committed" and not current.exists())
            or (phase in {"old_moved", "committed"} and current.exists() and restored.exists())
        ):
            raise WorkspaceError("ambiguous file state in Session memory rollback")

        if deferred and phase == "committed" and current.exists():
            assert recovery_id is not None
            self._require_managed_directory(current, "session workspace")
            if restored.exists():
                raise WorkspaceError("committed deferred rollback retains a restore path")
            index = self._load_index(agent_id, session_id)
            if index.head != checkpoint_id:
                raise WorkspaceError(
                    "committed deferred rollback does not match the checkpoint head"
                )
            if memory_rollback:
                assert isinstance(memory_target, dict)
                if index.memory_revision != memory_target.get("revision"):
                    raise WorkspaceError("committed rollback memory revision mismatch")
                self._apply_memory_target(agent_id, session_id, index, checkpoint_id, memory_target)
            receipt = self._load_recovery_receipt(
                agent_id, session_id, recovery_id
            )
            if receipt is None:
                if not discarded.exists():
                    raise WorkspaceError(
                        "committed deferred rollback is missing its discarded workspace"
                    )
                self._require_managed_directory(
                    discarded, "deferred rollback discarded workspace"
                )
                return False
            if (
                receipt.get("checkpoint_id") != checkpoint_id
                or receipt.get("discard_name") != discarded.name
            ):
                raise WorkspaceError(
                    "deferred rollback receipt does not match the transaction"
                )
            if discarded.exists():
                self._require_managed_directory(
                    discarded, "deferred rollback discarded workspace"
                )
                self.backend.delete_subvolume(discarded)
            completed_receipt = dict(receipt)
            completed_receipt["status"] = "completed"
            completed_receipt.setdefault("completed_at_ns", time.time_ns())
            self._write_recovery_receipt(
                agent_id, session_id, recovery_id, completed_receipt
            )
            self._clear_transaction(agent_id, session_id)
            self._append_event(
                {
                    "op": "session.rollback.cleanup.recovered",
                    "checkpoint_id": checkpoint_id,
                    "recovery_id": recovery_id,
                    "outcome": "completed",
                },
                agent_id,
                session_id,
            )
            return True

        commit = current.exists() and phase in {"old_moved", "committed"}

        if not current.exists():
            if phase in {"prepared", "old_moved", "committed"} and restored.exists():
                self._require_managed_directory(restored, "rollback restore workspace")
                restored.rename(current)
                commit = True
            elif discarded.exists() and not memory_rollback:
                self._require_managed_directory(discarded, "rollback discarded workspace")
                discarded.rename(current)
                commit = False
            else:
                raise WorkspaceError(
                    f"rollback recovery has no current workspace: {agent_id}/{session_id}"
                )

        if deferred and commit:
            assert recovery_id is not None
            self._require_managed_directory(current, "session workspace")
            if not discarded.exists():
                raise WorkspaceError(
                    "deferred rollback recovery is missing its discarded workspace"
                )
            self._require_managed_directory(
                discarded, "deferred rollback discarded workspace"
            )
            if restored.exists():
                raise WorkspaceError("deferred rollback recovery retains a restore path")
            removed = (
                self._scrub_runtime_entries(current / ".allox-tmp")
                if transaction["scrub_runtime"]
                else []
            )
            index = self._load_index(agent_id, session_id)
            try:
                resolved = index.resolve(checkpoint_id=checkpoint_id)
                index.move_head(resolved)
            except CheckpointIndexError as exc:
                raise WorkspaceError(str(exc)) from exc
            if memory_rollback:
                if resolved != checkpoint_id:
                    raise WorkspaceError("rollback checkpoint binding mismatch")
                self._apply_memory_target(agent_id, session_id, index, resolved, memory_target)
                self._sync_directory(current.parent)
            self._save_index(agent_id, session_id, index)
            transaction["phase"] = "committed"
            transaction["removed_runtime_entries"] = removed
            transaction["committed_at_ns"] = time.time_ns()
            self._write_transaction(agent_id, session_id, transaction)
            self._append_event(
                {
                    "op": "session.rollback.recovered",
                    "checkpoint_id": checkpoint_id,
                    "recovery_id": recovery_id,
                    "outcome": "committed-deferred",
                },
                agent_id,
                session_id,
            )
            return True

        if deferred:
            assert recovery_id is not None
            if self._load_recovery_receipt(agent_id, session_id, recovery_id) is not None:
                raise WorkspaceError(
                    "aborted deferred rollback unexpectedly has a cleanup receipt"
                )
        if memory_rollback and commit:
            self._require_managed_directory(current, "session workspace")
            if transaction["scrub_runtime"]:
                self._scrub_runtime_entries(current / ".allox-tmp")
            index = self._load_index(agent_id, session_id)
            try:
                if index.resolve(checkpoint_id=checkpoint_id) != checkpoint_id:
                    raise WorkspaceError("rollback checkpoint binding mismatch")
                index.move_head(checkpoint_id)
            except CheckpointIndexError as exc:
                raise WorkspaceError(str(exc)) from exc
            self._apply_memory_target(agent_id, session_id, index, checkpoint_id, memory_target)
            self._sync_directory(current.parent)
            self._save_index(agent_id, session_id, index)
            transaction["phase"] = "committed"
            self._write_transaction(agent_id, session_id, transaction)
        if discarded.exists():
            self._require_managed_directory(discarded, "rollback discarded workspace")
            self.backend.delete_subvolume(discarded)
        if restored.exists():
            self._require_managed_directory(restored, "rollback restore workspace")
            self.backend.delete_subvolume(restored)

        if commit and not memory_rollback:
            index = self._load_index(agent_id, session_id)
            try:
                resolved = index.resolve(checkpoint_id=checkpoint_id)
                index.move_head(resolved)
            except CheckpointIndexError as exc:
                raise WorkspaceError(str(exc)) from exc
            self._save_index(agent_id, session_id, index)
        self._clear_transaction(agent_id, session_id)
        self._append_event(
            {
                "op": "session.rollback.recovered",
                "checkpoint_id": checkpoint_id,
                "recovery_id": recovery_id,
                "outcome": "committed" if commit else "aborted",
            },
            agent_id,
            session_id,
        )
        return True

    def relative_current(self, agent_id: str, session_id: str) -> str:
        return self.current(agent_id, session_id).relative_to(self.root).as_posix()

    def relative_agent_workspace(self, agent_id: str) -> str:
        return self.agent_workspace(agent_id).relative_to(self.root).as_posix()

    def relative_agent_shared(self, agent_id: str) -> str:
        return self.agent_shared(agent_id).relative_to(self.root).as_posix()

    def relative_session_workspace(self, agent_id: str, session_id: str) -> str:
        return self.session_dir(agent_id, session_id).relative_to(self.root).as_posix()

    def _event_path(self, agent_id: str | None, session_id: str | None) -> Path:
        base = self.root / ".allox" / "events"
        if agent_id is None:
            return base / "store.jsonl"
        validate_id("agent", agent_id)
        if session_id is None:
            return base / agent_id / "agent.jsonl"
        validate_id("session", session_id)
        return base / agent_id / f"{session_id}.jsonl"

    def _append_event(
        self,
        event: dict[str, Any],
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        target = self._event_path(agent_id, session_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        record = dict(event)
        record["timestamp_ns"] = time.time_ns()
        if agent_id is not None:
            record["agent_id"] = agent_id
        if session_id is not None:
            record["session_id"] = session_id
        with target.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")

    def _thread_lock(self, agent_id: str, session_id: str) -> threading.RLock:
        key = (validate_id("agent", agent_id), validate_id("session", session_id))
        with self._thread_locks_guard:
            return self._thread_locks.setdefault(key, threading.RLock())

    @contextmanager
    def _session_lock(self, agent_id: str, session_id: str) -> Iterator[None]:
        """Serialize mutations in-process and, on Linux, across daemon processes."""
        lock = self._thread_lock(agent_id, session_id)
        with lock:
            lock_path = self.root / ".allox" / "locks" / agent_id / f"{session_id}.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+b") as stream:
                try:
                    import fcntl
                except ImportError:  # pragma: no cover - Windows unit tests
                    yield
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def create_agent(self, agent_id: str, *, origin: str = "unknown") -> dict[str, Any]:
        workspace = self.agent_workspace(agent_id)
        self.agent_shared(agent_id).mkdir(parents=True, exist_ok=True)
        self.sessions_dir(agent_id).mkdir(parents=True, exist_ok=True)
        self._append_event({"op": "agent.create", "origin": origin}, agent_id)
        return {
            "agent_id": agent_id,
            "workspace_level": "agent",
            "relative_workspace": workspace.relative_to(self.root).as_posix(),
            "relative_shared": self.relative_agent_shared(agent_id),
        }

    def create_session(
        self,
        agent_id: str,
        session_id: str,
        *,
        origin: str = "unknown",
    ) -> dict[str, Any]:
        self.create_agent(agent_id, origin=origin)
        with self._session_lock(agent_id, session_id):
            self.require_memory_ready(agent_id, session_id)
            session_dir = self.session_dir(agent_id, session_id)
            current = session_dir / "current"
            if current.exists() or current.is_symlink():
                raise WorkspaceError(f"session already exists: {agent_id}/{session_id}")
            (session_dir / "checkpoints").mkdir(parents=True, exist_ok=True)
            self.backend.create_subvolume(current)
            (current / ".allox-tmp").mkdir(mode=0o700)
            self._save_index(agent_id, session_id, CheckpointIndex())
            self._append_event(
                {"op": "session.create", "origin": origin}, agent_id, session_id
            )
        return self.describe(agent_id, session_id)

    def describe(self, agent_id: str, session_id: str) -> dict[str, Any]:
        with self._session_lock(agent_id, session_id):
            return self._describe_locked(agent_id, session_id)

    def _describe_locked(self, agent_id: str, session_id: str) -> dict[str, Any]:
        current = self.current(agent_id, session_id)
        if not current.exists():
            raise WorkspaceError(f"session does not exist: {agent_id}/{session_id}")
        self._require_managed_directory(current, "session workspace")
        return {
            "agent_id": agent_id,
            "session_id": session_id,
            "workspace_level": "session",
            "relative_agent_workspace": self.relative_agent_workspace(agent_id),
            "relative_agent_shared": self.relative_agent_shared(agent_id),
            "relative_session_workspace": self.relative_session_workspace(
                agent_id, session_id
            ),
            "relative_workspace": self.relative_current(agent_id, session_id),
            "checkpoint_ids": self.list_checkpoints(agent_id, session_id),
            "checkpoint_head": self._load_index(agent_id, session_id).head,
        }

    def create_checkpoint(
        self,
        agent_id: str,
        session_id: str,
        checkpoint_id: str | None = None,
        *,
        origin: str = "unknown",
        message: str | None = None,
        pinned: bool = False,
        metadata: dict[str, Any] | None = None,
        memory_update: tuple[Any, Any] | None = None,
        before_change: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        checkpoint_id = validate_id(
            "checkpoint", checkpoint_id or f"cp-{time.time_ns()}"
        )
        with self._session_lock(agent_id, session_id):
            self.require_memory_ready(agent_id, session_id)
            current = self.current(agent_id, session_id)
            if not current.exists():
                raise WorkspaceError(f"session does not exist: {agent_id}/{session_id}")
            self._require_managed_directory(current, "session workspace")
            target = self.checkpoints(agent_id, session_id) / checkpoint_id
            if target.exists():
                raise WorkspaceError(f"checkpoint already exists: {checkpoint_id}")
            index = self._load_index(agent_id, session_id)
            if memory_update is not None:
                self._stage_memory(agent_id, session_id, index, *memory_update)
            memory_version = index.memory_version
            if memory_version is not None and memory_update is None:
                self._read_memory_version(agent_id, session_id, memory_version)
            try:
                index.add(
                    checkpoint_id,
                    created_at_ns=time.time_ns(),
                    origin=origin,
                    message=message,
                    pinned=pinned,
                    metadata=metadata,
                )
            except CheckpointIndexError as exc:
                raise WorkspaceError(str(exc)) from exc
            if before_change is not None:
                before_change()
            self.backend.snapshot(current, target, readonly=True)
            if memory_version is not None:
                self._sync_directory(target.parent)
            self._save_index(agent_id, session_id, index)
            self._append_event(
                {
                    "op": "checkpoint.create",
                    "checkpoint_id": checkpoint_id,
                    "origin": origin,
                    "message": message,
                    "pinned": pinned,
                    "metadata": metadata,
                    **({"memory_version": memory_version, "memory_revision": index.memory_revision}
                       if memory_version is not None else {}),
                },
                agent_id,
                session_id,
            )
        return {
            "agent_id": agent_id,
            "session_id": session_id,
            "checkpoint_id": checkpoint_id,
            "parent_id": index.checkpoints[checkpoint_id]["parent_id"],
            "metadata": metadata,
            **({"memory_version": memory_version, "memory_revision": index.memory_revision}
               if memory_version is not None else {}),
        }

    def list_checkpoints(self, agent_id: str, session_id: str) -> list[str]:
        return sorted(self._actual_checkpoint_ids(agent_id, session_id))

    def checkpoint_status(self, agent_id: str, session_id: str) -> dict[str, Any]:
        with self._session_lock(agent_id, session_id):
            return self._checkpoint_status_locked(agent_id, session_id)

    def _checkpoint_status_locked(self, agent_id: str, session_id: str) -> dict[str, Any]:
        index = self._load_index(agent_id, session_id)
        return {
            "agent_id": agent_id,
            "session_id": session_id,
            "head": index.head,
            "checkpoint_ids": self.list_checkpoints(agent_id, session_id),
            "checkpoints": index.records(),
        }

    @staticmethod
    def _scrub_runtime_entries(tmp_dir: Path) -> list[str]:
        removed: list[str] = []
        if tmp_dir.is_symlink():
            raise WorkspaceError("cannot scrub a symlinked Session runtime directory")
        if not tmp_dir.exists():
            return removed
        for parent, directories, files in os.walk(tmp_dir):
            for name in [*directories, *files]:
                path = Path(parent) / name
                mode = path.lstat().st_mode
                if stat.S_ISSOCK(mode) or stat.S_ISFIFO(mode):
                    path.unlink()
                    removed.append(str(path.relative_to(tmp_dir)))
        return removed

    def validate_rollback_target(
        self, agent_id: str, session_id: str, checkpoint_id: str | None,
        *, num_ancestors: int | None = None, restore_memory: bool = True,
    ) -> str:
        """Read-only preflight for callers that will terminate runtime first.

        rollback revalidates under its mutation boundary; this check must never
        be used as a substitute for rollback's journal or memory validation.
        """
        if checkpoint_id is not None:
            checkpoint_id = validate_id("checkpoint", checkpoint_id)
        with self._session_lock(agent_id, session_id):
            self.require_memory_ready(agent_id, session_id)
            index = self._load_index(agent_id, session_id)
            try:
                target = index.resolve(checkpoint_id, num_ancestors)
            except CheckpointIndexError as exc:
                raise WorkspaceError(str(exc)) from exc
            self._require_managed_directory(self.checkpoints(agent_id, session_id) / target, "checkpoint")
            self._require_managed_directory(self.current(agent_id, session_id), "session workspace")
            if index.memory_version is not None:
                version = index.checkpoints[target].get("memory_version") if restore_memory else index.memory_version
                if version is None:
                    raise WorkspaceError("checkpoint has no bound memory; select explicit files-only rollback")
                self._read_memory_version(agent_id, session_id, version)
            return target

    def rollback(
        self,
        agent_id: str,
        session_id: str,
        checkpoint_id: str | None = None,
        *,
        num_ancestors: int | None = None,
        scrub_runtime: bool = True,
        origin: str = "unknown",
        recovery_id: str | None = None,
        defer_cleanup: bool = False,
        restore_memory: bool = True,
        before_change: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        if type(restore_memory) is not bool:
            raise WorkspaceError("restore_memory must be a boolean")
        if not isinstance(defer_cleanup, bool):
            raise WorkspaceError("defer_cleanup must be a boolean")
        if checkpoint_id is not None:
            checkpoint_id = validate_id("checkpoint", checkpoint_id)
        if recovery_id is not None:
            if not isinstance(recovery_id, str):
                raise WorkspaceError("recovery_id must be a string")
            recovery_id = validate_id("recovery", recovery_id)
        if defer_cleanup:
            if recovery_id is None:
                raise WorkspaceError(
                    "recovery_id is required when rollback cleanup is deferred"
                )
            if not isinstance(scrub_runtime, bool):
                raise WorkspaceError(
                    "scrub_runtime must be a boolean for deferred rollback"
                )
            if checkpoint_id is None or num_ancestors is not None:
                raise WorkspaceError(
                    "deferred rollback requires an exact checkpoint_id"
                )
        elif recovery_id is not None:
            raise WorkspaceError("recovery_id requires defer_cleanup=True")
        with self._session_lock(agent_id, session_id):
            if self._load_transaction(agent_id, session_id) is not None:
                raise WorkspaceError(
                    f"session has an unfinished rollback transaction: "
                    f"{agent_id}/{session_id}"
                )
            if (
                recovery_id is not None
                and self._load_recovery_receipt(
                    agent_id, session_id, recovery_id
                )
                is not None
            ):
                raise WorkspaceError(f"recovery_id was already used: {recovery_id}")
            index = self._load_index(agent_id, session_id)
            requested_checkpoint_id = checkpoint_id
            try:
                checkpoint_id = index.resolve(checkpoint_id, num_ancestors)
            except CheckpointIndexError as exc:
                raise WorkspaceError(str(exc)) from exc
            if defer_cleanup and checkpoint_id != requested_checkpoint_id:
                raise WorkspaceError(
                    "deferred rollback requires the complete checkpoint_id, not a prefix"
                )
            source = self.checkpoints(agent_id, session_id) / checkpoint_id
            current = self.current(agent_id, session_id)
            if not source.exists():
                raise WorkspaceError(f"checkpoint does not exist: {checkpoint_id}")
            if not current.exists():
                raise WorkspaceError(f"session does not exist: {agent_id}/{session_id}")
            self._require_managed_directory(source, "checkpoint")
            self._require_managed_directory(current, "session workspace")

            memory_target = None
            if index.memory_version is not None:
                version = (index.checkpoints[checkpoint_id].get("memory_version")
                           if restore_memory else index.memory_version)
                if version is None:
                    raise WorkspaceError("checkpoint has no bound memory; select explicit files-only rollback")
                memory_target = {"version": version, "revision": index.memory_revision + 1,
                                 "restore": restore_memory}
                # Validate all memory evidence before changing current or writing a journal.
                self._apply_memory_target(agent_id, session_id, index, checkpoint_id, memory_target)

            token = uuid.uuid4().hex
            restored = current.parent / f".restore-{token}"
            discarded = current.parent / f".discard-{token}"
            transaction = {
                "version": 3 if memory_target is not None else (2 if defer_cleanup else 1),
                "phase": "preparing",
                "checkpoint_id": checkpoint_id,
                "restore_name": restored.name,
                "discard_name": discarded.name,
                "scrub_runtime": scrub_runtime,
                "origin": origin,
            }
            if defer_cleanup:
                transaction["defer_cleanup"] = True
                transaction["recovery_id"] = recovery_id
            if memory_target is not None:
                transaction.update(memory_target=memory_target, defer_cleanup=defer_cleanup)
            if before_change is not None:
                before_change()
            self._write_transaction(agent_id, session_id, transaction)
            try:
                self.backend.snapshot(source, restored, readonly=False)
            except Exception:
                if restored.exists():
                    self.backend.delete_subvolume(restored)
                self._clear_transaction(agent_id, session_id)
                raise
            transaction["phase"] = "prepared"
            self._write_transaction(agent_id, session_id, transaction)
            current.rename(discarded)
            transaction["phase"] = "old_moved"
            self._write_transaction(agent_id, session_id, transaction)
            try:
                restored.rename(current)
            except Exception:
                if memory_target is not None:
                    # Do not guess a commit/abort outcome. Keep the journal and both
                    # versions for fenced recovery before another execution starts.
                    raise
                discarded.rename(current)
                if restored.exists():
                    self.backend.delete_subvolume(restored)
                self._clear_transaction(agent_id, session_id)
                raise
            if defer_cleanup or memory_target is not None:
                removed = (
                    self._scrub_runtime_entries(current / ".allox-tmp")
                    if scrub_runtime
                    else []
                )
                index.move_head(checkpoint_id)
                if memory_target is not None:
                    self._sync_directory(current.parent)
                self._save_index(agent_id, session_id, index)
                transaction["phase"] = "committed"
                transaction["removed_runtime_entries"] = removed
                transaction["committed_at_ns"] = time.time_ns()
                self._write_transaction(agent_id, session_id, transaction)
                if not defer_cleanup:
                    self.backend.delete_subvolume(discarded)
                    self._clear_transaction(agent_id, session_id)
            else:
                transaction["phase"] = "committed"
                self._write_transaction(agent_id, session_id, transaction)
                removed = (
                    self._scrub_runtime_entries(current / ".allox-tmp")
                    if scrub_runtime
                    else []
                )
                self.backend.delete_subvolume(discarded)
                index.move_head(checkpoint_id)
                self._save_index(agent_id, session_id, index)
                self._clear_transaction(agent_id, session_id)
            self._append_event(
                {
                    "op": "session.rollback",
                    "checkpoint_id": checkpoint_id,
                    "origin": origin,
                    "scrub_runtime": scrub_runtime,
                    "removed_runtime_entries": removed,
                    "recovery_id": recovery_id,
                    "deferred_cleanup": defer_cleanup,
                    **({"memory_target": memory_target} if memory_target is not None else {}),
                },
                agent_id,
                session_id,
            )
        return {
            "agent_id": agent_id,
            "session_id": session_id,
            "checkpoint_id": checkpoint_id,
            "relative_workspace": self.relative_current(agent_id, session_id),
            "removed_runtime_entries": removed,
            "recovery_id": recovery_id,
            "deferred_cleanup": defer_cleanup,
            "cleanup_pending": defer_cleanup,
            **({"memory_version": memory_target["version"], "memory_revision": memory_target["revision"],
                "memory_restored": memory_target["restore"]} if memory_target is not None else {}),
            "deferred_discard_relative_path": (
                discarded.relative_to(self.root).as_posix()
                if defer_cleanup
                else None
            ),
        }

    def complete_deferred_rollback(
        self,
        agent_id: str,
        session_id: str,
        recovery_id: str,
    ) -> dict[str, Any]:
        if not isinstance(recovery_id, str):
            raise WorkspaceError("recovery_id must be a string")
        recovery_id = validate_id("recovery", recovery_id)
        with self._session_lock(agent_id, session_id):
            transaction = self._load_transaction(agent_id, session_id)
            if transaction is None:
                receipt = self._load_recovery_receipt(
                    agent_id, session_id, recovery_id
                )
                if receipt is None or receipt.get("status") != "completed":
                    raise WorkspaceError(
                        f"no completed deferred rollback for recovery_id: {recovery_id}"
                    )
                discarded = self._transaction_subvolume_path(
                    self.session_dir(agent_id, session_id),
                    receipt.get("discard_name"),
                    ".discard-",
                )
                if discarded.exists():
                    raise WorkspaceError(
                        "completed deferred rollback still has a discarded workspace"
                    )
                return {
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "checkpoint_id": receipt["checkpoint_id"],
                    "recovery_id": recovery_id,
                    "cleanup_status": "already_completed",
                    "cleanup_completed": True,
                    "already_completed": True,
                    "cleanup_pending": False,
                    "deferred_discard_relative_path": discarded.relative_to(
                        self.root
                    ).as_posix(),
                }

            if (
                transaction.get("version") not in {2, 3}
                or transaction.get("phase") != "committed"
                or transaction.get("defer_cleanup") is not True
            ):
                raise WorkspaceError(
                    "rollback transaction is not a committed deferred rollback"
                )
            transaction_recovery = transaction.get("recovery_id")
            if not isinstance(transaction_recovery, str):
                raise WorkspaceError(
                    "deferred rollback transaction recovery_id must be a string"
                )
            transaction_recovery = validate_id("recovery", transaction_recovery)
            if transaction_recovery != recovery_id:
                raise WorkspaceError("recovery_id does not match deferred rollback")
            checkpoint_value = transaction.get("checkpoint_id")
            if not isinstance(checkpoint_value, str):
                raise WorkspaceError(
                    "deferred rollback transaction checkpoint_id must be a string"
                )
            checkpoint_id = validate_id("checkpoint", checkpoint_value)
            session_dir = self.session_dir(agent_id, session_id)
            current = self.current(agent_id, session_id)
            self._require_managed_directory(current, "session workspace")
            restored = self._transaction_subvolume_path(
                session_dir, transaction.get("restore_name"), ".restore-"
            )
            discarded = self._transaction_subvolume_path(
                session_dir, transaction.get("discard_name"), ".discard-"
            )
            if restored.exists():
                raise WorkspaceError(
                    "committed deferred rollback retains a restore workspace"
                )
            index = self._load_index(agent_id, session_id)
            if index.head != checkpoint_id:
                raise WorkspaceError(
                    "committed deferred rollback does not match the checkpoint head"
                )

            if transaction.get("version") == 3:
                target = transaction.get("memory_target")
                if not isinstance(target, dict) or index.memory_revision != target.get("revision"):
                    raise WorkspaceError("committed rollback memory revision mismatch")
                self._apply_memory_target(agent_id, session_id, index, checkpoint_id, target)

            receipt = self._load_recovery_receipt(
                agent_id, session_id, recovery_id
            )
            if receipt is not None and (
                receipt.get("checkpoint_id") != checkpoint_id
                or receipt.get("discard_name") != discarded.name
            ):
                raise WorkspaceError(
                    "deferred rollback receipt does not match the transaction"
                )
            if receipt is None:
                if not discarded.exists():
                    raise WorkspaceError(
                        "deferred rollback is missing its discarded workspace"
                    )
                self._require_managed_directory(
                    discarded, "deferred rollback discarded workspace"
                )
                receipt = {
                    "version": 1,
                    "status": "cleanup_started",
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "checkpoint_id": checkpoint_id,
                    "recovery_id": recovery_id,
                    "discard_name": discarded.name,
                    "cleanup_started_at_ns": time.time_ns(),
                }
                self._write_recovery_receipt(
                    agent_id, session_id, recovery_id, receipt
                )

            if discarded.exists():
                self._require_managed_directory(
                    discarded, "deferred rollback discarded workspace"
                )
                self.backend.delete_subvolume(discarded)

            completed_receipt = dict(receipt)
            completed_receipt["status"] = "completed"
            completed_receipt.setdefault("completed_at_ns", time.time_ns())
            self._write_recovery_receipt(
                agent_id, session_id, recovery_id, completed_receipt
            )
            self._clear_transaction(agent_id, session_id)
            self._append_event(
                {
                    "op": "session.rollback.cleanup",
                    "checkpoint_id": checkpoint_id,
                    "recovery_id": recovery_id,
                    "outcome": "completed",
                },
                agent_id,
                session_id,
            )

        return {
            "agent_id": agent_id,
            "session_id": session_id,
            "checkpoint_id": checkpoint_id,
            "recovery_id": recovery_id,
            "cleanup_status": "completed",
            "cleanup_completed": True,
            "already_completed": False,
            "cleanup_pending": False,
            "deferred_discard_relative_path": discarded.relative_to(
                self.root
            ).as_posix(),
        }

    def delete_checkpoint(
        self,
        agent_id: str,
        session_id: str,
        checkpoint_id: str,
        *,
        origin: str = "unknown",
        force: bool = False,
        before_change: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        checkpoint_id = validate_id("checkpoint", checkpoint_id)
        with self._session_lock(agent_id, session_id):
            self.require_memory_ready(agent_id, session_id)
            index = self._load_index(agent_id, session_id)
            try:
                checkpoint_id = index.resolve(checkpoint_id=checkpoint_id)
            except CheckpointIndexError as exc:
                raise WorkspaceError(str(exc)) from exc
            if index.checkpoints[checkpoint_id].get("pinned", False) and not force:
                raise WorkspaceError(
                    f"checkpoint is pinned; use force to delete: {checkpoint_id}"
                )
            target = self.checkpoints(agent_id, session_id) / checkpoint_id
            if not target.exists():
                raise WorkspaceError(f"checkpoint does not exist: {checkpoint_id}")
            self._require_managed_directory(target, "checkpoint")
            if before_change is not None:
                before_change()
            self.backend.delete_subvolume(target)
            index.remove(checkpoint_id)
            self._save_index(agent_id, session_id, index)
            self._append_event(
                {
                    "op": "checkpoint.delete",
                    "checkpoint_id": checkpoint_id,
                    "origin": origin,
                },
                agent_id,
                session_id,
            )
        return {
            "agent_id": agent_id,
            "session_id": session_id,
            "checkpoint_id": checkpoint_id,
            "deleted": True,
        }
