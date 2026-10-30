"""Two-level Agent/Session workspace tests."""

from __future__ import annotations

import json
import shutil
import stat
from pathlib import Path

import pytest

from allox.workspace.store import BtrfsBackend, WorkspaceError, WorkspaceStore


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
    assert first["relative_agent_workspace"] == "agents/agent-a/workspace"
    assert first["relative_agent_shared"] == "agents/agent-a/workspace/shared"
    assert first["relative_session_workspace"] == (
        "agents/agent-a/workspace/sessions/session-1"
    )
    assert first["workspace_level"] == "session"
    assert len(
        {
            first["relative_workspace"],
            second["relative_workspace"],
            third["relative_workspace"],
        }
    ) == 3


def test_agent_workspace_is_first_level_and_owns_shared_area(store):
    result = store.create_agent("agent-a")

    assert result == {
        "agent_id": "agent-a",
        "workspace_level": "agent",
        "relative_workspace": "agents/agent-a/workspace",
        "relative_shared": "agents/agent-a/workspace/shared",
    }
    assert store.agent_shared("agent-a").is_dir()
    assert store.sessions_dir("agent-a").is_dir()


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


def _deferred_rollback(
    store: WorkspaceStore, *, recovery_id: str = "recovery-1"
) -> tuple[dict[str, object], Path]:
    store.create_session("agent-a", "session-1")
    current = store.current("agent-a", "session-1")
    (current / "state.txt").write_text("before", encoding="utf-8")
    store.create_checkpoint("agent-a", "session-1", "checkpoint-exact")
    (current / "state.txt").write_text("after", encoding="utf-8")

    result = store.rollback(
        "agent-a",
        "session-1",
        "checkpoint-exact",
        recovery_id=recovery_id,
        defer_cleanup=True,
    )
    discarded = store.root / str(result["deferred_discard_relative_path"])
    return result, discarded


def test_deferred_rollback_retains_discard_until_exact_id_cleanup(store):
    result, discarded = _deferred_rollback(store)
    transaction_path = store._transaction_path("agent-a", "session-1")
    transaction = json.loads(transaction_path.read_text(encoding="utf-8"))

    assert result["recovery_id"] == "recovery-1"
    assert result["deferred_cleanup"] is True
    assert result["cleanup_pending"] is True
    assert transaction["version"] == 2
    assert transaction["phase"] == "committed"
    assert transaction["recovery_id"] == "recovery-1"
    assert (store.current("agent-a", "session-1") / "state.txt").read_text(
        encoding="utf-8"
    ) == "before"
    assert (discarded / "state.txt").read_text(encoding="utf-8") == "after"
    assert store.checkpoint_status("agent-a", "session-1")["head"] == (
        "checkpoint-exact"
    )

    with pytest.raises(WorkspaceError, match="does not match"):
        store.complete_deferred_rollback(
            "agent-a", "session-1", "different-recovery"
        )
    assert discarded.exists()
    assert transaction_path.exists()

    completed = store.complete_deferred_rollback(
        "agent-a", "session-1", "recovery-1"
    )
    receipt_path = store._recovery_receipt_path(
        "agent-a", "session-1", "recovery-1"
    )

    assert completed["cleanup_status"] == "completed"
    assert completed["cleanup_completed"] is True
    assert completed["already_completed"] is False
    assert completed["cleanup_pending"] is False
    assert not discarded.exists()
    assert not transaction_path.exists()
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(receipt_path.parent.stat().st_mode) == 0o700

    repeated = store.complete_deferred_rollback(
        "agent-a", "session-1", "recovery-1"
    )
    assert repeated["cleanup_status"] == "already_completed"
    assert repeated["already_completed"] is True

    with pytest.raises(WorkspaceError, match="no completed deferred rollback"):
        store.complete_deferred_rollback("agent-a", "session-1", "unknown-recovery")


def test_deferred_rollback_requires_unused_recovery_id_and_exact_checkpoint(store):
    store.create_session("agent-a", "session-1")
    store.create_checkpoint("agent-a", "session-1", "checkpoint-exact")

    with pytest.raises(WorkspaceError, match="recovery_id is required"):
        store.rollback(
            "agent-a",
            "session-1",
            "checkpoint-exact",
            defer_cleanup=True,
        )
    with pytest.raises(WorkspaceError, match="requires defer_cleanup"):
        store.rollback(
            "agent-a",
            "session-1",
            "checkpoint-exact",
            recovery_id="recovery-1",
        )
    with pytest.raises(WorkspaceError, match="complete checkpoint_id"):
        store.rollback(
            "agent-a",
            "session-1",
            "checkpoint",
            recovery_id="recovery-1",
            defer_cleanup=True,
        )

    store.rollback(
        "agent-a",
        "session-1",
        "checkpoint-exact",
        recovery_id="recovery-1",
        defer_cleanup=True,
    )
    store.complete_deferred_rollback(
        "agent-a", "session-1", "recovery-1"
    )
    with pytest.raises(WorkspaceError, match="already used"):
        store.rollback(
            "agent-a",
            "session-1",
            "checkpoint-exact",
            recovery_id="recovery-1",
            defer_cleanup=True,
        )


def test_initialize_preserves_committed_deferred_rollback(store):
    result, discarded = _deferred_rollback(store)
    transaction_path = store._transaction_path("agent-a", "session-1")

    restarted = WorkspaceStore(store.root, store.backend)
    initialized = restarted.initialize()

    assert initialized["recovered_sessions"] == []
    assert transaction_path.exists()
    assert discarded.exists()
    assert (discarded / "state.txt").read_text(encoding="utf-8") == "after"
    assert result["cleanup_pending"] is True

    completed = restarted.complete_deferred_rollback(
        "agent-a", "session-1", "recovery-1"
    )
    assert completed["cleanup_status"] == "completed"
    assert not discarded.exists()


def test_deferred_cleanup_rejects_discard_path_outside_session(store):
    _, discarded = _deferred_rollback(store)
    transaction_path = store._transaction_path("agent-a", "session-1")
    transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
    outside = store.root / "outside"
    outside.mkdir()
    link = store.session_dir("agent-a", "session-1") / ".discard-outside"
    link.symlink_to(outside, target_is_directory=True)
    transaction["discard_name"] = link.name
    store._write_transaction("agent-a", "session-1", transaction)

    with pytest.raises(WorkspaceError, match="escapes the session"):
        store.complete_deferred_rollback(
            "agent-a", "session-1", "recovery-1"
        )

    assert outside.exists()
    assert discarded.exists()


def test_initialize_finishes_interrupted_deferred_swap_without_cleanup(store):
    store.create_session("agent-a", "session-1")
    current = store.current("agent-a", "session-1")
    (current / "state.txt").write_text("before", encoding="utf-8")
    store.create_checkpoint("agent-a", "session-1", "checkpoint-exact")
    (current / "state.txt").write_text("after", encoding="utf-8")

    session_dir = store.session_dir("agent-a", "session-1")
    restored = session_dir / ".restore-deferred-crash"
    discarded = session_dir / ".discard-deferred-crash"
    store.backend.snapshot(
        store.checkpoints("agent-a", "session-1") / "checkpoint-exact",
        restored,
        readonly=False,
    )
    current.rename(discarded)
    store._write_transaction(
        "agent-a",
        "session-1",
        {
            "version": 2,
            "phase": "old_moved",
            "checkpoint_id": "checkpoint-exact",
            "restore_name": restored.name,
            "discard_name": discarded.name,
            "scrub_runtime": True,
            "origin": "test-crash",
            "defer_cleanup": True,
            "recovery_id": "recovery-1",
        },
    )

    restarted = WorkspaceStore(store.root, store.backend)
    initialized = restarted.initialize()
    transaction = json.loads(
        restarted._transaction_path("agent-a", "session-1").read_text(
            encoding="utf-8"
        )
    )

    assert initialized["recovered_sessions"] == ["agent-a/session-1"]
    assert (current / "state.txt").read_text(encoding="utf-8") == "before"
    assert (discarded / "state.txt").read_text(encoding="utf-8") == "after"
    assert transaction["phase"] == "committed"
    assert transaction["recovery_id"] == "recovery-1"
    assert restarted.checkpoint_status("agent-a", "session-1")["head"] == (
        "checkpoint-exact"
    )


def test_initialize_finishes_authorized_cleanup_after_delete_crash(
    store, monkeypatch
):
    _, discarded = _deferred_rollback(store)
    delete_subvolume = store.backend.delete_subvolume

    def delete_then_crash(path: Path) -> None:
        delete_subvolume(path)
        raise RuntimeError("simulated crash after delete")

    monkeypatch.setattr(store.backend, "delete_subvolume", delete_then_crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        store.complete_deferred_rollback(
            "agent-a", "session-1", "recovery-1"
        )

    assert not discarded.exists()
    receipt_path = store._recovery_receipt_path(
        "agent-a", "session-1", "recovery-1"
    )
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["status"] == (
        "cleanup_started"
    )
    monkeypatch.setattr(store.backend, "delete_subvolume", delete_subvolume)

    restarted = WorkspaceStore(store.root, store.backend)
    initialized = restarted.initialize()

    assert initialized["recovered_sessions"] == ["agent-a/session-1"]
    assert not restarted._transaction_path("agent-a", "session-1").exists()
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["status"] == "completed"
    repeated = restarted.complete_deferred_rollback(
        "agent-a", "session-1", "recovery-1"
    )
    assert repeated["cleanup_status"] == "already_completed"


def test_btrfs_backend_deletes_only_deferred_discard(store, monkeypatch):
    _, discarded = _deferred_rollback(store)
    commands: list[list[str]] = []
    backend = BtrfsBackend()

    def run(argv: list[str], *, capture: bool = False) -> str:
        assert capture is False
        commands.append(argv)
        shutil.rmtree(Path(argv[-1]))
        return ""

    monkeypatch.setattr(backend, "_run", run)
    store.backend = backend

    completed = store.complete_deferred_rollback(
        "agent-a", "session-1", "recovery-1"
    )

    assert completed["cleanup_status"] == "completed"
    assert commands == [
        ["btrfs", "subvolume", "delete", str(discarded)],
    ]


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
