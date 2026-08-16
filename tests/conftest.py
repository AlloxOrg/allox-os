"""Shared CLI test fixtures with isolated config."""

from __future__ import annotations

import httpx
import pytest
from click.testing import CliRunner

from allox.cli.main import cli

_MINIMAL_CONFIG = """\
[connection]
domain = "localhost:8080"
protocol = "http"
"""


def opensandbox_server_reachable(domain: str = "localhost:8080") -> bool:
    """True when OpenSandbox lifecycle API is available (not bare AIO on :8080)."""
    try:
        r = httpx.get(f"http://{domain}/health", timeout=2.0)
    except httpx.HTTPError:
        return False
    if r.status_code != 200:
        return False
    try:
        data = r.json()
    except ValueError:
        return False
    return data.get("status") == "healthy"


@pytest.fixture(scope="module")
def require_opensandbox_server():
    if not opensandbox_server_reachable():
        pytest.skip(
            "OpenSandbox server not reachable at localhost:8080 "
            "(GET /health). Start opensandbox-server, not bare AIO."
        )


@pytest.fixture
def runner(tmp_path):
    """CliRunner that always uses a valid temporary config.toml."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(_MINIMAL_CONFIG, encoding="utf-8")
    cli_runner = CliRunner()

    def invoke(args: list[str], **kwargs):
        return cli_runner.invoke(
            cli,
            ["--config", str(cfg), *args],
            **kwargs,
        )

    invoke.runner = cli_runner
    invoke.config_path = cfg
    return invoke


@pytest.fixture(scope="module")
def module_runner(tmp_path_factory):
    """Module-scoped runner for integration tests sharing one sandbox."""
    cfg_dir = tmp_path_factory.mktemp("config")
    cfg = cfg_dir / "config.toml"
    cfg.write_text(_MINIMAL_CONFIG, encoding="utf-8")
    cli_runner = CliRunner()

    def invoke(args: list[str], **kwargs):
        return cli_runner.invoke(
            cli,
            ["--config", str(cfg), *args],
            **kwargs,
        )

    invoke.runner = cli_runner
    invoke.config_path = cfg
    return invoke
