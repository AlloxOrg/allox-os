"""Mock tests: sandbox create writes current session."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from allox.commands.sandbox import _needs_windows_browser_no_sandbox
from allox.session import get_current_session


@patch("allox.commands.sandbox.SandboxSync.create")
def test_sandbox_create_writes_session(mock_create, runner, tmp_path, monkeypatch):
    monkeypatch.setattr("allox.session.DEFAULT_SESSIONS_PATH", tmp_path / "sessions.json")

    mock_sandbox = MagicMock()
    mock_sandbox.id = "sbx-test-123"
    mock_endpoint = MagicMock()
    mock_endpoint.endpoint = "127.0.0.1:54321"
    mock_sandbox.get_endpoint.return_value = mock_endpoint
    mock_create.return_value = mock_sandbox

    result = runner(["sandbox", "create", "-o", "json", "--skip-health-check"])
    assert result.exit_code == 0, result.output

    data = json.loads(result.output)
    assert data["id"] == "sbx-test-123"
    assert data["aio_url"] == "http://127.0.0.1:54321"

    session = get_current_session(tmp_path / "sessions.json")
    assert session is not None
    assert session.sandbox_id == "sbx-test-123"
    assert session.aio_url == "http://127.0.0.1:54321"
    assert session.created_at

    mock_sandbox.close.assert_called_once()


@patch("allox.commands.sandbox.SandboxSync.create")
def test_sandbox_create_writes_session_without_aio_url(mock_create, runner, tmp_path, monkeypatch):
    monkeypatch.setattr("allox.session.DEFAULT_SESSIONS_PATH", tmp_path / "sessions.json")

    mock_sandbox = MagicMock()
    mock_sandbox.id = "sbx-no-endpoint"
    mock_sandbox.get_endpoint.side_effect = RuntimeError("endpoint unavailable")
    mock_create.return_value = mock_sandbox

    result = runner(["sandbox", "create", "-o", "json", "--skip-health-check"])
    assert result.exit_code == 0, result.output

    session = get_current_session(tmp_path / "sessions.json")
    assert session is not None
    assert session.sandbox_id == "sbx-no-endpoint"
    assert session.aio_url == ""


@patch("allox.commands.sandbox.SandboxSync.create")
def test_sandbox_create_passes_ready_timeout(mock_create, runner, tmp_path, monkeypatch):
    monkeypatch.setattr("allox.session.DEFAULT_SESSIONS_PATH", tmp_path / "sessions.json")
    monkeypatch.setattr("allox.commands.sandbox.sys.platform", "linux")
    mock_sandbox = MagicMock(id="sbx-ready-timeout")
    mock_sandbox.get_endpoint.return_value = MagicMock(endpoint="127.0.0.1:54321")
    mock_create.return_value = mock_sandbox

    result = runner(["sandbox", "create", "--ready-timeout", "180s", "-o", "json"])

    assert result.exit_code == 0, result.output
    assert mock_create.call_args.kwargs["ready_timeout"].total_seconds() == 180


@patch("allox.commands.sandbox.SandboxSync.create")
def test_sandbox_create_adds_no_sandbox_for_local_windows_aio(
    mock_create, runner, tmp_path, monkeypatch
):
    monkeypatch.setattr("allox.session.DEFAULT_SESSIONS_PATH", tmp_path / "sessions.json")
    monkeypatch.setattr("allox.commands.sandbox.sys.platform", "win32")
    mock_sandbox = MagicMock(id="sbx-windows-browser")
    mock_sandbox.get_endpoint.return_value = MagicMock(endpoint="127.0.0.1:54321")
    mock_create.return_value = mock_sandbox

    result = runner(["sandbox", "create", "--skip-health-check", "-o", "json"])

    assert result.exit_code == 0, result.output
    assert mock_create.call_args.kwargs["env"]["BROWSER_NO_SANDBOX"] == "--no-sandbox"


@patch("allox.commands.sandbox.SandboxSync.create")
def test_sandbox_create_preserves_explicit_browser_sandbox_env(
    mock_create, runner, tmp_path, monkeypatch
):
    monkeypatch.setattr("allox.session.DEFAULT_SESSIONS_PATH", tmp_path / "sessions.json")
    monkeypatch.setattr("allox.commands.sandbox.sys.platform", "win32")
    mock_sandbox = MagicMock(id="sbx-explicit-browser-env")
    mock_sandbox.get_endpoint.return_value = MagicMock(endpoint="127.0.0.1:54321")
    mock_create.return_value = mock_sandbox

    result = runner(
        [
            "sandbox",
            "create",
            "--skip-health-check",
            "--env",
            "BROWSER_NO_SANDBOX",
            "",
            "-o",
            "json",
        ]
    )

    assert result.exit_code == 0, result.output
    assert mock_create.call_args.kwargs["env"]["BROWSER_NO_SANDBOX"] == ""


def test_windows_browser_no_sandbox_is_not_enabled_for_remote_server(monkeypatch):
    monkeypatch.setattr("allox.commands.sandbox.sys.platform", "win32")
    obj = SimpleNamespace(resolved_config={"domain": "sandbox.example.com:8080"})

    assert not _needs_windows_browser_no_sandbox(
        obj, "ghcr.io/agent-infra/sandbox:latest"
    )


@patch("allox.commands.sandbox.SandboxSync.create")
def test_sandbox_create_passes_workspace_host_volume(
    mock_create, runner, tmp_path, monkeypatch
):
    monkeypatch.setattr("allox.session.DEFAULT_SESSIONS_PATH", tmp_path / "sessions.json")
    sandbox = MagicMock(id="allox-vm")
    sandbox.get_endpoint.side_effect = RuntimeError("no endpoint")
    mock_create.return_value = sandbox

    result = runner(
        [
            "sandbox",
            "create",
            "--skip-health-check",
            "--host-volume",
            "/data/allox/user-1",
            "/var/lib/allox-store",
            "-o",
            "json",
        ]
    )

    assert result.exit_code == 0, result.output
    volume = mock_create.call_args.kwargs["volumes"][0]
    assert volume.host.path == "/data/allox/user-1"
    assert volume.mount_path == "/var/lib/allox-store"
