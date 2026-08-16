"""Optional LangChain integration for per-Session turn checkpoints.

There is intentionally no LangChain import in this module. LangChain's
``Runnable`` protocol is duck-typed, so installing ``allox-cli`` never pulls a
specific Agent framework into the daemon or CLI environment.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from allox.workspace.lifecycle import TurnCheckpointLifecycle, WorkspaceRPC


class Runnable(Protocol):
    """Subset of the LangChain Runnable API consumed by the adapter."""

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any: ...

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any: ...


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(_content_to_text(item) for item in content)
    if isinstance(content, Mapping):
        return _content_to_text(content.get("text", content.get("content", "")))
    return str(content) if content is not None else ""


def extract_user_message(input: Any) -> str:
    """Extract the last human/user message from common LangChain inputs."""
    messages = input.get("messages") if isinstance(input, Mapping) else None
    if not isinstance(messages, list):
        return "agent turn"
    for message in reversed(messages):
        if isinstance(message, Mapping):
            role = message.get("role") or message.get("type")
            content = message.get("content", "")
        else:
            role = getattr(message, "type", None) or getattr(message, "role", None)
            content = getattr(message, "content", "")
        if str(role).lower() in {"human", "user"}:
            text = _content_to_text(content).strip()
            return text or "agent turn"
    return "agent turn"


def extract_turn_id(config: Any) -> str | None:
    """Read an optional stable Allox turn id from RunnableConfig metadata."""
    if not isinstance(config, Mapping):
        return None
    metadata = config.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    turn_id = metadata.get("allox_turn_id")
    return str(turn_id) if turn_id is not None else None


class CheckpointingRunnable:
    """Runnable proxy that maps one outer ``invoke`` call to one Allox turn."""

    def __init__(
        self,
        runnable: Runnable,
        lifecycle: TurnCheckpointLifecycle,
        *,
        runtime_session_id: str | None = None,
    ) -> None:
        self.runnable = runnable
        self.lifecycle = lifecycle
        self.runtime_session_id = runtime_session_id

    def _start(self) -> None:
        self.lifecycle.on_session_start(runtime_session_id=self.runtime_session_id)

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        self._start()
        with self.lifecycle.turn(
            extract_user_message(input), turn_id=extract_turn_id(config)
        ):
            return self.runnable.invoke(input, config=config, **kwargs)

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        self._start()
        self.lifecycle.on_user_message(extract_user_message(input))
        turn_id = extract_turn_id(config)
        try:
            result = await self.runnable.ainvoke(input, config=config, **kwargs)
        except BaseException as exc:
            self.lifecycle.on_turn_end(
                turn_id=turn_id,
                completed=False,
                interrupted=isinstance(exc, (KeyboardInterrupt, SystemExit)),
            )
            raise
        self.lifecycle.on_turn_end(turn_id=turn_id, completed=True, interrupted=False)
        return result


class LangChainTurnCheckpointPlugin:
    """Allox 2.0 Session lifecycle plugin for a LangChain Agent runnable."""

    plugin_name = "langchain-turn-checkpoint"

    def __init__(
        self,
        client: WorkspaceRPC,
        agent_id: str,
        session_id: str,
        *,
        enabled: bool = False,
        resolved_config: dict[str, Any] | None = None,
    ) -> None:
        if resolved_config is not None:
            self.lifecycle = TurnCheckpointLifecycle.from_resolved_config(
                client, agent_id, session_id, resolved_config
            )
        else:
            self.lifecycle = TurnCheckpointLifecycle(
                client, agent_id, session_id, enabled=enabled
            )

    def wrap(
        self, runnable: Runnable, *, runtime_session_id: str | None = None
    ) -> CheckpointingRunnable:
        """Return the Agent runnable with automatic Allox lifecycle hooks."""
        return CheckpointingRunnable(
            runnable, self.lifecycle, runtime_session_id=runtime_session_id
        )

    def rollback(self, checkpoint_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        """Rollback and suppress the current turn's automatic checkpoint."""
        return self.lifecycle.rollback(checkpoint_id, **kwargs)
