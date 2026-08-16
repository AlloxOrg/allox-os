"""Unit tests for sandbox manager command/SDK compatibility."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import create_autospec, patch

from opensandbox.models.sandboxes import SandboxImageSpec
from opensandbox.sync.manager import SandboxManagerSync


def _sandbox_info(sandbox_id: str, state: str = "running") -> SimpleNamespace:
    return SimpleNamespace(
        id=sandbox_id,
        status=SimpleNamespace(state=state),
        image=SandboxImageSpec("example/image:latest"),
    )


def _page(items, *, page: int, has_next_page: bool) -> SimpleNamespace:
    return SimpleNamespace(
        sandbox_infos=items,
        pagination=SimpleNamespace(page=page, has_next_page=has_next_page),
    )


def test_sandbox_list_uses_current_sdk_and_reads_all_pages(runner):
    manager = create_autospec(SandboxManagerSync, instance=True)
    manager.list_sandbox_infos.side_effect = [
        _page([_sandbox_info("sbx-1")], page=1, has_next_page=True),
        _page([_sandbox_info("sbx-2", "stopped")], page=2, has_next_page=False),
    ]

    with patch("allox.cli.context.ClientContext.get_manager", return_value=manager):
        result = runner(["sandbox", "list", "-o", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == [
        {"id": "sbx-1", "state": "running"},
        {"id": "sbx-2", "state": "stopped"},
    ]
    assert [call.args[0].page for call in manager.list_sandbox_infos.call_args_list] == [1, 2]


def test_sandbox_get_uses_current_sdk_and_serializes_image(runner):
    manager = create_autospec(SandboxManagerSync, instance=True)
    manager.get_sandbox_info.return_value = _sandbox_info("sbx-get")

    with patch("allox.cli.context.ClientContext.get_manager", return_value=manager):
        result = runner(["sandbox", "get", "sbx-get", "-o", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "id": "sbx-get",
        "state": "running",
        "image": "example/image:latest",
    }
    manager.get_sandbox_info.assert_called_once_with("sbx-get")


def test_sandbox_kill_uses_current_sdk(runner):
    manager = create_autospec(SandboxManagerSync, instance=True)

    with (
        patch("allox.cli.context.ClientContext.get_manager", return_value=manager),
        patch("allox.cli.commands.vm.get_current_session", return_value=None),
    ):
        result = runner(["sandbox", "kill", "sbx-kill", "-o", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"id": "sbx-kill", "status": "killed"}
    manager.kill_sandbox.assert_called_once_with("sbx-kill")


def test_sandbox_pause_and_resume_use_current_sdk(runner):
    manager = create_autospec(SandboxManagerSync, instance=True)
    with patch("allox.cli.context.ClientContext.get_manager", return_value=manager):
        paused = runner(["sandbox", "pause", "sbx-state", "-o", "json"])
        resumed = runner(["sandbox", "resume", "sbx-state", "-o", "json"])
    assert paused.exit_code == 0, paused.output
    assert resumed.exit_code == 0, resumed.output
    assert json.loads(paused.output) == {"id": "sbx-state", "status": "paused"}
    assert json.loads(resumed.output) == {"id": "sbx-state", "status": "running"}
    manager.pause_sandbox.assert_called_once_with("sbx-state")
    manager.resume_sandbox.assert_called_once_with("sbx-state")
