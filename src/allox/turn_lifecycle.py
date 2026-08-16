"""ANOLISA-inspired Agent turn hooks for Session workspace checkpoints.

This module deliberately stays independent of LangChain, OpenClaw, Hermes, or
any other Agent runtime.  A runtime adapter calls the three lifecycle hooks:
``on_session_start``, ``on_user_message``, and ``on_turn_end``.  The feature is
opt-in and checkpoint failures are reported to the adapter without failing the
Agent turn.
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Protocol

MESSAGE_LIMIT = 80


class WorkspaceRPC(Protocol):
    def rpc(self, action: str, **params: Any) -> Any: ...


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class TurnCheckpointLifecycle:
    """Create a baseline and one automatic checkpoint per Agent turn.

    The object is scoped to one ``agent_id`` / ``session_id`` pair and should
    live as long as the corresponding Agent runtime session.  It mirrors the
    ANOLISA plugin policy while using Allox's Session-scoped checkpoint RPC.
    """

    def __init__(
        self,
        client: WorkspaceRPC,
        agent_id: str,
        session_id: str,
        *,
        enabled: bool = False,
        id_factory: Callable[[], str] | None = None,
        timestamp_factory: Callable[[], str] = _timestamp,
    ) -> None:
        self.client = client
        self.agent_id = agent_id
        self.session_id = session_id
        self.enabled = enabled
        self._id_factory = id_factory or (lambda: secrets.token_hex(4))
        self._timestamp_factory = timestamp_factory
        self._turn_count = 0
        self._last_user_message = ""
        self._session_started = False
        self._skip_next_auto_checkpoint = False
        self._seen_turn_ids: set[str] = set()
        self._lock = threading.RLock()

    @classmethod
    def from_resolved_config(
        cls,
        client: WorkspaceRPC,
        agent_id: str,
        session_id: str,
        resolved_config: dict[str, Any],
        **kwargs: Any,
    ) -> TurnCheckpointLifecycle:
        return cls(
            client,
            agent_id,
            session_id,
            enabled=bool(resolved_config.get("workspace_auto_checkpoint_turns", False)),
            **kwargs,
        )

    @property
    def turn_count(self) -> int:
        return self._turn_count

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable future automatic lifecycle checkpoints."""
        with self._lock:
            self.enabled = enabled

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.enabled,
                "agent_id": self.agent_id,
                "session_id": self.session_id,
                "turn_count": self._turn_count,
                "session_started": self._session_started,
                "skip_next_auto_checkpoint": self._skip_next_auto_checkpoint,
            }

    def _disabled(self, event: str) -> dict[str, Any]:
        return {"enabled": False, "event": event, "created": False, "skipped": "disabled"}

    def _create_checkpoint(
        self,
        *,
        event: str,
        turn: int,
        message: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        checkpoint_id = self._id_factory()
        complete_metadata = {
            "auto": True,
            "event": event,
            "turn": turn,
            "timestamp": self._timestamp_factory(),
            **metadata,
        }
        try:
            checkpoint = self.client.rpc(
                "checkpoint.create",
                agent_id=self.agent_id,
                session_id=self.session_id,
                checkpoint_id=checkpoint_id,
                origin="turn-lifecycle",
                message=message[:MESSAGE_LIMIT],
                pinned=False,
                metadata=complete_metadata,
            )
        except Exception as exc:  # noqa: BLE001 - lifecycle failures must not fail the Agent
            return {
                "enabled": True,
                "event": event,
                "created": False,
                "checkpoint_id": checkpoint_id,
                "error": str(exc),
                "non_blocking": True,
            }
        return {
            "enabled": True,
            "event": event,
            "created": True,
            "checkpoint_id": checkpoint.get("checkpoint_id", checkpoint_id),
            "checkpoint": checkpoint,
        }

    def on_session_start(self, *, runtime_session_id: str | None = None) -> dict[str, Any]:
        """Create the optional turn-0 baseline checkpoint once per runtime instance."""
        with self._lock:
            if not self.enabled:
                return self._disabled("session_start")
            if self._session_started:
                return {
                    "enabled": True,
                    "event": "session_start",
                    "created": False,
                    "skipped": "duplicate",
                }
            self._session_started = True
            metadata: dict[str, Any] = {}
            if runtime_session_id:
                metadata["runtime_session_id"] = runtime_session_id
            return self._create_checkpoint(
                event="session_start",
                turn=0,
                message="session-start",
                metadata=metadata,
            )

    def on_user_message(self, message: str) -> dict[str, Any]:
        """Remember the user message used as the next turn checkpoint message."""
        with self._lock:
            self._last_user_message = message[:MESSAGE_LIMIT] if isinstance(message, str) else ""
            return {
                "enabled": self.enabled,
                "event": "message_received",
                "captured": bool(self._last_user_message),
            }

    def on_turn_end(
        self,
        *,
        turn_id: str | None = None,
        completed: bool = True,
        interrupted: bool = False,
    ) -> dict[str, Any]:
        """Create one non-blocking checkpoint after the runtime finishes a turn."""
        with self._lock:
            if not self.enabled:
                return self._disabled("turn_end")
            if turn_id and turn_id in self._seen_turn_ids:
                return {
                    "enabled": True,
                    "event": "turn_end",
                    "created": False,
                    "skipped": "duplicate",
                    "turn_id": turn_id,
                }
            if turn_id:
                self._seen_turn_ids.add(turn_id)
            if self._skip_next_auto_checkpoint:
                self._skip_next_auto_checkpoint = False
                return {
                    "enabled": True,
                    "event": "turn_end",
                    "created": False,
                    "skipped": "rollback",
                    "turn_id": turn_id,
                }
            self._turn_count += 1
            message = self._last_user_message or "agent turn"
            metadata: dict[str, Any] = {
                "success": completed,
                "interrupted": interrupted,
            }
            if turn_id:
                metadata["turn_id"] = turn_id
            return self._create_checkpoint(
                event="turn_end",
                turn=self._turn_count,
                message=message,
                metadata=metadata,
            )

    def mark_rollback(self) -> None:
        """Skip the turn-end snapshot immediately following a successful rollback."""
        with self._lock:
            self._skip_next_auto_checkpoint = True

    def rollback(
        self,
        checkpoint_id: str | None = None,
        *,
        num_ancestors: int | None = None,
        scrub_runtime: bool = True,
    ) -> dict[str, Any]:
        """Rollback through Allox and suppress the current turn's automatic snapshot."""
        result = self.client.rpc(
            "session.rollback",
            agent_id=self.agent_id,
            session_id=self.session_id,
            checkpoint_id=checkpoint_id,
            num_ancestors=num_ancestors,
            scrub_runtime=scrub_runtime,
            origin="turn-lifecycle",
        )
        self.mark_rollback()
        return result

    @contextmanager
    def turn(self, message: str, *, turn_id: str | None = None) -> Iterator[None]:
        """Wrap one Agent invocation and checkpoint its success or failure."""
        self.on_user_message(message)
        try:
            yield
        except BaseException as exc:
            self.on_turn_end(
                turn_id=turn_id,
                completed=False,
                interrupted=isinstance(exc, (KeyboardInterrupt, SystemExit)),
            )
            raise
        else:
            self.on_turn_end(turn_id=turn_id, completed=True, interrupted=False)
