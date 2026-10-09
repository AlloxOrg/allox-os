"""Backend-neutral Agent/Session process lifecycle service."""

from __future__ import annotations

import json
import os
import queue
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

from allox.runtime.extensions import (
    ExecutionQuiescence,
    ExecutionRecoveryPlan,
    ExecutionStartContext,
)
from allox.runtime.process_tracking.backends import create_backend
from allox.runtime.process_tracking.cgroup import SessionCgroup
from allox.runtime.sandboxing import build_bwrap_argv
from allox.workspace.store import WorkspaceError, WorkspaceStore, validate_id

_WORKLOAD_VISIBLE_HOST_ROOTS = tuple(Path(path).resolve() for path in ("/usr", "/etc"))
_POPEN_TYPE = subprocess.Popen


class _StableProcessLauncher:
    """Spawn workloads from one daemon-lifetime thread.

    Linux parent-death signals are tied to the particular thread that created
    a child.  Launching Bubblewrap from a transient RPC handler would therefore
    make ``--die-with-parent`` kill the sandbox as soon as that handler exits.
    """

    def __init__(self) -> None:
        self._requests: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._lock = threading.Lock()
        self._closed = False
        self._thread = threading.Thread(
            target=self._serve,
            name="allox-process-launcher",
            daemon=True,
        )
        self._thread.start()

    def spawn(self, argv: list[str], **options: Any) -> subprocess.Popen:
        request: dict[str, Any] = {
            "argv": argv,
            "options": options,
            "done": threading.Event(),
        }
        with self._lock:
            if self._closed:
                raise WorkspaceError("process launcher is closed")
            self._requests.put(request)
        request["done"].wait()
        error = request.get("error")
        if error is not None:
            raise error
        process = request.get("process")
        if not isinstance(process, _POPEN_TYPE):
            raise WorkspaceError("process launcher returned an invalid child")
        return process

    def _serve(self) -> None:
        while True:
            request = self._requests.get()
            if request is None:
                return
            try:
                request["process"] = subprocess.Popen(
                    request["argv"], **request["options"]
                )
            except BaseException as exc:  # noqa: BLE001 - relay launch failures exactly
                request["error"] = exc
            finally:
                request["done"].set()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._requests.put(None)
        self._thread.join()


def _prepare_audit_root(audit_root: Path, store_root: Path) -> Path:
    audit_root = audit_root.resolve()
    store_root = store_root.resolve()
    if audit_root == Path("/"):
        raise WorkspaceError("process audit directory cannot be the host root")
    if (
        audit_root == store_root
        or store_root in audit_root.parents
        or audit_root in store_root.parents
    ):
        raise WorkspaceError("process audit directory must be disjoint from the workspace store")
    if any(
        audit_root == visible or visible in audit_root.parents
        for visible in _WORKLOAD_VISIBLE_HOST_ROOTS
    ):
        raise WorkspaceError(
            "process audit directory cannot be below a host tree visible to workloads"
        )
    try:
        audit_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        audit_root.chmod(0o700)
        metadata = audit_root.stat()
    except OSError as exc:
        raise WorkspaceError(f"failed to prepare process audit directory: {exc}") from exc
    if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise WorkspaceError("process audit directory must be private and daemon-owned")
    return audit_root


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
        self.features = None
        self.executions = executions
        self.audit_root = _prepare_audit_root(audit_root, store.root)
        self._lock = threading.RLock()
        self._launch_lock = threading.RLock()
        self._runs: dict[str, dict] = {}
        self._cookies: dict[int, str] = {}
        self._closed = False
        self._backend_error: str | None = None
        self._next_run_epoch = 1
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
            run_epoch = row.get("run_epoch")
            if type(run_epoch) is int and run_epoch > 0:
                self._next_run_epoch = max(self._next_run_epoch, run_epoch + 1)
            if row["state"] in {"starting", "running"} or row.get("session_fenced"):
                row["state"] = "interrupted"
                row["session_fenced"] = True
                row["trace_complete"] = False
                feature_runs = row.get("feature_runs")
                if isinstance(feature_runs, dict):
                    for status in feature_runs.values():
                        if isinstance(status, dict) and status.get("status") != "finalized":
                            status["status"] = "interrupted"
                for task in row["processes"].values():
                    if task.get("alive"):
                        task["alive"] = None
                self._save(row)
                key = (row["agent_id"], row["session_id"])
                if key not in recovered:
                    self.executions.acquire(*key)
                    recovered.add(key)
        self._launcher = _StableProcessLauncher()

    def status(self) -> dict:
        result = self.backend.status()
        result.update(
            {
                "enabled": self.backend.name != "disabled",
                "execution_enabled": True,
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

    def _prepare_recovery_plan(
        self,
        agent_id: str,
        session_id: str,
        run_id: str,
        recovery: dict[str, Any] | None,
    ) -> ExecutionRecoveryPlan | None:
        if recovery is None:
            return None
        if not isinstance(recovery, dict):
            raise WorkspaceError("process recovery must be an object")
        unknown = sorted(
            set(recovery)
            - {"checkpoint_id", "recovery_id", "scrub_runtime", "require_consistent"}
        )
        if unknown:
            raise WorkspaceError(
                "unknown process recovery keys: " + ", ".join(unknown)
            )
        if self.features is None or not self.features.recovery_lifecycle_enabled:
            raise WorkspaceError(
                "process recovery requires an ABI-v2 recovery lifecycle plugin"
            )
        for key in ("scrub_runtime", "require_consistent"):
            if key in recovery and not isinstance(recovery[key], bool):
                raise WorkspaceError(f"process recovery {key} must be a boolean")
        recovery_id_raw = recovery.get("recovery_id", f"recovery-{run_id}")
        if not isinstance(recovery_id_raw, str):
            raise WorkspaceError("process recovery recovery_id must be a string")
        recovery_id = validate_id("recovery", recovery_id_raw)
        checkpoint_raw = recovery.get("checkpoint_id")
        baseline_created = checkpoint_raw is None
        if baseline_created:
            checkpoint_id = f"baseline-{run_id}"
            self.store.create_checkpoint(
                agent_id,
                session_id,
                checkpoint_id,
                origin="allox-system-effects-recovery",
                message=f"automatic pre-run baseline for {run_id}",
                pinned=True,
                metadata={"run_id": run_id, "recovery_id": recovery_id},
            )
        else:
            if not isinstance(checkpoint_raw, str):
                raise WorkspaceError("process recovery checkpoint_id must be a string")
            checkpoint_id = validate_id("checkpoint", checkpoint_raw)
            # Recovery never accepts prefix resolution: A/B/C evidence binds
            # one exact checkpoint identity selected before the Agent runs.
            if checkpoint_id not in self.store.list_checkpoints(agent_id, session_id):
                raise WorkspaceError(
                    f"exact recovery checkpoint does not exist: {checkpoint_id}"
                )
        return ExecutionRecoveryPlan(
            checkpoint_id=checkpoint_id,
            recovery_id=recovery_id,
            scrub_runtime=recovery.get("scrub_runtime", True),
            require_consistent=recovery.get("require_consistent", True),
            baseline_created=baseline_created,
        )

    def start(
        self,
        agent_id: str,
        session_id: str,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout: float = 300,
        recovery: dict[str, Any] | None = None,
    ) -> dict:
        with self._launch_lock:
            return self._start(
                agent_id,
                session_id,
                argv,
                env=env,
                timeout=timeout,
                recovery=recovery,
            )

    def _start(
        self,
        agent_id: str,
        session_id: str,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout: float = 300,
        recovery: dict[str, Any] | None = None,
    ) -> dict:
        validate_id("agent", agent_id)
        validate_id("session", session_id)
        self._validate_start(argv, env, timeout)
        self.store.describe(agent_id, session_id)
        lease = self.executions.acquire(
            agent_id, session_id, recovery=recovery is not None
        )
        run_id = uuid.uuid4().hex
        cookie = secrets.randbits(63) or 1
        process = None
        row = None
        seeded = False
        attached = False
        cgroup_identity = None
        feature_execution = None
        target_pid = None
        target_pidfd = None
        recovery_plan = None
        try:
            recovery_plan = self._prepare_recovery_plan(
                agent_id, session_id, run_id, recovery
            )
            with self._lock:
                if self._closed:
                    raise WorkspaceError("process tracking service is shutting down")
                if self._backend_error:
                    raise WorkspaceError(f"process tracking provider failed: {self._backend_error}")
                while cookie in self._cookies:
                    cookie = secrets.randbits(63) or 1
                run_epoch = self._next_run_epoch
                self._next_run_epoch += 1
                directory = self.audit_root / run_id
                directory.mkdir(mode=0o700)
                current = self.store.current(agent_id, session_id)
                shared = self.store.agent_shared(agent_id)
                contribution = (
                    self.features.prepare_execution(agent_id, session_id)
                    if self.features is not None
                    else None
                )
                execution_environment = dict(env or {})
                if contribution is not None:
                    # Trusted feature variables win over caller-supplied values.
                    execution_environment.update(dict(contribution.environment))
                lifecycle_enabled = bool(
                    self.features is not None
                    and self.features.execution_lifecycle_enabled
                )
                launch_barrier = False
                if lifecycle_enabled:
                    launch_barrier = True
                execution_argv = build_bwrap_argv(
                    str(current),
                    str(shared),
                    agent_id,
                    session_id,
                    tuple(argv),
                    tuple(execution_environment.items()),
                    mounts=contribution.mounts if contribution is not None else (),
                    launch_barrier=launch_barrier,
                    run_id=run_id,
                )
                if contribution is not None:
                    execution_argv = [*contribution.argv_prefix, *execution_argv]
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
                    "run_epoch": run_epoch,
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
                if lifecycle_enabled:
                    row["feature_runs"] = {}
                if recovery_plan is not None:
                    row["recovery"] = {
                        "checkpoint_id": recovery_plan.checkpoint_id,
                        "recovery_id": recovery_plan.recovery_id,
                        "scrub_runtime": recovery_plan.scrub_runtime,
                        "require_consistent": recovery_plan.require_consistent,
                        "baseline_created": recovery_plan.baseline_created,
                    }
                if contribution is not None:
                    row.update(contribution.metadata)
                self._runs[run_id] = {
                    "row": row,
                    "process": None,
                    "lease": lease,
                    "stop_reason": None,
                    "done": threading.Event(),
                    "timer": None,
                    "execution_exited": False,
                    "feature_execution": None,
                    "cgroup_identity": None,
                    "target_pid": None,
                    "recovery_plan": recovery_plan,
                }
                self._cookies[cookie] = run_id
                self._save(row)
            process = self._launcher.spawn(
                [sys.executable, "-m", "allox.runtime.process_tracking.gate"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
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
            cgroup_identity = self.cgroups.identity(agent_id, session_id)
            with self._lock:
                self._runs[run_id]["cgroup_identity"] = cgroup_identity
            self.backend.seed(process.pid, cookie)
            seeded = True
            if lifecycle_enabled:
                os.kill(process.pid, signal.SIGCONT)
                target_pid = self._wait_inner_gate(
                    process,
                    agent_id,
                    session_id,
                    timeout=10,
                )
                target_pidfd = self._open_pidfd(target_pid)
                self.cgroups.freeze(
                    agent_id,
                    session_id,
                    expected_cgroup_id=int(cgroup_identity["id"]),
                    require_populated=True,
                )
                mount_namespace_id, network_namespace_id = self._target_identity(
                    agent_id,
                    session_id,
                    target_pid,
                    target_pidfd,
                    phase="before Observer readiness",
                )
                context = ExecutionStartContext(
                    agent_id=agent_id,
                    session_id=session_id,
                    run_id=run_id,
                    run_epoch=run_epoch,
                    root_pid=target_pid,
                    cgroup_path=str(cgroup_identity["path"]),
                    cgroup_relative_path=str(cgroup_identity["relative_path"]),
                    cgroup_id=int(cgroup_identity["id"]),
                    mount_namespace_id=mount_namespace_id,
                    network_namespace_id=network_namespace_id,
                    audit_directory=str(directory),
                    boot_id=self.boot_id,
                    execution_metadata=dict(contribution.metadata) if contribution else {},
                    on_failure=lambda reason: self._feature_failed(run_id, reason),
                    recovery=recovery_plan,
                )
                feature_execution = self.features.start_execution(context)
                feature_execution.assert_healthy()
                with self._lock:
                    self._runs[run_id]["feature_execution"] = feature_execution
                    self._runs[run_id]["target_pid"] = target_pid
                    row["feature_runs"] = feature_execution.status()
            with self._lock:
                if self._backend_error:
                    raise WorkspaceError(self._backend_error)
                if feature_execution is not None:
                    feature_execution.assert_healthy()
                row["root_pid"] = target_pid or process.pid
                if target_pid is not None and target_pid != process.pid:
                    row["launcher_pid"] = process.pid
                row["cgroup"] = str(cgroup)
                row["state"] = "running"
                self._save_durable(row) if lifecycle_enabled else self._save(row)
            if lifecycle_enabled:
                assert target_pid is not None
                assert target_pidfd is not None
                ready_mount_namespace_id, ready_network_namespace_id = (
                    self._target_identity(
                        agent_id,
                        session_id,
                        target_pid,
                        target_pidfd,
                        phase="after Observer readiness",
                    )
                )
                if (
                    ready_mount_namespace_id != mount_namespace_id
                    or ready_network_namespace_id != network_namespace_id
                ):
                    raise WorkspaceError(
                        "inner launch gate namespaces changed after Observer readiness"
                    )
                feature_execution.assert_healthy()
                feature_execution.agent_started()
                feature_execution.assert_healthy()
                try:
                    self._pidfd_send_signal(target_pidfd, signal.SIGCONT)
                    self.cgroups.thaw(
                        agent_id,
                        session_id,
                        expected_cgroup_id=int(cgroup_identity["id"]),
                    )
                finally:
                    self._close_pidfd(target_pidfd)
                    target_pidfd = None
            else:
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
            self._close_pidfd(target_pidfd)
            target_pidfd = None
            empty = process is None
            tracker_finalized = not seeded
            features_finalized = feature_execution is None
            quiescence = None
            if process is not None and process.poll() is None:
                if attached:
                    try:
                        self.cgroups.kill(agent_id, session_id)
                    except (OSError, WorkspaceError):
                        pass
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if process is not None:
                try:
                    empty = process.poll() is not None and (
                        self.cgroups.wait_empty(agent_id, session_id, 5)
                        if attached
                        else True
                    )
                except (OSError, WorkspaceError):
                    empty = False
            if empty and attached and cgroup_identity is not None:
                try:
                    identity = self.cgroups.quiesce_empty(
                        agent_id,
                        session_id,
                        expected_cgroup_id=int(cgroup_identity["id"]),
                    )
                    quiescence = ExecutionQuiescence(
                        cgroup_path=str(identity["path"]),
                        cgroup_id=int(identity["id"]),
                    )
                except WorkspaceError:
                    empty = False
            if seeded and empty:
                try:
                    self.backend.finish(process.pid, cookie)
                    tracker_finalized = True
                except WorkspaceError:
                    tracker_finalized = False
            if feature_execution is not None:
                if empty and quiescence is not None:
                    try:
                        result = feature_execution.finish(
                            outcome="launch_failed", quiescence=quiescence
                        )
                        if row is not None:
                            row["feature_runs"] = result
                        features_finalized = True
                    except Exception:  # noqa: BLE001 - failed finalization must fence
                        feature_execution.close_unverified()
                else:
                    feature_execution.close_unverified()
            release_safe = empty and tracker_finalized and features_finalized
            if attached and release_safe:
                try:
                    release_safe = self.cgroups.remove(agent_id, session_id) is not False
                except Exception:  # noqa: BLE001 - unknown cgroup state must fence
                    release_safe = False
            with self._lock:
                self._cookies.pop(cookie, None)
                failed = self._runs.get(run_id)
                if failed:
                    failed["row"].update(
                        state="failed",
                        trace_complete=False,
                        lifecycle_complete=False,
                        session_fenced=not release_safe,
                        error=type(exc).__name__,
                    )
                    try:
                        self._save_durable(failed["row"])
                    except Exception:  # noqa: BLE001 - durable failure must fence
                        release_safe = False
                        failed["row"]["session_fenced"] = True
                    failed["done"].set()
            if release_safe:
                self.executions.release(lease["lease_token"])
            raise

    def _wait_inner_gate(
        self,
        process: subprocess.Popen,
        agent_id: str,
        session_id: str,
        *,
        timeout: float,
    ) -> int:
        """Wait for the trusted shell inside final Bubblewrap namespaces to stop."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise WorkspaceError("Bubblewrap exited before the inner launch barrier")
            stopped = []
            for pid in self.cgroups.pids(agent_id, session_id):
                if pid == process.pid:
                    continue
                try:
                    state = next(
                        line for line in Path(f"/proc/{pid}/status").read_text().splitlines()
                        if line.startswith("State:")
                    )
                except (FileNotFoundError, StopIteration):
                    continue
                if state.split()[1] in {"T", "t"}:
                    stopped.append(pid)
            if len(stopped) == 1:
                return stopped[0]
            if len(stopped) > 1:
                raise WorkspaceError("inner launch barrier matched multiple stopped processes")
            time.sleep(0.01)
        raise WorkspaceError("inner Bubblewrap launch barrier timed out")

    @staticmethod
    def _open_pidfd(pid: int) -> int:
        opener = getattr(os, "pidfd_open", None)
        sender = getattr(signal, "pidfd_send_signal", None)
        if opener is None or sender is None:
            raise WorkspaceError("execution lifecycle requires Linux pidfd support")
        try:
            return opener(pid, 0)
        except OSError as exc:
            raise WorkspaceError(f"failed to pin inner launch gate identity: {exc}") from exc

    @staticmethod
    def _close_pidfd(pidfd: int | None) -> None:
        if pidfd is None:
            return
        try:
            os.close(pidfd)
        except OSError:
            pass

    @staticmethod
    def _pidfd_send_signal(pidfd: int, signum: int) -> None:
        sender = getattr(signal, "pidfd_send_signal", None)
        if sender is None:
            raise WorkspaceError("execution lifecycle requires Linux pidfd signaling")
        try:
            sender(pidfd, signum)
        except OSError as exc:
            action = "validate" if signum == 0 else "resume"
            raise WorkspaceError(
                f"failed to {action} pinned inner launch gate identity: {exc}"
            ) from exc

    def _target_identity(
        self,
        agent_id: str,
        session_id: str,
        pid: int,
        pidfd: int,
        *,
        phase: str,
    ) -> tuple[int, int]:
        """Bind numeric /proc and cgroup lookups to one live pidfd identity."""
        self._pidfd_send_signal(pidfd, 0)
        if not self.cgroups.owns(agent_id, session_id, pid):
            raise WorkspaceError(f"inner launch gate left the Session cgroup {phase}")
        self._pidfd_send_signal(pidfd, 0)
        mount_namespace_id = self._namespace_inode(pid, "mnt")
        self._pidfd_send_signal(pidfd, 0)
        network_namespace_id = self._namespace_inode(pid, "net")
        self._pidfd_send_signal(pidfd, 0)
        if not self.cgroups.owns(agent_id, session_id, pid):
            raise WorkspaceError(f"inner launch gate identity changed {phase}")
        self._pidfd_send_signal(pidfd, 0)
        return mount_namespace_id, network_namespace_id

    @staticmethod
    def _namespace_inode(pid: int, namespace: str) -> int:
        if namespace not in {"mnt", "net"}:
            raise WorkspaceError("unsupported execution namespace identity")
        try:
            identity = Path(f"/proc/{pid}/ns/{namespace}").stat().st_ino
        except OSError as exc:
            raise WorkspaceError(
                f"failed to bind execution {namespace} namespace: {exc}"
            ) from exc
        if identity <= 0:
            raise WorkspaceError(f"execution {namespace} namespace has an invalid identity")
        return identity

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
            raise WorkspaceError("exec gate exited before Session attribution was installed")
        raise WorkspaceError("exec gate did not stop before attribution timeout")

    def _watch(self, run_id: str) -> None:
        run = self._runs[run_id]
        row = run["row"]
        process = run["process"]
        feature_execution = run.get("feature_execution")
        cgroup_identity = run.get("cgroup_identity")
        recovery_plan = run.get("recovery_plan")
        cgroup_quiescent = False
        tracker_finalized = False
        features_finalized = feature_execution is None
        try:
            process.wait()
            with self._lock:
                run["execution_exited"] = True
                timer = run["timer"]
                if timer is not None:
                    timer.cancel()
                    run["timer"] = None
                row["root_exit_code"] = process.returncode
            if not self.cgroups.wait_empty(row["agent_id"], row["session_id"], 10):
                raise WorkspaceError("Session cgroup did not become empty after root exit")
            if cgroup_identity is None:
                raise WorkspaceError("Session cgroup identity is missing at finalization")
            identity = self.cgroups.quiesce_empty(
                row["agent_id"],
                row["session_id"],
                expected_cgroup_id=int(cgroup_identity["id"]),
            )
            quiescence = ExecutionQuiescence(
                cgroup_path=str(identity["path"]),
                cgroup_id=int(identity["id"]),
            )
            cgroup_quiescent = True
            self.backend.finish(process.pid, row["cookie"])
            tracker_finalized = True
            if feature_execution is not None:
                feature_execution.assert_healthy()
                outcome = "stopped" if run["stop_reason"] else "completed"
                row["feature_runs"] = feature_execution.finish(
                    outcome=outcome, quiescence=quiescence
                )
                if recovery_plan is not None and any(
                    result.get("release_allowed") is not True
                    for result in row["feature_runs"].values()
                ):
                    raise WorkspaceError(
                        "recovery verification did not authorize Session release"
                    )
                features_finalized = True
        except Exception as exc:  # noqa: BLE001 - keep the lease unless emptiness is confirmed
            if feature_execution is not None and not features_finalized:
                feature_execution.close_unverified()
            with self._lock:
                row["error"] = type(exc).__name__
                row["trace_complete"] = False
        with self._lock:
            timer = run["timer"]
            if timer:
                timer.cancel()
                run["timer"] = None
            successful = (
                cgroup_quiescent
                and tracker_finalized
                and features_finalized
                and self._backend_error is None
                and "error" not in row
            )
            row["trace_complete"] = successful and self.backend.name != "disabled"
            row["lifecycle_complete"] = successful
            row["state"] = (
                ("stopped" if run["stop_reason"] else "completed")
                if successful
                else "failed"
            )
            row["session_fenced"] = not successful
            if successful:
                for task in row["processes"].values():
                    task["alive"] = False
            row["finished_ns"] = time.time_ns()
            self._cookies.pop(row["cookie"], None)
            try:
                self._save_durable(row)
            except Exception as exc:  # noqa: BLE001 - durable receipt is release barrier
                successful = False
                row["state"] = "failed"
                row["lifecycle_complete"] = False
                row["trace_complete"] = False
                row["session_fenced"] = True
                row["error"] = type(exc).__name__
            if successful:
                try:
                    removed = self.cgroups.remove(row["agent_id"], row["session_id"])
                except Exception as exc:  # noqa: BLE001 - unknown cgroup state fences the run
                    removed = False
                    row["error"] = type(exc).__name__
                row["cgroup_removed"] = removed is not False
                if removed is False:
                    successful = False
                    row["state"] = "failed"
                    row["lifecycle_complete"] = False
                    row["trace_complete"] = False
                    row["session_fenced"] = True
                    row.setdefault("error", "CgroupRemovalIncomplete")
                try:
                    self._save_durable(row)
                except Exception as exc:  # noqa: BLE001 - never release without final commit
                    successful = False
                    row["state"] = "failed"
                    row["lifecycle_complete"] = False
                    row["trace_complete"] = False
                    row["session_fenced"] = True
                    row["error"] = type(exc).__name__
            if successful:
                self.executions.release(run["lease"]["lease_token"])
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

    def _feature_failed(self, run_id: str, reason: str) -> None:
        """Fence and terminate a run when a lifecycle sidecar fails asynchronously."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run["done"].is_set():
                return
            row = run["row"]
            row["session_fenced"] = True
            row["feature_error"] = reason[:1000]
            feature_runs = row.get("feature_runs")
            if isinstance(feature_runs, dict):
                for status in feature_runs.values():
                    if isinstance(status, dict) and status.get("status") != "finalized":
                        status["status"] = "failed"
                        status["error"] = reason[:1000]
            if run["stop_reason"] is None:
                run["stop_reason"] = "feature_failure"
                row["stop_reason"] = "feature_failure"
            agent_id = row["agent_id"]
            session_id = row["session_id"]
            process = run["process"]
            try:
                self._save(row)
            except OSError:
                pass
        try:
            self.cgroups.kill(agent_id, session_id)
        except (OSError, WorkspaceError):
            if process is not None and process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass

    def _save(self, row: dict) -> None:
        path = Path(row["audit_directory"]) / "state.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(row, indent=2))
        os.replace(temporary, path)

    def _save_durable(self, row: dict) -> None:
        """Atomically fsync security-critical run state through a directory fd."""
        directory = Path(row["audit_directory"])
        encoded = (json.dumps(row, indent=2) + "\n").encode("utf-8")
        temporary = f".state.{secrets.token_hex(16)}.tmp"
        directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
        create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            directory_fd = os.open(directory, directory_flags)
        except OSError as exc:
            raise WorkspaceError(f"failed to open process audit directory: {exc}") from exc
        temporary_created = False
        try:
            try:
                temporary_fd = os.open(temporary, create_flags, 0o600, dir_fd=directory_fd)
                temporary_created = True
            except OSError as exc:
                raise WorkspaceError(f"failed to create durable process state: {exc}") from exc
            try:
                view = memoryview(encoded)
                while view:
                    written = os.write(temporary_fd, view)
                    if written <= 0:
                        raise WorkspaceError("short write while committing process state")
                    view = view[written:]
                os.fsync(temporary_fd)
            finally:
                os.close(temporary_fd)
            os.rename(
                temporary,
                "state.json",
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            temporary_created = False
            os.fsync(directory_fd)
        except OSError as exc:
            raise WorkspaceError(f"failed to commit durable process state: {exc}") from exc
        finally:
            if temporary_created:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except OSError:
                    pass
            os.close(directory_fd)

    def _terminate(self, run_id: str, reason: str) -> None:
        with self._launch_lock:
            self._terminate_locked(run_id, reason)

    def _terminate_locked(self, run_id: str, reason: str) -> None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run["done"].is_set():
                return
            process = run["process"]
            if reason == "timeout" and (
                run.get("execution_exited")
                or (process is not None and process.poll() is not None)
            ):
                run["execution_exited"] = True
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
        try:
            self.backend.close()
        finally:
            self._launcher.close()
