"""Provider contract and backend-neutral process tracking tests."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from allox_process_tree.backend import EbpfBackend

from allox.runtime.extensions import (
    ExecutionContribution,
    FeatureManager,
    WorkspaceFeatureContext,
)
from allox.runtime.process_tracking.service import (
    ProcessTrackingService,
    _StableProcessLauncher,
)
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
        self.root = Path(root).resolve()
        self.members = {}
        self.identities = {}
        self.actions = []

    def path(self, agent_id, session_id):
        return self.root / agent_id / session_id

    def attach(self, agent_id, session_id, pid):
        key = (agent_id, session_id)
        self.members[key] = pid
        self.identities.setdefault(key, 1000 + len(self.identities))
        self.actions.append(("attach", pid))
        return self.path(agent_id, session_id)

    def identity(self, agent_id, session_id):
        key = (agent_id, session_id)
        if key not in self.identities:
            raise WorkspaceError("unknown fake Session cgroup")
        self.actions.append(("identity", self.identities[key]))
        return {
            "path": str(self.path(agent_id, session_id)),
            "relative_path": f"/allox/agents/{agent_id}/sessions/{session_id}",
            "id": self.identities[key],
        }

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
        self.actions.append(("kill", agent_id, session_id))
        pid = self.members.get((agent_id, session_id))
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def pids(self, agent_id, session_id):
        pid = self.members.get((agent_id, session_id))
        return [pid] if pid is not None and self.populated(agent_id, session_id) else []

    def owns(self, agent_id, session_id, pid):
        return self.members.get((agent_id, session_id)) == pid and self.populated(
            agent_id, session_id
        )

    def freeze(
        self,
        agent_id,
        session_id,
        *,
        expected_cgroup_id,
        require_populated,
        timeout=5.0,
    ):
        identity = self.identity(agent_id, session_id)
        if identity["id"] != expected_cgroup_id:
            raise WorkspaceError("fake Session cgroup identity changed")
        if require_populated and not self.populated(agent_id, session_id):
            raise WorkspaceError("fake Session cgroup became empty")
        self.actions.append(("freeze", expected_cgroup_id, require_populated))
        return identity

    def thaw(self, agent_id, session_id, *, expected_cgroup_id, timeout=5.0):
        identity = self.identity(agent_id, session_id)
        if identity["id"] != expected_cgroup_id:
            raise WorkspaceError("fake Session cgroup identity changed")
        self.actions.append(("thaw", expected_cgroup_id))

    def wait_empty(self, agent_id, session_id, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.populated(agent_id, session_id):
                self.actions.append(("empty", agent_id, session_id))
                return True
            time.sleep(0.01)
        empty = not self.populated(agent_id, session_id)
        if empty:
            self.actions.append(("empty", agent_id, session_id))
        return empty

    def quiesce_empty(self, agent_id, session_id, *, expected_cgroup_id, timeout=5.0):
        if not self.wait_empty(agent_id, session_id, timeout):
            raise WorkspaceError("fake Session cgroup did not become empty")
        identity = self.identity(agent_id, session_id)
        if identity["id"] != expected_cgroup_id:
            raise WorkspaceError("fake Session cgroup identity changed")
        self.actions.append(("quiesce", expected_cgroup_id))
        return identity

    def remove(self, agent_id, session_id):
        if self.populated(agent_id, session_id):
            self.actions.append(("remove-blocked", agent_id, session_id))
            return False
        key = (agent_id, session_id)
        self.members.pop(key, None)
        self.identities.pop(key, None)
        self.actions.append(("remove", agent_id, session_id))
        return True


class FakeLifecycleHandle:
    def __init__(self, timeline):
        self.timeline = timeline
        self.closed = False

    def status(self):
        self.timeline.append(("feature-status",))
        return {"status": "READY"}

    def assert_healthy(self):
        self.timeline.append(("feature-healthy",))

    def agent_started(self):
        self.timeline.append(("feature-agent-started",))

    def finish(self, *, outcome, quiescence):
        self.timeline.append(
            ("feature-finish", outcome, quiescence.status, quiescence.cgroup_id)
        )
        return {"status": "FINISHED", "outcome": outcome}

    def close_unverified(self):
        self.closed = True
        self.timeline.append(("feature-close-unverified",))


class FakeLifecycleFeature:
    name = "fake-lifecycle"
    abi = 1
    execution_lifecycle_abi = 2
    rpc_prefixes = ()

    def __init__(self, *, context, config):
        self.context = context
        self.timeline = config["timeline"]
        self.handle = None

    def prepare_execution(self, agent_id, session_id):
        return ExecutionContribution(metadata={"feature_scope": f"{agent_id}/{session_id}"})

    def start_execution(self, context):
        self.timeline.append(
            ("feature-start", context.root_pid, context.cgroup_id, context.execution_metadata)
        )
        self.handle = FakeLifecycleHandle(self.timeline)
        return self.handle

    def dispatch(self, action, params):
        raise WorkspaceError(f"unexpected fake lifecycle RPC: {action} {params}")

    def close(self):
        pass


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

    def test_recovery_plan_creates_and_pins_an_exact_baseline(self):
        self.manager.features = SimpleNamespace(recovery_lifecycle_enabled=True)

        plan = self.manager._prepare_recovery_plan("a", "s", "run123", {})

        self.assertEqual(plan.checkpoint_id, "baseline-run123")
        self.assertEqual(plan.recovery_id, "recovery-run123")
        self.assertTrue(plan.baseline_created)
        status = self.store.checkpoint_status("a", "s")
        baseline = next(
            item
            for item in status["checkpoints"]
            if item["checkpoint_id"] == plan.checkpoint_id
        )
        self.assertTrue(baseline["pinned"])
        self.assertEqual(
            baseline["metadata"],
            {"run_id": "run123", "recovery_id": "recovery-run123"},
        )

    def test_recovery_plan_rejects_checkpoint_prefixes(self):
        self.store.create_checkpoint("a", "s", "checkpoint-complete")
        self.manager.features = SimpleNamespace(recovery_lifecycle_enabled=True)

        with self.assertRaisesRegex(WorkspaceError, "exact recovery checkpoint"):
            self.manager._prepare_recovery_plan(
                "a", "s", "run123", {"checkpoint_id": "checkpoint"}
            )

    def test_recovery_plan_requires_an_enabled_recovery_plugin(self):
        with self.assertRaisesRegex(WorkspaceError, "recovery lifecycle plugin"):
            self.manager._prepare_recovery_plan("a", "s", "run123", {})

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

    @unittest.skipUnless(sys.platform == "linux", "exec gate requires Linux SIGSTOP")
    def test_lifecycle_feature_runs_between_frozen_inner_gate_and_thaw(self):
        timeline = self.manager.cgroups.actions
        features = FeatureManager(
            WorkspaceFeatureContext(
                store=self.store,
                executions=self.service.executions,
                cgroups=self.manager.cgroups,
            ),
            (("fake-lifecycle", {"timeline": timeline}),),
            factories={"fake-lifecycle": FakeLifecycleFeature},
        )
        self.service.features = features
        self.manager.features = features

        def fake_build_bwrap(
            workspace_path,
            agent_shared_path,
            agent_id,
            session_id,
            command,
            environment=(),
            *,
            mounts=(),
            launch_barrier=False,
            run_id=None,
        ):
            del (
                workspace_path,
                agent_shared_path,
                agent_id,
                session_id,
                environment,
                mounts,
                run_id,
            )
            self.assertIs(launch_barrier, True)
            return list(command)

        def fake_inner_gate(process, agent_id, session_id, *, timeout):
            del agent_id, session_id, timeout
            timeline.append(("inner-gate", process.pid))
            return process.pid

        opened_pidfds = []
        closed_pidfds = []
        pidfd_signals = []
        real_open_pidfd = self.manager._open_pidfd
        real_close_pidfd = self.manager._close_pidfd
        real_pidfd_send_signal = self.manager._pidfd_send_signal

        def track_open_pidfd(pid):
            pidfd = real_open_pidfd(pid)
            opened_pidfds.append(pidfd)
            return pidfd

        def track_close_pidfd(pidfd):
            closed_pidfds.append(pidfd)
            real_close_pidfd(pidfd)

        def track_pidfd_send_signal(pidfd, signum):
            pidfd_signals.append((pidfd, signum))
            real_pidfd_send_signal(pidfd, signum)

        with (
            patch(
                "allox.runtime.process_tracking.service.build_bwrap_argv",
                side_effect=fake_build_bwrap,
            ),
            patch.object(self.manager, "_wait_inner_gate", side_effect=fake_inner_gate),
            patch.object(self.manager, "_open_pidfd", side_effect=track_open_pidfd),
            patch.object(self.manager, "_close_pidfd", side_effect=track_close_pidfd),
            patch.object(
                self.manager,
                "_pidfd_send_signal",
                side_effect=track_pidfd_send_signal,
            ),
        ):
            row = self.manager.start(
                "a",
                "s",
                [sys.executable, "-c", "import time; time.sleep(.2)"],
            )
            self.assertEqual(row["feature_runs"], {"fake-lifecycle": {"status": "READY"}})
            finished = self.wait(row)

        self.assertEqual(finished["state"], "completed")
        self.assertTrue(finished["lifecycle_complete"])
        self.assertFalse(finished["session_fenced"])
        self.assertEqual(
            finished["feature_runs"],
            {"fake-lifecycle": {"status": "FINISHED", "outcome": "completed"}},
        )
        names = [entry[0] for entry in timeline]
        self.assertLess(names.index("freeze"), names.index("feature-start"))
        self.assertLess(names.index("feature-start"), names.index("feature-agent-started"))
        self.assertLess(names.index("feature-agent-started"), names.index("thaw"))
        self.assertLess(names.index("quiesce"), names.index("feature-finish"))
        self.assertLess(names.index("feature-finish"), names.index("remove"))
        self.assertEqual(len(opened_pidfds), 1)
        self.assertEqual(closed_pidfds, opened_pidfds)
        self.assertIn((opened_pidfds[0], signal.SIGCONT), pidfd_signals)
        self.assertGreaterEqual(pidfd_signals.count((opened_pidfds[0], 0)), 10)

    @unittest.skipUnless(
        sys.platform == "linux" and hasattr(os, "pidfd_open"),
        "requires Linux pidfds",
    )
    def test_dead_pidfd_rejects_numeric_identity_before_proc_or_cgroup_lookup(self):
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        pidfd = os.pidfd_open(process.pid)
        process.wait(timeout=5)
        try:
            with (
                patch.object(self.manager.cgroups, "owns", return_value=True) as owns,
                patch.object(self.manager, "_namespace_inode", return_value=1) as namespace,
            ):
                with self.assertRaisesRegex(WorkspaceError, "pinned inner launch gate"):
                    self.manager._target_identity(
                        "a", "s", process.pid, pidfd, phase="after Observer readiness"
                    )
                owns.assert_not_called()
                namespace.assert_not_called()
        finally:
            os.close(pidfd)

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

    def test_process_launcher_uses_one_daemon_lifetime_thread(self):
        caller_thread = threading.get_ident()
        launch_threads = []
        real_popen = subprocess.Popen

        def recording_popen(*args, **kwargs):
            launch_threads.append(threading.get_ident())
            return real_popen(*args, **kwargs)

        launcher = _StableProcessLauncher()
        try:
            with patch(
                "allox.runtime.process_tracking.service.subprocess.Popen",
                side_effect=recording_popen,
            ):
                first = launcher.spawn([sys.executable, "-c", "pass"])
                second = launcher.spawn([sys.executable, "-c", "pass"])
                self.assertEqual(first.wait(timeout=5), 0)
                self.assertEqual(second.wait(timeout=5), 0)
        finally:
            launcher.close()

        self.assertEqual(len(launch_threads), 2)
        self.assertEqual(len(set(launch_threads)), 1)
        self.assertNotEqual(launch_threads[0], caller_thread)

    @unittest.skipUnless(sys.platform == "linux", "exec gate requires Linux SIGSTOP")
    def test_timeout_stops_at_process_exit_not_observer_finalization(self):
        finalizing = threading.Event()
        release_finalizer = threading.Event()
        original_finish = self.tracker.finish

        def delayed_finish(pid, cookie):
            finalizing.set()
            if not release_finalizer.wait(5):
                raise AssertionError("test finalizer release timed out")
            original_finish(pid, cookie)

        self.tracker.finish = delayed_finish
        row = self.manager.start(
            "a",
            "s",
            [sys.executable, "-c", "pass"],
            timeout=0.1,
        )
        self.assertTrue(finalizing.wait(5))
        time.sleep(0.2)
        release_finalizer.set()
        finished = self.wait(row)
        self.assertEqual(finished["state"], "completed")
        self.assertNotIn("stop_reason", finished)

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
