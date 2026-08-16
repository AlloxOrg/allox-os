"""Tests for aio exec argument parsing (flags like ls -la)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from allox.session import Session


@patch("allox.context.ClientContext.aio_client")
def test_aio_exec_ls_dash_la_with_session(mock_aio_client, runner, monkeypatch):
    session = Session("sbx-1", "http://127.0.0.1:1", "2026-01-01T00:00:00+00:00")
    monkeypatch.setattr("allox.commands.aio.get_current_session", lambda: session)
    monkeypatch.setattr("allox.context.get_current_session", lambda: session)
    mock_client = MagicMock()
    mock_client.shell.exec_command.return_value = MagicMock(
        data=MagicMock(output="total 0\n", exit_code=0)
    )
    mock_aio_client.return_value = mock_client

    result = runner(["aio", "exec", "ls", "-la"])
    assert result.exit_code == 0, result.output
    mock_client.shell.exec_command.assert_called_once()
    assert mock_client.shell.exec_command.call_args.kwargs["command"] == "ls -la"


def test_aio_exec_split_args_uuid():
    from allox.utils import split_exec_args

    sid = "29613df6-106f-4d3d-b194-e931171ecbe0"
    sandbox_id, cmd = split_exec_args((sid, "ls", "-la"), has_current_session=True)
    assert sandbox_id == sid
    assert cmd == ("ls", "-la")
