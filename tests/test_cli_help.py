def test_cli_help(runner):
    result = runner(["--help"])
    assert result.exit_code == 0
    assert "sandbox" in result.output
    assert "aio" in result.output


def test_sandbox_help(runner):
    result = runner(["sandbox", "--help"])
    assert result.exit_code == 0
    assert "create" in result.output
