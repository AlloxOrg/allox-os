"""Config resolution tests."""

from __future__ import annotations

from allox.config import resolve_config


def test_skip_health_check_from_config(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        """
[defaults]
skip_health_check = true
image = "opensandbox/code-interpreter:latest"
""",
        encoding="utf-8",
    )
    resolved = resolve_config(config_path=cfg)
    assert resolved["skip_health_check"] is True
    assert resolved["default_image"] == "opensandbox/code-interpreter:latest"


def test_skip_health_check_default_false(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[connection]\ndomain = "localhost:8080"\n', encoding="utf-8")
    resolved = resolve_config(config_path=cfg)
    assert resolved["skip_health_check"] is False


def test_workspace_auto_checkpoint_turns_defaults_off(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[workspace]\n", encoding="utf-8")

    assert resolve_config(config_path=cfg)["workspace_auto_checkpoint_turns"] is False


def test_workspace_auto_checkpoint_turns_config_and_env(monkeypatch, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[workspace]\nauto_checkpoint_turns = true\n", encoding="utf-8")

    assert resolve_config(config_path=cfg)["workspace_auto_checkpoint_turns"] is True
    monkeypatch.setenv("ALLOX_WORKSPACE_AUTO_CHECKPOINT_TURNS", "off")
    assert resolve_config(config_path=cfg)["workspace_auto_checkpoint_turns"] is False
