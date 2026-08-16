"""Tests for the pluggable Agent runtime lifecycle adapters."""

from __future__ import annotations

import asyncio
from typing import ClassVar
from unittest.mock import MagicMock

import pytest

from allox.integrations import builtin_registry
from allox.integrations.langchain import extract_user_message


class FakeRunnable:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[tuple[object, object, dict[object, object]]] = []

    def invoke(self, input, config=None, **kwargs):
        self.calls.append((input, config, kwargs))
        if self.error:
            raise self.error
        return {"ok": True}

    async def ainvoke(self, input, config=None, **kwargs):
        return self.invoke(input, config, **kwargs)


def rpc_client():
    client = MagicMock()
    client.rpc.side_effect = lambda action, **params: {
        "action": action,
        "checkpoint_id": params.get("checkpoint_id"),
    }
    return client


def test_builtin_plugin_wraps_langchain_invoke_without_manual_hooks():
    client = rpc_client()
    plugin = builtin_registry().create(
        "langchain-turn-checkpoint", client, "agent-a", "session-1", enabled=True
    )
    wrapped = plugin.wrap(FakeRunnable(), runtime_session_id="langchain-thread-1")

    result = wrapped.invoke(
        {"messages": [{"role": "user", "content": "write state"}]},
        config={"metadata": {"allox_turn_id": "turn-1"}},
    )

    assert result == {"ok": True}
    assert client.rpc.call_count == 2
    baseline, turn = client.rpc.call_args_list
    assert baseline.kwargs["metadata"]["runtime_session_id"] == "langchain-thread-1"
    assert turn.kwargs["message"] == "write state"
    assert turn.kwargs["metadata"]["turn_id"] == "turn-1"


def test_plugin_wraps_async_invoke_and_records_failure():
    client = rpc_client()
    plugin = builtin_registry().create(
        "langchain-turn-checkpoint", client, "agent-a", "session-1", enabled=True
    )
    wrapped = plugin.wrap(FakeRunnable(error=ValueError("bad model call")))

    with pytest.raises(ValueError, match="bad model call"):
        asyncio.run(wrapped.ainvoke({"messages": [{"role": "user", "content": "hello"}]}))

    assert client.rpc.call_count == 2
    assert client.rpc.call_args_list[-1].kwargs["metadata"]["success"] is False


def test_plugin_rollback_suppresses_following_turn_checkpoint():
    client = rpc_client()
    plugin = builtin_registry().create(
        "langchain-turn-checkpoint", client, "agent-a", "session-1", enabled=True
    )
    wrapped = plugin.wrap(FakeRunnable())

    plugin.rollback("before")
    wrapped.invoke({"messages": [{"role": "user", "content": "retry"}]})

    checkpoint_calls = [
        call for call in client.rpc.call_args_list if call.args[0] == "checkpoint.create"
    ]
    assert len(checkpoint_calls) == 1  # session baseline only; retry turn is skipped


def test_message_extractor_supports_langchain_style_human_messages():
    class HumanMessage:
        type = "human"
        content: ClassVar[list[dict[str, str]]] = [{"type": "text", "text": "keep this"}]

    assert extract_user_message({"messages": [HumanMessage()]}) == "keep this"
