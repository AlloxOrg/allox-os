"""Process tracker provider contract and provider discovery."""

from __future__ import annotations

import json
import shlex
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from importlib.metadata import entry_points
from pathlib import Path
from typing import Protocol

from allox.workspace.store import WorkspaceError

TRACKER_ABI = 1
EventCallback = Callable[[dict], None]
FailureCallback = Callable[[str], None]


class ProcessTrackingBackend(Protocol):
    """Stable control-plane contract implemented by process tracker providers."""

    name: str

    def seed(self, pid: int, cookie: int) -> None: ...

    def finish(self, pid: int, cookie: int) -> None: ...

    def status(self) -> dict: ...

    def close(self) -> None: ...


class EbpfBackend:
    """Adapter for the independently versioned Allox eBPF collector."""

    name = "ebpf"

    def __init__(
        self,
        command: str,
        on_event: EventCallback,
        on_failure: FailureCallback,
        *,
        ready_timeout: float = 10,
    ) -> None:
        argv = shlex.split(command)
        if not argv:
            raise WorkspaceError("eBPF tracker command must not be empty")
        executable = Path(argv[0])
        if executable.parent != Path(".") and not executable.exists():
            raise WorkspaceError(f"eBPF tracker executable does not exist: {executable}")
        self._on_event = on_event
        self._on_failure = on_failure
        self._condition = threading.Condition()
        self._acks: set[tuple[str, int]] = set()
        self._ready = False
        self._closed = False
        self._error: str | None = None
        self._diagnostics = deque(maxlen=30)
        try:
            self._process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise WorkspaceError(f"failed to start eBPF tracker: {exc}") from exc
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._errors = threading.Thread(target=self._read_stderr, daemon=True)
        self._reader.start()
        self._errors.start()
        deadline = time.monotonic() + ready_timeout
        with self._condition:
            while not self._ready and self._error is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
        if not self._ready:
            error = self._error or "collector readiness timed out"
            self.close()
            detail = "".join(self._diagnostics)[-4000:]
            raise WorkspaceError(f"eBPF tracker unavailable: {error}\n{detail}")

    def _read_stdout(self) -> None:
        assert self._process.stdout is not None
        try:
            for line in self._process.stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    self._fail("collector returned invalid JSON")
                    return
                kind = message.get("kind")
                if kind == "ready":
                    if message.get("abi") != TRACKER_ABI:
                        self._fail(
                            f"collector ABI {message.get('abi')} != required ABI {TRACKER_ABI}"
                        )
                        return
                    with self._condition:
                        self._ready = True
                        self._condition.notify_all()
                elif kind == "ack":
                    with self._condition:
                        self._acks.add(
                            (str(message.get("operation")), int(message.get("cookie", 0)))
                        )
                        self._condition.notify_all()
                elif kind == "event":
                    self._on_event(message)
                elif kind == "error":
                    self._fail(str(message.get("error", "collector error")))
                    return
                else:
                    self._fail("collector returned an unknown message kind")
                    return
        except Exception as exc:  # noqa: BLE001 - consumer failures invalidate the trace
            self._fail(f"collector protocol/consumer failure: {type(exc).__name__}")
        finally:
            if not self._closed:
                self._fail("collector exited unexpectedly")

    def _read_stderr(self) -> None:
        assert self._process.stderr is not None
        # Drain stderr so a verbose loader cannot deadlock. Details remain local
        # to the trusted service and are intentionally not copied into RPC data.
        for line in self._process.stderr:
            self._diagnostics.append(line[:1000])

    def _fail(self, reason: str) -> None:
        notify = False
        with self._condition:
            if self._error is None:
                self._error = reason
                notify = not self._closed
            self._condition.notify_all()
        if notify:
            self._on_failure(reason)

    def _control(self, operation: str, pid: int, cookie: int) -> None:
        if pid <= 0 or cookie <= 0:
            raise WorkspaceError("tracker pid and cookie must be positive")
        key = (operation, cookie)
        with self._condition:
            if self._error:
                raise WorkspaceError(f"eBPF tracker failed: {self._error}")
            assert self._process.stdin is not None
            try:
                self._process.stdin.write(f"{operation} {pid} {cookie}\n")
                self._process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise WorkspaceError("eBPF tracker control channel failed") from exc
            deadline = time.monotonic() + 5
            while key not in self._acks and self._error is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkspaceError(f"eBPF tracker {operation} acknowledgement timed out")
                self._condition.wait(remaining)
            if self._error:
                raise WorkspaceError(f"eBPF tracker failed: {self._error}")
            self._acks.remove(key)

    def seed(self, pid: int, cookie: int) -> None:
        self._control("seed", pid, cookie)

    def finish(self, pid: int, cookie: int) -> None:
        self._control("finish", pid, cookie)

    def status(self) -> dict:
        with self._condition:
            return {
                "backend": self.name,
                "abi": TRACKER_ABI,
                "ready": self._ready and self._error is None,
                "error": self._error,
                "pid": self._process.pid,
            }

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
        self._reader.join(timeout=5)
        self._errors.join(timeout=5)
        for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
            if stream:
                stream.close()


def create_backend(
    name: str,
    *,
    command: str,
    on_event: EventCallback,
    on_failure: FailureCallback,
) -> ProcessTrackingBackend:
    """Create a built-in or installed `allox.process_trackers` provider."""
    normalized = name.strip().lower()
    if normalized == "ebpf":
        return EbpfBackend(command, on_event, on_failure)
    providers = entry_points(group="allox.process_trackers")
    for provider in providers:
        if provider.name == normalized:
            return provider.load()(command=command, on_event=on_event, on_failure=on_failure)
    raise WorkspaceError(f"unknown process tracking provider: {name}")
