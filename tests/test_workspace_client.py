"""Workspace daemon client tests."""

from __future__ import annotations

import click
import httpx
import pytest

from allox.workspace.client import WorkspaceClient


def test_rpc_returns_result():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"ok": True, "result": {"value": 1}})
    )
    client = WorkspaceClient("http://workspace.test", transport=transport)
    try:
        assert client.rpc("test.action") == {"value": 1}
    finally:
        client.close()


def test_rpc_surfaces_daemon_error_message():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            400,
            json={"ok": False, "error": "session has active executions"},
        )
    )
    client = WorkspaceClient("http://workspace.test", transport=transport)
    try:
        with pytest.raises(click.ClickException, match="active executions"):
            client.rpc("session.rollback")
    finally:
        client.close()
