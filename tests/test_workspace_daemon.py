"""Workspace daemon coordination tests."""

from __future__ import annotations

import pytest

from allox.workspace_daemon import ExecutionRegistry
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
