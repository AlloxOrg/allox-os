"""Process observer contract and independently installed provider discovery."""

from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import entry_points
from typing import Protocol

from allox.workspace.store import WorkspaceError

TRACKER_ABI = 1
EventCallback = Callable[[dict], None]
FailureCallback = Callable[[str], None]


class ProcessTrackingBackend(Protocol):
    """Stable control-plane contract implemented by process-tree plugins."""

    name: str

    def seed(self, pid: int, cookie: int) -> None: ...

    def finish(self, pid: int, cookie: int) -> None: ...

    def status(self) -> dict: ...

    def close(self) -> None: ...


class DisabledBackend:
    """No-op observer used when execution features do not need process tracing."""

    name = "disabled"

    def seed(self, pid: int, cookie: int) -> None:
        return None

    def finish(self, pid: int, cookie: int) -> None:
        return None

    def status(self) -> dict:
        return {"backend": self.name, "abi": TRACKER_ABI, "ready": True, "error": None}

    def close(self) -> None:
        return None


def create_backend(
    name: str,
    *,
    command: str,
    on_event: EventCallback,
    on_failure: FailureCallback,
) -> ProcessTrackingBackend:
    """Create a disabled observer or installed ``allox.process_trackers`` provider."""

    normalized = name.strip().lower()
    if normalized == "disabled":
        return DisabledBackend()
    providers = entry_points(group="allox.process_trackers")
    matches = [provider for provider in providers if provider.name == normalized]
    if not matches:
        raise WorkspaceError(f'process-tree plugin "{name}" is enabled but not installed')
    if len(matches) != 1:
        raise WorkspaceError(f'multiple process-tree plugins provide "{name}"')
    backend = matches[0].load()(
        command=command,
        on_event=on_event,
        on_failure=on_failure,
    )
    if getattr(backend, "abi", TRACKER_ABI) != TRACKER_ABI:
        backend.close()
        raise WorkspaceError(
            f'process-tree plugin "{name}" ABI is incompatible with {TRACKER_ABI}'
        )
    return backend
