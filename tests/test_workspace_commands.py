"""Allox 2.0 workspace CLI and Bubblewrap launcher tests."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from allox.commands.workspace_cmd import _workspace_path, build_bwrap_argv
from allox.workspace_store import WorkspaceError


def test_bwrap_maps_only_selected_session_and_tmp_inside_workspace():
    source = "/var/lib/allox-store/agents/a/workspace/sessions/s1/current"
    argv = build_bwrap_argv(source, "a", "s1", ("sh", "-c", "pwd"))

    bind_index = argv.index("--bind")
    assert argv[bind_index + 1 : bind_index + 3] == [source, "/workspace"]
    tmp_index = argv.index("/tmp")
    assert argv[tmp_index - 1 : tmp_index + 1] == ["--tmpfs", "/tmp"]
    assert "/var/lib/allox-store/agents" not in argv
    assert argv[-3:] == ["sh", "-c", "pwd"]
    assert "-type s" in argv[-5]


def test_workspace_path_rejects_daemon_escape():
    with pytest.raises(WorkspaceError, match="unsafe"):
        _workspace_path("/var/lib/allox-store", "../agent-b/current")


def test_workspace_checkpoint_is_scoped_by_agent_and_session(runner):
    client = MagicMock()
    client.rpc.return_value = {
        "agent_id": "agent-a",
        "session_id": "session-1",
        "checkpoint_id": "cp1",
    }
    with patch("allox.context.ClientContext.get_workspace_client", return_value=client):
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
    with patch("allox.context.ClientContext.get_workspace_client", return_value=client):
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
    client.rpc.return_value = {
        "agent_id": "agent-a",
        "session_id": "session-1",
        "checkpoint_id": "cp1",
    }
    with patch("allox.context.ClientContext.get_workspace_client", return_value=client):
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
    client.rpc.assert_called_once_with(
        "session.rollback",
        agent_id="agent-a",
        session_id="session-1",
        checkpoint_id=None,
        num_ancestors=2,
        scrub_runtime=True,
        origin="allox-cli",
    )
