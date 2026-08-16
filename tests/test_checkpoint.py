"""Checkpoint command and policy tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from allox.checkpoint import checkpoint_after_success, latest_ready_snapshot
from allox.context import ClientContext


def _snapshot(snapshot_id: str, sandbox_id: str, state: str, minute: int = 0):
    return SimpleNamespace(
        id=snapshot_id, sandbox_id=sandbox_id, name=snapshot_id,
        status=SimpleNamespace(state=state, reason=None, message=None),
        created_at=datetime(2026, 1, 1, 0, minute, tzinfo=timezone.utc),
    )


def _page(items):
    return SimpleNamespace(snapshot_infos=items, pagination=SimpleNamespace(has_next_page=False))


def test_checkpoint_create_uses_current_sandbox(runner):
    manager = MagicMock()
    manager.create_snapshot.return_value = _snapshot("snap-1", "sbx-1", "ready")
    session = SimpleNamespace(sandbox_id="sbx-1", aio_url="", created_at="now")
    with (
        patch("allox.context.ClientContext.get_manager", return_value=manager),
        patch("allox.context.get_current_session", return_value=session),
    ):
        result = runner(["checkpoint", "create", "--name", "good", "-o", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["id"] == "snap-1"
    manager.create_snapshot.assert_called_once_with("sbx-1", "good")


def test_latest_ready_ignores_newer_failed_snapshot(tmp_path):
    obj = ClientContext({"color": False}, tmp_path / "config.toml")
    manager = MagicMock()
    manager.list_snapshots.return_value = _page([
        _snapshot("snap-ready", "sbx-1", "ready", 1),
        _snapshot("snap-failed", "sbx-1", "failed", 2),
    ])
    obj._manager = manager
    assert latest_ready_snapshot(obj, "sbx-1").id == "snap-ready"


def test_restore_specific_checkpoint_updates_current_session(runner):
    manager = MagicMock()
    manager.get_snapshot.return_value = _snapshot("snap-1", "sbx-old", "ready")
    restored = MagicMock(id="sbx-new")
    restored.get_endpoint.return_value = SimpleNamespace(endpoint="127.0.0.1:4567")
    with (
        patch("allox.context.ClientContext.get_manager", return_value=manager),
        patch("allox.commands.checkpoint_cmd.SandboxSync.create", return_value=restored) as create,
        patch("allox.commands.checkpoint_cmd.set_current_session") as set_session,
    ):
        result = runner(["checkpoint", "restore", "snap-1", "-o", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["id"] == "sbx-new"
    assert create.call_args.kwargs["snapshot_id"] == "snap-1"
    assert create.call_args.kwargs["entrypoint"] == ["/opt/gem/run.sh"]
    set_session.assert_called_once_with("sbx-new", "http://127.0.0.1:4567")
    restored.close.assert_called_once()


def test_auto_checkpoint_only_for_configured_operation(tmp_path):
    obj = ClientContext({
        "color": False, "checkpoint_enabled": True, "checkpoint_on_success": True,
        "checkpoint_operations": ["run"], "checkpoint_strict": False,
    }, tmp_path / "config.toml")
    manager = MagicMock()
    manager.create_snapshot.return_value = _snapshot("snap-auto", "sbx-1", "ready")
    obj._manager = manager
    assert checkpoint_after_success(obj, "sbx-1", "file.write") is None
    assert checkpoint_after_success(obj, "sbx-1", "run").id == "snap-auto"
    manager.create_snapshot.assert_called_once()


def test_create_checkpoint_waits_until_ready(tmp_path):
    from allox.checkpoint import create_checkpoint

    obj = ClientContext({"color": False, "checkpoint_create_timeout": 5}, tmp_path / "config.toml")
    manager = MagicMock()
    manager.create_snapshot.return_value = _snapshot("snap-wait", "sbx-1", "creating")
    manager.get_snapshot.return_value = _snapshot("snap-wait", "sbx-1", "ready")
    obj._manager = manager
    assert create_checkpoint(obj, "sbx-1", name="wait").status.state == "ready"
    manager.get_snapshot.assert_called_once_with("snap-wait")
