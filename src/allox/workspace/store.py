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
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

from allox.workspace.checkpoint_index import CheckpointIndex, CheckpointIndexError

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
        argv = ["btrfs", "subvolume", "snapshot"]
        if readonly:
            argv.append("-r")
        argv.extend([str(source), str(target)])
        self._run(argv)

    def delete_subvolume(self, path: Path) -> None:
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
                json.dump(value, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            if os.name == "posix":
                descriptor = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)

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
        self._atomic_write_json(
            self._transaction_path(agent_id, session_id), transaction
        )

    def _clear_transaction(self, agent_id: str, session_id: str) -> None:
        self._transaction_path(agent_id, session_id).unlink(missing_ok=True)

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
        if version not in {1, 2}:
            raise WorkspaceError("unsupported rollback transaction version")
        deferred = version == 2
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
            elif discarded.exists():
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
        if discarded.exists():
            self._require_managed_directory(discarded, "rollback discarded workspace")
            self.backend.delete_subvolume(discarded)
        if restored.exists():
            self._require_managed_directory(restored, "rollback restore workspace")
            self.backend.delete_subvolume(restored)

        if commit:
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
    ) -> dict[str, Any]:
        checkpoint_id = validate_id(
            "checkpoint", checkpoint_id or f"cp-{time.time_ns()}"
        )
        with self._session_lock(agent_id, session_id):
            current = self.current(agent_id, session_id)
            if not current.exists():
                raise WorkspaceError(f"session does not exist: {agent_id}/{session_id}")
            self._require_managed_directory(current, "session workspace")
            target = self.checkpoints(agent_id, session_id) / checkpoint_id
            if target.exists():
                raise WorkspaceError(f"checkpoint already exists: {checkpoint_id}")
            index = self._load_index(agent_id, session_id)
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
            self.backend.snapshot(current, target, readonly=True)
            self._save_index(agent_id, session_id, index)
            self._append_event(
                {
                    "op": "checkpoint.create",
                    "checkpoint_id": checkpoint_id,
                    "origin": origin,
                    "message": message,
                    "pinned": pinned,
                    "metadata": metadata,
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
        }

    def list_checkpoints(self, agent_id: str, session_id: str) -> list[str]:
        return sorted(self._actual_checkpoint_ids(agent_id, session_id))

    def checkpoint_status(self, agent_id: str, session_id: str) -> dict[str, Any]:
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
    ) -> dict[str, Any]:
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

            token = uuid.uuid4().hex
            restored = current.parent / f".restore-{token}"
            discarded = current.parent / f".discard-{token}"
            transaction = {
                "version": 2 if defer_cleanup else 1,
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
                discarded.rename(current)
                if restored.exists():
                    self.backend.delete_subvolume(restored)
                self._clear_transaction(agent_id, session_id)
                raise
            if defer_cleanup:
                removed = (
                    self._scrub_runtime_entries(current / ".allox-tmp")
                    if scrub_runtime
                    else []
                )
                index.move_head(checkpoint_id)
                self._save_index(agent_id, session_id, index)
                transaction["phase"] = "committed"
                transaction["removed_runtime_entries"] = removed
                transaction["committed_at_ns"] = time.time_ns()
                self._write_transaction(agent_id, session_id, transaction)
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
                transaction.get("version") != 2
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
    ) -> dict[str, Any]:
        checkpoint_id = validate_id("checkpoint", checkpoint_id)
        with self._session_lock(agent_id, session_id):
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
