"""Restore invariants, bounded history litmus tests and fail-closed lifecycle."""
import hashlib
import os
import shutil
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from itertools import combinations, product

import pytest
from allox_recovery_checks.assertions import compare, tree_manifest
from allox_recovery_checks.history import check_history
from allox_recovery_checks.plugin import RecoveryChecksFeature

from allox.workspace.daemon import WorkspaceHTTPServer, WorkspaceService
from allox.workspace.session_memory import empty_state
from allox.workspace.store import WorkspaceError, WorkspaceStore


class Backend:
    def assert_root(self, root):
        assert root.is_dir()

    def create_subvolume(self, path):
        path.mkdir()

    def snapshot(self, source, target, *, readonly):
        shutil.copytree(source, target, symlinks=True)

    def delete_subvolume(self, path):
        shutil.rmtree(path)


def make_service(tmp_path):
    (tmp_path / "store").mkdir(exist_ok=True)
    store = WorkspaceStore(tmp_path / "store", Backend())
    store.initialize()
    return WorkspaceService(
        store, session_memory=True, recovery_checks_config={"root": tmp_path / "evidence"},
        feature_factories={"recovery-checks": RecoveryChecksFeature},
    )


@pytest.fixture
def service(tmp_path):
    value = make_service(tmp_path)
    yield value
    value.close()


IDS = {"agent_id": "a", "session_id": "s"}


def rpc(service, action, **params):
    return service.dispatch(action, {**IDS, **params})


def baseline(service, *, coverage=True):
    rpc(service, "session.create")
    if coverage:
        rpc(service, "recovery.coverage", adapter_id="test-adapter", complete=True)
    work = service.store.current("a", "s")
    (work / "f").write_text("before")
    state = empty_state()
    state["summary"] = "before"
    rpc(service, "checkpoint.create", checkpoint_id="c0", memory_state=state,
        expected_memory_revision=0)
    (work / "f").write_text("after")
    return work


def test_joint_restore_and_repeat(service):
    work = baseline(service)
    for i in range(3):
        (work / "f").write_text("branch")
        state = empty_state()
        state["summary"] = "branch"
        rpc(service, "memory.write", state=state, expected_revision=1 + i * 2)
        result = rpc(service, "session.rollback", checkpoint_id="c0")
        assert result["recovery_checks"]["status"] == "CONSISTENT_WITHIN_SCOPE"
        assert result["recovery_checks"]["checks"]["runtime_reset"]["status"] == "NOT_CHECKED"
        assert (work / "f").read_text() == "before"
        assert rpc(service, "memory.read")["state"]["summary"] == "before"
        assert rpc(service, "recovery.status")["fence"] is None


def test_other_session_and_other_agent_unchanged(service):
    baseline(service)
    for a, s in [("a", "other"), ("b", "s")]:
        service.dispatch("session.create", {"agent_id": a, "session_id": s})
        (service.store.current(a, s) / "f").write_text(a + s)
    rpc(service, "session.rollback", checkpoint_id="c0")
    for a, s in [("a", "other"), ("b", "s")]:
        assert (service.store.current(a, s) / "f").read_text() == a + s


def test_unknown_coverage_does_not_guess_success(service):
    work = baseline(service, coverage=False)
    with pytest.raises(WorkspaceError, match="coverage absent"):
        rpc(service, "session.rollback", checkpoint_id="c0")
    assert (work / "f").read_text() == "after"
    assert rpc(service, "recovery.status")["fence"] is None


def prepare(service):
    return rpc(service, "recovery.effect.prepare", operation_id="payment-1",
               fingerprint=hashlib.sha256(b"test").hexdigest())


def test_external_effect_survives_and_blocks_unsafe_rollback(service):
    work = baseline(service)
    prepare(service)
    with pytest.raises(WorkspaceError, match="uncertain"):
        rpc(service, "session.rollback", checkpoint_id="c0")
    rpc(service, "recovery.effect.settle", operation_id="payment-1", outcome="committed")
    with pytest.raises(WorkspaceError, match="committed external"):
        rpc(service, "session.rollback", checkpoint_id="c0")
    assert (work / "f").read_text() == "after"
    assert prepare(service)["dispatch_new"] is False
    with pytest.raises(WorkspaceError, match="different effect"):
        rpc(service, "recovery.effect.prepare", operation_id="payment-1", fingerprint="a" * 64)


def test_confirmed_abort_can_rollback(service):
    baseline(service)
    prepare(service)
    rpc(service, "recovery.effect.settle", operation_id="payment-1", outcome="aborted")
    assert rpc(service, "session.rollback", checkpoint_id="c0")["recovery_checks"]["status"] == "CONSISTENT_WITHIN_SCOPE"
    assert prepare(service)["dispatch_new"] is False


def test_checkpoint_refuses_pending_operation(service):
    baseline(service)
    prepare(service)
    with pytest.raises(WorkspaceError, match="unresolved"):
        rpc(service, "checkpoint.create", checkpoint_id="uncertain")
    assert "uncertain" not in service.store.list_checkpoints("a", "s")


def test_committed_operation_before_checkpoint_is_preserved(service):
    baseline(service)
    prepare(service)
    rpc(service, "recovery.effect.settle", operation_id="payment-1", outcome="committed")
    rpc(service, "checkpoint.create", checkpoint_id="after-payment")
    assert rpc(service, "session.rollback", checkpoint_id="after-payment")["recovery_checks"]["status"] == "CONSISTENT_WITHIN_SCOPE"


def test_corrupt_restore_is_detected_and_fenced(service, monkeypatch):
    work = baseline(service)
    original = service.store.rollback

    def corrupt(*args, **kwargs):
        result = original(*args, **kwargs)
        (work / "f").write_text("wrong")
        return result

    monkeypatch.setattr(service.store, "rollback", corrupt)
    with pytest.raises(WorkspaceError, match="verification failed"):
        rpc(service, "session.rollback", checkpoint_id="c0")
    report = rpc(service, "recovery.status")["report"]
    assert report["status"] == "INCONSISTENT_WITHIN_SCOPE"
    assert report["checks"]["filesystem"]["changed_paths"] == ["f"]
    with pytest.raises(WorkspaceError, match="fenced"):
        rpc(service, "execution.acquire")
    assert rpc(service, "recovery.verify")["status"] == "INCONSISTENT_WITHIN_SCOPE"
    (work / "f").write_text("before")
    assert rpc(service, "recovery.verify")["status"] == "CONSISTENT_WITHIN_SCOPE"
    token = rpc(service, "execution.acquire")["lease_token"]
    rpc(service, "execution.release", lease_token=token)


def test_crash_fence_persists_across_service_restart(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    baseline(service)
    original = service.store.rollback

    def die(*args, **kwargs):
        raise RuntimeError("injected daemon interruption")

    monkeypatch.setattr(service.store, "rollback", die)
    with pytest.raises(RuntimeError, match="interruption"):
        rpc(service, "session.rollback", checkpoint_id="c0")
    monkeypatch.setattr(service.store, "rollback", original)
    service.close()
    fresh = make_service(tmp_path)
    try:
        with pytest.raises(WorkspaceError, match="fenced"):
            rpc(fresh, "execution.acquire")
        assert rpc(fresh, "session.rollback", checkpoint_id="c0")["recovery_checks"]["status"] == "CONSISTENT_WITHIN_SCOPE"
    finally:
        fresh.close()


def test_no_new_lease_during_verification_without_deadlock(service, monkeypatch):
    baseline(service)
    started, release = threading.Event(), threading.Event()
    plugin = service.features._features[0]
    verify = plugin._verify

    def pause(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return verify(*args, **kwargs)

    monkeypatch.setattr(plugin, "_verify", pause)
    with ThreadPoolExecutor(2) as pool:
        future = pool.submit(rpc, service, "session.rollback", checkpoint_id="c0")
        assert started.wait(3)
        try:
            with pytest.raises(WorkspaceError, match="fenced"):
                pool.submit(rpc, service, "execution.acquire").result(timeout=1)
        finally:
            release.set()
        assert future.result(timeout=3)["recovery_checks"]["status"] == "CONSISTENT_WITHIN_SCOPE"


@pytest.mark.parametrize("params", [{"restore_memory": False}, {"num_ancestors": 1},
                                   {"checkpoint_id": "c"}])
def test_no_partial_scope_or_prefix_bypass(service, params):
    baseline(service)
    with pytest.raises(WorkspaceError):
        rpc(service, "session.rollback", **{"checkpoint_id": "c0", **params})


def test_memory_only_and_unauthenticated_management_rejected(service):
    baseline(service)
    with pytest.raises(WorkspaceError, match="joint"):
        rpc(service, "memory.restore", checkpoint_id="c0")
    with pytest.raises(WorkspaceError, match="authenticated"):
        WorkspaceHTTPServer(("127.0.0.1", 0), service, "")


def test_snapshot_tampering_rejected_before_restore(service):
    work = baseline(service)
    (service.store.checkpoints("a", "s") / "c0" / "f").write_text("tampered")
    with pytest.raises(WorkspaceError, match="no longer matches"):
        rpc(service, "session.rollback", checkpoint_id="c0")
    assert (work / "f").read_text() == "after"


def test_no_follow_and_hardlink_mode_content_assertions(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "outside").symlink_to("/etc", target_is_directory=True)
    (root / "a").write_text("data")
    os.link(root / "a", root / "b")
    initial = tree_manifest(root)
    assert initial["outside"]["type"] == "symlink"
    assert initial["b"]["link_group"] == "a"
    (root / "b").unlink()
    (root / "b").write_text("data")
    assert compare(initial, tree_manifest(root))["changed_paths"] == ["b"]
    (root / "a").chmod(0o700)
    assert "a" in compare(initial, tree_manifest(root))["changed_paths"]


def test_special_nodes_are_not_opened_and_scrub_is_explicit(tmp_path):
    (tmp_path / ".allox-tmp").mkdir()
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(tmp_path / ".allox-tmp" / "s"))
        os.mkfifo(tmp_path / ".allox-tmp" / "pipe")
        (tmp_path / ".allox-tmp" / "normal").write_text("tmp data")
        all_nodes = tree_manifest(tmp_path)
        scrubbed = tree_manifest(tmp_path, scrub_runtime=True)
        assert all_nodes[".allox-tmp/s"]["type"] == "socket-node"
        assert ".allox-tmp/s" not in scrubbed and ".allox-tmp/pipe" not in scrubbed
        assert ".allox-tmp/normal" in scrubbed


def test_manifest_bound_fails_instead_of_truncating(tmp_path, monkeypatch):
    from allox_recovery_checks import assertions
    (tmp_path / "large").write_bytes(b"abc")
    monkeypatch.setattr(assertions, "MAX_BYTES", 2)
    with pytest.raises(WorkspaceError, match="byte limit"):
        tree_manifest(tmp_path)


def test_history_shared_prefix_regression():
    result = check_history([
        [["x", "y"], ["y", "x"]], [["x", "z"], ["z", "x"]],
    ], [["y", "x"], ["x", "z"]])
    assert not result["admitted"]
    assert result["descending_chain"] == [2, 1, 0]
    assert result["authorizes_effects"] is False


def test_history_keeps_outcome_identity():
    result = check_history([[["x"]], [["x"]]], [["x"]])
    assert result["admitted"]
    assert {x["outcome"] for x in result["survivors"]} == {0, 1}


def test_history_requires_every_outcome_and_has_bounds():
    assert not check_history([[["a"]], [["b"]]], [["a"]])["admitted"]
    with pytest.raises(WorkspaceError):
        check_history([[["a"]], [["a", "b"]]], [["a"]])
    with pytest.raises(WorkspaceError):
        check_history([[["a"]]] * 17, [["a"]])


def test_plugin_disabled_has_no_wrapper(service, tmp_path):
    plain = WorkspaceService(service.store)
    assert "recovery-checks" not in plain.feature_names
    plain.close()


def test_root_inside_workspace_rejected(tmp_path):
    store = WorkspaceStore(tmp_path, Backend())
    store.initialize()
    with pytest.raises(WorkspaceError, match="outside"):
        WorkspaceService(store, recovery_checks_config={"root": tmp_path / "bad"},
                         feature_factories={"recovery-checks": RecoveryChecksFeature})


def test_memory_divergence_is_not_hidden_by_correct_files(service, monkeypatch):
    work = baseline(service)
    original = service.store.rollback

    def corrupt(*args, **kwargs):
        result = original(*args, **kwargs)
        state = empty_state()
        state["summary"] = "wrong-memory"
        view = service.store.memory_read("a", "s")
        service.store.memory_write("a", "s", state, expected_revision=view["memory_revision"])
        return result

    monkeypatch.setattr(service.store, "rollback", corrupt)
    with pytest.raises(WorkspaceError, match="verification failed"):
        rpc(service, "session.rollback", checkpoint_id="c0")
    assert (work / "f").read_text() == "before"
    checks = rpc(service, "recovery.status")["report"]["checks"]
    assert checks["filesystem"]["status"] == "CONSISTENT_WITHIN_SCOPE"
    assert checks["memory"]["status"] == "INCONSISTENT_WITHIN_SCOPE"


def test_equal_bytes_do_not_hide_wrong_checkpoint_head(service, monkeypatch):
    baseline(service)
    describe = service.store.describe

    def wrong_head(*args):
        return {**describe(*args), "checkpoint_head": "different-checkpoint"}

    monkeypatch.setattr(service.store, "describe", wrong_head)
    with pytest.raises(WorkspaceError, match="verification failed"):
        rpc(service, "session.rollback", checkpoint_id="c0")
    checks = rpc(service, "recovery.status")["report"]["checks"]
    assert checks["filesystem"]["status"] == "CONSISTENT_WITHIN_SCOPE"
    assert checks["memory"]["status"] == "CONSISTENT_WITHIN_SCOPE"
    assert checks["checkpoint_binding"]["status"] == "INCONSISTENT_WITHIN_SCOPE"


def test_evidence_cannot_be_rebound_to_another_store(service, tmp_path):
    other = tmp_path / "different-store"
    other.mkdir()
    store = WorkspaceStore(other, Backend())
    store.initialize()
    with pytest.raises(WorkspaceError, match="different workspace"):
        WorkspaceService(store, recovery_checks_config={"root": tmp_path / "evidence"},
                         feature_factories={"recovery-checks": RecoveryChecksFeature})


def test_finite_checker_matches_exhaustive_subset_oracle():
    # Independent brute-force oracle: enumerate every admitted subfamily,
    # rather than repeating the descending fixed-point algorithm.
    words = [("a", "b"), ("b", "a")]
    families = [[words[0]], [words[1]], words]
    for outcomes in product(families, repeat=2):
        for mask in range(4):
            policy = {words[i] for i in range(2) if mask & (1 << i)}
            candidates = [(i, t) for i, f in enumerate(outcomes) for t in f if t in policy]
            greatest = set()
            for n in range(len(candidates) + 1):
                for subset in combinations(candidates, n):
                    valid = True
                    for _, trace in subset:
                        for length in range(len(trace) + 1):
                            prefix = trace[:length]
                            for i, family in enumerate(outcomes):
                                if any(t[:length] == prefix for t in family):
                                    valid &= any(j == i and t[:length] == prefix for j, t in subset)
                    if valid:
                        greatest.update(subset)
            result = check_history([[list(t) for t in f] for f in outcomes],
                                   [list(t) for t in policy])
            assert greatest == {(x["outcome"], tuple(x["trace"])) for x in result["survivors"]}
