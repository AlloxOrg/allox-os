"""Regression tests for TOML config serialization."""

from __future__ import annotations

from allox.config import load_config_file


def test_config_set_preserves_and_quotes_string_values(runner):
    runner.config_path.write_text(
        '[connection]\ndomain = "localhost:8080"\nprotocol = "http"\n\n'
        '[log]\nlevel = "DEBUG"\n',
        encoding="utf-8",
    )
    result = runner(["config", "set", "defaults.ready_timeout", "180s"])
    assert result.exit_code == 0, result.output
    data = load_config_file(runner.config_path)
    assert data["connection"]["protocol"] == "http"
    assert data["log"]["level"] == "DEBUG"
    assert data["defaults"]["ready_timeout"] == "180s"


def test_config_show_resolves_values_from_selected_file(runner):
    runner.config_path.write_text(
        '[connection]\ndomain = "localhost:9999"\nprotocol = "http"\n\n'
        '[defaults]\nready_timeout = "180s"\n',
        encoding="utf-8",
    )
    result = runner(["config", "show", "-o", "json"])
    assert result.exit_code == 0, result.output
    import json

    resolved = json.loads(result.output)["resolved"]
    assert resolved["domain"] == "localhost:9999"
    assert resolved["ready_timeout"] == "180s"
