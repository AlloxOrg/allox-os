"""Trusted Agent adapter; never pass the management client into an untrusted sandbox."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from allox.workspace.lifecycle import WorkspaceRPC


class SessionMemory:
    def __init__(self, client: WorkspaceRPC, agent_id: str, session_id: str):
        self.client = client
        self.identity = {"agent_id": agent_id, "session_id": session_id}

    @contextmanager
    def for_turn(self) -> Iterator[dict[str, Any]]:
        """Re-read under a lease and hold it through the model invocation."""
        lease = self.client.rpc("execution.acquire", **self.identity)
        try:
            yield self.client.rpc("memory.read", **self.identity)
        finally:
            self.client.rpc("execution.release", lease_token=lease["lease_token"])

    def save(self, state: dict[str, Any], *, expected_revision: int) -> dict[str, Any]:
        """Save after releasing the turn lease; a rewind invalidates stale writers."""
        return self.client.rpc("memory.write", **self.identity, state=state,
                               expected_revision=expected_revision)

    def checkpoint(
        self, state: dict[str, Any], *, expected_revision: int, checkpoint_id: str | None = None,
    ) -> dict[str, Any]:
        """Publish a workspace checkpoint and its logical state together."""
        return self.client.rpc(
            "checkpoint.create", **self.identity, checkpoint_id=checkpoint_id,
            memory_state=state, expected_memory_revision=expected_revision,
        )
