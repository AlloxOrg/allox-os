"""Allox configuration commands."""

from __future__ import annotations

import json

import click

from allox.cli.context import ClientContext
from allox.cli.utils import emit_json, handle_errors
from allox.config import DEFAULT_CONFIG_PATH, init_config_file, load_config_file, resolve_config


@click.group("config", invoke_without_command=True)
@click.pass_context
def config_group(ctx: click.Context) -> None:
    """Manage ~/.allox/config.toml."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@config_group.command("init")
@click.option("--force", is_flag=True, help="Overwrite existing config file.")
@click.pass_obj
def config_init(obj: ClientContext, force: bool) -> None:
    """Create default config file."""
    path = obj.config_path
    if path.exists() and not force:
        click.echo(f"Config already exists: {path}")
        return
    if force and path.exists():
        path.unlink()
    init_config_file(path)
    click.echo(f"Created {path}")


@config_group.command("show")
@click.option("-o", "--output", "output_format", type=click.Choice(["json", "table"]), default="table")
@click.pass_obj
@handle_errors
def config_show(obj: ClientContext, output_format: str) -> None:
    """Show resolved configuration."""
    resolved = resolve_config(config_path=obj.config_path)
    resolved["color"] = obj.resolved_config.get("color", resolved.get("color", True))
    data = {
        "config_path": str(obj.config_path),
        "resolved": resolved,
        "file": load_config_file(obj.config_path),
    }
    if output_format == "json":
        emit_json(data)
        return
    click.echo(f"Config file: {obj.config_path}")
    for key, value in resolved.items():
        click.echo(f"  {key}: {value}")


@config_group.command("set")
@click.argument("key")
@click.argument("value")
@click.pass_obj
def config_set(obj: ClientContext, key: str, value: str) -> None:
    """Set a config value (e.g. connection.domain localhost:8080)."""
    path = obj.config_path
    init_config_file(path)
    data = load_config_file(path) or {}

    parts = key.split(".")
    if len(parts) != 2:
        raise click.ClickException("Key must be section.name (e.g. connection.domain)")
    section, name = parts
    section_data = data.setdefault(section, {})

    if name == "entrypoint":
        section_data[name] = [p.strip() for p in value.split(",") if p.strip()]
    elif name in ("aio_port", "request_timeout"):
        section_data[name] = int(value)
    elif name == "use_server_proxy":
        section_data[name] = value.lower() in ("1", "true", "yes", "on")
    else:
        section_data[name] = value

    lines: list[str] = []
    for sec, items in data.items():
        lines.append(f"[{sec}]")
        for k, v in items.items():
            if isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, list):
                inner = ", ".join(json.dumps(x, ensure_ascii=False) for x in v)
                lines.append(f"{k} = [{inner}]")
            elif isinstance(v, int | float):
                lines.append(f"{k} = {v}")
            else:
                sv = json.dumps(str(v), ensure_ascii=False)
                # TOML bare strings cannot contain ':' — quote host:port etc.
                lines.append(f"{k} = {sv}")
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    click.echo(f"Updated {key} in {path}")


@config_group.command("path")
def config_path() -> None:
    """Print default config file path."""
    click.echo(DEFAULT_CONFIG_PATH)
