"""Trusted Agent/Session workspace storage for Allox 2.0.

The store lives outside the Allox VM's agent trust boundary.  Each Session's
``current`` directory is a writable Btrfs subvolume; checkpoints are read-only
snapshots of that subvolume.  Rollback replaces only the selected Session's
``current`` subvolume and never restores the whole Allox VM.
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
    """Hierarchical ``Allox VM -> Agent -> Session`` workspace manager."""

    def __init__(self, root: Path | str, backend: SnapshotBackend | None = None):
        self.root = Path(root).resolve()
        self.backend = backend or BtrfsBackend()
        self._thread_locks: dict[tuple[str, str], threading.RLock] = {}
        self._thread_locks_guard = threading.Lock()

    def initialize(self) -> dict[str, Any]:
        self.backend.assert_root(self.root)
        (self.root / "agents").mkdir(parents=True, exist_ok=True)
        (self.root / ".allox" / "events").mkdir(parents=True, exist_ok=True)
        (self.root / ".allox" / "locks").mkdir(parents=True, exist_ok=True)
        self._append_event({"op": "store.initialize"})
        return {"root": str(self.root)}

    def agent_dir(self, agent_id: str) -> Path:
        return self.root / "agents" / validate_id("agent", agent_id)

    def session_dir(self, agent_id: str, session_id: str) -> Path:
        validate_id("session", session_id)
        return self.agent_dir(agent_id) / "workspace" / "sessions" / session_id

    def current(self, agent_id: str, session_id: str) -> Path:
        return self.session_dir(agent_id, session_id) / "current"

    def checkpoints(self, agent_id: str, session_id: str) -> Path:
        return self.session_dir(agent_id, session_id) / "checkpoints"

    def relative_current(self, agent_id: str, session_id: str) -> str:
        return self.current(agent_id, session_id).relative_to(self.root).as_posix()

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
        target = self.agent_dir(agent_id)
        (target / "workspace" / "sessions").mkdir(parents=True, exist_ok=True)
        self._append_event({"op": "agent.create", "origin": origin}, agent_id)
        return {"agent_id": agent_id}

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
            if current.exists():
                raise WorkspaceError(f"session already exists: {agent_id}/{session_id}")
            (session_dir / "checkpoints").mkdir(parents=True, exist_ok=True)
            self.backend.create_subvolume(current)
            (current / ".allox-tmp").mkdir(mode=0o700)
            self._append_event(
                {"op": "session.create", "origin": origin}, agent_id, session_id
            )
        return self.describe(agent_id, session_id)

    def describe(self, agent_id: str, session_id: str) -> dict[str, Any]:
        current = self.current(agent_id, session_id)
        if not current.exists():
            raise WorkspaceError(f"session does not exist: {agent_id}/{session_id}")
        return {
            "agent_id": agent_id,
            "session_id": session_id,
            "relative_workspace": self.relative_current(agent_id, session_id),
            "checkpoint_ids": self.list_checkpoints(agent_id, session_id),
        }

    def create_checkpoint(
        self,
        agent_id: str,
        session_id: str,
        checkpoint_id: str | None = None,
        *,
        origin: str = "unknown",
    ) -> dict[str, Any]:
        checkpoint_id = validate_id(
            "checkpoint", checkpoint_id or f"cp-{time.time_ns()}"
        )
        with self._session_lock(agent_id, session_id):
            current = self.current(agent_id, session_id)
            if not current.exists():
                raise WorkspaceError(f"session does not exist: {agent_id}/{session_id}")
            target = self.checkpoints(agent_id, session_id) / checkpoint_id
            if target.exists():
                raise WorkspaceError(f"checkpoint already exists: {checkpoint_id}")
            self.backend.snapshot(current, target, readonly=True)
            self._append_event(
                {
                    "op": "checkpoint.create",
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
        }

    def list_checkpoints(self, agent_id: str, session_id: str) -> list[str]:
        target = self.checkpoints(agent_id, session_id)
        if not target.exists():
            return []
        return sorted(path.name for path in target.iterdir() if path.is_dir())

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
        checkpoint_id: str,
        *,
        scrub_runtime: bool = True,
        origin: str = "unknown",
    ) -> dict[str, Any]:
        checkpoint_id = validate_id("checkpoint", checkpoint_id)
        with self._session_lock(agent_id, session_id):
            source = self.checkpoints(agent_id, session_id) / checkpoint_id
            current = self.current(agent_id, session_id)
            if not source.exists():
                raise WorkspaceError(f"checkpoint does not exist: {checkpoint_id}")
            if not current.exists():
                raise WorkspaceError(f"session does not exist: {agent_id}/{session_id}")

            token = uuid.uuid4().hex
            restored = current.parent / f".restore-{token}"
            discarded = current.parent / f".discard-{token}"
            self.backend.snapshot(source, restored, readonly=False)
            current.rename(discarded)
            try:
                restored.rename(current)
            except Exception:
                discarded.rename(current)
                if restored.exists():
                    self.backend.delete_subvolume(restored)
                raise

            removed = (
                self._scrub_runtime_entries(current / ".allox-tmp")
                if scrub_runtime
                else []
            )
            self.backend.delete_subvolume(discarded)
            self._append_event(
                {
                    "op": "session.rollback",
                    "checkpoint_id": checkpoint_id,
                    "origin": origin,
                    "scrub_runtime": scrub_runtime,
                    "removed_runtime_entries": removed,
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
        }

    def delete_checkpoint(
        self,
        agent_id: str,
        session_id: str,
        checkpoint_id: str,
        *,
        origin: str = "unknown",
    ) -> dict[str, Any]:
        checkpoint_id = validate_id("checkpoint", checkpoint_id)
        with self._session_lock(agent_id, session_id):
            target = self.checkpoints(agent_id, session_id) / checkpoint_id
            if not target.exists():
                raise WorkspaceError(f"checkpoint does not exist: {checkpoint_id}")
            self.backend.delete_subvolume(target)
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
