"""Configuration: CLI flags > env > ~/.allox/config.toml > defaults."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]

DEFAULT_CONFIG_DIR = Path.home() / ".allox"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.toml"

DEFAULT_CONFIG_TEMPLATE = """\
# Allox CLI configuration
# Priority: CLI flags > environment variables > this file

[connection]
# api_key = ""
# domain = "localhost:8080"
# protocol = "http"
# request_timeout = 30
# use_server_proxy = false

[output]
# color = true

[defaults]
# image = "ghcr.io/agent-infra/sandbox:latest"
# timeout = "30m"
# entrypoint = ["/opt/gem/run.sh"]
# aio_port = 8080
# aio_health_path = "/v1/shell/sessions"
# ready_timeout = "30s"
# skip_health_check = false   # true for non-AIO images (e.g. code-interpreter)

[checkpoint]
# enabled = false
# on_success = false
# operations = ["run", "file.write", "file.upload", "aio.exec", "aio.jupyter"]
# interval = "5m"
# strict = false
# create_timeout = 900

[workspace]
# daemon_url = "http://127.0.0.1:8092"
# request_timeout = 30
# vm_root = "/var/lib/allox-store"
# token is read from ALLOX_WORKSPACE_TOKEN; do not store it in this file
"""


def resolve_config_path(config_path: Path | None = None, profile: str | None = None) -> Path:
    """Resolve config file from explicit path or profile name."""
    if config_path is not None:
        return config_path
    if profile:
        return DEFAULT_CONFIG_DIR / f"{profile}.toml"
    return DEFAULT_CONFIG_PATH


def load_config_file(config_path: Path | None = None) -> dict[str, Any]:
    path = config_path or DEFAULT_CONFIG_PATH
    if not path.exists() or tomllib is None:
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception as exc:
        import click

        raise click.ClickException(f"Invalid config file {path}: {exc}") from exc


def _env(key: str) -> str | None:
    value = os.environ.get(key)
    return value if value else None


def resolve_config(
    *,
    cli_api_key: str | None = None,
    cli_domain: str | None = None,
    cli_protocol: str | None = None,
    cli_timeout: int | None = None,
    cli_use_server_proxy: bool | None = None,
    config_path: Path | None = None,
) -> dict[str, Any]:
    file_cfg = load_config_file(config_path)
    conn = file_cfg.get("connection", {})
    out = file_cfg.get("output", {})
    defaults = file_cfg.get("defaults", {})

    api_key = cli_api_key or _env("ALLOX_API_KEY") or _env("OPEN_SANDBOX_API_KEY") or conn.get("api_key")
    domain = cli_domain or _env("ALLOX_DOMAIN") or _env("OPEN_SANDBOX_DOMAIN") or conn.get("domain")
    protocol = cli_protocol or _env("ALLOX_PROTOCOL") or conn.get("protocol") or "http"
    request_timeout = cli_timeout
    if request_timeout is None:
        raw = conn.get("request_timeout", 30)
        request_timeout = int(raw) if raw is not None else 30

    use_proxy = cli_use_server_proxy
    if use_proxy is None:
        use_proxy = conn.get("use_server_proxy", False)

    default_image = defaults.get("image", "ghcr.io/agent-infra/sandbox:latest")
    default_timeout = defaults.get("timeout", "30m")
    default_entrypoint = defaults.get("entrypoint", ["/opt/gem/run.sh"])
    if isinstance(default_entrypoint, str):
        default_entrypoint = [default_entrypoint]
    aio_port = int(defaults.get("aio_port", 8080))
    aio_health_path = defaults.get("aio_health_path", "/v1/shell/sessions")
    ready_timeout = defaults.get("ready_timeout", "30s")
    skip_health_check = bool(defaults.get("skip_health_check", False))
    checkpoint = file_cfg.get("checkpoint", {})
    workspace = file_cfg.get("workspace", {})

    return {
        "api_key": api_key,
        "domain": domain,
        "protocol": protocol,
        "request_timeout": request_timeout,
        "use_server_proxy": bool(use_proxy),
        "color": out.get("color", True),
        "default_image": default_image,
        "default_timeout": default_timeout,
        "default_entrypoint": list(default_entrypoint),
        "aio_port": aio_port,
        "aio_health_path": aio_health_path,
        "ready_timeout": ready_timeout,
        "skip_health_check": skip_health_check,
        "checkpoint_enabled": bool(checkpoint.get("enabled", False)),
        "checkpoint_on_success": bool(checkpoint.get("on_success", False)),
        "checkpoint_operations": list(checkpoint.get(
            "operations", ["run", "file.write", "file.upload", "aio.exec", "aio.jupyter"]
        )),
        "checkpoint_interval": checkpoint.get("interval", "5m"),
        "checkpoint_strict": bool(checkpoint.get("strict", False)),
        "checkpoint_create_timeout": int(checkpoint.get("create_timeout", 900)),
        "workspace_daemon_url": _env("ALLOX_WORKSPACE_DAEMON_URL")
        or workspace.get("daemon_url", "http://127.0.0.1:8092"),
        "workspace_token": _env("ALLOX_WORKSPACE_TOKEN") or "",
        "workspace_request_timeout": int(workspace.get("request_timeout", 30)),
        "workspace_vm_root": workspace.get("vm_root", "/var/lib/allox-store"),
    }


def init_config_file(config_path: Path | None = None) -> Path:
    path = config_path or DEFAULT_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path
    path.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    return path
