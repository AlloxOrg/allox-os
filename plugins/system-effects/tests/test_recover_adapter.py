"""Tests for the fixed Allox protocol-v3 workspace recovery helper."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import allox_system_effects.recover as recover_adapter
import pytest

from allox.workspace.store import WorkspaceStore


class DirectoryBackend:
    """Portable snapshot backend exercising the real WorkspaceStore logic."""

    def assert_root(self, root: Path) -> None:
        assert root.is_dir()

    def create_subvolume(self, path: Path) -> None:
        path.mkdir()

    def snapshot(self, source: Path, target: Path, *, readonly: bool) -> None:
        shutil.copytree(source, target)

    def delete_subvolume(self, path: Path) -> None:
        shutil.rmtree(path)


@pytest.fixture
def recovery_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> WorkspaceStore:
    backend = DirectoryBackend()
    store = WorkspaceStore(tmp_path, backend)
    store.initialize()
    store.create_session("agent-a", "session-1")
    current = store.current("agent-a", "session-1")
    (current / "state.txt").write_text("before", encoding="utf-8")
    store.create_checkpoint("agent-a", "session-1", "checkpoint-exact")
    (current / "state.txt").write_text("after", encoding="utf-8")

    def open_store(root: Path) -> WorkspaceStore:
        assert root == store.root
        return WorkspaceStore(root, backend)

    monkeypatch.setattr(recover_adapter, "WorkspaceStore", open_store)
    return store


def _argv(
    store: WorkspaceStore,
    *,
    checkpoint_id: str = "checkpoint-exact",
    recovery_id: str = "recovery-1",
    scrub_runtime: str = "true",
) -> list[str]:
    return [
        "--store-root",
        str(store.root),
        "--agent-id",
        "agent-a",
        "--session-id",
        "session-1",
        "--checkpoint-id",
        checkpoint_id,
        "--recovery-id",
        recovery_id,
        "--scrub-runtime",
        scrub_runtime,
    ]


def test_main_restores_and_retains_deferred_cleanup(
    recovery_workspace: WorkspaceStore,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = recovery_workspace

    assert recover_adapter.main(_argv(store)) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    transaction_path = store._transaction_path("agent-a", "session-1")
    transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
    discarded = store.root / result["deferred_discard_relative_path"]

    assert captured.err == ""
    assert captured.out == json.dumps(
        result, sort_keys=True, separators=(",", ":")
    ) + "\n"
    assert result["checkpoint_id"] == "checkpoint-exact"
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

    cleanup = store.complete_deferred_rollback(
        "agent-a", "session-1", "recovery-1"
    )

    assert cleanup["cleanup_status"] == "completed"
    assert cleanup["cleanup_completed"] is True
    assert cleanup["already_completed"] is False
    assert not discarded.exists()
    assert not transaction_path.exists()


def test_main_rejects_checkpoint_prefix(
    recovery_workspace: WorkspaceStore,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = recovery_workspace

    with pytest.raises(SystemExit, match="exact recovery checkpoint does not exist"):
        recover_adapter.main(_argv(store, checkpoint_id="checkpoint"))

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert (store.current("agent-a", "session-1") / "state.txt").read_text(
        encoding="utf-8"
    ) == "after"
    assert not store._transaction_path("agent-a", "session-1").exists()


@pytest.mark.parametrize("value", ["True", "1", "yes", "FALSE"])
def test_main_rejects_noncanonical_boolean(
    recovery_workspace: WorkspaceStore,
    capsys: pytest.CaptureFixture[str],
    value: str,
) -> None:
    store = recovery_workspace

    with pytest.raises(SystemExit) as raised:
        recover_adapter.main(_argv(store, scrub_runtime=value))

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert captured.out == ""
    assert "argument --scrub-runtime: expected true or false" in captured.err
    assert (store.current("agent-a", "session-1") / "state.txt").read_text(
        encoding="utf-8"
    ) == "after"
    assert not store._transaction_path("agent-a", "session-1").exists()


def test_main_rejects_reused_recovery_id_after_cleanup(
    recovery_workspace: WorkspaceStore,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = recovery_workspace
    assert recover_adapter.main(_argv(store)) == 0
    capsys.readouterr()
    store.complete_deferred_rollback("agent-a", "session-1", "recovery-1")

    with pytest.raises(SystemExit, match="recovery_id was already used"):
        recover_adapter.main(_argv(store))

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert not store._transaction_path("agent-a", "session-1").exists()
    repeated_cleanup = store.complete_deferred_rollback(
        "agent-a", "session-1", "recovery-1"
    )
    assert repeated_cleanup["cleanup_status"] == "already_completed"
