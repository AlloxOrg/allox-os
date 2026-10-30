"""Independent feature discovery, activation and execution composition tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from allox.runtime.extensions import (
    ExecutionContribution,
    ExecutionQuiescence,
    ExecutionStartContext,
    FeatureManager,
    SandboxMount,
    WorkspaceFeatureContext,
)
from allox.workspace.store import WorkspaceError


class DemoFeature:
    name = "demo"
    abi = 1
    rpc_prefixes = ("demo.",)

    def __init__(self, *, context, config):
        self.context = context
        self.config = config
        self.closed = False

    def prepare_execution(self, agent_id, session_id):
        return ExecutionContribution(
            argv_prefix=("prefix",),
            environment=(("DEMO", self.config["value"]),),
            mounts=(SandboxMount("/source", "/run/allox/demo"),),
            metadata={"demo": f"{agent_id}/{session_id}"},
        )

    def dispatch(self, action, params):
        return {"action": action, "value": params["value"]}

    def close(self):
        self.closed = True


def context():
    return WorkspaceFeatureContext(
        store=SimpleNamespace(),
        executions=SimpleNamespace(),
        cgroups=SimpleNamespace(),
    )


def test_not_enabled_means_factory_is_not_loaded():
    called = False

    def factory(**kwargs):
        nonlocal called
        called = True
        return DemoFeature(**kwargs)

    manager = FeatureManager(context(), factories={"demo": factory})
    assert manager.names == ()
    assert not called


def test_enabled_feature_contributes_mount_environment_metadata_and_rpc():
    manager = FeatureManager(
        context(),
        (("demo", {"value": "enabled"}),),
        factories={"demo": DemoFeature},
    )
    contribution = manager.prepare_execution("agent", "session")
    assert contribution.argv_prefix == ("prefix",)
    assert contribution.environment == (("DEMO", "enabled"),)
    assert contribution.mounts[0].target == "/run/allox/demo"
    assert contribution.metadata == {"demo": "agent/session"}
    assert manager.dispatch("demo.status", {"value": 3}) == {
        "action": "demo.status",
        "value": 3,
    }
    manager.close()


def test_enabled_but_missing_plugin_fails_explicitly(monkeypatch):
    monkeypatch.setattr("allox.runtime.extensions.entry_points", lambda **kwargs: [])
    with pytest.raises(WorkspaceError, match="enabled but not installed"):
        FeatureManager(context(), (("missing", {}),))


def test_plugin_mount_conflicts_fail_closed():
    class ConflictingFeature(DemoFeature):
        name = "conflict"
        rpc_prefixes = ("conflict.",)

    manager = FeatureManager(
        context(),
        (("demo", {"value": "same"}), ("conflict", {"value": "same"})),
        factories={"demo": DemoFeature, "conflict": ConflictingFeature},
    )
    with pytest.raises(WorkspaceError, match="mount target"):
        manager.prepare_execution("agent", "session")
    manager.close()


def execution_context(events):
    return ExecutionStartContext(
        agent_id="agent",
        session_id="session",
        run_id="run-1",
        run_epoch=7,
        root_pid=123,
        cgroup_path="/sys/fs/cgroup/allox/agent/session",
        cgroup_relative_path="/allox/agent/session",
        cgroup_id=42,
        mount_namespace_id=1001,
        network_namespace_id=1002,
        audit_directory="/var/lib/allox/audit/run-1",
        boot_id="boot-1",
        execution_metadata={"source": "test"},
        on_failure=events.append,
    )


class LifecycleHandle:
    def __init__(self, name, events, *, fail_finish=False):
        self.name = name
        self.events = events
        self.fail_finish = fail_finish
        self.closed = False

    def status(self):
        self.events.append(f"{self.name}.status")
        return {"status": "READY", "name": self.name}

    def assert_healthy(self):
        self.events.append(f"{self.name}.healthy")

    def finish(self, *, outcome, quiescence):
        self.events.append(f"{self.name}.finish")
        assert outcome == "completed"
        assert quiescence.cgroup_id == 42
        if self.fail_finish:
            raise WorkspaceError(f"{self.name} failed to finalize")
        return {"status": "FINISHED", "outcome": outcome}

    def close_unverified(self):
        self.closed = True
        self.events.append(f"{self.name}.close")


class LifecycleFeature(DemoFeature):
    execution_lifecycle_abi = 1

    def __init__(self, *, context, config):
        super().__init__(context=context, config=config)
        self.events = config["events"]
        self.handle = LifecycleHandle(
            self.name,
            self.events,
            fail_finish=config.get("fail_finish", False),
        )

    def start_execution(self, context):
        self.events.append(f"{self.name}.start")
        assert context.run_id == "run-1"
        assert context.execution_metadata == {"source": "test"}
        return self.handle


def test_lifecycle_hooks_start_in_plugin_order_and_finish_in_reverse_order():
    events = []

    class SecondLifecycleFeature(LifecycleFeature):
        name = "second"
        rpc_prefixes = ("second.",)

    manager = FeatureManager(
        context(),
        (("demo", {"events": events}), ("second", {"events": events})),
        factories={"demo": LifecycleFeature, "second": SecondLifecycleFeature},
    )
    assert manager.execution_lifecycle_enabled is True

    active = manager.start_execution(execution_context(events))
    assert active.enabled is True
    assert events == [
        "demo.start",
        "second.start",
        "demo.healthy",
        "second.healthy",
    ]
    assert active.status() == {
        "demo": {"status": "READY", "name": "demo"},
        "second": {"status": "READY", "name": "second"},
    }

    result = active.finish(
        outcome="completed",
        quiescence=ExecutionQuiescence(cgroup_path="/cgroup", cgroup_id=42),
    )
    assert result == {
        "second": {"status": "FINISHED", "outcome": "completed"},
        "demo": {"status": "FINISHED", "outcome": "completed"},
    }
    assert events[-2:] == ["second.finish", "demo.finish"]
    with pytest.raises(WorkspaceError, match="not active"):
        active.finish(
            outcome="completed",
            quiescence=ExecutionQuiescence(cgroup_path="/cgroup", cgroup_id=42),
        )
    manager.close()


def test_lifecycle_abi_v2_commits_agent_started_before_release():
    events = []

    class V2Handle(LifecycleHandle):
        def agent_started(self):
            self.events.append(f"{self.name}.agent-started")

    class V2Feature(LifecycleFeature):
        execution_lifecycle_abi = 2
        supports_recovery = True

        def __init__(self, *, context, config):
            DemoFeature.__init__(self, context=context, config=config)
            self.events = config["events"]
            self.handle = V2Handle(self.name, self.events)

    manager = FeatureManager(
        context(),
        (("demo", {"events": events}),),
        factories={"demo": V2Feature},
    )
    assert manager.execution_lifecycle_enabled is True
    assert manager.recovery_lifecycle_enabled is True
    active = manager.start_execution(execution_context(events))
    active.agent_started()
    assert events[-1] == "demo.agent-started"
    active.close_unverified()
    manager.close()


def test_lifecycle_partial_start_is_closed_in_reverse_order():
    events = []

    class FailingStartFeature(LifecycleFeature):
        name = "failing"
        rpc_prefixes = ("failing.",)

        def start_execution(self, context):
            del context
            self.events.append("failing.start")
            raise WorkspaceError("startup rejected")

    manager = FeatureManager(
        context(),
        (("demo", {"events": events}), ("failing", {"events": events})),
        factories={"demo": LifecycleFeature, "failing": FailingStartFeature},
    )

    with pytest.raises(WorkspaceError, match="startup rejected"):
        manager.start_execution(execution_context(events))

    assert events == ["demo.start", "failing.start", "demo.close"]
    assert manager._features[0].handle.closed is True
    manager.close()


def test_lifecycle_finish_failure_closes_every_handle_and_stays_closed():
    events = []

    class FailingFinishFeature(LifecycleFeature):
        name = "failing"
        rpc_prefixes = ("failing.",)

    manager = FeatureManager(
        context(),
        (
            ("demo", {"events": events}),
            ("failing", {"events": events, "fail_finish": True}),
        ),
        factories={"demo": LifecycleFeature, "failing": FailingFinishFeature},
    )
    active = manager.start_execution(execution_context(events))

    with pytest.raises(WorkspaceError, match="failed to finalize"):
        active.finish(
            outcome="completed",
            quiescence=ExecutionQuiescence(cgroup_path="/cgroup", cgroup_id=42),
        )

    assert events[-3:] == ["failing.finish", "failing.close", "demo.close"]
    assert all(feature.handle.closed for feature in manager._features)
    with pytest.raises(WorkspaceError, match="not active"):
        active.assert_healthy()
    manager.close()


def test_invalid_lifecycle_handle_is_closed_before_start_fails():
    events = []

    class InvalidHandle:
        def close_unverified(self):
            events.append("invalid.close")

    class InvalidHandleFeature(LifecycleFeature):
        def start_execution(self, context):
            del context
            events.append("invalid.start")
            return InvalidHandle()

    manager = FeatureManager(
        context(),
        (("demo", {"events": events}),),
        factories={"demo": InvalidHandleFeature},
    )

    with pytest.raises(WorkspaceError, match="invalid lifecycle handle"):
        manager.start_execution(execution_context(events))

    assert events == ["invalid.start", "invalid.close"]
    manager.close()


@pytest.mark.parametrize(
    ("lifecycle_abi", "provide_start"),
    [(3, True), (1, False), (None, True)],
)
def test_incompatible_lifecycle_abi_is_rejected_and_feature_is_closed(
    lifecycle_abi, provide_start
):
    class IncompatibleFeature(DemoFeature):
        pass

    if lifecycle_abi is not None:
        IncompatibleFeature.execution_lifecycle_abi = lifecycle_abi
    if provide_start:
        IncompatibleFeature.start_execution = lambda self, context: None

    created = []

    def factory(**kwargs):
        feature = IncompatibleFeature(**kwargs)
        created.append(feature)
        return feature

    with pytest.raises(WorkspaceError, match="incompatible execution lifecycle ABI"):
        FeatureManager(
            context(),
            (("demo", {"value": "unused"}),),
            factories={"demo": factory},
        )

    assert created[0].closed is True
