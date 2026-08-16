"""Shared Allox 2.0 CLI helpers."""

from __future__ import annotations

import functools
import json
import re
import sys
from datetime import timedelta

import click

from allox.cli.context import ClientContext

_SANDBOX_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

OUTPUT_FORMATS = ("table", "json", "raw", "yaml")

_DURATION_RE = re.compile(
    r"^(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+)s)?$"
)


def parse_duration(value: str) -> timedelta:
    value = value.strip()
    if not value:
        raise click.BadParameter("Duration cannot be empty")
    if value.isdigit():
        return timedelta(seconds=int(value))
    m = _DURATION_RE.match(value)
    if not m or not any(m.groups()):
        raise click.BadParameter(f"Invalid duration '{value}'. Use 10m, 1h30m, 90s.")
    return timedelta(
        hours=int(m.group("hours") or 0),
        minutes=int(m.group("minutes") or 0),
        seconds=int(m.group("seconds") or 0),
    )


def parse_nullable_duration(value: str) -> timedelta | None:
    if value.strip().lower() in ("none", "null", "0", "off", "false"):
        return None
    return parse_duration(value)


KEY_VALUE = click.Tuple([str, str])


def output_option(*allowed: str, default: str = "table"):
    def decorator(f):
        opts = list(allowed) if allowed else list(OUTPUT_FORMATS)
        return click.option(
            "-o",
            "--output",
            "output_format",
            type=click.Choice(opts),
            default=None,
            help=f"Output format ({', '.join(opts)}).",
        )(f)

    return decorator


def prepare_output(
    obj: ClientContext,
    output_format: str | None,
    *,
    allowed: tuple[str, ...] = OUTPUT_FORMATS,
    fallback: str = "table",
) -> None:
    fmt = output_format or fallback
    if fmt not in allowed:
        raise click.ClickException(
            f"Output format '{fmt}' not allowed here. Choose: {', '.join(allowed)}"
        )
    obj.make_output(fmt)


def handle_errors(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except click.ClickException:
            raise
        except click.Abort:
            raise
        except Exception as exc:  # noqa: BLE001 - CLI boundary normalizes SDK errors
            from agent_sandbox.core.api_error import ApiError
            from opensandbox.exceptions import SandboxException

            if isinstance(exc, SandboxException) and exc.error:
                click.echo(
                    f"Sandbox error [{exc.error.code}]: {exc.error.message}",
                    err=True,
                )
            elif isinstance(exc, ApiError):
                click.echo(format_api_error(exc), err=True)
            else:
                click.echo(f"Error: {exc}", err=True)
            sys.exit(1)

    return wrapper


def emit_json(data: object) -> None:
    click.echo(json.dumps(data, indent=2, default=str))


def format_api_error(exc: object) -> str:
    """Format agent-sandbox ApiError for CLI output."""
    status = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        message = body.get("message") or body
        text = f"AIO API error ({status}): {message}"
        msg = str(message)
        if status == 404 and "not found in configuration" in msg:
            text += (
                "\nHint: run `allox aio mcp servers -o json` and use a listed server."
                " Shell/file may be unavailable on this image — try `allox aio exec` / `aio read`."
            )
        elif "Failed to execute tool" in msg:
            text += (
                "\nHint: run `allox aio mcp tools <server> -o json` and use the exact tool name"
                " (e.g. `browser_navigate`, not `navigate`)."
            )
        return text
    return f"AIO API error ({status}): {body}"


def looks_like_sandbox_id(value: str) -> bool:
    """Return True if value matches OpenSandbox sandbox UUID."""
    return bool(_SANDBOX_ID_RE.match(value))


def split_exec_args(
    args: tuple[str, ...],
    *,
    has_current_session: bool,
) -> tuple[str | None, tuple[str, ...]]:
    """Split optional sandbox_id from a shell command (supports `ls -la` flags).

    Rules:
    - Leading UUID → explicit sandbox_id, remainder is command.
    - Otherwise, if a current session exists → entire argv is the command.
    - Otherwise → entire argv is the command (resolve_sandbox_id will error).
    """
    if not args:
        raise click.ClickException(
            "Missing command. Usage: allox aio exec [SANDBOX_ID] COMMAND...\n"
            "Example: allox aio exec ls -la   OR   allox aio exec -- ls -la"
        )
    if looks_like_sandbox_id(args[0]):
        command = args[1:]
        if not command:
            raise click.ClickException("Missing command after sandbox_id.")
        return args[0], command
    # Current session or argv is the shell command (e.g. ls -la).
    if has_current_session or not looks_like_sandbox_id(args[0]):
        return None, args
    return None, args  # pragma: no cover
