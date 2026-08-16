"""Unit tests for duration parsing and API error formatting."""

from agent_sandbox.core.api_error import ApiError

from allox.utils import format_api_error, parse_duration, parse_nullable_duration


def test_parse_duration_minutes():
    assert parse_duration("30m").total_seconds() == 30 * 60


def test_parse_nullable_duration_none():
    assert parse_nullable_duration("none") is None
    assert parse_nullable_duration("NONE") is None


def test_format_api_error_mcp_server_not_found():
    exc = ApiError(
        status_code=404,
        body={"success": False, "message": "MCP server 'shell' not found in configuration"},
    )
    text = format_api_error(exc)
    assert "404" in text
    assert "shell" in text
    assert "mcp servers" in text


def test_format_api_error_mcp_tool_failed():
    exc = ApiError(
        status_code=500,
        body={"success": False, "message": "Failed to execute tool 'navigate' on MCP server 'browser'"},
    )
    text = format_api_error(exc)
    assert "browser_navigate" in text or "mcp tools" in text
