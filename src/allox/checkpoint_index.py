"""ANOLISA-inspired checkpoint lineage kept outside rollback scope.

The filesystem snapshots remain the source of truth for data.  This index adds
human metadata and a small DAG so Allox can resolve ancestor-based rollback
without coupling the workspace daemon to ANOLISA's CLI or Rust daemon.
"""

from __future__ import annotations

from typing import Any

LIVE_CHILD = "__live__"
INDEX_VERSION = 1


class CheckpointIndexError(ValueError):
    """The persisted checkpoint lineage is invalid or cannot resolve a target."""


class CheckpointIndex:
    def __init__(
        self,
        *,
        head: str | None = None,
        checkpoints: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.head = head
        self.checkpoints = checkpoints or {}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CheckpointIndex:
        if value.get("version", INDEX_VERSION) != INDEX_VERSION:
            raise CheckpointIndexError("unsupported checkpoint index version")
        head = value.get("head")
        checkpoints = value.get("checkpoints", {})
        if head is not None and not isinstance(head, str):
            raise CheckpointIndexError("checkpoint index head must be a string")
        if not isinstance(checkpoints, dict) or any(
            not isinstance(key, str) or not isinstance(meta, dict)
            for key, meta in checkpoints.items()
        ):
            raise CheckpointIndexError("checkpoint index entries are invalid")
        return cls(head=head, checkpoints=checkpoints)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": INDEX_VERSION,
            "head": self.head,
            "checkpoints": self.checkpoints,
        }

    def add(
        self,
        checkpoint_id: str,
        *,
        created_at_ns: int,
        origin: str,
        message: str | None,
        pinned: bool,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if checkpoint_id in self.checkpoints:
            raise CheckpointIndexError(f"checkpoint already indexed: {checkpoint_id}")
        if self.head is not None and self.head in self.checkpoints:
            children = self.checkpoints[self.head].setdefault("child_ids", [])
            children[:] = [child for child in children if child != LIVE_CHILD]
            if checkpoint_id not in children:
                children.append(checkpoint_id)
        self.checkpoints[checkpoint_id] = {
            "created_at_ns": created_at_ns,
            "origin": origin,
            "message": message,
            "pinned": pinned,
            "metadata": metadata,
            "missing": False,
            "parent_id": self.head,
            "child_ids": [LIVE_CHILD],
        }
        self.head = checkpoint_id

    def reconcile(self, actual_ids: set[str]) -> bool:
        """Reconcile metadata against snapshot subvolumes after interrupted writes."""
        changed = False
        for checkpoint_id in actual_ids - self.checkpoints.keys():
            self.checkpoints[checkpoint_id] = {
                "created_at_ns": 0,
                "origin": "filesystem-recovery",
                "message": None,
                "pinned": False,
                "metadata": None,
                "missing": False,
                "parent_id": None,
                "child_ids": [],
            }
            changed = True
        for checkpoint_id, meta in self.checkpoints.items():
            missing = checkpoint_id not in actual_ids
            if bool(meta.get("missing", False)) != missing:
                meta["missing"] = missing
                changed = True
        if self.head is not None and self.head not in actual_ids:
            self.head = None
            changed = True
        return changed

    def resolve(
        self,
        checkpoint_id: str | None = None,
        num_ancestors: int | None = None,
    ) -> str:
        if (checkpoint_id is None) == (num_ancestors is None):
            raise CheckpointIndexError(
                "provide exactly one of checkpoint_id or num_ancestors"
            )
        if num_ancestors is not None:
            if num_ancestors < 1:
                raise CheckpointIndexError("num_ancestors must be >= 1")
            if self.head is None:
                raise CheckpointIndexError("checkpoint lineage has no head")
            resolved = self.head
            for _ in range(1, num_ancestors):
                parent = self.checkpoints.get(resolved, {}).get("parent_id")
                if not isinstance(parent, str):
                    raise CheckpointIndexError(
                        f"checkpoint lineage ends before ancestor {num_ancestors}"
                    )
                resolved = parent
            return resolved

        assert checkpoint_id is not None
        exact = self.checkpoints.get(checkpoint_id)
        if exact is not None and not exact.get("missing", False):
            return checkpoint_id
        matches = sorted(
            item
            for item, meta in self.checkpoints.items()
            if item.startswith(checkpoint_id) and not meta.get("missing", False)
        )
        if not matches:
            raise CheckpointIndexError(f"checkpoint does not exist: {checkpoint_id}")
        if len(matches) > 1:
            raise CheckpointIndexError(f"checkpoint prefix is ambiguous: {checkpoint_id}")
        return matches[0]

    def move_head(self, checkpoint_id: str) -> None:
        if checkpoint_id not in self.checkpoints:
            raise CheckpointIndexError(f"checkpoint is not indexed: {checkpoint_id}")
        if self.head is not None and self.head in self.checkpoints:
            children = self.checkpoints[self.head].setdefault("child_ids", [])
            children[:] = [child for child in children if child != LIVE_CHILD]
        children = self.checkpoints[checkpoint_id].setdefault("child_ids", [])
        if LIVE_CHILD not in children:
            children.append(LIVE_CHILD)
        self.head = checkpoint_id

    def remove(self, checkpoint_id: str) -> None:
        meta = self.checkpoints.get(checkpoint_id)
        if meta is None:
            return
        parent = meta.get("parent_id")
        children = [
            child for child in meta.get("child_ids", []) if child != LIVE_CHILD
        ]
        for child in children:
            child_meta = self.checkpoints.get(child)
            if child_meta is not None:
                child_meta["parent_id"] = parent
        if isinstance(parent, str) and parent in self.checkpoints:
            parent_children = self.checkpoints[parent].setdefault("child_ids", [])
            parent_children[:] = [
                child for child in parent_children if child != checkpoint_id
            ]
            for child in children:
                if child not in parent_children:
                    parent_children.append(child)
        if self.head == checkpoint_id:
            self.head = parent if isinstance(parent, str) else None
        del self.checkpoints[checkpoint_id]
        if self.head is not None and self.head in self.checkpoints:
            head_children = self.checkpoints[self.head].setdefault("child_ids", [])
            if LIVE_CHILD not in head_children:
                head_children.append(LIVE_CHILD)

    def records(self) -> list[dict[str, Any]]:
        return [
            {"checkpoint_id": checkpoint_id, **meta}
            for checkpoint_id, meta in sorted(
                self.checkpoints.items(),
                key=lambda item: (int(item[1].get("created_at_ns", 0)), item[0]),
            )
        ]
