"""CLI parsing for file commands with an optional sandbox ID."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _session():
    return SimpleNamespace(sandbox_id="session-sbx", aio_url="", created_at="now")


def test_file_write_single_path_uses_current_session(runner):
    sandbox = MagicMock()
    with (
        patch("allox.cli.context.get_current_session", return_value=_session()),
        patch("allox.cli.context.ClientContext.connect_sandbox", return_value=sandbox) as connect,
    ):
        result = runner(["file", "write", "/workspace/a.txt", "--content", "hello", "-o", "json"])
    assert result.exit_code == 0, result.output
    connect.assert_called_once_with("session-sbx")
    sandbox.files.write_file.assert_called_once_with("/workspace/a.txt", "hello", encoding="utf-8")


def test_file_cat_single_path_uses_current_session(runner):
    sandbox = MagicMock()
    sandbox.files.read_file.return_value = "hello"
    with (
        patch("allox.cli.context.get_current_session", return_value=_session()),
        patch("allox.cli.context.ClientContext.connect_sandbox", return_value=sandbox) as connect,
    ):
        result = runner(["file", "cat", "/workspace/a.txt"])
    assert result.exit_code == 0, result.output
    assert result.output == "hello"
    connect.assert_called_once_with("session-sbx")


def test_file_write_explicit_sandbox_id(runner):
    sandbox = MagicMock()
    with patch("allox.cli.context.ClientContext.connect_sandbox", return_value=sandbox) as connect:
        result = runner(["file", "write", "explicit-sbx", "/tmp/a.txt", "--content", "x"])
    assert result.exit_code == 0, result.output
    connect.assert_called_once_with("explicit-sbx")
