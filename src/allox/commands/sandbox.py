"""Sandbox lifecycle via OpenSandbox."""

from __future__ import annotations

import sys
from datetime import timedelta

import click
from opensandbox.models.sandboxes import Host, SandboxFilter, Volume
from opensandbox.sync.sandbox import SandboxSync

from allox import __version__
from allox.aio_health import make_aio_health_check
from allox.context import ClientContext
from allox.session import clear_current_session, get_current_session, set_current_session
from allox.utils import (
    KEY_VALUE,
    handle_errors,
    output_option,
    parse_duration,
    parse_nullable_duration,
    prepare_output,
)


@click.group("sandbox", invoke_without_command=True)
@click.pass_context
def sandbox_group(ctx: click.Context) -> None:
    """Manage sandboxes on OpenSandbox."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


def _resolve_ready_timeout(obj: ClientContext, ready_timeout: str | None) -> float:
    if ready_timeout:
        return parse_duration(ready_timeout).total_seconds()
    cfg = obj.resolved_config.get("ready_timeout", "30s")
    return parse_duration(str(cfg)).total_seconds()


def _needs_windows_browser_no_sandbox(obj: ClientContext, image: str) -> bool:
    """Use Chromium's no-sandbox mode for the local Docker Desktop AIO image.

    Docker Desktop does not grant the namespace operations Chromium's Linux
    sandbox needs. Keep this compatibility fallback narrowly scoped so remote
    and non-Windows deployments retain Chromium's own sandbox by default.
    """
    if sys.platform != "win32":
        return False
    domain = str(obj.resolved_config.get("domain") or "").lower()
    host = domain.rsplit(":", 1)[0].strip("[]")
    is_local_server = host in {"localhost", "127.0.0.1", "::1"}
    is_official_aio = image.startswith("ghcr.io/agent-infra/sandbox:")
    return is_local_server and is_official_aio


@sandbox_group.command("create")
@click.option("--image", "-i", default=None, help="Container image (default: AIO sandbox).")
@click.option("--timeout", "-t", "timeout_raw", default=None, help="Lifetime e.g. 30m, or none.")
@click.option("--metadata", "-m", "metadata_kv", multiple=True, type=KEY_VALUE)
@click.option("--env", "-e", "env_kv", multiple=True, type=KEY_VALUE, help="Environment KEY=VALUE (repeatable).")
@click.option(
    "--host-volume",
    "host_volumes",
    multiple=True,
    nargs=2,
    type=(click.Path(path_type=str), str),
    metavar="HOST_PATH MOUNT_PATH",
    help="Mount a trusted host directory into the Allox VM (repeatable).",
)
@click.option(
    "--entrypoint",
    multiple=True,
    help="Override entrypoint (default: /opt/gem/run.sh for AIO).",
)
@click.option("--skip-health-check", is_flag=True, help="Do not wait for AIO /v1 API.")
@click.option("--ready-timeout", default=None, help="Max wait for readiness (e.g. 60s). Overrides config.")
@output_option("table", "json", "yaml")
@click.pass_obj
@handle_errors
def sandbox_create(
    obj: ClientContext,
    image: str | None,
    timeout_raw: str | None,
    metadata_kv: tuple[tuple[str, str], ...],
    env_kv: tuple[tuple[str, str], ...],
    host_volumes: tuple[tuple[str, str], ...],
    entrypoint: tuple[str, ...],
    skip_health_check: bool,
    ready_timeout: str | None,
    output_format: str | None,
) -> None:
    """Create an AIO sandbox (default image + health check)."""
    prepare_output(obj, output_format, allowed=("table", "json", "yaml", "raw"))

    if not obj.resolved_config.get("domain"):
        raise click.ClickException(
            "OpenSandbox domain not set. Run: allox config set connection.domain localhost:8080"
        )

    image = image or obj.resolved_config.get("default_image")
    if not image:
        raise click.ClickException("Image required. Use --image or defaults.image in config.")

    timeout: timedelta | None = None
    timeout_is_set = False
    if timeout_raw is not None:
        timeout = parse_nullable_duration(timeout_raw)
        timeout_is_set = True
    else:
        default_timeout = obj.resolved_config.get("default_timeout")
        if default_timeout:
            timeout = parse_nullable_duration(default_timeout)
            timeout_is_set = True

    ep = list(entrypoint) if entrypoint else obj.resolved_config.get("default_entrypoint", ["/opt/gem/run.sh"])

    metadata = {"tool": "allox", "version": __version__}
    metadata.update(dict(metadata_kv))

    config_skip = bool(obj.resolved_config.get("skip_health_check", False))
    effective_skip = skip_health_check or config_skip

    kwargs: dict = {
        "connection_config": obj.connection_config,
        "entrypoint": ep,
        "skip_health_check": effective_skip,
        "metadata": metadata,
    }
    if timeout_is_set:
        kwargs["timeout"] = timeout
    env = dict(env_kv)
    if _needs_windows_browser_no_sandbox(obj, image):
        env.setdefault("BROWSER_NO_SANDBOX", "--no-sandbox")
    if env:
        kwargs["env"] = env
    if host_volumes:
        kwargs["volumes"] = [
            Volume(
                name=f"allox-host-{index}",
                host=Host(path=host_path),
                mount_path=mount_path,
            )
            for index, (host_path, mount_path) in enumerate(host_volumes)
        ]

    ready_info: dict = {}
    if not effective_skip:
        port = obj.aio_port()
        wait = _resolve_ready_timeout(obj, ready_timeout)
        kwargs["ready_timeout"] = timedelta(seconds=wait)
        health_path = obj.resolved_config.get("aio_health_path", "/v1/shell/sessions")
        kwargs["health_check"] = make_aio_health_check(
            port,
            health_path=health_path,
            max_wait_seconds=wait,
            verbose=obj.verbose,
            ready_info=ready_info,
        )

    with obj.output.spinner("Creating AIO sandbox..."):
        sandbox = SandboxSync.create(image, **kwargs)

    aio_url = None
    try:
        endpoint = sandbox.get_endpoint(obj.aio_port())
        aio_url = f"http://{endpoint.endpoint}"
    except Exception:
        pass

    set_current_session(sandbox.id, aio_url or "")

    panel: dict = {
        "id": sandbox.id,
        "image": image,
        "aio_url": aio_url or "(run: allox sandbox endpoint <id>)",
        "entrypoint": " ".join(ep),
    }
    if ready_info.get("elapsed_seconds") is not None:
        panel["aio_ready_seconds"] = ready_info["elapsed_seconds"]
    obj.output.success_panel(panel, title="Sandbox Created")
    sandbox.close()


@sandbox_group.command("list")
@output_option("table", "json", "yaml", "raw")
@click.pass_obj
@handle_errors
def sandbox_list(obj: ClientContext, output_format: str | None) -> None:
    """List sandboxes."""
    prepare_output(obj, output_format, allowed=("table", "json", "yaml", "raw"))
    manager = obj.get_manager()
    page = 1
    sandboxes = []
    while True:
        result = manager.list_sandbox_infos(SandboxFilter(page=page))
        sandboxes.extend(result.sandbox_infos)
        if not result.pagination.has_next_page:
            break
        page += 1
    rows = [{"id": s.id, "state": str(s.status.state)} for s in sandboxes]
    obj.output.print_rows(rows, ["id", "state"], title="Sandboxes")


@sandbox_group.command("get")
@click.argument("sandbox_id", required=False, default=None)
@output_option("table", "json", "yaml")
@click.pass_obj
@handle_errors
def sandbox_get(obj: ClientContext, sandbox_id: str | None, output_format: str | None) -> None:
    """Get sandbox details."""
    prepare_output(obj, output_format, allowed=("table", "json", "yaml", "raw"))
    resolved = obj.resolve_sandbox_id(sandbox_id)
    manager = obj.get_manager()
    sbx = manager.get_sandbox_info(resolved)
    image_spec = getattr(sbx, "image", None)
    data = {
        "id": sbx.id,
        "state": str(sbx.status.state),
        "image": getattr(image_spec, "image", None),
    }
    obj.output.success_panel(data, title="Sandbox")


@sandbox_group.command("endpoint")
@click.argument("sandbox_id", required=False, default=None)
@output_option("table", "json", "yaml")
@click.pass_obj
@handle_errors
def sandbox_endpoint(obj: ClientContext, sandbox_id: str | None, output_format: str | None) -> None:
    """Print AIO portal URL for sandbox port."""
    prepare_output(obj, output_format, allowed=("table", "json", "yaml", "raw"))
    resolved = obj.resolve_sandbox_id(sandbox_id)
    url = obj.aio_base_url(resolved)
    obj.output.success_panel({"sandbox_id": resolved, "aio_url": url}, title="AIO Endpoint")


@sandbox_group.command("renew")
@click.argument("sandbox_id", required=False, default=None)
@click.option("--timeout", "-t", required=True, help="New TTL duration (e.g. 30m, 2h).")
@output_option("table", "json", "yaml")
@click.pass_obj
@handle_errors
def sandbox_renew(
    obj: ClientContext,
    sandbox_id: str | None,
    timeout: str,
    output_format: str | None,
) -> None:
    """Renew sandbox expiration."""
    prepare_output(obj, output_format, allowed=("table", "json", "yaml", "raw"))
    resolved = obj.resolve_sandbox_id(sandbox_id)
    ttl = parse_duration(timeout)
    manager = obj.get_manager()
    with obj.output.spinner(f"Renewing {resolved}..."):
        resp = manager.renew_sandbox(resolved, ttl)
    obj.output.success_panel(
        {"sandbox_id": resolved, "expires_at": str(resp.expires_at)},
        title="Sandbox Renewed",
    )


@sandbox_group.command("pause")
@click.argument("sandbox_id", required=False, default=None)
@output_option("table", "json", "yaml")
@click.pass_obj
@handle_errors
def sandbox_pause(
    obj: ClientContext,
    sandbox_id: str | None,
    output_format: str | None,
) -> None:
    """Pause a running sandbox while retaining its state."""
    prepare_output(obj, output_format, allowed=("table", "json", "yaml", "raw"))
    resolved = obj.resolve_sandbox_id(sandbox_id)
    obj.get_manager().pause_sandbox(resolved)
    obj.output.success_panel({"id": resolved, "status": "paused"}, title="Sandbox Paused")


@sandbox_group.command("resume")
@click.argument("sandbox_id", required=False, default=None)
@output_option("table", "json", "yaml")
@click.pass_obj
@handle_errors
def sandbox_resume(
    obj: ClientContext,
    sandbox_id: str | None,
    output_format: str | None,
) -> None:
    """Resume a paused sandbox."""
    prepare_output(obj, output_format, allowed=("table", "json", "yaml", "raw"))
    resolved = obj.resolve_sandbox_id(sandbox_id)
    obj.get_manager().resume_sandbox(resolved)
    obj.output.success_panel({"id": resolved, "status": "running"}, title="Sandbox Resumed")


@sandbox_group.command("kill")
@click.argument("sandbox_id", required=False, default=None)
@output_option("table", "json", "yaml")
@click.pass_obj
@handle_errors
def sandbox_kill(obj: ClientContext, sandbox_id: str | None, output_format: str | None) -> None:
    """Delete a sandbox."""
    prepare_output(obj, output_format, allowed=("table", "json", "yaml", "raw"))
    resolved = obj.resolve_sandbox_id(sandbox_id)
    manager = obj.get_manager()
    with obj.output.spinner(f"Killing {resolved}..."):
        manager.kill_sandbox(resolved)
    session = get_current_session()
    if session and session.sandbox_id == resolved:
        clear_current_session()
    obj.output.success_panel({"id": resolved, "status": "killed"}, title="Sandbox Killed")
