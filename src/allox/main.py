"""Allox CLI entry point."""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console

from allox import __version__
from allox.commands.aio import aio_group
from allox.commands.checkpoint_cmd import checkpoint_group
from allox.commands.config_cmd import config_group
from allox.commands.file_cmd import file_group
from allox.commands.run_cmd import run_command
from allox.commands.sandbox import sandbox_group
from allox.commands.session_cmd import session_group
from allox.commands.workspace_cmd import workspace_group
from allox.config import resolve_config, resolve_config_path
from allox.context import ClientContext

BANNER = r"""[bold cyan]
     _    _ _                 _
    / \  | | | ___  _____  __| | __ _  ___
   / _ \ | | |/ _ \/ __\ \/ / _` |/ _` |/ _ \
  / ___ \| | | (_) \__ \>  <| (_| | (_| | (_) |
 /_/   \_\_|_|\___/|___/_/\_\\__,_|\__, |\___/
                                   |___/
[/]  [dim]v{version} — OpenSandbox + AIO[/]
"""


class BannerGroup(click.Group):
    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        Console().print(BANNER.format(version=__version__))
        super().format_help(ctx, formatter)


@click.group(cls=BannerGroup, context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--api-key", envvar="ALLOX_API_KEY", default=None)
@click.option("--domain", envvar="ALLOX_DOMAIN", default=None)
@click.option("--protocol", type=click.Choice(["http", "https"]), default=None)
@click.option("--request-timeout", type=int, default=None)
@click.option(
    "--use-server-proxy/--no-use-server-proxy",
    default=None,
    help="Route execd traffic through the sandbox server proxy.",
)
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
@click.option(
    "--profile",
    type=click.Choice(["dev", "staging", "prod", "custom", "code"]),
    default=None,
    help="Use ~/.allox/<profile>.toml (overridden by --config).",
)
@click.option("-v", "--verbose", is_flag=True, default=False, help="Verbose HTTP / health-check logging.")
@click.option("--no-color", is_flag=True, default=False)
@click.version_option(version=__version__, prog_name="allox")
@click.pass_context
def cli(
    ctx: click.Context,
    api_key: str | None,
    domain: str | None,
    protocol: str | None,
    request_timeout: int | None,
    use_server_proxy: bool | None,
    config_path: Path | None,
    profile: str | None,
    verbose: bool,
    no_color: bool,
) -> None:
    """Allox — manage AIO agent sandboxes on OpenSandbox."""
    effective_path = resolve_config_path(config_path, profile)

    if ctx.invoked_subcommand == "config":
        resolved = {
            "api_key": api_key,
            "domain": domain,
            "protocol": protocol or "http",
            "request_timeout": request_timeout or 30,
            "use_server_proxy": use_server_proxy if use_server_proxy is not None else False,
            "color": not no_color,
            "default_image": "ghcr.io/agent-infra/sandbox:latest",
            "default_timeout": "30m",
            "default_entrypoint": ["/opt/gem/run.sh"],
            "aio_port": 8080,
            "aio_health_path": "/v1/shell/sessions",
            "ready_timeout": "30s",
            "skip_health_check": False,
        }
    else:
        resolved = resolve_config(
            cli_api_key=api_key,
            cli_domain=domain,
            cli_protocol=protocol,
            cli_timeout=request_timeout,
            cli_use_server_proxy=use_server_proxy,
            config_path=effective_path,
        )
    resolved["color"] = not no_color and resolved.get("color", True)

    ctx.obj = ClientContext(
        resolved_config=resolved,
        config_path=effective_path,
        verbose=verbose,
    )
    ctx.call_on_close(lambda: ctx.obj.close())


cli.add_command(sandbox_group)
cli.add_command(aio_group)
cli.add_command(session_group)
cli.add_command(config_group)
cli.add_command(run_command)
cli.add_command(file_group)
cli.add_command(checkpoint_group)
cli.add_command(workspace_group)
