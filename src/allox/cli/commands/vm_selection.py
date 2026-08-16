"""Local selection of the current outer VM (legacy command surface)."""

from __future__ import annotations

import click

from allox.cli.context import ClientContext
from allox.cli.utils import handle_errors, output_option, prepare_output
from allox.vm.selection import clear_current_session, get_current_session, set_current_session


@click.group("session", invoke_without_command=True)
@click.pass_context
def session_group(ctx: click.Context) -> None:
    """Select the current outer VM (~/.allox/sessions.json)."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@session_group.command("current")
@output_option("table", "json", "yaml")
@click.pass_obj
@handle_errors
def session_current(obj: ClientContext, output_format: str | None) -> None:
    """Show the current session."""
    prepare_output(obj, output_format, allowed=("table", "json", "yaml", "raw"))
    session = get_current_session()
    if session is None:
        raise click.ClickException(
            "No current session. Run: allox sandbox create  OR  allox session use <id>"
        )
    obj.output.success_panel(session.to_dict(), title="Current Session")


@session_group.command("use")
@click.argument("sandbox_id")
@output_option("table", "json", "yaml")
@click.pass_obj
@handle_errors
def session_use(obj: ClientContext, sandbox_id: str, output_format: str | None) -> None:
    """Set current session to an existing sandbox."""
    prepare_output(obj, output_format, allowed=("table", "json", "yaml", "raw"))
    aio_url = ""
    try:
        aio_url = obj.aio_base_url(sandbox_id)
    except Exception:  # noqa: BLE001 - selecting a VM does not require AIO
        aio_url = ""
    session = set_current_session(sandbox_id, aio_url)
    obj.output.success_panel(session.to_dict(), title="Session Updated")


@session_group.command("clear")
@output_option("table", "json", "yaml")
@click.pass_obj
@handle_errors
def session_clear(obj: ClientContext, output_format: str | None) -> None:
    """Clear the current session."""
    prepare_output(obj, output_format, allowed=("table", "json", "yaml", "raw"))
    had = clear_current_session()
    if not had:
        raise click.ClickException("No current session to clear.")
    obj.output.success_panel({"status": "cleared"}, title="Session Cleared")
