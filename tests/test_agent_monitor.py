"""Optional monitoring: data integrity, timing, concurrency, persistence and Core hooks."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from allox_agent_monitor.plugin import AgentMonitorFeature
from allox_agent_monitor.profile import profile
from allox_agent_monitor.resources import delta, snapshot
from allox_agent_monitor.sdk import Tracer, payload, record_vllm_metrics
from allox_agent_monitor.store import TraceStore

from allox.runtime.extensions import FeatureManager, WorkspaceFeatureContext
from allox.workspace.daemon import ExecutionRegistry, WorkspaceHTTPServer, WorkspaceService, main
from allox.workspace.store import WorkspaceError


@pytest.fixture
def store(tmp_path):
    result = TraceStore(tmp_path / "monitor")
    yield result
    result.close()


def tracer(store, trace_id="trace"):
    return Tracer(lambda event: store.ingest("a", "s", event), trace_id=trace_id, strict=True)


def event(span="s", **extra):
    return {"event": "start", "trace_id": "trace", "span_id": span, "kind": "tool",
            "name": "test", "clock_id": "clock", "start_ns": 0, **extra}


def end(span, when, **extra):
    return {"event": "end", "trace_id": "trace", "span_id": span, "clock_id": "clock",
            "end_ns": when, "status": "ok", **extra}


def test_nested_trace_tool_internal_and_model_metrics(store):
    t = tracer(store)
    with t.span("task", "run", semantic_path=["repair"]), t.span("turn", "turn", turn_id="turn1"):
        assert t.call_tool("sum", sum, [1, 2]) == 3
        t.call_model("qwen", lambda: {"metrics": {"queue_time_ms": 2,
            "time_to_first_token_ms": 3, "generation_time_ms": 4},
            "usage": {"prompt_tokens": 20, "completion_tokens": 5}})
    rows = store.spans("a", "s")
    assert len(rows) == 5
    phase = next(row for row in rows if row["kind"] == "phase")
    assert phase["name"] == "tool_execution" and phase["duration_ns"] > 0
    assert 0 < phase["metrics"]["executor.execution_ns"] <= phase["duration_ns"]
    assert phase["turn_id"] == "turn1" and phase["semantic_path"] == ["repair"]
    llm = next(row for row in rows if row["kind"] == "llm")
    assert llm["metrics"]["provider.queue_time_ms"] == 2
    assert "transport_ms" not in llm["metrics"]
    assert profile(rows)["complete"]
    assert t.health()["attempted_events"] == 10


def test_parallel_async_and_loop_causality(store):
    t = tracer(store)

    async def child(i):
        with t.span("retry", parallel_group="p", loop_id="loop", iteration=i) as span:
            await asyncio.sleep(.001)
            return span.span_id

    async def run():
        with t.span("task", "run") as parent:
            ids = await asyncio.gather(child(0), child(1))
            with t.span("join", links=ids):
                pass
            return parent.span_id, ids

    root, ids = asyncio.run(run())
    rows = store.spans("a", "s")
    assert all(row["parent_id"] == root for row in rows if row["span_id"] in ids)
    assert next(row for row in rows if row["name"] == "join")["links"] == ids
    assert profile(rows)["complete"]


def test_overlapping_children_are_subtracted_as_union(store):
    for item in (event("root"), event("c1", parent_id="root", start_ns=10),
                 event("c2", parent_id="root", start_ns=30), end("c1", 60), end("c2", 80),
                 end("root", 100)):
        store.ingest("a", "s", item)
    report = profile(store.spans("a", "s"))
    # All names equal: root self 30 + child self 50 + child self 50 = 130, not 100 wall.
    assert report["groups"][0]["self_ns"] == 130
    assert report["groups"][0]["inclusive_ns"] == 200


def test_cross_run_semantic_grouping(store):
    for run in ("one", "two"):
        t = tracer(store, run)
        with t.span("parse", semantic_path=["analyse", "parse"]):
            pass
    report = profile(store.spans("a", "s"))
    assert report["runs"] == 2
    assert report["groups"][0]["calls"] == report["groups"][0]["runs"] == 2


def test_errors_and_cancellation_are_recorded_without_exception_text(store):
    t = tracer(store)
    with pytest.raises(ValueError):
        t.call_tool("bad", lambda: (_ for _ in ()).throw(ValueError("secret-message")))
    rows = store.spans("a", "s")
    assert all(row["status"] == "error" for row in rows)
    assert "secret-message" not in json.dumps(rows)

    async def cancelled():
        with t.span("cancel"):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancelled())
    assert store.spans("a", "s")[-1]["status"] == "cancelled"


def test_monitor_failure_does_not_mask_application_exception():
    def fail(event):
        raise OSError("disk full")

    soft = Tracer(fail)
    assert soft.call_tool("tool", lambda: 7) == 7
    assert soft.health()["failed_events"] == 4
    strict = Tracer(fail, strict=True)
    with pytest.raises(OSError):
        strict.call_tool("tool", lambda: 7)
    disabled = Tracer(fail, enabled=False)
    disabled.call_tool("tool", lambda: 7)
    assert disabled.health()["attempted_events"] == 0


def test_default_payload_is_private_and_capture_redacts_keys():
    secret = {"api_key": "dont-log", "nested": {"password": "dont-log"}, "data": "hi"}
    assert "content" not in payload(secret)
    assert "dont-log" not in json.dumps(payload(secret, capture=True))
    assert payload(secret, capture=True)["content"]["data"] == "hi"


def test_unusual_tool_results_and_bad_provider_metrics_do_not_break_soft_mode(store):
    recursive = []
    recursive.append(recursive)
    assert payload(recursive)["bytes"] > 0
    t = Tracer(lambda e: store.ingest("a", "s", e))
    assert t.call_tool("nan", lambda: float("nan")) != 0
    assert t.call_model("bad-provider", lambda: {"metrics": {"queue_time_ms": -1}})
    assert t.health()["metric_errors"] == 1 and not t.health()["complete"]


def test_missing_vllm_fields_are_missing_not_zero(store):
    t = tracer(store)
    with t.span("qwen", "llm") as span:
        record_vllm_metrics(span, {"metrics": None})
    assert not any(k.startswith("provider.") for k in store.spans("a", "s")[0]["metrics"])


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, True, "1"])
def test_invalid_metrics_rejected(store, bad):
    store.ingest("a", "s", event())
    with pytest.raises(WorkspaceError):
        store.ingest("a", "s", end("s", 2, metrics={"bad": bad}))
    assert store.spans("a", "s")[0]["status"] == "running"


@pytest.mark.parametrize("changes", [
    {"parent_id": "absent"}, {"links": ["absent"]}, {"start_ns": -1},
    {"start_ns": True}, {"kind": "unknown"}, {"semantic_path": "not-a-list"},
    {"clock_id": "../escape"}, {"iteration": -1}, {"kind": "phase", "name": "unknown"},
])
def test_malformed_spans_rejected(store, changes):
    with pytest.raises(WorkspaceError):
        store.ingest("a", "s", event(**changes))
    assert store.status()["rejected_events_this_process"] == 1


def test_duplicate_clock_and_source_mismatch_rejected(store):
    store.ingest("a", "s", event(), source="allox-executor")
    with pytest.raises(WorkspaceError, match="duplicate"):
        store.ingest("a", "s", event())
    with pytest.raises(WorkspaceError, match="source"):
        store.ingest("a", "s", end("s", 2))
    with pytest.raises(WorkspaceError, match="clocks"):
        store.ingest("a", "s", end("s", 2, clock_id="other"), source="allox-executor")


def test_sessions_isolated_and_unfinished_survives_restart(tmp_path):
    root = tmp_path / "monitor"
    store = TraceStore(root)
    store.ingest("a", "s", event())
    assert store.spans("other", "s") == []
    store.close()
    restarted = TraceStore(root)
    try:
        report = profile(restarted.spans("a", "s"))
        assert not report["complete"] and report["warnings"][0]["reason"] == "unfinished"
    finally:
        restarted.close()


def test_bounded_events_and_queries(tmp_path):
    store = TraceStore(tmp_path, max_spans=1)
    try:
        store.ingest("a", "s", event())
        with pytest.raises(WorkspaceError, match="limit"):
            store.ingest("a", "s", event("two"))
        with pytest.raises(WorkspaceError, match="64 KiB"):
            store.ingest("a", "s", event("three", input="x"*65536))
    finally:
        store.close()


def make_plugin(tmp_path):
    cgroups = SimpleNamespace(path=lambda a, s: tmp_path / "cgroup" / a / s)
    context = WorkspaceFeatureContext(
        store=SimpleNamespace(root=tmp_path / "workspace"),
        executions=ExecutionRegistry(), cgroups=cgroups,
    )
    return AgentMonitorFeature(context=context, config={"root": tmp_path / "monitor",
                                                       "socket_root": tmp_path / "sockets"}), context


def test_optional_core_observer_reads_final_cgroup_counters(tmp_path):
    plugin, context = make_plugin(tmp_path)
    cgroup = context.cgroups.path("a", "s")
    cgroup.mkdir(parents=True)
    (cgroup / "cpu.stat").write_text("usage_usec 10\n")
    (cgroup / "io.stat").write_text("8:0 rbytes=2 wbytes=3\n")
    manager = FeatureManager(context, (("agent-monitor", {}),),
                             factories={"agent-monitor": lambda **kwargs: plugin})
    try:
        row = {"agent_id": "a", "session_id": "s", "run_id": "run"}
        manager.execution_started(row)
        (cgroup / "cpu.stat").write_text("usage_usec 55\n")
        (cgroup / "memory.peak").write_text("1000")
        (cgroup / "io.stat").write_text("8:0 rbytes=20 wbytes=30\n")
        row["root_exit_code"] = 0
        manager.execution_finished(row)
        rows = plugin.store.spans("a", "s")
        assert rows[0]["source"] == "allox-executor"
        assert rows[0]["metrics"]["cgroup.cpu_usage_usec"] == 45
        assert rows[0]["metrics"]["cgroup.memory_peak_bytes"] == 1000
        assert rows[0]["metrics"]["cgroup.io_read_bytes"] == 18
        assert "monitor_trace_id" in row
    finally:
        manager.close()


def test_resource_absence_is_not_zero(tmp_path):
    assert snapshot(tmp_path) == {}
    assert delta({}, {}) == {}


def test_session_socket_is_append_only_and_rejects_wrong_peer(tmp_path):
    import os

    from allox_agent_monitor.transport import emit_session

    plugin, context = make_plugin(tmp_path)
    context.cgroups.owns = lambda a, s, pid: pid == os.getpid()
    plugin._runs["mocked-run"] = {"row": {"agent_id": "a", "session_id": "s", "state": "running"}}
    try:
        endpoint = plugin._endpoints.endpoint("a", "s")
        t = Tracer(lambda e: emit_session(e, endpoint), strict=True)
        assert t.call_tool("socket-tool", lambda: 42) == 42
        assert len(plugin.store.spans("a", "s")) == 2
        assert all(s["process_run_id"] == "mocked-run" for s in plugin.store.spans("a", "s"))
        with pytest.raises(ValueError, match="identity"):
            emit_session(event("fake", agent_id="other"), endpoint)
        with pytest.raises(ValueError, match="Core-owned"):
            emit_session(event("fake", kind="process"), endpoint)
        context.cgroups.owns = lambda *args: False
        with pytest.raises(ValueError, match="belong"):
            emit_session(event("fake"), endpoint)
    finally:
        plugin.close()


def test_profile_across_sessions_does_not_merge_trace_identity(tmp_path):
    plugin, _ = make_plugin(tmp_path)
    try:
        for session in ("one", "two"):
            plugin.store.ingest("a", session, event())
            plugin.store.ingest("a", session, end("s", 10))
        report = plugin.dispatch("monitor.profile", {"agent_id": "a", "session_id": "one",
            "session_ids": ["one", "two"]})
        assert report["runs"] == 2 and report["groups"][0]["calls"] == 2
    finally:
        plugin.close()


def test_process_hooks_on_real_child_with_fake_kernel_boundary(tmp_path, monkeypatch):
    """Exercise real SIGSTOP/exec/wait, not a claim about Btrfs or real cgroup isolation."""
    import sys

    from test_process_tracking import DirectoryBackend, FakeCgroup

    from allox.runtime.process_tracking.service import ProcessTrackingService
    from allox.workspace.store import WorkspaceStore

    workspace = WorkspaceStore(tmp_path / "workspace", DirectoryBackend())
    workspace.initialize()
    workspace.create_session("a", "s")
    monkeypatch.setattr("allox.runtime.process_tracking.service.build_bwrap_argv",
                        lambda *a, **k: [sys.executable, "-c", "print('monitor-smoke')"])
    service = WorkspaceService(workspace, monitor_root=tmp_path / "monitor",
        process_audit_root=tmp_path / "process-audit", process_cgroup_root=tmp_path / "cgroup",
        feature_factories={"agent-monitor": AgentMonitorFeature},
        process_service_factory=lambda store, executions, audit, **options: ProcessTrackingService(
            store, executions, audit, **options, cgroup_factory=FakeCgroup, boot_id="test"))
    t = Tracer(lambda e: service.dispatch("monitor.ingest", {
        "agent_id": "a", "session_id": "s", "event": e}), strict=True)
    try:
        with t.span("shell", "tool", semantic_path=["test"]), t.span("command") as action:
            row = service.dispatch("process.start", {"agent_id": "a", "session_id": "s",
                "argv": ["unused"], "trace_context": action.context})
            assert service.processes._runs[row["run_id"]]["done"].wait(5)
        spans = service.dispatch("monitor.trace", {"agent_id": "a", "session_id": "s"})["spans"]
        process = next(s for s in spans if s["kind"] == "process")
        assert process["status"] == "ok" and process["source"] == "allox-executor"
        assert process["semantic_path"] == ["test"]
        assert "monitor-smoke" in (Path(row["audit_directory"]) / "stdout.log").read_text()
    finally:
        service.close()


def test_monitor_plugin_activation_auth_and_path_boundaries(tmp_path, monkeypatch):
    store = MagicMock()
    store.root = tmp_path / "workspace"
    with pytest.raises(WorkspaceError, match="outside"):
        AgentMonitorFeature(context=SimpleNamespace(store=store),
                            config={"root": store.root / "inside"})
    monkeypatch.delenv("ALLOX_WORKSPACE_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="TOKEN"):
        main(["--root", "/unused", "--monitor-root", "/audit"])
    fake_processes = MagicMock()
    service = WorkspaceService(store, monitor_root=tmp_path / "monitor",
        process_audit_root=tmp_path / "process-audit",
        process_service_factory=lambda *a, **k: fake_processes,
        feature_factories={"agent-monitor": AgentMonitorFeature})
    try:
        assert service.feature_names == ("agent-monitor",)
        with pytest.raises(WorkspaceError, match="authenticated"):
            WorkspaceHTTPServer(("127.0.0.1", 0), service, "")
        with pytest.raises(WorkspaceError, match="Core-owned"):
            service.dispatch("monitor.ingest", {"agent_id": "a", "session_id": "s",
                                               "event": event(kind="process")})
    finally:
        service.close()


def test_real_http_collector_and_framework_sdk(tmp_path):
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    plugin, _ = make_plugin(tmp_path)
    service = SimpleNamespace(feature_names=("agent-monitor",),
                              session_memory=False,
                              dispatch=plugin.dispatch)
    server = WorkspaceHTTPServer(("127.0.0.1", 0), service, "test-only-token")
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    def send(event, token="test-only-token"):
        request = Request(f"http://127.0.0.1:{server.server_port}/v1/rpc",
            data=json.dumps({"action": "monitor.ingest", "params": {
                "agent_id": "a", "session_id": "s", "event": event}}).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        with urlopen(request, timeout=2) as response:
            result = json.load(response)
            if not result["ok"]:
                raise RuntimeError(result)

    try:
        with pytest.raises(HTTPError) as error:
            send(event(), token="wrong")
        assert error.value.code == 401
        t = Tracer(send, strict=True)
        assert t.call_tool("actual-call", lambda: 42) == 42
        assert profile(plugin.store.spans("a", "s"))["complete"]
    finally:
        server.shutdown()
        worker.join()
        server.server_close()
        plugin.close()
