"""Workspace daemon coordination tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from allox.workspace_daemon import ExecutionRegistry, WorkspaceService
from allox.workspace_store import WorkspaceError


def test_mutation_rejects_active_session_execution():
    registry = ExecutionRegistry()
    lease = registry.acquire("agent-a", "session-1")

    with (
        pytest.raises(WorkspaceError, match="active executions"),
        registry.mutation("agent-a", "session-1"),
    ):
        pass

    assert registry.release(lease["lease_token"])["released"] is True
    with registry.mutation("agent-a", "session-1"):
        pass


def test_mutation_of_one_session_does_not_block_another():
    registry = ExecutionRegistry()
    registry.acquire("agent-a", "session-1")

    with registry.mutation("agent-a", "session-2"):
        pass


def test_same_session_allows_only_one_active_execution():
    registry = ExecutionRegistry()
    registry.acquire("agent-a", "session-1")

    with pytest.raises(WorkspaceError, match="already has an active execution"):
        registry.acquire("agent-a", "session-1")


def test_service_forwards_checkpoint_metadata_and_ancestor_rollback():
    store = MagicMock()
    store.create_checkpoint.return_value = {"checkpoint_id": "cp1"}
    store.rollback.return_value = {"checkpoint_id": "cp1"}
    service = WorkspaceService(store)

    service.dispatch(
        "checkpoint.create",
        {
            "agent_id": "agent-a",
            "session_id": "session-1",
            "checkpoint_id": "cp1",
            "message": "before retry",
            "pinned": True,
            "origin": "agent-loop",
            "metadata": {"event": "turn_end", "turn": 1},
        },
    )
    service.dispatch(
        "session.rollback",
        {
            "agent_id": "agent-a",
            "session_id": "session-1",
            "num_ancestors": 2,
        },
    )

    store.create_checkpoint.assert_called_once_with(
        "agent-a",
        "session-1",
        "cp1",
        origin="agent-loop",
        message="before retry",
        pinned=True,
        metadata={"event": "turn_end", "turn": 1},
    )
    store.rollback.assert_called_once_with(
        "agent-a",
        "session-1",
        None,
        num_ancestors=2,
        scrub_runtime=True,
        origin="allox-cli",
    )
