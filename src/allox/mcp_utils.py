"""Helpers for ``allox aio mcp`` argument parsing and output formatting."""

from __future__ import annotations

import json
from typing import Any

import click

from allox.utils import looks_like_sandbox_id


def parse_mcp_target(
    args: tuple[str, ...],
    *,
    require_tool: bool = False,
) -> tuple[str | None, str, str | None]:
    """Split optional sandbox_id, server name, and optional tool name from argv.

    With current session: ``browser`` or ``browser browser_navigate``.
    With explicit id: ``<uuid> browser`` or ``<uuid> browser browser_navigate``.
    """
    if not args:
        raise click.ClickException(
            "Missing arguments. Usage:\n"
            "  allox aio mcp tools [SANDBOX_ID] <server>\n"
            "  allox aio mcp call [SANDBOX_ID] <server> <tool>"
        )

    sandbox_id: str | None = None
    idx = 0
    if looks_like_sandbox_id(args[0]):
        sandbox_id = args[0]
        idx = 1

    remaining = args[idx:]
    if require_tool:
        if len(remaining) < 2:
            raise click.ClickException(
                "Missing server and tool. Usage: allox aio mcp call [SANDBOX_ID] <server> <tool>"
            )
        return sandbox_id, remaining[0], remaining[1]

    if len(remaining) < 1:
        raise click.ClickException(
            "Missing server name. Usage: allox aio mcp tools [SANDBOX_ID] <server>"
        )
    return sandbox_id, remaining[0], None


def build_mcp_request(
    args_json: str | None,
    arg_pairs: tuple[tuple[str, str], ...],
) -> dict[str, Any]:
    """Merge ``--args`` JSON object with repeated ``--arg key=value`` pairs."""
    request: dict[str, Any] = {}
    if args_json is not None:
        try:
            parsed = json.loads(args_json)
        except json.JSONDecodeError as exc:
            raise click.BadParameter(f"Invalid JSON for --args: {exc}") from exc
        if not isinstance(parsed, dict):
            raise click.BadParameter("--args must be a JSON object")
        request.update(parsed)
    for key, value in arg_pairs:
        request[key] = value
    return request


def model_to_dict(obj: Any) -> dict[str, Any]:
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    if isinstance(obj, dict):
        return obj
    return {"value": obj}


def format_mcp_call_raw(result: Any) -> str:
    """Extract human-readable text from an MCP tool call response."""
    parts: list[str] = []
    data = getattr(result, "data", None)
    if data is None:
        message = getattr(result, "message", None)
        return message or ""

    if getattr(data, "is_error", None):
        for item in getattr(data, "content", []) or []:
            text = getattr(item, "text", None)
            if text:
                parts.append(text)
        if not parts:
            parts.append("MCP tool returned an error")
        return "\n".join(parts)

    for item in getattr(data, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            parts.append(text)
        elif getattr(item, "type", None) == "image":
            parts.append(f"[image {getattr(item, 'mime_type', 'image')}, {len(getattr(item, 'data', '') or '')} bytes]")
        else:
            parts.append(json.dumps(model_to_dict(item), ensure_ascii=False))

    structured = getattr(data, "structured_content", None)
    if structured:
        parts.append(json.dumps(structured, indent=2, ensure_ascii=False, default=str))

    hint = getattr(result, "hint", None)
    if hint:
        parts.append(f"hint: {hint}")

    return "\n".join(parts) if parts else ""
