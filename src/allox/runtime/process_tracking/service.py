"""Backend-neutral Agent/Session process lifecycle service."""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from allox.runtime.process_tracking.backends import create_backend
from allox.runtime.process_tracking.cgroup import SessionCgroup
from allox.runtime.sandboxing import build_bwrap_argv
from allox.workspace.store import WorkspaceError, WorkspaceStore, validate_id


class ProcessTrackingService:
    """Persist lifecycle events while delegating observation to a provider."""

    def __init__(
        self,
        store: WorkspaceStore,
        executions,
        audit_root: Path,
        *,
        provider: str,
        provider_command: str,
        cgroup_root: Path,
        backend_factory=create_backend,
        cgroup_factory=SessionCgroup,
        boot_id: str | None = None,
    ) -> None:
        self.store = store
        self.share_endpoints = None
        self.executions = executions
        self.audit_root = audit_root.resolve()
        if self.audit_root == store.root or store.root in self.audit_root.parents:
            raise WorkspaceError("process audit directory must be outside the workspace store")
        self.audit_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.RLock()
        self._launch_lock = threading.RLock()
        self._runs: dict[str, dict] = {}
        self._cookies: dict[int, str] = {}
        self._closed = False
        self._backend_error: str | None = None
        self.boot_id = boot_id or Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        self.cgroups = cgroup_factory(cgroup_root)
        self.backend = backend_factory(
            provider,
            command=provider_command,
            on_event=self._record_event,
            on_failure=self._backend_failed,
        )
        recovered: set[tuple[str, str]] = set()
        for state_path in self.audit_root.glob("*/state.json"):
            row = json.loads(state_path.read_text())
            if row["state"] in {"starting", "running"} or row.get("session_fenced"):
                row["state"] = "interrupted"
                row["session_fenced"] = True
                row["trace_complete"] = False
                for task in row["processes"].values():
                    if task.get("alive"):
                        task["alive"] = None
                self._save(row)
                key = (row["agent_id"], row["session_id"])
                if key not in recovered:
                    self.executions.acquire(*key)
                    recovered.add(key)

    def status(self) -> dict:
        result = self.backend.status()
        result.update(
            {
                "enabled": True,
                "cgroup_root": str(self.cgroups.root),
                "audit_root": str(self.audit_root),
                "isolation": "bubblewrap",
            }
        )
        return result

    @contextmanager
    def session_launch_fence(
        self,
        agent_id: str,
        session_id: str,
        *,
        terminate: bool,
        reason: str = "rollback",
    ):
        """Block launches and optionally drain one Session before mutation."""
        validate_id("agent", agent_id)
        validate_id("session", session_id)
        with self._launch_lock:
            stopped = []
            if terminate:
                with self._lock:
                    run_ids = [
                        run_id
                        for run_id, run in self._runs.items()
                        if not run["done"].is_set()
                        and (run["row"]["agent_id"], run["row"]["session_id"])
                        == (agent_id, session_id)
                    ]
                for run_id in run_ids:
                    self._terminate_locked(run_id, reason)
                for run_id in run_ids:
                    if not self._runs[run_id]["done"].wait(10):
                        raise WorkspaceError(
                            "Session cgroup termination timed out; rollback remains fenced"
                        )
                    stopped.append(run_id)
            yield stopped

    @staticmethod
    def _validate_start(argv: list[str], env: dict[str, str] | None, timeout: float) -> None:
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(value, str) and "\0" not in value for value in argv)
            or not argv[0]
        ):
            raise WorkspaceError("argv must be a nonempty array of nonempty strings")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0 < timeout <= 86400
        ):
            raise WorkspaceError("timeout must be in (0, 86400] seconds")
        if env is not None and (
            not isinstance(env, dict)
            or not all(
                isinstance(key, str)
                and isinstance(value, str)
                and key
                and "=" not in key
                and "\0" not in key + value
                for key, value in env.items()
            )
        ):
            raise WorkspaceError("env must be a string mapping with valid environment names")

    def start(
        self,
        agent_id: str,
        session_id: str,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout: float = 300,
    ) -> dict:
        with self._launch_lock:
            return self._start(agent_id, session_id, argv, env=env, timeout=timeout)

    def _start(
        self,
        agent_id: str,
        session_id: str,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout: float = 300,
    ) -> dict:
        validate_id("agent", agent_id)
        validate_id("session", session_id)
        self._validate_start(argv, env, timeout)
        self.store.describe(agent_id, session_id)
        lease = self.executions.acquire(agent_id, session_id)
        run_id = uuid.uuid4().hex
        cookie = secrets.randbits(63) or 1
        process = None
        seeded = False
        attached = False
        try:
            with self._lock:
                if self._closed:
                    raise WorkspaceError("process tracking service is shutting down")
                if self._backend_error:
                    raise WorkspaceError(f"process tracking provider failed: {self._backend_error}")
                while cookie in self._cookies:
                    cookie = secrets.randbits(63) or 1
                directory = self.audit_root / run_id
                directory.mkdir(mode=0o700)
                current = self.store.current(agent_id, session_id)
                shared = self.store.agent_shared(agent_id)
                execution_argv = build_bwrap_argv(
                    str(current),
                    str(shared),
                    agent_id,
                    session_id,
                    tuple(argv),
                    tuple((env or {}).items()),
                    share_socket=(self.share_endpoints.endpoint(agent_id, session_id)
                                  if self.share_endpoints else None),
                )
                child_env = {
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "HOME": str(current),
                    "TMPDIR": str(current / ".allox-tmp"),
                    "ALLOX_AGENT_ID": agent_id,
                    "ALLOX_SESSION_ID": session_id,
                    "ALLOX_RUN_ID": run_id,
                    "ALLOX_AGENT_SHARED": str(shared),
                    "ALLOX_AGENT_WORKSPACE": str(shared.parent),
                }
                config = {
                    "argv": execution_argv,
                    "cwd": str(current),
                    "env": child_env,
                    "stdout": str(directory / "stdout.log"),
                    "stderr": str(directory / "stderr.log"),
                }
                row: dict[str, Any] = {
                    "run_id": run_id,
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "backend": self.backend.name,
                    "isolation": "bubblewrap",
                    "boot_id": self.boot_id,
                    "state": "starting",
                    "created_ns": time.time_ns(),
                    "event_count": 0,
                    "processes": {},
                    "audit_directory": str(directory),
                    "trace_complete": False,
                    "cookie": cookie,
                }
                self._runs[run_id] = {
                    "row": row,
                    "process": None,
                    "lease": lease,
                    "stop_reason": None,
                    "done": threading.Event(),
                    "timer": None,
                }
                self._cookies[cookie] = run_id
                self._save(row)
            process = subprocess.Popen(
                [sys.executable, "-m", "allox.runtime.process_tracking.gate"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={
                    key: value
                    for key, value in os.environ.items()
                    if key in {"PATH", "PYTHONPATH", "LANG", "LD_LIBRARY_PATH"}
                },
            )
            assert process.stdin is not None
            process.stdin.write(json.dumps(config).encode())
            process.stdin.close()
            with self._lock:
                self._runs[run_id]["process"] = process
            self._wait_stopped(process, timeout=5)
            cgroup = self.cgroups.attach(agent_id, session_id, process.pid)
            attached = True
            self.backend.seed(process.pid, cookie)
            seeded = True
            with self._lock:
                if self._backend_error:
                    raise WorkspaceError(self._backend_error)
                row["root_pid"] = process.pid
                row["cgroup"] = str(cgroup)
                row["state"] = "running"
                self._save(row)
            os.kill(process.pid, signal.SIGCONT)
            timer = threading.Timer(timeout, self._terminate, args=(run_id, "timeout"))
            timer.daemon = True
            with self._lock:
                self._runs[run_id]["timer"] = timer
            timer.start()
            threading.Thread(target=self._watch, args=(run_id,), daemon=True).start()
            with self._lock:
                return json.loads(json.dumps(row))
        except BaseException as exc:
            empty = process is None
            if process is not None and process.poll() is None:
                try:
                    if attached:
                        self.cgroups.kill(agent_id, session_id)
                    process.kill()
                    process.wait(timeout=5)
                except (OSError, WorkspaceError, subprocess.TimeoutExpired):
                    pass
            if process is not None:
                empty = process.poll() is not None and not self.cgroups.populated(
                    agent_id, session_id
                )
            if seeded and empty:
                try:
                    self.backend.finish(process.pid, cookie)
                except WorkspaceError:
                    pass
            with self._lock:
                self._cookies.pop(cookie, None)
                failed = self._runs.get(run_id)
                if failed:
                    failed["row"].update(
                        state="failed",
                        trace_complete=False,
                        session_fenced=not empty,
                        error=type(exc).__name__,
                    )
                    self._save(failed["row"])
                    failed["done"].set()
            if empty:
                self.executions.release(lease["lease_token"])
            raise

    @staticmethod
    def _wait_stopped(process: subprocess.Popen, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pid, status = os.waitpid(process.pid, os.WNOHANG | os.WUNTRACED)
            if pid == 0:
                time.sleep(0.01)
                continue
            if os.WIFSTOPPED(status):
                return
            process.returncode = os.waitstatus_to_exitcode(status)
            raise WorkspaceError("exec gate exited before eBPF attribution was installed")
        raise WorkspaceError("exec gate did not stop before attribution timeout")

    def _watch(self, run_id: str) -> None:
        run = self._runs[run_id]
        row = run["row"]
        process = run["process"]
        clean = False
        try:
            process.wait()
            with self._lock:
                row["root_exit_code"] = process.returncode
            while self.cgroups.populated(row["agent_id"], row["session_id"]):
                time.sleep(0.02)
            clean = True
            self.backend.finish(process.pid, row["cookie"])
        except Exception as exc:  # noqa: BLE001 - keep the lease unless emptiness is confirmed
            with self._lock:
                row["error"] = type(exc).__name__
                row["trace_complete"] = False
        with self._lock:
            timer = run["timer"]
            if timer:
                timer.cancel()
            row["trace_complete"] = clean and self._backend_error is None and "error" not in row
            row["state"] = (
                ("stopped" if run["stop_reason"] else "completed")
                if row["trace_complete"]
                else "failed"
            )
            row["session_fenced"] = not clean
            if clean:
                for task in row["processes"].values():
                    task["alive"] = False
            row["finished_ns"] = time.time_ns()
            self._save(row)
            self._cookies.pop(row["cookie"], None)
            if clean:
                self.executions.release(run["lease"]["lease_token"])
                self.cgroups.remove(row["agent_id"], row["session_id"])
            run["done"].set()

    def _record_event(self, message: dict) -> None:
        cookie = message.get("cookie")
        if type(cookie) is not int:
            return
        with self._lock:
            run_id = self._cookies.get(cookie)
            if run_id is None:
                return
            row = self._runs[run_id]["row"]
            event = {
                key: value
                for key, value in message.items()
                if key
                in {
                    "event",
                    "pid",
                    "ppid",
                    "former_tid",
                    "comm",
                    "filename",
                    "kernel_time_ns",
                    "birth_ns",
                    "parent_birth_ns",
                    "birth_pid",
                    "parent_birth_pid",
                }
            }
            self._append_event(row, event)
            self._save(row)

    def _append_event(self, row: dict, event: dict) -> None:
        row["event_count"] += 1
        event.update(
            {
                "sequence": row["event_count"],
                "agent_id": row["agent_id"],
                "session_id": row["session_id"],
                "run_id": row["run_id"],
                "boot_id": row["boot_id"],
                "observed_ns": time.time_ns(),
            }
        )
        pid = event.get("pid")
        if pid:
            prefix = f"{row['boot_id']}:{row['run_id']}"
            identity = f"{prefix}:{event['birth_pid']}:{event['birth_ns']}"
            event["process_id"] = identity
            if event.get("event") == "fork":
                event["parent_process_id"] = (
                    f"{prefix}:{event['parent_birth_pid']}:{event['parent_birth_ns']}"
                )
            item = row["processes"].setdefault(identity, {"pid": pid})
            item.update(event)
            item["alive"] = event.get("event") != "exit"
        with (Path(row["audit_directory"]) / "events.jsonl").open("a") as stream:
            stream.write(json.dumps(event) + "\n")

    def _backend_failed(self, reason: str) -> None:
        with self._lock:
            self._backend_error = reason
            active = [run_id for run_id, run in self._runs.items() if not run["done"].is_set()]
            for run_id in active:
                row = self._runs[run_id]["row"]
                row["session_fenced"] = True
                row["provider_error"] = reason
                try:
                    self._save(row)
                except OSError:
                    pass  # An unavailable audit disk must not prevent termination.
        # Never block the collector reader behind an in-flight seed/launch.
        for run_id in active:
            threading.Thread(
                target=self._terminate, args=(run_id, "provider_failure"), daemon=True
            ).start()

    def _save(self, row: dict) -> None:
        path = Path(row["audit_directory"]) / "state.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(row, indent=2))
        os.replace(temporary, path)

    def _terminate(self, run_id: str, reason: str) -> None:
        with self._launch_lock:
            self._terminate_locked(run_id, reason)

    def _terminate_locked(self, run_id: str, reason: str) -> None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run["done"].is_set():
                return
            if run["stop_reason"] is None:
                run["stop_reason"] = reason
                run["row"]["stop_reason"] = reason
                try:
                    self._save(run["row"])
                except OSError:
                    pass
            row = run["row"]
        self.cgroups.kill(row["agent_id"], row["session_id"])

    def get(self, agent_id: str, session_id: str, run_id: str) -> dict:
        validate_id("run", run_id)
        validate_id("agent", agent_id)
        validate_id("session", session_id)
        with self._lock:
            run = self._runs.get(run_id)
            if run:
                row = json.loads(json.dumps(run["row"]))
            else:
                try:
                    row = json.loads((self.audit_root / run_id / "state.json").read_text())
                except (FileNotFoundError, ValueError) as exc:
                    raise WorkspaceError("unknown run") from exc
            if (row["agent_id"], row["session_id"]) != (agent_id, session_id):
                raise WorkspaceError("run does not belong to this Agent/Session")
            return row

    def list_runs(self, agent_id: str, session_id: str) -> list[dict]:
        validate_id("agent", agent_id)
        validate_id("session", session_id)
        results = []
        for path in self.audit_root.glob("*/state.json"):
            row = json.loads(path.read_text())
            if (row["agent_id"], row["session_id"]) == (agent_id, session_id):
                results.append({key: value for key, value in row.items() if key != "processes"})
        return sorted(results, key=lambda row: row["created_ns"])

    def events(
        self,
        agent_id: str,
        session_id: str,
        run_id: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> dict:
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise WorkspaceError("after must be nonnegative; limit must be 1..1000")
        row = self.get(agent_id, session_id, run_id)
        items = []
        path = self.audit_root / run_id / "events.jsonl"
        if path.exists():
            with path.open() as stream:
                for line in stream:
                    if not line.endswith("\n"):
                        break
                    event = json.loads(line)
                    if event["sequence"] > after:
                        items.append(event)
                    if len(items) >= limit:
                        break
        return {
            "events": items,
            "next_after": items[-1]["sequence"] if items else after,
            "state": row["state"],
        }

    def stop(self, agent_id: str, session_id: str, run_id: str) -> dict:
        self.get(agent_id, session_id, run_id)
        if run_id not in self._runs:
            raise WorkspaceError("cannot control a historical run")
        self._terminate(run_id, "requested")
        if not self._runs[run_id]["done"].wait(10):
            raise WorkspaceError("Session cgroup termination is incomplete; session remains fenced")
        return self.get(agent_id, session_id, run_id)

    def close(self) -> None:
        with self._launch_lock:
            self._close()

    def _close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            active = [run_id for run_id, run in self._runs.items() if not run["done"].is_set()]
        for run_id in active:
            self._terminate(run_id, "daemon_shutdown")
        for run_id in active:
            self._runs[run_id]["done"].wait(10)
        self.backend.close()
