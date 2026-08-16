"""Allox 2.0 workspace CLI and Bubblewrap launcher tests."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from allox.cli.commands.workspace import (
    _workspace_path,
    build_bwrap_argv,
    build_managed_workspace_argv,
)
from allox.workspace.store import WorkspaceError


def test_bwrap_maps_only_selected_session_and_tmp_inside_workspace():
    source = "/var/lib/allox/workspaces/agents/a/workspace/sessions/s1/current"
    argv = build_bwrap_argv(source, "a", "s1", ("sh", "-c", "pwd"))

    bind_index = argv.index("--bind")
    assert argv[bind_index + 1 : bind_index + 3] == [source, "/workspace"]
    tmp_index = argv.index("/tmp")
    assert argv[tmp_index - 1 : tmp_index + 1] == ["--tmpfs", "/tmp"]
    assert "/var/lib/allox/workspaces/agents" not in argv
    assert argv[-3:] == ["sh", "-c", "pwd"]
    assert "-type s" in argv[-5]


def test_managed_mode_uses_session_workspace_as_cwd_without_pid_namespace():
    source = "/var/lib/allox/workspaces/agents/a/workspace/sessions/s1/current"
    argv = build_managed_workspace_argv(
        source, "a", "s1", ("sh", "-c", "sleep 60 &"), (("MODEL", "test"),)
    )

    assert argv[:2] == ["env", "-i"]
    assert f"HOME={source}" in argv
    assert f"TMPDIR={source}/.allox-tmp" in argv
    assert "MODEL=test" in argv
    assert "bwrap" not in argv
    assert argv[-5:] == ["allox-workspace", source, "sh", "-c", "sleep 60 &"]


def test_workspace_path_rejects_daemon_escape():
    with pytest.raises(WorkspaceError, match="unsafe"):
        _workspace_path("/var/lib/allox/workspaces", "../agent-b/current")


def test_workspace_checkpoint_is_scoped_by_agent_and_session(runner):
    client = MagicMock()
    client.rpc.return_value = {
        "agent_id": "agent-a",
        "session_id": "session-1",
        "checkpoint_id": "cp1",
    }
    with patch("allox.cli.context.ClientContext.get_workspace_client", return_value=client):
        result = runner(
            [
                "workspace",
                "checkpoint",
                "agent-a",
                "session-1",
                "--name",
                "cp1",
                "--message",
                "before retry",
                "--pin",
                "-o",
                "json",
            ]
        )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["checkpoint_id"] == "cp1"
    client.rpc.assert_called_once_with(
        "checkpoint.create",
        agent_id="agent-a",
        session_id="session-1",
        checkpoint_id="cp1",
        origin="allox-cli",
        message="before retry",
        pinned=True,
        metadata=None,
    )


def test_workspace_checkpoint_accepts_metadata(runner):
    client = MagicMock()
    client.rpc.return_value = {
        "agent_id": "agent-a",
        "session_id": "session-1",
        "checkpoint_id": "cp1",
    }
    with patch("allox.cli.context.ClientContext.get_workspace_client", return_value=client):
        result = runner(
            [
                "workspace",
                "checkpoint",
                "agent-a",
                "session-1",
                "--name",
                "cp1",
                "--metadata",
                '{"event":"turn_end","turn":1}',
                "-o",
                "json",
            ]
        )

    assert result.exit_code == 0, result.output
    client.rpc.assert_called_once_with(
        "checkpoint.create",
        agent_id="agent-a",
        session_id="session-1",
        checkpoint_id="cp1",
        origin="allox-cli",
        message=None,
        pinned=False,
        metadata={"event": "turn_end", "turn": 1},
    )


def test_workspace_rollback_accepts_ancestor_depth(runner):
    client = MagicMock()
    client.rpc.side_effect = [
        {"reset_token": "reset-1", "executions": []},
        {"agent_id": "agent-a", "session_id": "session-1", "checkpoint_id": "cp1"},
        {"completed": True, "success": True},
    ]
    with patch("allox.cli.context.ClientContext.get_workspace_client", return_value=client):
        result = runner(
            [
                "workspace",
                "rollback",
                "agent-a",
                "session-1",
                "--num-ancestors",
                "2",
                "-o",
                "json",
            ]
        )

    assert result.exit_code == 0, result.output
    assert client.rpc.call_args_list[0].args == ("runtime.begin_reset",)
    assert client.rpc.call_args_list[1].kwargs == {
        "agent_id": "agent-a",
        "session_id": "session-1",
        "checkpoint_id": None,
        "num_ancestors": 2,
        "scrub_runtime": True,
        "origin": "allox-cli",
        "reset_token": "reset-1",
    }
    assert client.rpc.call_args_list[1].args == ("session.rollback_after_runtime_reset",)
    assert client.rpc.call_args_list[2].args == ("runtime.complete_reset",)
