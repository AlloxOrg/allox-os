"""Integration tests for ``allox aio mcp`` (require OpenSandbox + AIO image).

Run explicitly:
  uv run pytest -m integration tests/test_integration_mcp.py -v
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.integration

_OPTIONAL_SERVERS = ("file", "shell", "markitdown")
_BROWSER_CALL_TOOLS = ("browser_screenshot", "browser_navigate")


@pytest.fixture(scope="module")
def require_server(require_opensandbox_server):
    if shutil.which("docker") and subprocess.run(
        ["docker", "info"],
        capture_output=True,
        timeout=10,
    ).returncode != 0:
        pytest.skip("Docker daemon not running")


@pytest.fixture(scope="module")
def sandbox_id(module_runner, require_server):
    create = module_runner(["sandbox", "create", "-o", "json", "--timeout", "5m"])
    assert create.exit_code == 0, create.output
    sid = json.loads(create.output)["id"]
    yield sid
    module_runner(["sandbox", "kill", sid, "-o", "json"])


@pytest.fixture(scope="module")
def mcp_servers(module_runner, sandbox_id) -> set[str]:
    result = module_runner(["aio", "mcp", "servers", sandbox_id, "-o", "json"])
    assert result.exit_code == 0, result.output
    servers = set(json.loads(result.output).get("servers") or [])
    assert servers, "expected at least one MCP server"
    return servers


def _list_tool_names(runner, sandbox_id: str, server: str) -> set[str]:
    result = runner(["aio", "mcp", "tools", sandbox_id, server, "-o", "json"])
    assert result.exit_code == 0, result.output
    tools = json.loads(result.output).get("tools") or []
    return {t["name"] for t in tools if "name" in t}


def test_mcp_servers_includes_browser(runner, mcp_servers):
    assert "browser" in mcp_servers, f"expected browser in {mcp_servers}"


def test_mcp_browser_tools_non_empty(runner, sandbox_id, mcp_servers):
    if "browser" not in mcp_servers:
        pytest.skip("browser MCP server not configured")
    names = _list_tool_names(runner, sandbox_id, "browser")
    assert names
    assert any(n.startswith("browser_") for n in names), f"unexpected tool names: {names}"


def test_mcp_call_browser_tool(runner, sandbox_id, mcp_servers):
    if "browser" not in mcp_servers:
        pytest.skip("browser MCP server not configured")
    names = _list_tool_names(runner, sandbox_id, "browser")
    tool = next((t for t in _BROWSER_CALL_TOOLS if t in names), None)
    assert tool, f"need one of {_BROWSER_CALL_TOOLS}, got {sorted(names)}"

    cmd = ["aio", "mcp", "call", sandbox_id, "browser", tool, "-o", "json"]
    if tool == "browser_navigate":
        cmd.extend(["--args", '{"url":"https://example.com"}'])
    result = runner(cmd)
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("server", _OPTIONAL_SERVERS)
def test_mcp_optional_server_tools(runner, sandbox_id, mcp_servers, server):
    if server not in mcp_servers:
        pytest.skip(f"MCP server '{server}' not configured on this image")
    names = _list_tool_names(runner, sandbox_id, server)
    assert names


def test_mcp_call_shell_exec_if_configured(runner, sandbox_id, mcp_servers):
    if "shell" not in mcp_servers:
        pytest.skip("shell MCP server not configured on this image")
    names = _list_tool_names(runner, sandbox_id, "shell")
    tool = "exec" if "exec" in names else next(iter(names))
    result = runner(
        [
            "aio",
            "mcp",
            "call",
            sandbox_id,
            "shell",
            tool,
            "--arg",
            "command=echo mcp-hello",
            "-o",
            "json",
        ]
    )
    assert result.exit_code == 0, result.output


def test_mcp_call_file_list_if_configured(runner, sandbox_id, mcp_servers):
    if "file" not in mcp_servers:
        pytest.skip("file MCP server not configured on this image")
    names = _list_tool_names(runner, sandbox_id, "file")
    tool = "list" if "list" in names else next(iter(names))
    result = runner(
        [
            "aio",
            "mcp",
            "call",
            sandbox_id,
            "file",
            tool,
            "--args",
            '{"path":"/home/gem"}',
            "-o",
            "json",
        ]
    )
    assert result.exit_code == 0, result.output


def test_mcp_call_markitdown_if_configured(runner, sandbox_id, mcp_servers):
    if "markitdown" not in mcp_servers:
        pytest.skip("markitdown MCP server not configured on this image")
    prep = runner(
        [
            "aio",
            "exec",
            sandbox_id,
            "bash",
            "-c",
            'echo "# sample" > /tmp/alox-mcp-sample.md',
        ]
    )
    assert prep.exit_code == 0, prep.output
    names = _list_tool_names(runner, sandbox_id, "markitdown")
    tool = "convert" if "convert" in names else next(iter(names))
    result = runner(
        [
            "aio",
            "mcp",
            "call",
            sandbox_id,
            "markitdown",
            tool,
            "--args",
            '{"path":"/tmp/alox-mcp-sample.md"}',
            "-o",
            "json",
        ]
    )
    assert result.exit_code == 0, result.output


def test_mcp_omit_sandbox_id_uses_session(runner, sandbox_id, mcp_servers):
    """Session is set by sandbox create; omit id on mcp subcommands."""
    server = "browser" if "browser" in mcp_servers else next(iter(mcp_servers))
    tools = runner(["aio", "mcp", "tools", server, "-o", "json"])
    assert tools.exit_code == 0, tools.output
    assert json.loads(tools.output)["server"] == server
