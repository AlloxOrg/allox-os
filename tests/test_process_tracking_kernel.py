"""Opt-in real kernel tests. Run inside the Allox test VM on server 117."""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from allox.workspace.daemon import WorkspaceService
from allox.workspace.store import WorkspaceError, WorkspaceStore

pytestmark = pytest.mark.skipif(
    os.environ.get("ALLOX_EBPF_TEST") != "1",
    reason="requires an isolated Linux VM with eBPF/cgroup v2",
)


class DirectoryBackend:
    def assert_root(self, root):
        pass

    def create_subvolume(self, path):
        path.mkdir()

    def snapshot(self, source, target, *, readonly):
        shutil.copytree(source, target)

    def delete_subvolume(self, path):
        shutil.rmtree(path)


@pytest.fixture
def service(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    store = WorkspaceStore(root, DirectoryBackend())
    store.initialize()
    instance = WorkspaceService(
        store,
        process_tracking="ebpf",
        process_audit_root=tmp_path / "audit",
        process_cgroup_root=Path("/sys/fs/cgroup/allox-kernel-tests") / tmp_path.name,
        process_tracker_command=os.environ.get(
            "ALLOX_EBPF_COMMAND", "/src/native/process-tracker/allox-process-tracker"
        ),
    )
    yield instance
    instance.close()


def start(service, code, agent="a", session="s", timeout=15):
    if not service.store.current(agent, session).exists():
        service.store.create_session(agent, session)
    return service.processes.start(agent, session, [sys.executable, "-c", code], timeout=timeout)


def wait(service, row, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = service.processes.get(row["agent_id"], row["session_id"], row["run_id"])
        if result["state"] not in {"starting", "running"}:
            assert result["trace_complete"], result
            assert not result["session_fenced"], result
            return result
        time.sleep(0.02)
    pytest.fail(f"run did not finish: {result}")


def events(service, row):
    return service.processes.events(row["agent_id"], row["session_id"], row["run_id"], limit=1000)[
        "events"
    ]


def wait_file(service, row, name):
    file = service.store.current(row["agent_id"], row["session_id"]) / name
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if file.exists() and file.stat().st_size:
            return file
        time.sleep(0.02)
    pytest.fail("missing workload output " + str(file))


def test_short_forks_and_shell_exec_are_recorded_before_completion(service):
    row = wait(
        service,
        start(
            service,
            "import os,json,subprocess\n"
            "subprocess.run(['sh','-c','/bin/true'])\n"
            "pids=[]\n"
            "for i in range(40):\n"
            " p=os.fork()\n"
            " if not p: os._exit(0)\n"
            " pids.append(p)\n"
            "for p in pids: os.waitpid(p,0)\n"
            "open('pids','w').write(json.dumps(pids))\n",
        ),
    )
    actual = set(json.loads(wait_file(service, row, "pids").read_text()))
    journal = events(service, row)
    # The target sees PID-namespace IDs; alloxd/eBPF intentionally records
    # stable Guest-global IDs outside the Bubblewrap namespace.
    assert len([e for e in journal if e["event"] == "fork"]) >= len(actual)
    assert len([e for e in journal if e["event"] == "exit"]) >= len(actual)
    assert len([e for e in journal if e["event"] == "exec"]) >= 3
    assert row["root_exit_code"] == 0
    assert all(not task["alive"] for task in row["processes"].values())


def test_thread_exec_preserves_identity_and_removes_former_tid(service):
    row = wait(
        service,
        start(
            service,
            "import threading,os,time\n"
            "def f():\n"
            " time.sleep(.05)\n"
            " os.execv('/bin/true',['true'])\n"
            "threading.Thread(target=f).start()\n"
            "time.sleep(10)\n",
        ),
    )
    journal = events(service, row)
    moved = [e for e in journal if e["event"] == "exec" and e["former_tid"] != e["pid"]]
    assert moved, journal
    assert any(e["event"] == "fork" and e["process_id"] == moved[0]["process_id"] for e in journal)
    assert any(e["event"] == "exit" and e["process_id"] == moved[0]["process_id"] for e in journal)


def test_all_thread_exits_and_forks_after_leader_exit(service):
    row = wait(
        service,
        start(
            service,
            "import threading,subprocess,time\n"
            "def f():\n"
            " time.sleep(.1)\n"
            " subprocess.run(['/bin/true'])\n"
            "threads=[threading.Thread(target=f) for _ in range(8)]\n"
            "for t in threads: t.start()\n"
            "for t in threads: t.join()\n",
        ),
    )
    journal = events(service, row)
    born = {e["process_id"] for e in journal if e["event"] in {"root", "fork"}}
    dead = {e["process_id"] for e in journal if e["event"] == "exit"}
    assert born <= dead, born - dead


def test_double_fork_setsid_and_environment_clear_still_stop_as_one_session(service):
    row = start(
        service,
        "import os,time\n"
        "if os.fork(): os._exit(0)\n"
        "os.setsid()\n"
        "if os.fork(): os._exit(0)\n"
        "os.environ.clear()\n"
        "open('grandchild','w').write(str(os.getpid()))\n"
        "time.sleep(60)\n",
        timeout=90,
    )
    wait_file(service, row, "grandchild")
    with pytest.raises(WorkspaceError, match="active executions"):
        service.dispatch("checkpoint.create", {"agent_id": "a", "session_id": "s"})
    stopped = service.processes.stop("a", "s", row["run_id"])
    assert stopped["state"] == "stopped", stopped
    assert stopped["trace_complete"], stopped
    assert any(e["event"] == "exit" for e in events(service, row))
    assert all(not task["alive"] for task in stopped["processes"].values())
    service.dispatch("checkpoint.create", {"agent_id": "a", "session_id": "s"})


def test_parallel_agent_session_membership_and_timeout(service):
    rows = [
        start(service, "import time; time.sleep(.2)", a, s)
        for a, s in [("a", "s1"), ("a", "s2"), ("b", "s1")]
    ]
    finished = [wait(service, row) for row in rows]
    for row in finished:
        assert all(
            e["session_id"] == row["session_id"] and e["agent_id"] == row["agent_id"]
            for e in events(service, row)
        )
    assert len({row["cgroup"] for row in finished}) == 3
    timed = wait(service, start(service, "import time; time.sleep(60)", timeout=0.2))
    assert timed["stop_reason"] == "timeout"


def test_collector_death_kills_workloads_and_reports_incomplete_audit(service):
    row = start(service, "import time; open('ready','w').write('yes'); time.sleep(60)")
    wait_file(service, row, "ready")
    service.processes.backend._process.kill()
    run = service.processes._runs[row["run_id"]]
    assert run["done"].wait(10)
    result = service.processes.get("a", "s", row["run_id"])
    assert result["state"] == "failed"
    assert not result["trace_complete"]
    assert not service.processes.cgroups.populated("a", "s")


def test_bubblewrap_hides_cgroupfs_and_outer_cgroup_still_stops_descendant(service):
    row = start(
        service,
        "import os,time,pathlib\n"
        "if os.fork(): os._exit(0)\n"
        "open('cgroup-visible','w').write(str(pathlib.Path('/sys/fs/cgroup').exists()))\n"
        "time.sleep(60)\n",
        timeout=90,
    )
    assert wait_file(service, row, "cgroup-visible").read_text() == "False"
    assert service.processes.cgroups.populated("a", "s")
    stopped = service.processes.stop("a", "s", row["run_id"])
    assert stopped["state"] == "stopped"
    assert not service.processes.cgroups.populated("a", "s")


def test_audit_survives_workspace_restore(service):
    service.store.create_session("a", "s")
    service.store.create_checkpoint("a", "s", "before")
    row = wait(service, start(service, "open('after','w').write('value')"))
    journal = events(service, row)
    service.dispatch(
        "session.rollback", {"agent_id": "a", "session_id": "s", "checkpoint_id": "before"}
    )
    assert not (service.store.current("a", "s") / "after").exists()
    assert events(service, row) == journal


def test_rollback_option_kills_session_tree_before_restoring_workspace(service):
    service.store.create_session("a", "s")
    checkpoint = service.store.create_checkpoint("a", "s", "before-process")
    row = start(
        service,
        "import os,time\n"
        "if os.fork():\n"
        " open('created-after-checkpoint','w').write('parent')\n"
        " time.sleep(60)\n"
        "else:\n"
        " os.setsid()\n"
        " open('child-ready','w').write(str(os.getpid()))\n"
        " time.sleep(60)\n",
        timeout=90,
    )
    wait_file(service, row, "child-ready")

    result = service.dispatch(
        "session.rollback",
        {
            "agent_id": "a",
            "session_id": "s",
            "checkpoint_id": checkpoint["checkpoint_id"],
            "kill_processes": True,
        },
    )

    stopped = service.processes.get("a", "s", row["run_id"])
    assert result["terminated_process_runs"] == [row["run_id"]]
    assert stopped["state"] == "stopped"
    assert stopped["stop_reason"] == "rollback"
    assert stopped["trace_complete"]
    assert not service.processes.cgroups.populated("a", "s")
    assert not (service.store.current("a", "s") / "created-after-checkpoint").exists()
    assert not (service.store.current("a", "s") / "child-ready").exists()
