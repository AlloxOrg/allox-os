"""Unit tests for MCP argument parsing."""

import click
import pytest

from allox.mcp_utils import build_mcp_request, parse_mcp_target

_SAMPLE_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_parse_mcp_target_tools_with_session():
    sid, server, tool = parse_mcp_target(("browser",), require_tool=False)
    assert sid is None
    assert server == "browser"
    assert tool is None


def test_parse_mcp_target_tools_with_explicit_id():
    sid, server, tool = parse_mcp_target((_SAMPLE_ID, "file"), require_tool=False)
    assert sid == _SAMPLE_ID
    assert server == "file"
    assert tool is None


def test_parse_mcp_call_with_session():
    sid, server, tool = parse_mcp_target(("shell", "exec"), require_tool=True)
    assert sid is None
    assert server == "shell"
    assert tool == "exec"


def test_parse_mcp_call_missing_tool():
    with pytest.raises(click.ClickException, match="Missing server and tool"):
        parse_mcp_target(("shell",), require_tool=True)


def test_build_mcp_request_from_json():
    req = build_mcp_request('{"url":"https://example.com"}', ())
    assert req == {"url": "https://example.com"}


def test_build_mcp_request_merge_arg_pairs():
    req = build_mcp_request('{"path":"/tmp"}', (("recursive", "true"),))
    assert req == {"path": "/tmp", "recursive": "true"}


def test_build_mcp_request_invalid_json():
    with pytest.raises(click.BadParameter):
        build_mcp_request("{not json", ())
