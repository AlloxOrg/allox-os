"""End-to-end share tool inside real Bubblewrap/eBPF on 117's isolated Guest."""

import json
import os
import time
from pathlib import Path

import pytest
from test_sharing import DirectoryBackend

from allox.workspace.daemon import WorkspaceService
from allox.workspace.store import WorkspaceStore

pytestmark = pytest.mark.skipif(
    os.environ.get("ALLOX_EBPF_TEST") != "1", reason="requires isolated Linux eBPF test Guest"
)


@pytest.fixture
def service(tmp_path):
    store_root = tmp_path / "store"
    store_root.mkdir()
    store = WorkspaceStore(store_root, DirectoryBackend())
    store.initialize()
    for agent in ("a", "b", "c"):
        store.create_session(agent, "s")
    instance = WorkspaceService(
        store, process_tracking="ebpf", process_audit_root=tmp_path / "audit",
        process_cgroup_root=Path("/sys/fs/cgroup/allox-share-tests") / tmp_path.name,
        process_tracker_command=os.environ.get("ALLOX_EBPF_COMMAND", "/src/native/process-tracker/allox-process-tracker"),
        share_tools=True, share_socket_root=Path("/dev/shm/allox-share-tests"),
    )
    yield instance
    instance.close()


def run(service, agent, shell):
    row = service.processes.start(agent, "s", ["/bin/sh", "-ec", shell], timeout=20)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = service.processes.get(agent, "s", row["run_id"])
        if result["state"] not in {"starting", "running"}:
            assert result["trace_complete"], result
            stdout = (Path(result["audit_directory"]) / "stdout.log").read_text()
            stderr = (Path(result["audit_directory"]) / "stderr.log").read_text()
            assert result["root_exit_code"] == 0, (stdout, stderr, result)
            return stdout
        time.sleep(0.02)
    pytest.fail("share workload did not exit")


TOOL = "python3 /run/allox/share.py"


def test_two_real_sessions_enable_share_and_revoke(service):
    run(service, "b", f"mkdir output; printf hello > output/report; {TOOL} enable --scope output")
    # B enabled alone is insufficient; then A enables from its own trusted endpoint.
    output = run(service, "a", f"! {TOOL} read b/s report; {TOOL} enable; "
                 f"{TOOL} read b/s report > received; {TOOL} list b/s; "
                 f"! {TOOL} write b/s report </dev/null; "
                 f"! {TOOL} read b/s ../report; test ! -e /agents/b")
    assert "report" in output
    assert (service.store.current("a", "s") / "received").read_text() == "hello"
    run(service, "c", f"! {TOOL} read b/s report")
    run(service, "b", f"{TOOL} disable")
    run(service, "a", f"! {TOOL} read b/s report")


def test_shared_write_then_target_rollback_with_real_agent_tool(service):
    run(service, "b", f"mkdir output; printf before > output/report; "
                     f"{TOOL} enable --scope output --permission write")
    service.store.create_checkpoint("b", "s", "before")
    run(service, "a", f"{TOOL} enable; printf after | {TOOL} write b/s report; "
                     f"{TOOL} read b/s report > received")
    assert (service.store.current("b", "s") / "output/report").read_text() == "after"
    service.dispatch("session.rollback", {
        "agent_id": "b", "session_id": "s", "checkpoint_id": "before", "kill_processes": True,
    })
    run(service, "a", f"{TOOL} read b/s report > restored")
    assert (service.store.current("a", "s") / "restored").read_text() == "before"
    assert (service.store.current("a", "s") / "received").read_text() == "after"
    # The host-side process knows the socket path, but is not in A's cgroup.
    from allox.runtime.share_tool import call

    with pytest.raises(ValueError, match="does not belong"):
        call("enable", {}, service.share_endpoints.endpoint("a", "s"))
    records = [json.loads(line) for line in service.store._event_path("b", "s").read_text().splitlines()]
    assert any(r["op"] == "share.write" and r["caller_agent_id"] == "a" for r in records)
