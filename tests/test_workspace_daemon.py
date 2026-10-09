"""Workspace daemon coordination tests."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from allox.runtime.extensions import ExecutionContribution
from allox.workspace.daemon import (
    ExecutionRegistry,
    WorkspaceService,
    _system_effects_config_from_args,
    build_parser,
)
from allox.workspace.store import WorkspaceError


class FakeProcessService:
    def __init__(self):
        self.fences = []

    @contextmanager
    def session_launch_fence(self, agent_id, session_id, *, terminate, reason):
        self.fences.append((agent_id, session_id, terminate, reason))
        yield ["run-1"]

    def close(self):
        pass


def test_share_tools_require_management_authentication(monkeypatch):
    from allox.workspace.daemon import main

    monkeypatch.delenv("ALLOX_WORKSPACE_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="ALLOX_WORKSPACE_TOKEN"):
        main(["--root", "/unused", "--share-tools", "--process-tracking", "ebpf"])


def test_mutation_rejects_active_session_execution():
    registry = ExecutionRegistry()
    lease = registry.acquire("agent-a", "session-1")

    with (
        pytest.raises(WorkspaceError, match="active executions"),
        registry.mutation("agent-a", "session-1"),
    ):
        pass

    assert registry.release(lease["lease_token"])["released"] is True
    with registry.mutation("agent-a", "session-1"):
        pass


def test_mutation_of_one_session_does_not_block_another():
    registry = ExecutionRegistry()
    registry.acquire("agent-a", "session-1")

    with registry.mutation("agent-a", "session-2"):
        pass


def test_same_session_allows_only_one_active_execution():
    registry = ExecutionRegistry()
    registry.acquire("agent-a", "session-1")

    with pytest.raises(WorkspaceError, match="already has an active execution"):
        registry.acquire("agent-a", "session-1")


def test_ordinary_execution_lease_response_remains_compatible():
    registry = ExecutionRegistry()

    lease = registry.acquire("agent-a", "session-1")

    assert set(lease) == {"lease_token"}
    assert isinstance(lease["lease_token"], str)
    assert lease["lease_token"]


def test_recovery_execution_rejects_active_share_reader_and_allows_it_afterward():
    registry = ExecutionRegistry()

    with (
        registry.share_access("agent-a", "session-1", write=False),
        pytest.raises(WorkspaceError, match="active share reads"),
    ):
        registry.acquire("agent-a", "session-1", recovery=True)

    lease = registry.acquire("agent-a", "session-1", recovery=True)
    assert lease["recovery_reserved"] is True
    assert registry.release(lease["lease_token"]) == {"released": True}

    with registry.share_access("agent-a", "session-1", write=False):
        pass


def test_recovery_execution_rejects_persistent_background_execution():
    registry = ExecutionRegistry()
    registry.register_background("agent-a", "session-1", "sandbox-1", "execution-1")

    with pytest.raises(WorkspaceError, match="persistent runtime executions"):
        registry.acquire("agent-a", "session-1", recovery=True)


def test_recovery_reservation_blocks_new_share_and_background_until_release():
    registry = ExecutionRegistry()
    lease = registry.acquire("agent-a", "session-1", recovery=True)

    with (
        pytest.raises(WorkspaceError, match="active recovery execution reservation"),
        registry.share_access("agent-a", "session-1", write=False),
    ):
        pass
    with pytest.raises(WorkspaceError, match="active recovery execution reservation"):
        registry.register_background(
            "agent-a", "session-1", "sandbox-1", "execution-1"
        )

    assert registry.release(lease["lease_token"]) == {"released": True}
    with registry.share_access("agent-a", "session-1", write=False):
        pass
    assert registry.register_background(
        "agent-a", "session-1", "sandbox-1", "execution-1"
    ) == {"registered": True, "execution_id": "execution-1"}


def test_runtime_reset_fences_and_clears_persistent_session_executions():
    registry = ExecutionRegistry()
    registry.register_background("agent-a", "session-1", "sandbox-1", "execution-1")

    with (
        pytest.raises(WorkspaceError, match="persistent runtime"),
        registry.mutation("agent-a", "session-1"),
    ):
        pass

    reset = registry.begin_runtime_reset("agent-a", "session-1")
    assert reset["executions"] == [
        {"sandbox_id": "sandbox-1", "execution_id": "execution-1"}
    ]
    with registry.reset_mutation(reset["reset_token"], "agent-a", "session-1"):
        pass
    assert registry.complete_runtime_reset(reset["reset_token"], success=True)["completed"] is True
    with registry.mutation("agent-a", "session-1"):
        pass


def test_service_forwards_checkpoint_metadata_and_ancestor_rollback():
    store = MagicMock()
    store.create_checkpoint.return_value = {"checkpoint_id": "cp1"}
    store.rollback.return_value = {"checkpoint_id": "cp1"}
    service = WorkspaceService(store)

    service.dispatch(
        "checkpoint.create",
        {
            "agent_id": "agent-a",
            "session_id": "session-1",
            "checkpoint_id": "cp1",
            "message": "before retry",
            "pinned": True,
            "origin": "agent-loop",
            "metadata": {"event": "turn_end", "turn": 1},
        },
    )
    service.dispatch(
        "session.rollback",
        {
            "agent_id": "agent-a",
            "session_id": "session-1",
            "num_ancestors": 2,
        },
    )

    store.create_checkpoint.assert_called_once_with(
        "agent-a",
        "session-1",
        "cp1",
        origin="agent-loop",
        message="before retry",
        pinned=True,
        metadata={"event": "turn_end", "turn": 1},
    )
    store.rollback.assert_called_once_with(
        "agent-a",
        "session-1",
        None,
        num_ancestors=2,
        scrub_runtime=True,
        origin="allox-control-plane",
    )


@pytest.mark.parametrize(
    ("global_setting", "request_setting"),
    [(False, {"kill_processes": True}), (True, {})],
)
def test_rollback_can_request_tracked_session_termination(global_setting, request_setting):
    store = MagicMock()
    store.root = Path("/workspace-store")
    store.rollback.return_value = {"checkpoint_id": "cp1"}
    processes = FakeProcessService()
    service = WorkspaceService(
        store,
        process_tracking="fake",
        process_audit_root=Path("/audit"),
        process_service_factory=lambda *args, **kwargs: processes,
        kill_session_processes_on_rollback=global_setting,
    )

    result = service.dispatch(
        "session.rollback",
        {
            "agent_id": "agent-a",
            "session_id": "session-1",
            "checkpoint_id": "cp1",
            **request_setting,
        },
    )

    assert result["terminated_process_runs"] == ["run-1"]
    assert processes.fences == [("agent-a", "session-1", True, "rollback")]
    store.rollback.assert_called_once()


def test_rollback_kill_is_rejected_without_process_tracking():
    service = WorkspaceService(MagicMock())

    with pytest.raises(WorkspaceError, match="requires process tracking"):
        service.dispatch(
            "session.rollback",
            {
                "agent_id": "agent-a",
                "session_id": "session-1",
                "kill_processes": True,
            },
        )


def test_request_can_disable_global_rollback_termination():
    store = MagicMock()
    store.root = Path("/workspace-store")
    store.rollback.return_value = {"checkpoint_id": "cp1"}
    processes = FakeProcessService()
    service = WorkspaceService(
        store,
        process_tracking="fake",
        process_audit_root=Path("/audit"),
        process_service_factory=lambda *args, **kwargs: processes,
        kill_session_processes_on_rollback=True,
    )

    result = service.dispatch(
        "session.rollback",
        {
            "agent_id": "agent-a",
            "session_id": "session-1",
            "kill_processes": False,
        },
    )

    assert "terminated_process_runs" not in result
    assert processes.fences == []


def test_process_start_forwards_explicit_recovery_contract():
    service = WorkspaceService(MagicMock())
    processes = MagicMock()
    processes.start.return_value = {"run_id": "run-1"}
    service.processes = processes
    recovery = {
        "checkpoint_id": "clean",
        "recovery_id": "restore-1",
        "require_consistent": True,
    }

    result = service.dispatch(
        "process.start",
        {
            "agent_id": "agent-a",
            "session_id": "session-1",
            "argv": ["/bin/true"],
            "recovery": recovery,
        },
    )

    assert result == {"run_id": "run-1"}
    processes.start.assert_called_once_with(
        "agent-a",
        "session-1",
        ["/bin/true"],
        env=None,
        timeout=300,
        recovery=recovery,
    )


class FakeSystemEffectsFeature:
    name = "system-effects"
    abi = 1
    execution_lifecycle_abi = 1
    rpc_prefixes = ()

    def __init__(self, *, context, config):
        self.context = context
        self.config = config
        self.closed = False

    def prepare_execution(self, agent_id, session_id):
        del agent_id, session_id
        return ExecutionContribution()

    def start_execution(self, context):  # pragma: no cover - activation test only
        raise AssertionError(f"unexpected execution start: {context}")

    def dispatch(self, action, params):  # pragma: no cover - no RPC surface
        raise AssertionError((action, params))

    def close(self):
        self.closed = True


class FakeExecutionProcessService:
    def __init__(self):
        self.cgroups = object()
        self.features = None
        self.closed = False

    def close(self):
        self.closed = True


def test_system_effects_configuration_activates_feature_and_core_execution_service():
    store = MagicMock()
    store.root = Path("/workspace-store")
    processes = FakeExecutionProcessService()
    process_factory_calls = []
    created_features = []

    def process_factory(*args, **kwargs):
        process_factory_calls.append((args, kwargs))
        return processes

    def feature_factory(**kwargs):
        feature = FakeSystemEffectsFeature(**kwargs)
        created_features.append(feature)
        return feature

    config = {"observer_path": "/trusted/deployment-observe"}
    service = WorkspaceService(
        store,
        process_tracking="disabled",
        process_audit_root=Path("/audit"),
        system_effects_config=config,
        process_service_factory=process_factory,
        feature_factories={"system-effects": feature_factory},
    )

    assert service.feature_names == ("system-effects",)
    assert service.processes is processes
    assert processes.features is service.features
    assert process_factory_calls[0][1]["provider"] == "disabled"
    assert created_features[0].config == config
    assert created_features[0].context.cgroups is processes.cgroups

    service.close()
    assert processes.closed is True
    assert created_features[0].closed is True


def test_system_effects_rejects_a_separate_process_tracker():
    store = MagicMock()
    store.root = Path("/workspace-store")

    with pytest.raises(WorkspaceError, match="disable the separate process tracker"):
        WorkspaceService(
            store,
            process_tracking="ebpf",
            process_audit_root=Path("/audit"),
            system_effects_config={"observer_path": "/trusted/deployment-observe"},
        )


@pytest.mark.parametrize(
    "arguments",
    [
        ["--system-effects-observer", "/observer"],
        ["--system-effects-recorder-sha256", "a" * 64],
        ["--system-effects-filesystem-scope", "/scope.json"],
        ["--system-effects-verifier", "/verifier"],
        ["--system-effects-profile", "full"],
        ["--system-effects-ready-timeout", "10"],
        ["--system-effects-host-lock", "/run/custom.lock"],
        ["--system-effects-recovery-helper", "/recover"],
        ["--system-effects-recovery-helper-sha256", "c" * 64],
        ["--system-effects-recovery-verifier", "/recovery-verifier"],
    ],
)
def test_partial_system_effects_cli_configuration_is_rejected(arguments):
    args = build_parser().parse_args(["--root", "/store", *arguments])

    with pytest.raises(WorkspaceError, match="system-effects|incomplete system-effects"):
        _system_effects_config_from_args(args)


def test_complete_system_effects_cli_configuration_enables_plugin_config():
    args = build_parser().parse_args(
        [
            "--root",
            "/store",
            "--system-effects-observer",
            "/opt/allox/deployment-observe",
            "--system-effects-recorder-object",
            "/opt/allox/system_effects.bpf.o",
            "--system-effects-recorder-sha256",
            "a" * 64,
            "--system-effects-lifecycle-object",
            "/opt/allox/process_lifecycle.bpf.o",
            "--system-effects-lifecycle-sha256",
            "b" * 64,
        ]
    )

    config = _system_effects_config_from_args(args)
    assert config["observer_path"] == "/opt/allox/deployment-observe"
    assert config["verifier_path"] == "/opt/allox/deployment-observe-verify"
    assert config["object_resolution_mode"] == "KERNEL_OBJECT_EXPANDED_PARTIAL"
    assert config["recorder_object_sha256"] == "a" * 64
    assert config["lifecycle_object_sha256"] == "b" * 64
    assert config["host_lock_path"] == "/run/allox-system-effects.lock"
    assert config["recovery_helper_path"] is None
    assert config["recovery_verifier_path"] is None


def test_complete_system_effects_cli_enables_recovery_protocol():
    args = build_parser().parse_args(
        [
            "--root",
            "/store",
            "--system-effects-observer",
            "/opt/allox/deployment-observe",
            "--system-effects-recorder-object",
            "/opt/allox/system_effects.bpf.o",
            "--system-effects-recorder-sha256",
            "a" * 64,
            "--system-effects-lifecycle-object",
            "/opt/allox/process_lifecycle.bpf.o",
            "--system-effects-lifecycle-sha256",
            "b" * 64,
            "--system-effects-recovery-helper",
            "/opt/allox/allox-system-effects-recover",
            "--system-effects-recovery-helper-sha256",
            "c" * 64,
            "--system-effects-recovery-timeout-ms",
            "90000",
        ]
    )

    config = _system_effects_config_from_args(args)
    assert config["recovery_helper_path"] == "/opt/allox/allox-system-effects-recover"
    assert config["recovery_helper_sha256"] == "c" * 64
    assert (
        config["recovery_verifier_path"]
        == "/opt/allox/deployment-observe-recovery-verify"
    )
    assert config["recovery_timeout_ms"] == 90000


def test_complete_system_effects_cli_accepts_shared_host_lock():
    args = build_parser().parse_args(
        [
            "--root",
            "/store",
            "--system-effects-observer",
            "/opt/allox/deployment-observe",
            "--system-effects-recorder-object",
            "/opt/allox/system_effects.bpf.o",
            "--system-effects-recorder-sha256",
            "a" * 64,
            "--system-effects-lifecycle-object",
            "/opt/allox/process_lifecycle.bpf.o",
            "--system-effects-lifecycle-sha256",
            "b" * 64,
            "--system-effects-host-lock",
            "/run/allox-effects-custom.lock",
        ]
    )

    config = _system_effects_config_from_args(args)
    assert config["host_lock_path"] == "/run/allox-effects-custom.lock"
