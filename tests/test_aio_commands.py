"""Smoke tests for AIO subcommands (no live sandbox)."""


def test_aio_jupyter_help(runner):
    result = runner(["aio", "jupyter", "--help"])
    assert result.exit_code == 0
    assert "run" in result.output


def test_aio_browser_help(runner):
    result = runner(["aio", "browser", "--help"])
    assert result.exit_code == 0
    assert "info" in result.output


def test_sandbox_create_has_env_option(runner):
    result = runner(["sandbox", "create", "--help"])
    assert result.exit_code == 0
    assert "--env" in result.output or "-e" in result.output


def test_aio_mcp_help(runner):
    result = runner(["aio", "mcp", "--help"])
    assert result.exit_code == 0
    assert "servers" in result.output
    assert "tools" in result.output
    assert "call" in result.output


def test_aio_mcp_call_help(runner):
    result = runner(["aio", "mcp", "call", "--help"])
    assert result.exit_code == 0
    assert "--args" in result.output
    assert "--arg" in result.output
