"""Provider contract and backend-neutral process tracking tests."""

from __future__ import annotations

import json
import os
import shutil
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

from allox.runtime.process_tracking.backends import EbpfBackend
from allox.runtime.process_tracking.service import ProcessTrackingService
from allox.workspace.daemon import WorkspaceService
from allox.workspace.store import WorkspaceError, WorkspaceStore


class DirectoryBackend:
    def assert_root(self, root):
        pass

    def create_subvolume(self, path):
        path.mkdir()

    def snapshot(self, source, target, *, readonly):
        shutil.copytree(source, target)

    def delete_subvolume(self, path):
        shutil.rmtree(path)


class FakeTracker:
    name = "fake-ebpf"

    def __init__(self, on_event, on_failure):
        self.on_event = on_event
        self.on_failure = on_failure
        self.seeds = {}
        self.closed = False

    def seed(self, pid, cookie):
        self.seeds[pid] = cookie
        self.emit(pid, "root")

    def finish(self, pid, cookie):
        self.seeds.pop(pid, None)

    def status(self):
        return {"backend": self.name, "abi": 1, "ready": not self.closed, "error": None}

    def emit(self, pid, event="exec", **extra):
        self.on_event(
            {
                "kind": "event",
                "cookie": self.seeds[pid],
                "pid": pid,
                "event": event,
                "birth_ns": pid * 1000,
                "birth_pid": pid,
                **extra,
            }
        )

    def close(self):
        self.closed = True


class FakeCgroup:
    def __init__(self, root):
        self.root = Path(root)
        self.members = {}

    def path(self, agent_id, session_id):
        return self.root / agent_id / session_id

    def attach(self, agent_id, session_id, pid):
        self.members[(agent_id, session_id)] = pid
        return self.path(agent_id, session_id)

    def populated(self, agent_id, session_id):
        pid = self.members.get((agent_id, session_id))
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            self.members.pop((agent_id, session_id), None)
            return False

    def kill(self, agent_id, session_id):
        pid = self.members.get((agent_id, session_id))
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def remove(self, agent_id, session_id):
        self.members.pop((agent_id, session_id), None)


class ProcessTrackingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "store").mkdir()
        self.store = WorkspaceStore(root / "store", DirectoryBackend())
        self.store.initialize()
        self.store.create_session("a", "s")
        self.tracker = None

        def backend_factory(name, *, command, on_event, on_failure):
            self.assertEqual(name, "ebpf")
            self.tracker = FakeTracker(on_event, on_failure)
            return self.tracker

        self.service = WorkspaceService(
            self.store,
            process_tracking="ebpf",
            process_audit_root=root / "audit",
            process_cgroup_root=root / "cgroup",
            process_service_factory=lambda store, executions, audit, **options: (
                ProcessTrackingService(
                    store,
                    executions,
                    audit,
                    **options,
                    backend_factory=backend_factory,
                    cgroup_factory=FakeCgroup,
                    boot_id="test-boot",
                )
            ),
        )
        self.manager = self.service.processes

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def wait(self, row, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            current = self.manager.get("a", "s", row["run_id"])
            if current["state"] not in {"starting", "running"}:
                return current
            time.sleep(0.02)
        self.fail("run did not finish")

    @unittest.skipUnless(sys.platform == "linux", "exec gate requires Linux SIGSTOP")
    def test_ebpf_provider_is_seeded_before_target_exec(self):
        row = self.manager.start("a", "s", [sys.executable, "-c", "import time; time.sleep(.1)"])
        self.assertEqual(row["backend"], "fake-ebpf")
        self.assertIn(row["root_pid"], self.tracker.seeds)
        self.assertFalse((Path(row["audit_directory"]) / "launch.json").exists())
        self.tracker.emit(row["root_pid"], filename=sys.executable, comm="python")
        finished = self.wait(row)
        self.assertEqual(finished["state"], "completed")
        self.assertTrue(finished["trace_complete"])
        events = self.manager.events("a", "s", row["run_id"], limit=100)["events"]
        self.assertEqual([event["event"] for event in events], ["root", "exec"])
        self.assertTrue(all(event["agent_id"] == "a" for event in events))

    @unittest.skipUnless(
        sys.platform == "linux" and shutil.which("bwrap"),
        "requires Bubblewrap and Linux namespaces",
    )
    def test_tracked_target_runs_inside_bubblewrap(self):
        code = (
            "import json,os,pathlib; "
            "pathlib.Path('isolation.json').write_text(json.dumps({"
            "'cwd':os.getcwd(),'home':os.environ['HOME'],'tmp':os.environ['TMPDIR'],"
            "'agent':os.environ['ALLOX_AGENT_ID'],'session':os.environ['ALLOX_SESSION_ID'],"
            "'proc':pathlib.Path('/proc/self/status').is_file()}))"
        )
        row = self.manager.start("a", "s", [sys.executable, "-c", code])
        self.assertEqual(row["isolation"], "bubblewrap")
        finished = self.wait(row)
        self.assertEqual(finished["root_exit_code"], 0)
        proof = json.loads((self.store.current("a", "s") / "isolation.json").read_text())
        self.assertEqual(
            proof,
            {
                "cwd": "/workspace",
                "home": "/workspace",
                "tmp": "/tmp",
                "agent": "a",
                "session": "s",
                "proc": True,
            },
        )

    @unittest.skipUnless(sys.platform == "linux", "exec gate requires Linux SIGSTOP")
    def test_stop_uses_cgroup_component(self):
        row = self.manager.start("a", "s", [sys.executable, "-c", "import time; time.sleep(60)"])
        stopped = self.manager.stop("a", "s", row["run_id"])
        self.assertEqual(stopped["state"], "stopped")
        self.assertEqual(stopped["stop_reason"], "requested")

    def test_provider_failure_is_reported(self):
        self.assertEqual(self.manager.status()["backend"], "fake-ebpf")
        self.tracker.on_failure("ring buffer failed")
        self.assertEqual(self.manager._backend_error, "ring buffer failed")

    def test_disabled_status_and_enabled_configuration_validation(self):
        disabled = WorkspaceService(self.store)
        self.assertEqual(
            disabled.dispatch("process.status", {}), {"enabled": False, "backend": "disabled"}
        )
        with self.assertRaisesRegex(WorkspaceError, "audit root"):
            WorkspaceService(self.store, process_tracking="ebpf")


class EbpfAdapterProtocolTests(unittest.TestCase):
    def test_versioned_collector_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "collector.py"
            script.write_text(
                "import json,sys\n"
                "print(json.dumps({'kind':'ready','abi':1}),flush=True)\n"
                "for line in sys.stdin:\n"
                " op,pid,cookie=line.split()\n"
                " print(json.dumps({'kind':'ack','operation':op,'cookie':int(cookie)}),flush=True)\n"
                " if op=='seed':\n"
                "  print(json.dumps({'kind':'event','event':'exec','cookie':int(cookie),"
                "'pid':int(pid),'filename':'/bin/test'}),flush=True)\n"
            )
            events = []
            failures = []
            backend = EbpfBackend(f'"{sys.executable}" "{script}"', events.append, failures.append)
            try:
                backend.seed(123, 456)
                deadline = time.monotonic() + 2
                while not events and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(events[0]["cookie"], 456)
                self.assertEqual(backend.status()["abi"], 1)
                self.assertEqual(failures, [])
            finally:
                backend.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
