"""Content-bound Allox workspace recovery adapter for Observer protocol v3."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from allox.workspace.store import WorkspaceError, WorkspaceStore, validate_id


def _boolean(value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store-root", required=True, type=Path)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--checkpoint-id", required=True)
    parser.add_argument("--recovery-id", required=True)
    parser.add_argument("--scrub-runtime", required=True, type=_boolean)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    agent_id = validate_id("agent", args.agent_id)
    session_id = validate_id("session", args.session_id)
    checkpoint_id = validate_id("checkpoint", args.checkpoint_id)
    recovery_id = validate_id("recovery", args.recovery_id)
    try:
        root = args.store_root.resolve(strict=True)
    except OSError as exc:
        raise SystemExit(f"workspace store root is unavailable: {exc}") from exc
    if root != args.store_root or not root.is_dir():
        raise SystemExit("workspace store root must be a canonical existing directory")
    store = WorkspaceStore(root)
    # Prefix resolution is forbidden by the surrounding recovery contract.
    if checkpoint_id not in store.list_checkpoints(agent_id, session_id):
        raise SystemExit(f"exact recovery checkpoint does not exist: {checkpoint_id}")
    try:
        result = store.rollback(
            agent_id,
            session_id,
            checkpoint_id,
            scrub_runtime=args.scrub_runtime,
            origin="allox-system-effects-recovery",
            recovery_id=recovery_id,
            defer_cleanup=True,
        )
    except WorkspaceError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
