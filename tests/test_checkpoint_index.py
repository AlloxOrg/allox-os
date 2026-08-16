"""Tests for the distilled ANOLISA-style checkpoint lineage."""

from __future__ import annotations

import pytest

from allox.checkpoint_index import LIVE_CHILD, CheckpointIndex, CheckpointIndexError


def _add(index: CheckpointIndex, checkpoint_id: str, created_at_ns: int) -> None:
    index.add(
        checkpoint_id,
        created_at_ns=created_at_ns,
        origin="test",
        message=None,
        pinned=False,
    )


def test_checkpoint_lineage_resolves_ancestors_and_branches():
    index = CheckpointIndex()
    _add(index, "cp1", 1)
    _add(index, "cp2", 2)
    _add(index, "cp3", 3)

    assert index.resolve(num_ancestors=1) == "cp3"
    assert index.resolve(num_ancestors=2) == "cp2"
    assert index.resolve(num_ancestors=3) == "cp1"

    index.move_head("cp1")
    _add(index, "branch", 4)

    assert index.head == "branch"
    assert index.checkpoints["branch"]["parent_id"] == "cp1"
    assert set(index.checkpoints["cp1"]["child_ids"]) == {"cp2", "branch"}
    assert index.checkpoints["branch"]["child_ids"] == [LIVE_CHILD]


def test_delete_reparents_children_without_breaking_lineage():
    index = CheckpointIndex()
    _add(index, "cp1", 1)
    _add(index, "cp2", 2)
    _add(index, "cp3", 3)

    index.remove("cp2")

    assert index.checkpoints["cp3"]["parent_id"] == "cp1"
    assert "cp3" in index.checkpoints["cp1"]["child_ids"]
    assert index.resolve(num_ancestors=2) == "cp1"


def test_prefix_resolution_rejects_ambiguous_prefix():
    index = CheckpointIndex()
    _add(index, "alpha-one", 1)
    _add(index, "alpha-two", 2)

    with pytest.raises(CheckpointIndexError, match="ambiguous"):
        index.resolve(checkpoint_id="alpha")


def test_reconcile_recovers_unindexed_snapshot_and_marks_missing_entry():
    index = CheckpointIndex()
    _add(index, "known", 1)

    assert index.reconcile({"recovered"}) is True

    assert index.head is None
    assert index.checkpoints["known"]["missing"] is True
    assert index.checkpoints["recovered"]["origin"] == "filesystem-recovery"
