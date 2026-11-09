"""Session state semantics and crash recovery; optional real Btrfs verification."""

import json
import os
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from test_workspace_store import DirectorySnapshotBackend

from allox.workspace.daemon import WorkspaceHTTPServer, WorkspaceService
from allox.workspace.lifecycle import TurnCheckpointLifecycle
from allox.workspace.memory_client import SessionMemory
from allox.workspace.session_memory import empty_state
from allox.workspace.store import WorkspaceError, WorkspaceStore


class Client:
    def __init__(self, service):
        self.service = service

    def rpc(self, action, **params):
        return self.service.dispatch(action, params)


def logical_state(marker):
    return {
        "messages": [{"role": "user", "content": marker}],
        "summary": marker,
        "task_state": {"step": marker},
        "tool_results": [{"output": marker}],
    }


@pytest.fixture
def workspace(tmp_path):
    store = WorkspaceStore(tmp_path, DirectorySnapshotBackend())
    store.initialize()
    return store


def setup_branch(store):
    service = WorkspaceService(store, session_memory=True)
    client = Client(service)
    ids = {"agent_id": "agent", "session_id": "session"}
    client.rpc("session.create", **ids)
    current = store.current(*ids.values())
    memory = SessionMemory(client, *ids.values())
    old = logical_state("BEFORE_CHECKPOINT")
    new = logical_state("DISCARDED_BRANCH_ONLY")
    (current / "work.txt").write_text(old["summary"], encoding="utf-8")
    assert client.rpc("memory.read", **ids)["state"] == empty_state()
    first = memory.save(old, expected_revision=0)
    checkpoint = client.rpc("checkpoint.create", **ids, checkpoint_id="good")
    assert checkpoint["memory_version"] == first["memory_version"]
    (current / "work.txt").write_text(new["summary"], encoding="utf-8")
    latest = memory.checkpoint(new, expected_revision=1, checkpoint_id="future")
    return client, ids, memory, current, first, latest


def verify_model_request(store):
    client, ids, memory, current, first, latest = setup_branch(store)
    requests = []

    def invoke_model():
        with memory.for_turn() as view:
            # The instrumented model boundary sees what the adapter actually submits,
            # not the contents of an audit trace or a state file opened out of band.
            requests.append(json.loads(json.dumps(view["state"])))
            with pytest.raises(WorkspaceError, match="active executions"):
                client.rpc("session.rollback", **ids, checkpoint_id="good")

    invoke_model()
    rollback = client.rpc("session.rollback", **ids, checkpoint_id="good")
    assert rollback["memory_restored"] is True
    assert rollback["memory_version"] == first["memory_version"]
    assert rollback["memory_revision"] == 3
    invoke_model()
    assert requests == [logical_state("DISCARDED_BRANCH_ONLY"), logical_state("BEFORE_CHECKPOINT")]
    assert (current / "work.txt").read_text(encoding="utf-8") == "BEFORE_CHECKPOINT"
    # Snapshots and branch history remain auditable, but are not prompt inputs.
    assert "future" in store.list_checkpoints(*ids.values())
    assert store._read_memory_version(*ids.values(), latest["memory_version"]) == requests[0]
    with pytest.raises(WorkspaceError, match="revision conflict"):
        memory.save(requests[0], expected_revision=2)
    audit = store.root / ".allox" / "events" / "agent" / "session.jsonl"
    assert "DISCARDED_BRANCH_ONLY" not in audit.read_text(encoding="utf-8")


def test_joint_rollback_updates_actual_model_input(workspace):
    verify_model_request(workspace)


def test_authenticated_management_http(workspace):
    class HTTPClient:
        def rpc(self, action, **params):
            request = Request(
                url, data=json.dumps({"action": action, "params": params}).encode(),
                headers={"Authorization": "Bearer test-only-token"},
            )
            try:
                with urlopen(request, timeout=3) as response:
                    return json.load(response)["result"]
            except HTTPError as exc:
                with exc:
                    raise WorkspaceError(json.load(exc)["error"]) from exc

    with WorkspaceHTTPServer(("127.0.0.1", 0), WorkspaceService(workspace, session_memory=True),
                             "test-only-token") as server:
        url = f"http://127.0.0.1:{server.server_port}/v1/rpc"
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with pytest.raises(HTTPError) as unauthenticated:
                urlopen(Request(url, data=b'{"action":"memory.read"}'), timeout=3)
            with unauthenticated.value as exc:
                assert exc.code == 401
            client = HTTPClient()
            ids = {"agent_id": "a", "session_id": "s"}
            client.rpc("session.create", **ids)
            memory = SessionMemory(client, *ids.values())
            memory.checkpoint(logical_state("before"), expected_revision=0, checkpoint_id="good")
            memory.save(logical_state("after"), expected_revision=1)
            client.rpc("session.rollback", **ids, checkpoint_id="good")
            with memory.for_turn() as view:
                assert view["state"] == logical_state("before")
        finally:
            server.shutdown()
            worker.join(timeout=3)


def test_lifecycle_pairs_turn_checkpoint_and_memory(workspace):
    client = Client(WorkspaceService(workspace, session_memory=True))
    ids = {"agent_id": "a", "session_id": "s"}
    client.rpc("session.create", **ids)
    checkpoint_ids = iter(("baseline", "turn-1", "stale"))
    lifecycle = TurnCheckpointLifecycle(client, *ids.values(), enabled=True,
                                        id_factory=lambda: next(checkpoint_ids))
    baseline = lifecycle.on_session_start(memory_update=(empty_state(), 0))
    assert baseline["created"] and baseline["checkpoint"]["memory_revision"] == 1
    turn = lifecycle.on_turn_end(turn_id="1", memory_update=(logical_state("first turn"), 1))
    assert turn["created"] and turn["checkpoint"]["memory_revision"] == 2
    stale = lifecycle.on_turn_end(turn_id="2", memory_update=(logical_state("stale turn"), 1))
    assert not stale["created"] and "revision conflict" in stale["error"]
    assert workspace.list_checkpoints(*ids.values()) == ["baseline", "turn-1"]
    client.rpc("session.rollback", **ids, checkpoint_id="baseline")
    assert client.rpc("memory.read", **ids)["state"] == empty_state()


def test_failed_checkpoint_does_not_publish_staged_memory(workspace, monkeypatch):
    _, ids, memory, _, _, latest = setup_branch(workspace)

    def fail_snapshot(*args, **kwargs):
        raise OSError("injected snapshot error")

    monkeypatch.setattr(workspace.backend, "snapshot", fail_snapshot)
    with pytest.raises(OSError, match="injected"):
        memory.checkpoint(logical_state("uncommitted"), expected_revision=2, checkpoint_id="failed")
    with memory.for_turn() as view:
        assert view["memory_version"] == latest["memory_version"]
        assert view["memory_revision"] == 2
    assert "failed" not in workspace.list_checkpoints(*ids.values())


@pytest.mark.skipif(not os.environ.get("ALLOX_MEMORY_BTRFS_TEST_ROOT"), reason="requires dedicated Btrfs mount")
def test_joint_rollback_on_real_btrfs():
    store = WorkspaceStore(Path(os.environ["ALLOX_MEMORY_BTRFS_TEST_ROOT"]))
    store.initialize()
    verify_model_request(store)


def test_explicit_memory_and_files_only_modes(workspace):
    client, ids, memory, current, first, latest = setup_branch(workspace)
    result = client.rpc("memory.restore", **ids, checkpoint_id="good", expected_revision=2)
    assert result["scope"] == "memory-only" and result["memory_version"] == first["memory_version"]
    assert (current / "work.txt").read_text() == "DISCARDED_BRANCH_ONLY"
    with memory.for_turn() as view:
        assert view["state"] == logical_state("BEFORE_CHECKPOINT")
    (current / "work.txt").write_text("UNSAVED_WORKSPACE_CHANGE")
    result = client.rpc("session.rollback", **ids, checkpoint_id="future", restore_memory=False)
    assert result["memory_restored"] is False and result["memory_revision"] == 4
    assert result["memory_version"] == first["memory_version"]
    assert (current / "work.txt").read_text() == "DISCARDED_BRANCH_ONLY"
    assert latest["memory_version"] != result["memory_version"]


def test_legacy_metadata_cannot_forge_a_memory_binding(workspace):
    client, ids, _, _, first, _ = setup_branch(workspace)
    workspace.create_session("agent", "other")
    other = {**ids, "session_id": "other"}
    client.rpc("checkpoint.create", **other, checkpoint_id="legacy",
               metadata={"memory_version": first["memory_version"]})
    (workspace.current("agent", "other") / "other.txt").write_text("unchanged")
    client.rpc("memory.write", **other, state=logical_state("other"), expected_revision=0)
    with pytest.raises(WorkspaceError, match="no bound memory"):
        client.rpc("session.rollback", **other, checkpoint_id="legacy")
    with pytest.raises(WorkspaceError, match="integrity check"):
        workspace._read_memory_version("agent", "other", first["memory_version"])
    assert client.rpc("memory.read", **other)["state"] == logical_state("other")
    assert (workspace.current("agent", "other") / "other.txt").exists()
    client.rpc("session.rollback", **other, checkpoint_id="legacy", restore_memory=False)
    assert not (workspace.current("agent", "other") / "other.txt").exists()
    assert client.rpc("memory.read", **other)["memory_revision"] == 2


def test_corrupt_target_is_rejected_before_swap(workspace):
    client, ids, _, current, first, latest = setup_branch(workspace)
    blob = workspace.root / ".allox" / "memory" / (first["memory_version"] + ".json")
    payload = json.loads(blob.read_text())
    payload["state"]["summary"] = "tampered"
    blob.write_text(json.dumps(payload))
    with pytest.raises(WorkspaceError, match="integrity check"):
        client.rpc("session.rollback", **ids, checkpoint_id="good")
    assert (current / "work.txt").read_text() == "DISCARDED_BRANCH_ONLY"
    assert client.rpc("memory.read", **ids)["memory_version"] == latest["memory_version"]
    assert workspace._load_transaction(*ids.values()) is None


@pytest.mark.parametrize("phase", ["prepared", "old_moved", "index", "committed", "cleanup"])
def test_joint_recovery_fences_execution_and_is_idempotent(workspace, monkeypatch, phase):
    client, ids, _, current, first, latest = setup_branch(workspace)
    original_tx = workspace._write_transaction
    original_index = workspace._save_index
    original_delete = workspace.backend.delete_subvolume

    def write_tx(agent, session, transaction):
        if transaction["phase"] == phase:
            raise OSError("injected transaction interruption")
        original_tx(agent, session, transaction)

    def save_index(agent, session, index):
        if phase == "index" and index.memory_revision == 3:
            raise OSError("injected before atomic index publication")
        original_index(agent, session, index)

    def delete(path):
        if phase == "cleanup" and path.name.startswith(".discard-"):
            raise OSError("injected cleanup failure")
        original_delete(path)

    with monkeypatch.context() as patch:
        patch.setattr(workspace, "_write_transaction", write_tx)
        patch.setattr(workspace, "_save_index", save_index)
        patch.setattr(workspace.backend, "delete_subvolume", delete)
        with pytest.raises(OSError, match="injected"):
            client.rpc("session.rollback", **ids, checkpoint_id="good")
        with pytest.raises(WorkspaceError, match="pending memory rollback"):
            client.rpc("execution.acquire", **ids)
        with pytest.raises(WorkspaceError, match="pending memory rollback"):
            client.rpc("memory.read", **ids)

    restarted = WorkspaceStore(workspace.root, workspace.backend)
    service = WorkspaceService(restarted, session_memory=True)
    with pytest.raises(WorkspaceError, match="pending memory rollback"):
        service.dispatch("execution.acquire", ids)
    restarted.initialize()
    expected = latest if phase == "prepared" else first
    view = service.dispatch("memory.read", ids)
    assert view["memory_version"] == expected["memory_version"]
    assert view["memory_revision"] == (2 if phase == "prepared" else 3)
    assert (current / "work.txt").read_text() == view["state"]["summary"]
    restarted.initialize()
    assert service.dispatch("memory.read", ids) == view


def test_deferred_cleanup_keeps_memory_fenced(workspace):
    client, ids, _, _, first, _ = setup_branch(workspace)
    result = workspace.rollback(*ids.values(), "good", recovery_id="audit", defer_cleanup=True)
    assert result["cleanup_pending"] and result["memory_version"] == first["memory_version"]
    workspace.initialize()
    with pytest.raises(WorkspaceError, match="pending memory rollback"):
        client.rpc("execution.acquire", **ids)
    workspace.complete_deferred_rollback(*ids.values(), "audit")
    assert client.rpc("memory.read", **ids)["memory_revision"] == 3


@pytest.mark.parametrize("state", [None, {"messages": []}, logical_state("x"*524288),
                                    {**empty_state(), "task_state": {"invalid": float("nan")}}])
def test_schema_and_size_fail_without_writing(workspace, state):
    workspace.create_session("a", "s")
    with pytest.raises(WorkspaceError, match="memory"):
        workspace.memory_write("a", "s", state, expected_revision=0)
    assert workspace.memory_read("a", "s")["memory_revision"] == 0


def test_memory_is_opt_in_and_requires_management_auth(workspace):
    with pytest.raises(WorkspaceError, match="not enabled"):
        WorkspaceService(workspace).dispatch("memory.read", {"agent_id": "a", "session_id": "s"})
    with pytest.raises(WorkspaceError, match="authenticated management"):
        WorkspaceHTTPServer(("127.0.0.1", 0), WorkspaceService(workspace, session_memory=True), "")
