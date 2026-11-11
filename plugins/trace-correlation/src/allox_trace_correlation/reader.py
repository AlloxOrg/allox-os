"""Bounded evidence access; caller-supplied paths never enter the reader."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from allox.workspace.store import WorkspaceError

MAX_JSON = 2 * 1024 * 1024
MAX_STREAM = 64 * 1024 * 1024
MAX_LINE = 128 * 1024
MAX_SPANS = 10000


class EvidenceReader:
    def __init__(self, root: Path):
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise WorkspaceError("evidence root must be an existing directory")

    @contextmanager
    def open(self, parts: tuple[str, ...], limit: int):
        if not parts or any(not p or p in {".", ".."} or "/" in p or "\0" in p for p in parts):
            raise WorkspaceError("unsafe evidence path component")
        descriptors = []
        try:
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(fd)
            for part in parts[:-1]:
                fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                descriptors.append(fd)
            target = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(target, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                    raise WorkspaceError("evidence file is unsafe or exceeds its size limit")
                yield stream
                after = os.fstat(stream.fileno())
                if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
                ):
                    raise WorkspaceError("evidence changed during query; retry after finalization")
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def read(self, *parts: str) -> bytes:
        with self.open(parts, MAX_JSON) as stream:
            data = stream.read(MAX_JSON + 1)
        if len(data) > MAX_JSON:
            raise WorkspaceError("JSON evidence exceeds size limit")
        return data

    def json(self, *parts: str) -> dict[str, Any]:
        try:
            value = json.loads(self.read(*parts))
        except (ValueError, RecursionError) as exc:
            raise WorkspaceError("invalid JSON evidence") from exc
        if not isinstance(value, dict):
            raise WorkspaceError("JSON evidence must be an object")
        return value


def read_spans(root: Path, agent: str, session: str, trace: str | None) -> list[dict]:
    db = root.resolve(strict=True) / "traces.sqlite3"
    for path in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm")):
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise WorkspaceError("unsafe trace database path")
    if not db.is_file():
        raise WorkspaceError("trace database is unavailable")
    rows = []
    # mode=ro respects a live WAL. immutable=1 would silently ignore recent WAL data.
    try:
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=2)) as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.set_progress_handler(lambda: 1, 2_000_000)
            sql = "SELECT trace,span,substr(payload,1,?) FROM spans WHERE agent=? AND session=?"
            args: tuple = (MAX_LINE + 1, agent, session)
            if trace is not None:
                sql += " AND trace=?"
                args += (trace,)
            cursor = connection.execute(sql + " ORDER BY trace,span LIMIT ?", (*args, MAX_SPANS + 1))
            for trace_id, span_id, payload in cursor:
                if len(rows) == MAX_SPANS or not isinstance(payload, str) or len(payload) > MAX_LINE:
                    raise WorkspaceError("trace query exceeds its bounded limit")
                row = json.loads(payload)
                if not isinstance(row, dict) or (
                    row.get("agent_id"), row.get("session_id"), row.get("trace_id"), row.get("span_id")
                ) != (agent, session, trace_id, span_id):
                    raise WorkspaceError("trace row identity mismatch")
                rows.append(row)
    except (sqlite3.Error, ValueError, RecursionError) as exc:
        raise WorkspaceError("cannot read trace records") from exc
    return rows
