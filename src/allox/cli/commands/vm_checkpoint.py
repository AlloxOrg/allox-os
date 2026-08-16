"""Legacy whole-VM snapshot commands; distinct from workspace rollback."""

from __future__ import annotations

import time

import click
from opensandbox.sync.sandbox import SandboxSync

from allox.cli.context import ClientContext
from allox.cli.utils import handle_errors, output_option, parse_duration, prepare_output
from allox.runtime.health import make_aio_health_check
from allox.vm.checkpoints import (
    create_checkpoint,
    latest_ready_snapshot,
    list_snapshots,
    snapshot_to_dict,
)
from allox.vm.selection import set_current_session


@click.group("checkpoint", invoke_without_command=True)
@click.pass_context
def checkpoint_group(ctx: click.Context) -> None:
    """Manage legacy whole-VM image snapshots, not Session rollback."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@checkpoint_group.command("create")
@click.argument("sandbox_id", required=False)
@click.option("--name", default=None, help="Optional checkpoint name.")
@output_option("table", "json", "yaml")
@click.pass_obj
@handle_errors
def checkpoint_create(obj: ClientContext, sandbox_id: str | None, name: str | None, output_format: str | None) -> None:
    """Create a blocking checkpoint from a sandbox."""
    prepare_output(obj, output_format, allowed=("table", "json", "yaml", "raw"))
    resolved = obj.resolve_sandbox_id(sandbox_id)
    with obj.output.spinner("Creating checkpoint..."):
        snapshot = create_checkpoint(obj, resolved, name=name)
    obj.output.success_panel(snapshot_to_dict(snapshot), title="Checkpoint Created")


@checkpoint_group.command("list")
@click.argument("sandbox_id", required=False)
@click.option("--all", "all_sandboxes", is_flag=True, help="List checkpoints from all sandboxes.")
@output_option("table", "json", "yaml")
@click.pass_obj
@handle_errors
def checkpoint_list(obj: ClientContext, sandbox_id: str | None, all_sandboxes: bool, output_format: str | None) -> None:
    """List checkpoints, scoped to the current sandbox by default."""
    prepare_output(obj, output_format, allowed=("table", "json", "yaml", "raw"))
    resolved = None if all_sandboxes else obj.resolve_sandbox_id(sandbox_id)
    rows = [snapshot_to_dict(item) for item in list_snapshots(obj, resolved)]
    obj.output.print_rows(rows, ["id", "sandbox_id", "name", "state", "created_at"], title="Checkpoints")


def _restore(obj: ClientContext, snapshot_id: str, timeout_raw: str | None) -> tuple[SandboxSync, str]:
    timeout = parse_duration(timeout_raw) if timeout_raw else parse_duration(str(obj.resolved_config.get("default_timeout", "30m")))
    wait = parse_duration(str(obj.resolved_config.get("ready_timeout", "30s")))
    port = obj.aio_port()
    ready_info: dict = {}
    sandbox = SandboxSync.create(
        snapshot_id=snapshot_id,
        timeout=timeout,
        entrypoint=list(obj.resolved_config.get("default_entrypoint", ["/opt/gem/run.sh"])),
        connection_config=obj.connection_config,
        ready_timeout=wait,
        health_check=make_aio_health_check(
            port,
            health_path=obj.resolved_config.get("aio_health_path", "/v1/shell/sessions"),
            max_wait_seconds=wait.total_seconds(),
            verbose=obj.verbose,
            ready_info=ready_info,
        ),
        skip_health_check=bool(obj.resolved_config.get("skip_health_check", False)),
    )
    try:
        endpoint = sandbox.get_endpoint(port)
        aio_url = f"http://{endpoint.endpoint}"
    except Exception:  # noqa: BLE001 - a restored VM may not expose AIO
        aio_url = ""
    return sandbox, aio_url


@checkpoint_group.command("restore")
@click.argument("checkpoint", required=False, default="latest")
@click.option("--source-sandbox", default=None, help="Source sandbox used when resolving latest.")
@click.option("--timeout", default=None, help="Lifetime for the restored sandbox.")
@output_option("table", "json", "yaml")
@click.pass_obj
@handle_errors
def checkpoint_restore(obj: ClientContext, checkpoint: str, source_sandbox: str | None, timeout: str | None, output_format: str | None) -> None:
    """Restore a new sandbox from latest or a specific checkpoint ID."""
    prepare_output(obj, output_format, allowed=("table", "json", "yaml", "raw"))
    if checkpoint == "latest":
        source = obj.resolve_sandbox_id(source_sandbox)
        snapshot = latest_ready_snapshot(obj, source)
    else:
        snapshot = obj.get_manager().get_snapshot(checkpoint)
        if str(getattr(snapshot.status, "state", "")).lower() != "ready":
            raise click.ClickException(f"Checkpoint '{checkpoint}' is not READY.")
    with obj.output.spinner("Restoring checkpoint..."):
        sandbox, aio_url = _restore(obj, snapshot.id, timeout)
    try:
        set_current_session(sandbox.id, aio_url)
        obj.output.success_panel(
            {"id": sandbox.id, "checkpoint_id": snapshot.id, "source_sandbox_id": snapshot.sandbox_id, "aio_url": aio_url},
            title="Sandbox Restored",
        )
    finally:
        sandbox.close()


@checkpoint_group.command("delete")
@click.argument("checkpoint_id")
@output_option("table", "json", "yaml")
@click.pass_obj
@handle_errors
def checkpoint_delete(obj: ClientContext, checkpoint_id: str, output_format: str | None) -> None:
    """Delete a checkpoint."""
    prepare_output(obj, output_format, allowed=("table", "json", "yaml", "raw"))
    obj.get_manager().delete_snapshot(checkpoint_id)
    obj.output.success_panel({"checkpoint_id": checkpoint_id, "deleted": True}, title="Checkpoint Deleted")


@checkpoint_group.command("watch")
@click.argument("sandbox_id", required=False)
@click.option("--interval", default=None, help="Checkpoint interval, e.g. 5m.")
@click.option("--count", type=click.IntRange(min=1), default=None, help="Stop after N checkpoints.")
@click.option("--name-prefix", default="scheduled", show_default=True)
@click.pass_obj
@handle_errors
def checkpoint_watch(obj: ClientContext, sandbox_id: str | None, interval: str | None, count: int | None, name_prefix: str) -> None:
    """Run a foreground periodic checkpoint loop."""
    resolved = obj.resolve_sandbox_id(sandbox_id)
    raw = interval or str(obj.resolved_config.get("checkpoint_interval", "5m"))
    seconds = parse_duration(raw).total_seconds()
    if seconds <= 0:
        raise click.ClickException("Checkpoint interval must be greater than zero.")
    created = 0
    click.echo(f"Watching sandbox {resolved}; checkpoint every {raw}. Press Ctrl+C to stop.")
    try:
        while count is None or created < count:
            time.sleep(seconds)
            name = f"{name_prefix}-{created + 1}"
            snapshot = create_checkpoint(obj, resolved, name=name)
            created += 1
            click.echo(f"Created checkpoint {snapshot.id} ({name})")
    except KeyboardInterrupt:
        click.echo("Checkpoint watch stopped.")
