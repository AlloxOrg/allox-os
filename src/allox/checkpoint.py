"""Checkpoint helpers backed by OpenSandbox snapshots."""

from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Any

import click
from opensandbox.models.sandboxes import SnapshotFilter

from allox.context import ClientContext


def snapshot_to_dict(snapshot: Any) -> dict[str, Any]:
    status = getattr(snapshot, "status", None)
    created_at = getattr(snapshot, "created_at", None)
    return {
        "id": snapshot.id,
        "sandbox_id": snapshot.sandbox_id,
        "name": snapshot.name,
        "state": getattr(status, "state", None),
        "reason": getattr(status, "reason", None),
        "message": getattr(status, "message", None),
        "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
    }


def list_snapshots(obj: ClientContext, sandbox_id: str | None = None) -> list[Any]:
    manager = obj.get_manager()
    page = 1
    snapshots: list[Any] = []
    while True:
        result = manager.list_snapshots(SnapshotFilter(sandbox_id=sandbox_id, page=page))
        snapshots.extend(result.snapshot_infos)
        if not result.pagination.has_next_page:
            break
        page += 1
    return snapshots


def latest_ready_snapshot(obj: ClientContext, sandbox_id: str | None = None) -> Any:
    ready = [
        snapshot
        for snapshot in list_snapshots(obj, sandbox_id)
        if str(getattr(snapshot.status, "state", "")).lower() == "ready"
    ]
    if not ready:
        scope = f" for sandbox '{sandbox_id}'" if sandbox_id else ""
        raise click.ClickException(f"No READY checkpoint found{scope}.")
    floor = datetime.min.replace(tzinfo=timezone.utc)
    return max(ready, key=lambda item: getattr(item, "created_at", None) or floor)


def create_checkpoint(
    obj: ClientContext,
    sandbox_id: str,
    *,
    name: str | None = None,
) -> Any:
    manager = obj.get_manager()
    snapshot = manager.create_snapshot(sandbox_id, name)
    timeout = float(obj.resolved_config.get("checkpoint_create_timeout", 900))
    deadline = time.monotonic() + timeout
    while str(getattr(snapshot.status, "state", "")).lower() not in {"ready", "failed"}:
        if time.monotonic() >= deadline:
            raise click.ClickException(
                f"Checkpoint '{snapshot.id}' did not become READY within {timeout:g}s."
            )
        time.sleep(0.5)
        snapshot = manager.get_snapshot(snapshot.id)
    if str(snapshot.status.state).lower() == "failed":
        detail = snapshot.status.message or snapshot.status.reason or "unknown error"
        raise click.ClickException(f"Checkpoint '{snapshot.id}' failed: {detail}")
    return snapshot


def checkpoint_after_success(obj: ClientContext, sandbox_id: str, operation: str) -> Any | None:
    """Create a best-effort checkpoint when configured for this operation."""
    cfg = obj.resolved_config
    if not cfg.get("checkpoint_enabled") or not cfg.get("checkpoint_on_success"):
        return None
    operations = cfg.get("checkpoint_operations", [])
    if operation not in operations:
        return None
    name = f"auto-{operation.replace('.', '-')}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    try:
        snapshot = create_checkpoint(obj, sandbox_id, name=name)
        if obj.verbose:
            click.echo(f"[verbose] checkpoint created: {snapshot.id} ({operation})", err=True)
        return snapshot
    except Exception as exc:
        if cfg.get("checkpoint_strict"):
            raise
        click.echo(f"Warning: automatic checkpoint failed after {operation}: {exc}", err=True)
        return None
