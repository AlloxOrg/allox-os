"""Reuse EXACT audit workloads/oracles; change only expected bug outcomes."""
import contextlib
import importlib.util
import io
import json
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/audit")
import probes

from allox.workspace.daemon import WorkspaceService
from allox.workspace.store import WorkspaceError, WorkspaceStore
from allox.runtime.bootstrap import prepare_session_runtime
from allox_recovery_checks.plugin import RecoveryChecksFeature


def owner_probe(root, backend=None):
    service = probes.service_at(root / "two-daemons", backend)
    try:
        try:
            second = WorkspaceService(service.store, session_memory=True,
                                      recovery_checks_config={"root": root / "two-daemons-evidence"},
                                      feature_factories={"recovery-checks": RecoveryChecksFeature})
        except WorkspaceError as exc:
            assert "active owner" in str(exc)
            return {"second_service_rejected": True, "error": str(exc)}
        else:
            second.close()
            raise AssertionError("two active recovery owners were admitted")
    finally:
        service.close()


probes.split_brain_cache = owner_probe


def orphan_probe(root):
    root.mkdir()
    group = Path("/sys/fs/cgroup/allox-orphan-fix")
    prepare_session_runtime(cgroup_root=group)
    store = WorkspaceStore(root)
    store.initialize()
    service = WorkspaceService(store, session_memory=True, kill_session_processes_on_rollback=True,
                               process_audit_root=Path("/dev/shm/orphan-audit"), process_cgroup_root=group,
                               recovery_checks_config={"root": Path("/dev/shm/orphan-evidence")})
    processes = []
    ids = {"agent_id": "orphan", "session_id": "target"}
    try:
        service.dispatch("session.create", ids)
        service.dispatch("recovery.coverage", {**ids, "adapter_id": "fixture", "complete": True})
        work = store.current("orphan", "target")
        (work / "file").write_text("before")
        service.dispatch("checkpoint.create", {**ids, "checkpoint_id": "c0"})
        (work / "file").write_text("branch")
        child_code = ("import os,signal,socket,sys,time; os.kill(os.getpid(),signal.SIGSTOP); "
                      "s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); s.listen(); time.sleep(60)")
        target = subprocess.Popen([sys.executable, "-c", child_code, str(work / "listener.sock")])
        processes.append(target)
        assert os.WIFSTOPPED(os.waitpid(target.pid, os.WUNTRACED)[1])
        service.processes.cgroups.attach("orphan", "target", target.pid)
        os.kill(target.pid, signal.SIGCONT)
        deadline = __import__("time").monotonic() + 3
        while not (work / "listener.sock").exists():
            assert __import__("time").monotonic() < deadline
            __import__("time").sleep(.01)
        control = subprocess.Popen([sys.executable, "-c",
                                    "import os,signal,time; os.kill(os.getpid(),signal.SIGSTOP); time.sleep(60)"])
        processes.append(control)
        assert os.WIFSTOPPED(os.waitpid(control.pid, os.WUNTRACED)[1])
        service.processes.cgroups.attach("orphan", "control", control.pid)
        os.kill(control.pid, signal.SIGCONT)
        assert service.processes._runs == {}
        result = service.dispatch("session.rollback", {**ids, "checkpoint_id": "c0", "kill_processes": True})
        target.wait(timeout=3)
        assert target.returncode == -signal.SIGKILL and control.poll() is None
        assert (work / "file").read_text() == "before" and not (work / "listener.sock").exists()
        assert result["recovery_checks"]["status"] == "CONSISTENT_WITHIN_SCOPE"
        return {"orphan_listener_killed": True, "no_in_memory_runs": True,
                "control_process_unaffected": True, "restore_consistent": True}
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
        service.close()


spec = importlib.util.spec_from_file_location("original_audit", "/audit/guest.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
captured = io.StringIO()
with contextlib.redirect_stdout(captured):
    audit.main()
lines = captured.getvalue().splitlines()
rows = [json.loads(line.split(" ", 1)[1]) for line in lines if line.startswith("ALLOX_RECOVERY_REPORT ")]
assert len(rows) == 1
report = rows[0]
report["orphan_runtime"] = orphan_probe(Path(sys.argv[1]) / "orphan-check")
diagnostics = report["diagnostics"]
assert all(x["new_execution_allowed_after_rejection"] for x in diagnostics["rejected_requests"])
assert not diagnostics["prefix_delete"]["deleted_baseline_leaked"]
assert not diagnostics["global_lock"]["other_session_status_blocked"]
assert diagnostics["split_brain_cache"]["second_service_rejected"]
assert all(not x["listener_exited"] and x["file_not_rolled_back"] for x in diagnostics["invalid_reset"])
report.update(source_commit="4ad25c6 + recovery fixes (working tree)", parallel_hash_workers=4)
for line in lines:
    if not line.startswith("ALLOX_RECOVERY_REPORT "):
        print(line, flush=True)
print("PASS all audited bug reproductions now have safe outcomes", flush=True)
print("ALLOX_RECOVERY_REPORT " + json.dumps(report), flush=True)
