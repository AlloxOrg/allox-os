"""Real SQLite / management / CLI; Observer records here are explicit fixtures."""

import hashlib
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from allox_agent_monitor.store import TraceStore
from allox_trace_correlation.cli import main
from allox_trace_correlation.correlate import Correlator
from allox_trace_correlation.plugin import TraceCorrelationFeature

from allox.workspace.daemon import WorkspaceHTTPServer, WorkspaceService
from allox.workspace.store import WorkspaceError, WorkspaceStore


def write(path, value):
    data = (json.dumps(value, sort_keys=True) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def bind_span(store, *, agent="a", session="s", run="run-1", trace="trace-1",
              span=None, kind="process", turn="turn-1"):
    span = span or "process-" + run
    event = {"event": "start", "kind": kind, "name": "fixture", "trace_id": trace,
             "span_id": span, "start_ns": 100, "clock_id": "fixture-clock", "turn_id": turn}
    store.ingest(agent, session, event, source="allox-executor" if kind == "process" else "session-sdk",
                 process_run_id=run)
    store.ingest(agent, session, {"event": "end", "trace_id": trace, "span_id": span,
                                "clock_id": "fixture-clock", "end_ns": 200, "status": "ok"},
                 source="allox-executor" if kind == "process" else "session-sdk",
                 process_run_id=run)


def observer_fixture(audit, *, agent="a", session="s", run="run-1", protocol=2, events=None):
    root = audit / run
    state = {"agent_id": agent, "session_id": session, "run_id": run,
             "state": "completed", "run_epoch": 1, "boot_id": "fixture-boot"}
    write(root / "state.json", state)
    effect = root / "system-effects"
    effect.mkdir(exist_ok=True)
    if events is None:
        events = [{"kind": kind, "run_id": run, "pid": 42, "object": {"id": f"object-{i}"}}
                  for i, kind in enumerate(("file", "network", "mount"))]
    manifest_rows = []
    for stream in ("system-effects.jsonl", "process-lifecycle.jsonl"):
        data = b"".join((json.dumps(event) + "\n").encode() for event in events)
        (effect / stream).write_bytes(data)
        manifest_rows.append({"relative_path": stream, "size_bytes": len(data),
                              "sha256": hashlib.sha256(data).hexdigest()})
    manifest = "artifact-manifest.json" if protocol == 2 else "recovery-artifact-manifest.json"
    digest = write(effect / manifest, {"protocol_version": protocol, "run_id": run,
                                      "artifacts": manifest_rows})
    write(root / "system-effects-receipt.json", {
        **{k: state[k] for k in ("agent_id", "session_id", "run_id", "run_epoch")},
        "status": "FINISHED", "protocol_version": protocol,
        "artifact_manifest_sha256": digest, "offline_verification": "VERIFIED",
        "observer_evidence_status": "INCOMPLETE",
    })


@pytest.fixture
def records(tmp_path):
    traces, audit = tmp_path / "traces", tmp_path / "audit"
    audit.mkdir()
    store = TraceStore(traces)
    bind_span(store)
    observer_fixture(audit)
    yield traces, audit, store
    store.close()


def query(records, **kwargs):
    trace, audit, _ = records
    return Correlator(trace, audit).run("a", "s", "run-1", **kwargs)


def test_join_pagination_and_read_only_live_wal(records):
    traces, audit, _ = records
    paths = [*audit.rglob("*"), *traces.glob("*")]
    before = {p: p.read_bytes() for p in paths if p.is_file() and not p.name.endswith("-shm")}
    result = query(records, limit=2)
    assert result["status"] == "correlated"
    assert result["trace_id"] == "trace-1" and result["process_span_id"] == "process-run-1"
    assert result["turn"]["status"] == "declared_parent_turn"
    assert result["turn"]["turn_id"] == "turn-1"
    assert result["observer_evidence_status"] == "INCOMPLETE"
    assert result["event_count"] == 3 and result["next_offset"] == 2
    assert [x["event"]["kind"] for x in result["events"]] == ["file", "network"]
    last = query(records, offset=2, limit=2)
    assert last["next_offset"] is None and last["events"][0]["event"]["kind"] == "mount"
    assert query(records, stream="process-lifecycle.jsonl")["event_count"] == 3
    assert before == {p: p.read_bytes() for p in before}


def test_multiple_turns_stay_ambiguous(records):
    bind_span(records[2], span="sdk", kind="tool", turn="turn-2")
    result = query(records)
    assert result["turn"] == {
        "status": "ambiguous", "turn_id": None, "candidates": ["turn-1", "turn-2"],
        "basis": "recorded context, not kernel semantic proof",
    }


@pytest.mark.parametrize("turn", [None, ""])
def test_missing_turn_is_not_guessed_from_one_sdk_span(records, turn):
    bind_span(records[2], run="run-2", turn=turn)
    bind_span(records[2], run="run-2", span="sdk-2", kind="tool", turn="single-sdk-turn")
    observer_fixture(records[1], run="run-2")
    result = Correlator(records[0], records[1]).run("a", "s", "run-2")
    assert result["turn"]["status"] != "declared_parent_turn"
    assert result["turn"]["turn_id"] is None


@pytest.mark.parametrize("field,value", [
    ("agent_id", "other"), ("session_id", "other"), ("run_id", "other"), ("run_epoch", 99),
])
def test_mismatched_receipt_rejected(records, field, value):
    p = records[1] / "run-1/system-effects-receipt.json"
    receipt = json.loads(p.read_bytes())
    receipt[field] = value
    write(p, receipt)
    with pytest.raises(WorkspaceError, match="identity mismatch|epoch"):
        query(records)


def test_same_trace_in_other_session_cannot_leak(records):
    bind_span(records[2], session="other", run="other-run", trace="trace-1")
    observer_fixture(records[1], session="other", run="other-run")
    assert query(records)["run_id"] == "run-1"
    with pytest.raises(WorkspaceError, match="identity mismatch|Core-owned"):
        Correlator(records[0], records[1]).run("a", "other", "run-1")


@pytest.mark.parametrize("filename", ["artifact-manifest.json", "system-effects.jsonl"])
def test_tampered_artifacts_rejected(records, filename):
    with (records[1] / "run-1/system-effects" / filename).open("ab") as out:
        out.write(b" ")
    with pytest.raises(WorkspaceError, match="digest|invalid event"):
        query(records)


def test_stream_not_in_manifest_does_not_acquire_false_provenance(records):
    base = records[1] / "run-1"
    manifest = base / "system-effects/artifact-manifest.json"
    value = json.loads(manifest.read_bytes())
    value["artifacts"] = []
    digest = write(manifest, value)
    receipt = json.loads((base / "system-effects-receipt.json").read_bytes())
    receipt["artifact_manifest_sha256"] = digest
    write(base / "system-effects-receipt.json", receipt)
    result = query(records)
    assert result["status"] == "stream_not_committed"
    assert result["integrity"] == "unchecked" and result["events"] == []


def test_event_cannot_claim_a_different_run(records):
    observer_fixture(records[1], events=[{"run_id": "another-run", "kind": "network"}])
    with pytest.raises(WorkspaceError, match="event identity"):
        query(records)


def test_framework_claim_is_not_a_core_owner(records):
    store = records[2]
    store._db.execute("DELETE FROM spans")
    store._db.commit()
    event = {"event": "start", "kind": "tool", "name": "not-core", "trace_id": "trace-1",
             "span_id": "sdk", "start_ns": 1, "clock_id": "fixture"}
    store.ingest("a", "s", event, source="framework-sdk", process_run_id="run-1")
    with pytest.raises(WorkspaceError, match="Core-owned"):
        query(records)


def test_incomplete_or_absent_evidence_not_fabricated(records):
    p = records[1] / "run-1/system-effects-receipt.json"
    p.unlink()
    assert query(records)["status"] == "missing_evidence"
    state_path = records[1] / "run-1/state.json"
    state = json.loads(state_path.read_bytes())
    state["state"] = "running"
    write(state_path, state)
    assert query(records)["status"] == "run_not_finalized"
    state_path.unlink()
    assert query(records)["status"] == "missing_core_record"


def test_v3_recovery_verdict_is_not_promoted_to_complete(records):
    observer_fixture(records[1], protocol=3)
    result = query(records)
    assert result["status"] == "correlated"
    assert result["observer_evidence_status"] == "INCOMPLETE"
    assert "recovery-artifact-manifest" in result["provenance"]["manifest"]


def test_duplicate_core_owners_require_trace_selection(records):
    bind_span(records[2], trace="another-trace")
    with pytest.raises(WorkspaceError, match="one Core-owned"):
        query(records)
    assert query(records, trace_id="trace-1")["status"] == "correlated"


def test_core_trace_metadata_mismatch_is_not_silently_joined(records):
    path = records[1] / "run-1/state.json"
    state = json.loads(path.read_bytes())
    state["monitor_trace_id"] = "another-trace"
    write(path, state)
    with pytest.raises(WorkspaceError, match="metadata"):
        query(records)


@pytest.mark.parametrize("params", [{"limit": True}, {"limit": 201}, {"offset": -1},
                                   {"stream": "../state.json"}])
def test_bounds(records, params):
    with pytest.raises(WorkspaceError):
        query(records, **params)


@pytest.mark.skipif(os.name != "posix", reason="no-follow evidence directory access")
def test_symlink_run_directory_refused(records):
    audit = records[1]
    (audit / "link").symlink_to(audit / "run-1", target_is_directory=True)
    bind_span(records[2], run="link", trace="link-trace")
    with pytest.raises(OSError):
        Correlator(records[0], audit).run("a", "s", "link")


@pytest.mark.skipif(os.name != "posix", reason="no-follow evidence file access")
def test_symlink_event_stream_refused(records):
    stream = records[1] / "run-1/system-effects/system-effects.jsonl"
    original = stream.with_suffix(".original")
    stream.rename(original)
    stream.symlink_to(original)
    with pytest.raises(OSError):
        query(records)


@pytest.mark.skipif(os.name != "posix", reason="FIFO evidence access")
def test_fifo_evidence_does_not_block_query(records):
    stream = records[1] / "run-1/system-effects/system-effects.jsonl"
    stream.unlink()
    os.mkfifo(stream)
    with pytest.raises(WorkspaceError, match="unsafe"):
        query(records)


def test_optional_feature_never_starts_process_service(records, tmp_path):
    store = WorkspaceStore(tmp_path / "workspace")

    def forbidden(*args, **kwargs):
        pytest.fail("read-only correlation must not create a process service")

    service = WorkspaceService(
        store, correlation_config={"trace_root": records[0], "audit_root": records[1]},
        process_service_factory=forbidden,
        feature_factories={"trace-correlation": TraceCorrelationFeature},
    )
    try:
        assert service.processes is None
        assert service.feature_names == ("trace-correlation",)
        assert service.dispatch("correlation.status", {})["collection_hooks"] is False
        refs = service.dispatch("correlation.trace",
                                {"agent_id": "a", "session_id": "s", "trace_id": "trace-1"})
        assert refs["runs"][0]["run_id"] == "run-1"
        assert service.dispatch("correlation.run",
                                {"agent_id": "a", "session_id": "s", "run_id": "run-1"})["event_count"] == 3
        contribution = service.features.prepare_execution("a", "s")
        assert not contribution.environment and not contribution.mounts
        with pytest.raises(WorkspaceError, match="authenticated"):
            WorkspaceHTTPServer(("127.0.0.1", 0), service, "")
    finally:
        service.close()
    disabled = WorkspaceService(store)
    with pytest.raises(WorkspaceError, match="unknown action"):
        disabled.dispatch("correlation.status", {})


def test_cli_and_independent_python_process(records, capsys):
    traces, audit, _ = records
    args = ["--trace-root", str(traces), "--audit-root", str(audit),
            "--agent-id", "a", "--session-id", "s", "--run-id", "run-1"]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["integrity"] == "matched_recorded_sha256"
    # subprocess has the same source paths as pytest, not a globally installed plugin.
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(map(str, sys.path))}
    result = subprocess.run([sys.executable, "-m", "allox_trace_correlation.cli", *args],
                            capture_output=True, text=True, env=env, check=True)
    assert json.loads(result.stdout)["status"] == "correlated"


def test_inputs_cannot_live_inside_rollback_storage(records):
    context = SimpleNamespace(store=SimpleNamespace(root=records[1].parent))
    with pytest.raises(WorkspaceError, match="outside"):
        TraceCorrelationFeature(context=context, config={"trace_root": records[0],
                                                         "audit_root": records[1]})
