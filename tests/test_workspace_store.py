"""Allox 2.0 per-Agent/per-Session workspace tests."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from allox.workspace_store import WorkspaceError, WorkspaceStore


class DirectorySnapshotBackend:
    """Portable test backend with Btrfs-like directory semantics."""

    def assert_root(self, root: Path) -> None:
        assert root.is_dir()

    def create_subvolume(self, path: Path) -> None:
        path.mkdir()

    def snapshot(self, source: Path, target: Path, *, readonly: bool) -> None:
        shutil.copytree(source, target)

    def delete_subvolume(self, path: Path) -> None:
        shutil.rmtree(path)


@pytest.fixture
def store(tmp_path):
    result = WorkspaceStore(tmp_path, DirectorySnapshotBackend())
    result.initialize()
    return result


def test_agent_and_session_paths_are_distinct(store):
    first = store.create_session("agent-a", "session-1")
    second = store.create_session("agent-a", "session-2")
    third = store.create_session("agent-b", "session-1")

    assert first["relative_workspace"] == (
        "agents/agent-a/workspace/sessions/session-1/current"
    )
    assert len(
        {
            first["relative_workspace"],
            second["relative_workspace"],
            third["relative_workspace"],
        }
    ) == 3


def test_checkpoint_rolls_back_only_selected_session_and_tmp(store):
    store.create_session("agent-a", "session-1")
    store.create_session("agent-b", "session-1")
    session_a = store.current("agent-a", "session-1")
    session_b = store.current("agent-b", "session-1")
    (session_a / "state.txt").write_text("v1", encoding="utf-8")
    (session_a / ".allox-tmp" / "temporary.txt").write_text("old", encoding="utf-8")
    (session_b / "state.txt").write_text("agent-b", encoding="utf-8")

    store.create_checkpoint("agent-a", "session-1", "cp1")
    (session_a / "state.txt").write_text("v2", encoding="utf-8")
    (session_a / ".allox-tmp" / "temporary.txt").write_text("new", encoding="utf-8")
    (session_b / "state.txt").write_text("agent-b-new", encoding="utf-8")

    store.rollback("agent-a", "session-1", "cp1")

    assert (session_a / "state.txt").read_text(encoding="utf-8") == "v1"
    assert (session_a / ".allox-tmp" / "temporary.txt").read_text(encoding="utf-8") == "old"
    assert (session_b / "state.txt").read_text(encoding="utf-8") == "agent-b-new"


def test_audit_log_is_outside_rollback_scope(store):
    store.create_session("agent-a", "session-1")
    store.create_checkpoint("agent-a", "session-1", "cp1")
    store.rollback("agent-a", "session-1", "cp1")

    event_path = store.root / ".allox" / "events" / "agent-a" / "session-1.jsonl"
    events = event_path.read_text(encoding="utf-8")
    assert "checkpoint.create" in events
    assert "session.rollback" in events


@pytest.mark.parametrize("bad_id", ["", "../escape", "/absolute", "has space", "x" * 65])
def test_rejects_unsafe_ids(store, bad_id):
    with pytest.raises(WorkspaceError):
        store.create_session(bad_id, "session-1")
