"""Run commands via OpenSandbox execd (not AIO shell)."""

from __future__ import annotations

import shlex
import sys
from datetime import timedelta

import click
from opensandbox.models.execd import RunCommandOpts
from opensandbox.models.execd_sync import ExecutionHandlersSync

from allox.context import ClientContext
from allox.checkpoint import checkpoint_after_success
from allox.session import get_current_session
from allox.utils import (
    handle_errors,
    output_option,
    parse_duration,
    prepare_output,
    split_exec_args,
)


@click.command(
    "run",
    help="Run a command via execd. Use `--` before the command payload.",
    context_settings={"allow_interspersed_args": False},
)
@click.option("-w", "--workdir", default=None, help="Working directory in sandbox.")
@click.option("-t", "--timeout", default=None, help="Command timeout (e.g. 30s, 5m).")
@output_option("raw", "json")
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.pass_obj
@handle_errors
def run_command(
    obj: ClientContext,
    workdir: str | None,
    timeout: str | None,
    output_format: str | None,
    args: tuple[str, ...],
) -> None:
    """Execute a command through OpenSandbox execd (ops / non-AIO path)."""
    prepare_output(obj, output_format, allowed=("raw", "json"), fallback="raw")
    sandbox_id, command = split_exec_args(
        args,
        has_current_session=get_current_session() is not None,
    )
    resolved_id = obj.resolve_sandbox_id(sandbox_id)
    cmd_str = " ".join(shlex.quote(arg) for arg in command)
    cmd_timeout: timedelta | None = parse_duration(timeout) if timeout else None

    sandbox = obj.connect_sandbox(resolved_id)
    try:
        opts = RunCommandOpts(
            background=False,
            working_directory=workdir,
            timeout=cmd_timeout,
        )

        last_text = ""

        def on_stdout(msg) -> None:
            nonlocal last_text
            last_text = msg.text
            sys.stdout.write(msg.text)
            sys.stdout.flush()

        def on_stderr(msg) -> None:
            nonlocal last_text
            last_text = msg.text
            sys.stderr.write(msg.text)
            sys.stderr.flush()

        handlers = ExecutionHandlersSync(on_stdout=on_stdout, on_stderr=on_stderr)
        if obj.verbose:
            click.echo(f"[verbose] execd run: {cmd_str!r} (sandbox={resolved_id})", err=True)
        execution = sandbox.commands.run(cmd_str, opts=opts, handlers=handlers)
        succeeded = not execution.error and getattr(execution, "exit_code", 0) in (0, None)

        if last_text and not last_text.endswith("\n"):
            sys.stdout.write("\n")
            sys.stdout.flush()

        if obj.output.fmt == "json":
            from allox.utils import emit_json

            emit_json(
                {
                    "sandbox_id": resolved_id,
                    "execution_id": execution.id,
                    "exit_code": getattr(execution, "exit_code", None),
                    "error": (
                        {"name": execution.error.name, "value": execution.error.value}
                        if execution.error
                        else None
                    ),
                }
            )
            if execution.error:
                sys.exit(1)
            if succeeded:
                checkpoint_after_success(obj, resolved_id, "run")
            return

        if execution.error:
            obj.output.error_panel(
                f"{execution.error.name}: {execution.error.value}",
                title="Execution Error",
            )
            sys.exit(1)
        if succeeded:
            checkpoint_after_success(obj, resolved_id, "run")
    finally:
        sandbox.close()
