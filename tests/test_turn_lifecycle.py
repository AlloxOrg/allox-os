"""Framework-neutral Agent turn checkpoint lifecycle tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from allox.workspace.lifecycle import MESSAGE_LIMIT, TurnCheckpointLifecycle


def lifecycle(client=None, *, enabled=True):
    client = client or MagicMock()
    client.rpc.side_effect = lambda action, **params: {
        "checkpoint_id": params.get("checkpoint_id"),
        "action": action,
    }
    ids = iter(["baseline1", "turn0001", "turn0002", "turn0003"])
    value = TurnCheckpointLifecycle(
        client,
        "agent-a",
        "session-1",
        enabled=enabled,
        id_factory=lambda: next(ids),
        timestamp_factory=lambda: "2026-08-16T00:00:00+00:00",
    )
    return value, client


def test_disabled_lifecycle_never_calls_checkpoint():
    value, client = lifecycle(enabled=False)

    assert value.on_session_start()["skipped"] == "disabled"
    value.on_user_message("hello")
    assert value.on_turn_end(turn_id="turn-1")["skipped"] == "disabled"
    client.rpc.assert_not_called()


def test_session_baseline_and_turn_metadata():
    value, client = lifecycle()

    baseline = value.on_session_start(runtime_session_id="runtime-1")
    value.on_user_message("x" * 100)
    turn = value.on_turn_end(turn_id="turn-1", completed=True)

    assert baseline["created"] is True
    assert turn["created"] is True
    baseline_call = client.rpc.call_args_list[0]
    assert baseline_call.kwargs["metadata"] == {
        "auto": True,
        "event": "session_start",
        "turn": 0,
        "timestamp": "2026-08-16T00:00:00+00:00",
        "runtime_session_id": "runtime-1",
    }
    turn_call = client.rpc.call_args_list[1]
    assert turn_call.kwargs["message"] == "x" * MESSAGE_LIMIT
    assert turn_call.kwargs["metadata"] == {
        "auto": True,
        "event": "turn_end",
        "turn": 1,
        "timestamp": "2026-08-16T00:00:00+00:00",
        "success": True,
        "interrupted": False,
        "turn_id": "turn-1",
    }


def test_duplicate_events_and_session_start_are_idempotent():
    value, client = lifecycle()

    value.on_session_start()
    assert value.on_session_start()["skipped"] == "duplicate"
    value.on_turn_end(turn_id="turn-1")
    assert value.on_turn_end(turn_id="turn-1")["skipped"] == "duplicate"
    assert client.rpc.call_count == 2


def test_successful_rollback_skips_next_auto_checkpoint_once():
    value, client = lifecycle()
    value.on_session_start()

    result = value.rollback("baseline1")
    skipped = value.on_turn_end(turn_id="rollback-turn")
    created = value.on_turn_end(turn_id="next-turn")

    assert result["action"] == "session.rollback"
    assert skipped["skipped"] == "rollback"
    assert created["created"] is True
    assert client.rpc.call_args_list[1].args == ("session.rollback",)
    assert client.rpc.call_args_list[2].args == ("checkpoint.create",)


def test_checkpoint_failure_is_non_blocking():
    client = MagicMock()
    client.rpc.side_effect = RuntimeError("session has active executions")
    value = TurnCheckpointLifecycle(
        client,
        "agent-a",
        "session-1",
        enabled=True,
        id_factory=lambda: "turn0001",
    )

    result = value.on_turn_end(turn_id="turn-1")

    assert result["created"] is False
    assert result["non_blocking"] is True
    assert "active executions" in result["error"]


def test_turn_context_checkpoints_failure_without_swallowing_exception():
    value, client = lifecycle()

    with pytest.raises(ValueError, match="boom"), value.turn(
        "bad turn", turn_id="turn-bad"
    ):
        raise ValueError("boom")

    metadata = client.rpc.call_args.kwargs["metadata"]
    assert metadata["success"] is False
    assert metadata["interrupted"] is False
