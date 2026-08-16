"""Agent/Session workspace and rollback commands inside the Kata VM."""

from __future__ import annotations

import json
import posixpath
import shlex
import sys
import time
from pathlib import PurePosixPath
from typing import Any

import click
from opensandbox.models.execd import RunCommandOpts
from opensandbox.models.execd_sync import ExecutionHandlersSync

from allox.cli.context import ClientContext
from allox.cli.utils import KEY_VALUE, handle_errors, output_option, parse_duration, prepare_output
from allox.workspace.store import WorkspaceError, validate_id


@click.group("workspace", invoke_without_command=True)
@click.pass_context
def workspace_group(ctx: click.Context) -> None:
    """Manage Allox 2.0 Agent/Session workspaces."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


def _client(obj: ClientContext):
    return obj.get_workspace_client()


def _emit_result(obj: ClientContext, result: dict[str, Any], title: str) -> None:
    obj.output.success_panel(result, title=title)


@workspace_group.command("init")
@output_option("table", "json", "yaml")
@click.pass_obj
@handle_errors
def workspace_init(obj: ClientContext, output_format: str | None) -> None:
    """Initialize the daemon's Btrfs workspace store."""
    prepare_output(obj, output_format, allowed=("table", "json", "yaml", "raw"))
    _emit_result(obj, _client(obj).rpc("store.initialize"), "Workspace Store Initialized")


@workspace_group.command("agent-create")
@click.argument("agent_id")
@output_option("table", "json", "yaml")
@click.pass_obj
@handle_errors
def workspace_agent_create(
    obj: ClientContext, agent_id: str, output_format: str | None
) -> None:
    """Create an Agent's first-level workspace."""
    prepare_output(obj, output_format, allowed=("table", "json", "yaml", "raw"))
    validate_id("agent", agent_id)
    result = _client(obj).rpc("agent.create", agent_id=agent_id, origin="allox-cli")
    _emit_result(obj, result, "Agent Workspace Created")


@workspace_group.command("session-create")
@click.argument("agent_id")
@click.argument("session_id")
@output_option("table", "json", "yaml")
@click.pass_obj
@handle_errors
def workspace_session_create(
    obj: ClientContext,
    agent_id: str,
    session_id: str,
    output_format: str | None,
) -> None:
    """Create a Session Btrfs subvolume below an Agent."""
    prepare_output(obj, output_format, allowed=("table", "json", "yaml", "raw"))
    validate_id("agent", agent_id)
    validate_id("session", session_id)
    result = _client(obj).rpc(
        "session.create", agent_id=agent_id, session_id=session_id, origin="allox-cli"
    )
    _emit_result(obj, result, "Session Workspace Created")


@workspace_group.command("get")
@click.argument("agent_id")
@click.argument("session_id")
@output_option("table", "json", "yaml")
@click.pass_obj
@handle_errors
def workspace_get(
    obj: ClientContext,
    agent_id: str,
    session_id: str,
    output_format: str | None,
) -> None:
    """Describe one Agent/Session workspace."""
    prepare_output(obj, output_format, allowed=("table", "json", "yaml", "raw"))
    result = _client(obj).rpc(
        "session.describe", agent_id=agent_id, session_id=session_id
    )
    _emit_result(obj, result, "Session Workspace")


@workspace_group.command("checkpoint")
@click.argument("agent_id")
@click.argument("session_id")
@click.option("--name", "checkpoint_id", default=None)
@click.option("--message", default=None, help="Human-readable checkpoint message.")
@click.option("--metadata", default=None, help="JSON object stored with the checkpoint.")
@click.option("--pin", "pinned", is_flag=True, help="Mark this checkpoint as pinned.")
@output_option("table", "json", "yaml")
@click.pass_obj
@handle_errors
def workspace_checkpoint(
    obj: ClientContext,
    agent_id: str,
    session_id: str,
    checkpoint_id: str | None,
    message: str | None,
    metadata: str | None,
    pinned: bool,
    output_format: str | None,
) -> None:
    """Checkpoint only the selected Session workspace."""
    prepare_output(obj, output_format, allowed=("table", "json", "yaml", "raw"))
    parsed_metadata = None
    if metadata is not None:
        try:
            parsed_metadata = json.loads(metadata)
        except json.JSONDecodeError as exc:
            raise WorkspaceError(f"metadata must be valid JSON: {exc.msg}") from exc
        if not isinstance(parsed_metadata, dict):
            raise WorkspaceError("metadata must be a JSON object")
    result = _client(obj).rpc(
        "checkpoint.create",
        agent_id=agent_id,
        session_id=session_id,
        checkpoint_id=checkpoint_id,
        origin="allox-cli",
        message=message,
        pinned=pinned,
        metadata=parsed_metadata,
    )
    _emit_result(obj, result, "Session Checkpoint Created")


@workspace_group.command("checkpoint-list")
@click.argument("agent_id")
@click.argument("session_id")
@output_option("table", "json", "yaml")
@click.pass_obj
@handle_errors
def workspace_checkpoint_list(
    obj: ClientContext,
    agent_id: str,
    session_id: str,
    output_format: str | None,
) -> None:
    """List checkpoints belonging to one Session."""
    prepare_output(obj, output_format, allowed=("table", "json", "yaml", "raw"))
    result = _client(obj).rpc(
        "checkpoint.list", agent_id=agent_id, session_id=session_id
    )
    rows = []
    for item in result.get("checkpoints", []):
        rows.append(
            {
                "agent_id": agent_id,
                "session_id": session_id,
                "checkpoint_id": item["checkpoint_id"],
                "head": item["checkpoint_id"] == result.get("head"),
                "parent_id": item.get("parent_id"),
                "message": item.get("message"),
                "pinned": item.get("pinned", False),
                "metadata": item.get("metadata"),
            }
        )
    obj.output.print_rows(
        rows,
        [
            "agent_id",
            "session_id",
            "checkpoint_id",
            "head",
            "parent_id",
            "message",
            "pinned",
            "metadata",
        ],
        title="Session Checkpoints",
    )


@workspace_group.command("checkpoint-delete")
@click.argument("agent_id")
@click.argument("session_id")
@click.argument("checkpoint_id")
@click.option("--force", is_flag=True, help="Delete even when the checkpoint is pinned.")
@output_option("table", "json", "yaml")
@click.pass_obj
@handle_errors
def workspace_checkpoint_delete(
    obj: ClientContext,
    agent_id: str,
    session_id: str,
    checkpoint_id: str,
    force: bool,
    output_format: str | None,
) -> None:
    """Delete a Session checkpoint."""
    prepare_output(obj, output_format, allowed=("table", "json", "yaml", "raw"))
    result = _client(obj).rpc(
        "checkpoint.delete",
        agent_id=agent_id,
        session_id=session_id,
        checkpoint_id=checkpoint_id,
        origin="allox-cli",
        force=force,
    )
    _emit_result(obj, result, "Session Checkpoint Deleted")


@workspace_group.command("rollback")
@click.argument("agent_id")
@click.argument("session_id")
@click.argument("checkpoint_id", required=False, default=None)
@click.option(
    "--num-ancestors",
    "-n",
    type=click.IntRange(min=1),
    default=None,
    help="Rollback by lineage depth; 1 selects the current checkpoint head.",
)
@click.option(
    "--scrub-runtime/--keep-runtime-entries",
    default=True,
    help="Remove stale socket/FIFO nodes restored below the Session /tmp.",
)
@output_option("table", "json", "yaml")
@click.pass_obj
@handle_errors
def workspace_rollback(
    obj: ClientContext,
    agent_id: str,
    session_id: str,
    checkpoint_id: str | None,
    num_ancestors: int | None,
    scrub_runtime: bool,
    output_format: str | None,
) -> None:
    """Terminate Session background executions, then rollback its workspace."""
    prepare_output(obj, output_format, allowed=("table", "json", "yaml", "raw"))
    client = _client(obj)
    reset = client.rpc("runtime.begin_reset", agent_id=agent_id, session_id=session_id)
    reset_succeeded = False
    try:
        for execution in reset["executions"]:
            sandbox = obj.connect_sandbox(execution["sandbox_id"])
            try:
                sandbox.commands.interrupt(execution["execution_id"])
                deadline = time.monotonic() + 15
                while True:
                    status = sandbox.commands.get_command_status(execution["execution_id"])
                    state = str(getattr(status, "state", "")).lower()
                    if state not in {"running", "pending"}:
                        break
                    if time.monotonic() >= deadline:
                        raise WorkspaceError(
                            f"timed out terminating session execution {execution['execution_id']}"
                        )
                    time.sleep(0.1)
            finally:
                sandbox.close()
        result = client.rpc(
            "session.rollback_after_runtime_reset",
            agent_id=agent_id,
            session_id=session_id,
            checkpoint_id=checkpoint_id,
            num_ancestors=num_ancestors,
            scrub_runtime=scrub_runtime,
            origin="allox-cli",
            reset_token=reset["reset_token"],
        )
        result["runtime_reset"] = {
            "terminated_execution_ids": [item["execution_id"] for item in reset["executions"]]
        }
        reset_succeeded = True
    finally:
        client.rpc("runtime.complete_reset", reset_token=reset["reset_token"], success=reset_succeeded)
    _emit_result(obj, result, "Session Rolled Back")


def _workspace_path(vm_root: str, relative_workspace: str) -> str:
    relative = PurePosixPath(relative_workspace)
    if relative.is_absolute() or not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise WorkspaceError("workspace daemon returned an unsafe relative path")
    return posixpath.join(posixpath.normpath(vm_root), *relative.parts)


def build_bwrap_argv(
    workspace_path: str,
    agent_id: str,
    session_id: str,
    command: tuple[str, ...],
    environment: tuple[tuple[str, str], ...] = (),
) -> list[str]:
    """Build a mount namespace that exposes only one Session as /workspace."""
    validate_id("agent", agent_id)
    validate_id("session", session_id)
    if not command:
        raise WorkspaceError("workspace run requires a command after --")
    argv = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-pid",
        "--tmpfs",
        "/",
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/sbin",
        "/sbin",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--ro-bind",
        "/etc",
        "/etc",
        "--dev",
        "/dev",
        "--bind",
        workspace_path,
        "/workspace",
        "--tmpfs",
        "/tmp",
        "--clearenv",
        "--setenv",
        "PATH",
        "/usr/local/bin:/usr/bin:/bin",
        "--setenv",
        "HOME",
        "/workspace",
        "--setenv",
        "TMPDIR",
        "/tmp",
        "--setenv",
        "ALLOX_AGENT_ID",
        agent_id,
        "--setenv",
        "ALLOX_SESSION_ID",
        session_id,
    ]
    for key, value in environment:
        argv.extend(["--setenv", key, value])
    wrapper = """\
set -eu
cp -a /workspace/.allox-tmp/. /tmp/
status=0
"$@" || status=$?
find /tmp -xdev \\( -type s -o -type p -o -type b -o -type c \\) -delete
rm -rf /workspace/.allox-tmp.new
install -d -m 700 /workspace/.allox-tmp.new
cp -a /tmp/. /workspace/.allox-tmp.new/
rm -rf /workspace/.allox-tmp
mv /workspace/.allox-tmp.new /workspace/.allox-tmp
exit "$status"
"""
    argv.extend(["--chdir", "/workspace", "sh", "-c", wrapper, "allox-runtime", *command])
    return argv


def build_managed_workspace_argv(
    workspace_path: str,
    agent_id: str,
    session_id: str,
    command: tuple[str, ...],
    environment: tuple[tuple[str, str], ...] = (),
) -> list[str]:
    """Run a command in a managed Session workspace inside the Kata VM.

    Btrfs controls the Session directory and checkpoints. The VM runtime owns
    process lifetime, while the workspace daemon records and terminates
    registered background executions before rollback.
    """
    validate_id("agent", agent_id)
    validate_id("session", session_id)
    if not command:
        raise WorkspaceError("workspace run requires a command after --")
    env = [
        "env",
        "-i",
        "PATH=/usr/local/bin:/usr/bin:/bin",
        f"HOME={workspace_path}",
        f"TMPDIR={posixpath.join(workspace_path, '.allox-tmp')}",
        f"ALLOX_AGENT_ID={agent_id}",
        f"ALLOX_SESSION_ID={session_id}",
    ]
    env.extend(f"{key}={value}" for key, value in environment)
    return [
        *env,
        "sh",
        "-c",
        'cd -- "$1" && shift && exec "$@"',
        "allox-workspace",
        workspace_path,
        *command,
    ]


@workspace_group.command(
    "run",
    context_settings={"allow_interspersed_args": False},
)
@click.argument("agent_id")
@click.argument("session_id")
@click.option("--sandbox", "sandbox_id", default=None, help="Allox VM sandbox ID.")
@click.option("--timeout", default=None, help="Command timeout, e.g. 30s or 5m.")
@click.option("--env", "environment", multiple=True, type=KEY_VALUE)
@click.option(
    "--background",
    is_flag=True,
    help="Start a registered background execution in this Session.",
)
@click.option("--checkpoint-on-success", is_flag=True)
@output_option("raw", "json")
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
@click.pass_obj
@handle_errors
def workspace_run(
    obj: ClientContext,
    agent_id: str,
    session_id: str,
    sandbox_id: str | None,
    timeout: str | None,
    environment: tuple[tuple[str, str], ...],
    background: bool,
    checkpoint_on_success: bool,
    output_format: str | None,
    command: tuple[str, ...],
) -> None:
    """Run a command in one Session workspace inside the selected Kata VM."""
    prepare_output(obj, output_format, allowed=("raw", "json"), fallback="raw")
    if command and command[0] == "--":
        command = command[1:]
    resolved_sandbox = obj.resolve_sandbox_id(sandbox_id)
    client = _client(obj)
    description = client.rpc(
        "session.describe", agent_id=agent_id, session_id=session_id
    )
    workspace_path = _workspace_path(
        str(obj.resolved_config.get("workspace_vm_root", "/var/lib/allox/workspaces")),
        description["relative_workspace"],
    )
    execution_mode = str(obj.resolved_config.get("workspace_execution_mode", "managed")).lower()
    if execution_mode in {"managed", "anolisa"}:
        execution_argv = build_managed_workspace_argv(
            workspace_path, agent_id, session_id, command, environment
        )
    elif execution_mode == "ephemeral":
        if background:
            raise WorkspaceError("--background is only available in managed execution mode")
        execution_argv = build_bwrap_argv(
            workspace_path, agent_id, session_id, command, environment
        )
    else:
        raise WorkspaceError(
            "workspace.execution_mode must be 'managed' or 'ephemeral'"
        )
    command_string = shlex.join(execution_argv)
    lease = client.rpc(
        "execution.acquire",
        agent_id=agent_id,
        session_id=session_id,
    )
    sandbox = None
    execution = None
    execution_accepted = False
    completed_successfully = False
    try:
        sandbox = obj.connect_sandbox(resolved_sandbox)
        handlers = ExecutionHandlersSync(
            on_stdout=lambda message: (sys.stdout.write(message.text), sys.stdout.flush()),
            on_stderr=lambda message: (sys.stderr.write(message.text), sys.stderr.flush()),
        )
        execution = sandbox.commands.run(
            command_string,
            opts=RunCommandOpts(
                background=background,
                timeout=parse_duration(timeout) if timeout else None,
            ),
            handlers=handlers,
        )
        execution_accepted = not execution.error and (
            background or getattr(execution, "exit_code", 0) in (0, None)
        )
        completed_successfully = (
            not background and execution_accepted
        )
        if background and execution_accepted:
            try:
                client.rpc(
                    "runtime.register_background",
                    agent_id=agent_id,
                    session_id=session_id,
                    sandbox_id=resolved_sandbox,
                    execution_id=execution.id,
                )
            except Exception:
                sandbox.commands.interrupt(execution.id)
                raise
    finally:
        if sandbox is not None:
            sandbox.close()
        try:
            client.rpc("execution.release", lease_token=lease["lease_token"])
        except click.ClickException as exc:
            click.echo(f"Warning: failed to release workspace execution lease: {exc}", err=True)

    checkpoint = None
    if completed_successfully and checkpoint_on_success:
        checkpoint = client.rpc(
            "checkpoint.create",
            agent_id=agent_id,
            session_id=session_id,
            checkpoint_id=None,
            origin="workspace.run",
        )

    if obj.output.fmt == "json":
        from allox.cli.utils import emit_json

        emit_json(
            {
                "sandbox_id": resolved_sandbox,
                "agent_id": agent_id,
                "session_id": session_id,
                "execution_id": getattr(execution, "id", None),
                "background": background,
                "exit_code": getattr(execution, "exit_code", None),
                "checkpoint_id": checkpoint.get("checkpoint_id") if checkpoint else None,
                "error": (
                    {"name": execution.error.name, "value": execution.error.value}
                    if execution and execution.error
                    else None
                ),
            }
        )
    if not execution_accepted:
        raise SystemExit(1)
