"""Real Kata+Btrfs assertions, not LLM output grading or simulated restore."""
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "/fixtures")
import guest as h

from allox.runtime.bootstrap import prepare_session_runtime
from allox.workspace.daemon import WorkspaceService
from allox.workspace.store import BtrfsBackend, WorkspaceError, WorkspaceStore


def main():
    root = Path(sys.argv[1])
    h.require(h.command("findmnt", "-T", str(root), "-no", "FSTYPE") == "btrfs", "real Btrfs")
    cg = Path("/sys/fs/cgroup/allox-recovery-checks")
    prepare_session_runtime(cgroup_root=cg)
    store = WorkspaceStore(root)
    store.initialize()
    assert isinstance(store.backend, BtrfsBackend)
    service = WorkspaceService(
        store, session_memory=True, kill_session_processes_on_rollback=True,
        process_audit_root=Path("/dev/shm/check-audit"), process_cgroup_root=cg,
        recovery_checks_config={"root": Path("/dev/shm/recovery-evidence")},
    )
    reports, fds = [], []
    ids = {"agent_id": "agent", "session_id": "target"}
    other = {"agent_id": "agent", "session_id": "control"}

    def rpc(action, target=ids, **params):
        return service.dispatch(action, {**target, **params})

    try:
        for target in (ids, other):
            rpc("session.create", target)
            rpc("recovery.coverage", target, adapter_id="local-test-harness", complete=True)
            work = store.current(*target.values())
            (work / "state.txt").write_text("BASELINE")
            rpc("checkpoint.create", target, checkpoint_id="c0", memory_state=h.memory("BASELINE"),
                expected_memory_revision=0)
        wa, wb = store.current(*ids.values()), store.current(*other.values())
        rpc("process.start", other, argv=[sys.executable, "-c", h.LISTENER, "control"], timeout=180)
        control = h.poll_until(lambda: h.listening(wb / "service.sock"))
        control_fd = os.pidfd_open(control["pid"])
        fds.append(control_fd)
        for repeat in range(3):
            (wa / "state.txt").write_text("DISCARDED")
            view = rpc("memory.read")
            rpc("memory.write", state=h.memory("DISCARDED"), expected_revision=view["memory_revision"])
            rpc("process.start", argv=[sys.executable, "-c", h.LISTENER, "discarded"], timeout=90)
            old = h.poll_until(lambda: h.listening(wa / "service.sock"))
            old_fd = os.pidfd_open(old["pid"])
            fds.append(old_fd)
            result = rpc("session.rollback", checkpoint_id="c0", kill_processes=True)
            report = result["recovery_checks"]
            h.require(report["status"] == "CONSISTENT_WITHIN_SCOPE", "automatic post-restore assertions")
            h.require(h.pidfd_exited(old_fd), "discarded listener pidfd exited")
            h.require(not service.processes.cgroups.populated(*ids.values()), "target cgroup empty")
            h.require(not (wa / "service.sock").exists(), "discarded socket removed")
            h.require(h.listening(h.namespace_socket(old["pid"])) is None, "old /tmp listener unreachable")
            h.require((wa / "state.txt").read_text() == "BASELINE"
                      and rpc("memory.read")["state"] == h.memory("BASELINE"), "file and memory same c0")
            h.require(h.peer(wb / "service.sock")["pid"] == control["pid"]
                      and not h.pidfd_exited(control_fd), "control session listener unaffected")
            reports.append(report)

        original = store.rollback

        def corrupt_memory(*args, **kwargs):
            result = original(*args, **kwargs)
            view = store.memory_read(*ids.values())
            store.memory_write(*ids.values(), h.memory("INJECTED-MEMORY-DIVERGENCE"),
                               expected_revision=view["memory_revision"])
            return result

        store.rollback = corrupt_memory
        try:
            rpc("session.rollback", checkpoint_id="c0", kill_processes=True)
        except WorkspaceError as exc:
            h.require("verification failed" in str(exc), "injected memory divergence rejected")
        else:
            raise AssertionError("memory mismatch was accepted")
        finally:
            store.rollback = original
        checks = rpc("recovery.status")["report"]["checks"]
        h.require(checks["filesystem"]["status"] == "CONSISTENT_WITHIN_SCOPE"
                  and checks["memory"]["status"] == "INCONSISTENT_WITHIN_SCOPE",
                  "file success does not hide memory mismatch")
        try:
            rpc("execution.acquire")
        except WorkspaceError as exc:
            h.require("fenced" in str(exc), "failed restore cannot continue executing")
        else:
            raise AssertionError("failed restore acquired execution lease")
        h.require(h.peer(wb / "service.sock")["pid"] == control["pid"], "control survives failed verification")
        rpc("session.rollback", checkpoint_id="c0", kill_processes=True)
        h.require(rpc("recovery.status")["fence"] is None, "verified retry clears fence")

        # A provider outside workspace commits; preflight MUST reject before
        # killing the target's live listener or changing either state layer.
        rpc("process.start", argv=[sys.executable, "-c", h.LISTENER, "live"], timeout=90)
        live = h.poll_until(lambda: h.listening(wa / "service.sock"))
        rpc("recovery.effect.prepare", operation_id="test-payment",
            fingerprint=hashlib.sha256(b"local-fixture-payment").hexdigest())
        provider = Path("/dev/shm/provider-commit")
        provider.write_text("COMMITTED")
        rpc("recovery.effect.settle", operation_id="test-payment", outcome="committed")
        try:
            rpc("session.rollback", checkpoint_id="c0", kill_processes=True)
        except WorkspaceError as exc:
            h.require("committed external" in str(exc), "semantic rollback violation detected")
        else:
            raise AssertionError("unsafe semantic rollback admitted")
        h.require(provider.read_text() == "COMMITTED" and h.peer(wa / "service.sock")["pid"] == live["pid"],
                  "rejected rollback neither killed listener nor erased external effect")
        h.require(h.peer(wb / "service.sock")["pid"] == control["pid"], "control survives rejected rollback")
        model = rpc("recovery.admit", outcomes=[[["x", "y"], ["y", "x"]],
                                                [["x", "z"], ["z", "x"]]],
                    allowed=[["y", "x"], ["x", "z"]])
        h.require(model["descending_chain"] == [2, 1, 0] and not model["admitted"],
                  "prefix-robust model rejects unsafe common prefix")
        print("ALLOX_RECOVERY_REPORT " + json.dumps({"passed": True, "repetitions": 3,
              "reports": reports, "semantic_preflight": "rejected-before-kill",
              "negative_memory": "fenced-and-recovered",
              "history_chain": model["descending_chain"], "kernel": os.uname().release}), flush=True)
    finally:
        service.close()
        for fd in fds:
            os.close(fd)


if __name__ == "__main__":
    main()
