"""Canonical v1 state for an explicitly integrated Session memory provider."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

SCHEMA = "allox.session-memory/v1"
MAX_BYTES = 512 * 1024
_VERSION = re.compile(r"[0-9a-f]{64}")


class MemoryStateError(ValueError):
    """Invalid or unsupported logical state, without disclosing its contents."""


def is_version(value: Any) -> bool:
    return isinstance(value, str) and _VERSION.fullmatch(value) is not None


def empty_state() -> dict[str, Any]:
    return {"messages": [], "summary": "", "task_state": {}, "tool_results": []}


def _check_json(value: Any, depth: int = 0) -> None:
    if depth > 32:
        raise MemoryStateError("memory state exceeds the JSON depth limit")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise MemoryStateError("memory state requires string object keys")
            _check_json(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _check_json(item, depth + 1)
    elif value is not None and type(value) not in (str, int, float, bool):
        raise MemoryStateError("memory state must contain only JSON data")


def snapshot(agent_id: str, session_id: str, state: Any) -> tuple[dict[str, Any], str]:
    if not isinstance(state, dict) or state.keys() != empty_state().keys():
        raise MemoryStateError("memory state requires messages, summary, task_state and tool_results")
    if not isinstance(state["summary"], str) or not isinstance(state["task_state"], dict):
        raise MemoryStateError("memory summary must be a string and task_state an object")
    for name in ("messages", "tool_results"):
        if not isinstance(state[name], list) or any(not isinstance(x, dict) for x in state[name]):
            raise MemoryStateError(f"memory {name} must be an array of objects")
    if any(message.get("role") not in ("user", "assistant", "tool") for message in state["messages"]):
        raise MemoryStateError("memory messages require user, assistant or tool roles")
    _check_json(state)
    document = {"schema": SCHEMA, "agent_id": agent_id, "session_id": session_id, "state": state}
    try:
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (ValueError, TypeError, OverflowError) as exc:
        raise MemoryStateError("memory state is not finite JSON") from exc
    if len(encoded) > MAX_BYTES:
        raise MemoryStateError("memory snapshot exceeds 512 KiB")
    return json.loads(encoded), hashlib.sha256(encoded).hexdigest()
