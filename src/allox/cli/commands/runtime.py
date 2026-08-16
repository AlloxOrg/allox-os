"""Commands for tools running inside the outer Kata VM."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from allox.cli.context import ClientContext
from allox.cli.utils import (
    KEY_VALUE,
    emit_json,
    handle_errors,
    output_option,
    prepare_output,
    split_exec_args,
)
from allox.runtime.mcp import (
    build_mcp_request,
    format_mcp_call_raw,
    model_to_dict,
    parse_mcp_target,
)
from allox.vm.checkpoints import checkpoint_after_success
from allox.vm.selection import get_current_session


def _format_jupyter_outputs(outputs: list[Any]) -> str:
    parts: list[str] = []
    for out in outputs:
        otype = getattr(out, "output_type", None)
        if otype == "stream" and getattr(out, "text", None):
            name = getattr(out, "name", "stdout") or "stdout"
            parts.append(f"[{name}]\n{out.text}")
        elif otype == "error":
            tb = getattr(out, "traceback", None) or []
            if tb:
                parts.append("".join(tb))
            else:
                parts.append(f"{getattr(out, 'ename', 'Error')}: {getattr(out, 'evalue', '')}")
        elif getattr(out, "text", None):
            parts.append(out.text)
        elif getattr(out, "data", None):
            text = out.data.get("text/plain") if isinstance(out.data, dict) else None
            if text:
                parts.append(text if isinstance(text, str) else "".join(text))
    return "\n".join(parts)


@click.group("aio", invoke_without_command=True)
@click.pass_context
def aio_group(ctx: click.Context) -> None:
    """Agent tools inside the selected Kata VM (AIO, browser, MCP)."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@aio_group.command(
    "exec",
    context_settings={"allow_interspersed_args": False},
)
@click.option("--workdir", "-w", default=None, help="Working directory (absolute path in sandbox).")
@click.option("--timeout", type=int, default=60, help="Command timeout in seconds.")
@output_option("raw", "json")
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.pass_obj
@handle_errors
def aio_exec(
    obj: ClientContext,
    workdir: str | None,
    timeout: int,
    output_format: str | None,
    args: tuple[str, ...],
) -> None:
    """Run a shell command in the AIO sandbox.

    Options must appear before the command. Supports flags like ``ls -la``.
    You may also use ``--`` before the command: ``allox aio exec -- ls -la``.
    """
    prepare_output(obj, output_format, allowed=("raw", "json"), fallback="raw")
    sandbox_id, command = split_exec_args(
        args,
        has_current_session=get_current_session() is not None,
    )
    resolved = obj.resolve_sandbox_id(sandbox_id)
    cmd = " ".join(command)
    client = obj.aio_client(resolved)
    kwargs: dict[str, Any] = {"command": cmd, "timeout": timeout}
    if workdir:
        kwargs["exec_dir"] = workdir
    result = client.shell.exec_command(**kwargs)
    exit_code = getattr(result.data, "exit_code", None)
    if obj.output.fmt == "json":
        import json

        click.echo(json.dumps({"output": result.data.output, "exit_code": exit_code}))
        if exit_code in (0, None):
            checkpoint_after_success(obj, resolved, "aio.exec")
        return
    click.echo(result.data.output, nl=False)
    if exit_code in (0, None):
        checkpoint_after_success(obj, resolved, "aio.exec")


@aio_group.command("read")
@click.argument("sandbox_id", required=False, default=None)
@click.argument("path")
@output_option("raw", "json")
@click.pass_obj
@handle_errors
def aio_read(obj: ClientContext, sandbox_id: str | None, path: str, output_format: str | None) -> None:
    """Read a file from the AIO sandbox."""
    prepare_output(obj, output_format, allowed=("raw", "json"), fallback="raw")
    resolved = obj.resolve_sandbox_id(sandbox_id)
    client = obj.aio_client(resolved)
    content = client.file.read_file(file=path)
    if obj.output.fmt == "json":
        import json

        click.echo(json.dumps({"path": path, "content": content.data.content}))
        return
    click.echo(content.data.content, nl=False)


@aio_group.command("screenshot")
@click.argument("sandbox_id", required=False, default=None)
@click.option("-f", "--file", "out_path", type=click.Path(), default="screenshot.png", help="Local PNG path.")
@output_option("table", "json", "yaml")
@click.pass_obj
@handle_errors
def aio_screenshot(
    obj: ClientContext,
    sandbox_id: str | None,
    out_path: str,
    output_format: str | None,
) -> None:
    """Save a browser screenshot from the AIO sandbox."""
    prepare_output(obj, output_format, allowed=("table", "json", "yaml", "raw"))
    resolved = obj.resolve_sandbox_id(sandbox_id)
    client = obj.aio_client(resolved)
    path = Path(out_path)
    with path.open("wb") as f:
        for chunk in client.browser.screenshot():
            f.write(chunk)
    obj.output.success_panel(
        {"sandbox_id": resolved, "path": str(path.resolve())},
        title="Screenshot Saved",
    )


@click.group("jupyter", invoke_without_command=True)
@click.pass_context
def jupyter_group(ctx: click.Context) -> None:
    """Jupyter kernel operations in an AIO sandbox."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@jupyter_group.command("run")
@click.argument("sandbox_id", required=False, default=None)
@click.option("--code", "-c", required=True, help="Python code to execute.")
@click.option("--timeout", type=int, default=None, help="Execution timeout in seconds.")
@click.option("--session-id", default=None, help="Reuse an existing Jupyter session.")
@output_option("raw", "json")
@click.pass_obj
@handle_errors
def aio_jupyter_run(
    obj: ClientContext,
    sandbox_id: str | None,
    code: str,
    timeout: int | None,
    session_id: str | None,
    output_format: str | None,
) -> None:
    """Execute Python code via the AIO Jupyter kernel."""
    prepare_output(obj, output_format, allowed=("raw", "json"), fallback="raw")
    resolved = obj.resolve_sandbox_id(sandbox_id)
    client = obj.aio_client(resolved)
    kwargs: dict[str, Any] = {"code": code}
    if timeout is not None:
        kwargs["timeout"] = timeout
    if session_id:
        kwargs["session_id"] = session_id
    result = client.jupyter.execute_code(**kwargs)
    payload = model_to_dict(result.data)
    if obj.output.fmt == "json":
        emit_json(payload)
        if result.data.status == "ok":
            checkpoint_after_success(obj, resolved, "aio.jupyter")
        return
    text = _format_jupyter_outputs(result.data.outputs)
    if text:
        click.echo(text, nl=not text.endswith("\n"))
    if result.data.status != "ok":
        raise click.ClickException(f"Jupyter execution status: {result.data.status}")
    checkpoint_after_success(obj, resolved, "aio.jupyter")


@click.group("browser", invoke_without_command=True)
@click.pass_context
def browser_group(ctx: click.Context) -> None:
    """Browser / CDP operations in an AIO sandbox."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@browser_group.command("info")
@click.argument("sandbox_id", required=False, default=None)
@output_option("table", "json", "yaml")
@click.pass_obj
@handle_errors
def aio_browser_info(
    obj: ClientContext,
    sandbox_id: str | None,
    output_format: str | None,
) -> None:
    """Show browser CDP URL and viewport (for Playwright / automation)."""
    prepare_output(obj, output_format, allowed=("table", "json", "yaml", "raw"))
    resolved = obj.resolve_sandbox_id(sandbox_id)
    client = obj.aio_client(resolved)
    result = client.browser.get_info()
    info = result.data
    data = {
        "sandbox_id": resolved,
        "cdp_url": info.cdp_url,
        "vnc_url": info.vnc_url,
        "cdp_ui_url": info.cdp_ui_url,
        "user_agent": info.user_agent,
        "viewport": model_to_dict(info.viewport),
    }
    if info.page_viewport is not None:
        data["page_viewport"] = model_to_dict(info.page_viewport)
    obj.output.success_panel(data, title="Browser Info")


@click.group("mcp", invoke_without_command=True)
@click.pass_context
def mcp_group(ctx: click.Context) -> None:
    """MCP servers inside an AIO sandbox (browser, file, shell, markitdown)."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@mcp_group.command("servers")
@click.argument("sandbox_id", required=False, default=None)
@output_option("table", "json", "yaml")
@click.pass_obj
@handle_errors
def aio_mcp_servers(
    obj: ClientContext,
    sandbox_id: str | None,
    output_format: str | None,
) -> None:
    """List MCP servers available in the sandbox."""
    prepare_output(obj, output_format, allowed=("table", "json", "yaml", "raw"), fallback="table")
    resolved = obj.resolve_sandbox_id(sandbox_id)
    client = obj.aio_client(resolved)
    result = client.mcp.list_mcp_servers()
    servers = result.data or []
    if obj.output.fmt == "json":
        emit_json({"sandbox_id": resolved, "servers": servers})
        return
    rows = [{"server": name} for name in servers]
    obj.output.print_rows(rows, ["server"], title=f"MCP Servers ({resolved})")


@mcp_group.command("tools")
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@output_option("table", "json", "yaml")
@click.pass_obj
@handle_errors
def aio_mcp_tools(
    obj: ClientContext,
    args: tuple[str, ...],
    output_format: str | None,
) -> None:
    """List tools for an MCP server: ``allox aio mcp tools [SANDBOX_ID] <server>``."""
    prepare_output(obj, output_format, allowed=("table", "json", "yaml", "raw"), fallback="table")
    sandbox_id, server, _ = parse_mcp_target(args, require_tool=False)
    resolved = obj.resolve_sandbox_id(sandbox_id)
    client = obj.aio_client(resolved)
    result = client.mcp.list_mcp_tools(server_name=server)
    tools = (result.data.tools if result.data else []) or []
    if obj.output.fmt == "json":
        emit_json(
            {
                "sandbox_id": resolved,
                "server": server,
                "tools": [model_to_dict(t) for t in tools],
            }
        )
        return
    rows = [
        {
            "name": t.name,
            "description": (t.description or "-").replace("\n", " ")[:120],
        }
        for t in tools
    ]
    obj.output.print_rows(rows, ["name", "description"], title=f"MCP Tools: {server}")


@mcp_group.command("call")
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.option("--args", "args_json", default=None, help="Tool arguments as a JSON object.")
@click.option(
    "--arg",
    "arg_pairs",
    multiple=True,
    type=KEY_VALUE,
    help="Single tool argument as key=value (repeatable).",
)
@output_option("raw", "json")
@click.pass_obj
@handle_errors
def aio_mcp_call(
    obj: ClientContext,
    args: tuple[str, ...],
    args_json: str | None,
    arg_pairs: tuple[tuple[str, str], ...],
    output_format: str | None,
) -> None:
    """Call an MCP tool: ``allox aio mcp call [SANDBOX_ID] <server> <tool>``."""
    prepare_output(obj, output_format, allowed=("raw", "json"), fallback="raw")
    sandbox_id, server, tool = parse_mcp_target(args, require_tool=True)
    resolved = obj.resolve_sandbox_id(sandbox_id)
    client = obj.aio_client(resolved)
    request = build_mcp_request(args_json, arg_pairs)
    result = client.mcp.execute_mcp_tool(
        server_name=server,
        tool_name=tool,
        request=request,
    )
    if obj.output.fmt == "json":
        emit_json(
            {
                "sandbox_id": resolved,
                "server": server,
                "tool": tool,
                "request": request,
                "response": model_to_dict(result),
            }
        )
        if result.data and getattr(result.data, "is_error", None):
            raise click.ClickException(f"MCP tool '{server}/{tool}' failed")
        return

    text = format_mcp_call_raw(result)
    if text:
        click.echo(text, nl=not text.endswith("\n"))
    if result.data and getattr(result.data, "is_error", None):
        raise click.ClickException(f"MCP tool '{server}/{tool}' failed")


aio_group.add_command(jupyter_group)
aio_group.add_command(browser_group)
aio_group.add_command(mcp_group)
