"""Fail-closed cgroup identity, leaf-sealing, and quiescence tests."""

from pathlib import Path

import pytest

import allox.runtime.process_tracking.cgroup as cgroup_module
from allox.runtime.process_tracking.cgroup import SessionCgroup
from allox.workspace.store import WorkspaceError


def _session_cgroup(tmp_path: Path) -> SessionCgroup:
    instance = object.__new__(SessionCgroup)
    instance.mount = tmp_path.resolve()
    instance.root = (tmp_path / "allox").resolve()
    instance.root.mkdir()
    return instance


def test_attach_thaws_and_seals_leaf_before_writing_pid(tmp_path, monkeypatch):
    cgroup = _session_cgroup(tmp_path)
    calls = []
    monkeypatch.setattr(cgroup_module, "_set_frozen", lambda path, frozen: calls.append((path, frozen)))
    monkeypatch.setattr(cgroup, "populated", lambda agent_id, session_id: False)

    target = cgroup.attach("agent", "session", 123)

    assert calls == [(target, False)]
    assert (target / "cgroup.max.depth").read_text() == "0"
    assert (target / "cgroup.max.descendants").read_text() == "0"
    assert (target / "cgroup.procs").read_text() == "123"


def test_events_parser_rejects_missing_or_invalid_kernel_state(tmp_path):
    (tmp_path / "cgroup.events").write_text("populated 0\nfrozen 1\n")
    assert cgroup_module._events(tmp_path) == {"populated": "0", "frozen": "1"}

    (tmp_path / "cgroup.events").write_text("populated 2\nfrozen 1\n")
    with pytest.raises(WorkspaceError, match="invalid Session cgroup"):
        cgroup_module._events(tmp_path)


def test_identity_freeze_thaw_and_quiescence_bind_exact_cgroup(tmp_path, monkeypatch):
    cgroup = _session_cgroup(tmp_path)
    target = cgroup.path("agent", "session")
    target.mkdir(parents=True)
    identity = cgroup.identity("agent", "session")
    assert identity["path"] == str(target)
    assert identity["relative_path"] == "/allox/agents/agent/sessions/session"
    assert identity["id"] == target.stat().st_ino

    kernel_state = {"populated": "1", "frozen": "0"}
    transitions = []

    def set_frozen(path, frozen, timeout=5.0):
        assert path == target
        transitions.append((frozen, timeout))
        kernel_state["frozen"] = "1" if frozen else "0"

    monkeypatch.setattr(cgroup_module, "_set_frozen", set_frozen)
    monkeypatch.setattr(cgroup_module, "_events", lambda path: dict(kernel_state))

    assert cgroup.freeze(
        "agent",
        "session",
        expected_cgroup_id=identity["id"],
        require_populated=True,
        timeout=0.25,
    ) == identity
    cgroup.thaw(
        "agent", "session", expected_cgroup_id=identity["id"], timeout=0.5
    )
    assert transitions == [(True, 0.25), (False, 0.5)]

    kernel_state["populated"] = "0"
    monkeypatch.setattr(cgroup, "wait_empty", lambda agent_id, session_id, timeout: True)
    assert cgroup.quiesce_empty(
        "agent", "session", expected_cgroup_id=identity["id"], timeout=0.75
    ) == identity
    assert transitions[-1] == (True, 0.75)


def test_freeze_rejects_identity_mismatch_and_quiesce_rejects_nonempty_tree(
    tmp_path, monkeypatch
):
    cgroup = _session_cgroup(tmp_path)
    target = cgroup.path("agent", "session")
    target.mkdir(parents=True)
    identity = cgroup.identity("agent", "session")

    with pytest.raises(WorkspaceError, match="identity changed before freeze"):
        cgroup.freeze(
            "agent",
            "session",
            expected_cgroup_id=identity["id"] + 1,
            require_populated=False,
        )

    monkeypatch.setattr(cgroup, "wait_empty", lambda agent_id, session_id, timeout: False)
    with pytest.raises(WorkspaceError, match="did not become empty"):
        cgroup.quiesce_empty(
            "agent", "session", expected_cgroup_id=identity["id"], timeout=0
        )


def test_remove_returns_explicit_completion_status(tmp_path, monkeypatch):
    cgroup = _session_cgroup(tmp_path)
    target = cgroup.path("agent", "session")
    target.mkdir(parents=True)
    monkeypatch.setattr(cgroup_module, "_set_frozen", lambda path, frozen: None)
    monkeypatch.setattr(cgroup, "populated", lambda agent_id, session_id: True)

    assert cgroup.remove("agent", "session") is False
    assert target.exists()

    monkeypatch.setattr(cgroup, "populated", lambda agent_id, session_id: False)
    assert cgroup.remove("agent", "session") is True
    assert not target.exists()
    assert cgroup.remove("agent", "session") is True
