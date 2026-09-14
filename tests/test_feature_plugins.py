"""Independent feature discovery, activation and execution composition tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from allox.runtime.extensions import (
    ExecutionContribution,
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
