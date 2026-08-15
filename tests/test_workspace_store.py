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


def test_checkpoint_metadata_and_ancestor_rollback(store):
    store.create_session("agent-a", "session-1")
    current = store.current("agent-a", "session-1")
    (current / "state.txt").write_text("v1", encoding="utf-8")
    first = store.create_checkpoint(
        "agent-a",
        "session-1",
        "cp1",
        origin="agent-loop",
        message="before retry",
        pinned=True,
        metadata={"event": "turn_end", "turn": 1},
    )
    (current / "state.txt").write_text("v2", encoding="utf-8")
    second = store.create_checkpoint("agent-a", "session-1", "cp2")
    (current / "state.txt").write_text("v3", encoding="utf-8")

    restored = store.rollback("agent-a", "session-1", num_ancestors=2)
    status = store.checkpoint_status("agent-a", "session-1")

    assert first["parent_id"] is None
    assert second["parent_id"] == "cp1"
    assert restored["checkpoint_id"] == "cp1"
    assert (current / "state.txt").read_text(encoding="utf-8") == "v1"
    assert status["head"] == "cp1"
    assert status["checkpoints"][0]["message"] == "before retry"
    assert status["checkpoints"][0]["pinned"] is True
    assert status["checkpoints"][0]["metadata"] == {"event": "turn_end", "turn": 1}


def test_initialize_finishes_interrupted_rollback(store):
    store.create_session("agent-a", "session-1")
    current = store.current("agent-a", "session-1")
    (current / "state.txt").write_text("v1", encoding="utf-8")
    store.create_checkpoint("agent-a", "session-1", "cp1")
    (current / "state.txt").write_text("v2", encoding="utf-8")
    store.create_checkpoint("agent-a", "session-1", "cp2")

    session_dir = store.session_dir("agent-a", "session-1")
    restored = session_dir / ".restore-crash"
    discarded = session_dir / ".discard-crash"
    store.backend.snapshot(
        store.checkpoints("agent-a", "session-1") / "cp1",
        restored,
        readonly=False,
    )
    current.rename(discarded)
    store._write_transaction(
        "agent-a",
        "session-1",
        {
            "version": 1,
            "phase": "old_moved",
            "checkpoint_id": "cp1",
            "restore_name": restored.name,
            "discard_name": discarded.name,
            "scrub_runtime": True,
            "origin": "test-crash",
        },
    )

    restarted = WorkspaceStore(store.root, store.backend)
    result = restarted.initialize()

    assert result["recovered_sessions"] == ["agent-a/session-1"]
    assert (current / "state.txt").read_text(encoding="utf-8") == "v1"
    assert restarted.checkpoint_status("agent-a", "session-1")["head"] == "cp1"
    assert not restored.exists()
    assert not discarded.exists()
    assert not restarted._transaction_path("agent-a", "session-1").exists()


def test_initialize_aborts_prepared_rollback_before_workspace_swap(store):
    store.create_session("agent-a", "session-1")
    current = store.current("agent-a", "session-1")
    (current / "state.txt").write_text("v1", encoding="utf-8")
    store.create_checkpoint("agent-a", "session-1", "cp1")
    (current / "state.txt").write_text("v2", encoding="utf-8")
    store.create_checkpoint("agent-a", "session-1", "cp2")

    session_dir = store.session_dir("agent-a", "session-1")
    restored = session_dir / ".restore-prepared"
    discarded = session_dir / ".discard-prepared"
    store.backend.snapshot(
        store.checkpoints("agent-a", "session-1") / "cp1",
        restored,
        readonly=False,
    )
    store._write_transaction(
        "agent-a",
        "session-1",
        {
            "version": 1,
            "phase": "prepared",
            "checkpoint_id": "cp1",
            "restore_name": restored.name,
            "discard_name": discarded.name,
            "scrub_runtime": True,
            "origin": "test-crash",
        },
    )

    restarted = WorkspaceStore(store.root, store.backend)
    restarted.initialize()

    assert (current / "state.txt").read_text(encoding="utf-8") == "v2"
    assert restarted.checkpoint_status("agent-a", "session-1")["head"] == "cp2"
    assert not restored.exists()


def test_pinned_checkpoint_requires_force_to_delete(store):
    store.create_session("agent-a", "session-1")
    store.create_checkpoint("agent-a", "session-1", "cp1", pinned=True)

    with pytest.raises(WorkspaceError, match="pinned"):
        store.delete_checkpoint("agent-a", "session-1", "cp1")

    result = store.delete_checkpoint("agent-a", "session-1", "cp1", force=True)
    assert result["deleted"] is True


@pytest.mark.parametrize("bad_id", ["", "../escape", "/absolute", "has space", "x" * 65])
def test_rejects_unsafe_ids(store, bad_id):
    with pytest.raises(WorkspaceError):
        store.create_session(bad_id, "session-1")
