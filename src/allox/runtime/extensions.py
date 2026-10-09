"""Stable extension surface for independently distributed Allox features."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from allox.workspace.store import WorkspaceError

PLUGIN_ABI = 1
PLUGIN_ENTRY_POINT_GROUP = "allox.workspace_features"
EXECUTION_LIFECYCLE_ABI = 2
SUPPORTED_EXECUTION_LIFECYCLE_ABIS = frozenset({1, 2})


@dataclass(frozen=True)
class ExecutionRecoveryPlan:
    """Core-owned, immutable rollback contract for one observed execution."""

    checkpoint_id: str
    recovery_id: str
    scrub_runtime: bool = True
    require_consistent: bool = True
    baseline_created: bool = False


@dataclass(frozen=True)
class ExecutionStartContext:
    """Exact stopped/frozen Session boundary offered to lifecycle plugins."""

    agent_id: str
    session_id: str
    run_id: str
    run_epoch: int
    root_pid: int
    cgroup_path: str
    cgroup_relative_path: str
    cgroup_id: int
    mount_namespace_id: int
    network_namespace_id: int
    audit_directory: str
    boot_id: str
    execution_metadata: Mapping[str, Any]
    on_failure: Callable[[str], None] = field(repr=False, compare=False)
    recovery: ExecutionRecoveryPlan | None = None


@dataclass(frozen=True)
class ExecutionQuiescence:
    """Core-attested terminal boundary for one exact Session cgroup."""

    cgroup_path: str
    cgroup_id: int
    status: str = "QUIESCENT_WITHIN_CGROUP"
    basis: str = "CGROUP_V2_EMPTY_FROZEN_VERIFIED"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "basis": self.basis,
            "cgroup_path": self.cgroup_path,
            "cgroup_id": self.cgroup_id,
        }


class FeatureExecutionHandle(Protocol):
    """Per-run handle returned by a feature's optional lifecycle hook."""

    def status(self) -> Mapping[str, Any]: ...

    def assert_healthy(self) -> None: ...

    def agent_started(self) -> None: ...

    def finish(
        self, *, outcome: str, quiescence: ExecutionQuiescence
    ) -> Mapping[str, Any]: ...

    def close_unverified(self) -> None: ...


@dataclass(frozen=True)
class SandboxMount:
    """One trusted host/Guest path exposed inside the Agent Bubblewrap."""

    source: str
    target: str
    read_only: bool = True

    def __post_init__(self) -> None:
        source = Path(self.source)
        target = PurePosixPath(self.target)
        if not source.is_absolute() and not PurePosixPath(self.source).is_absolute():
            raise WorkspaceError("plugin mount source must be absolute")
        if not target.is_absolute() or ".." in target.parts or target == PurePosixPath("/"):
            raise WorkspaceError("plugin mount target must be a confined absolute path")


@dataclass(frozen=True)
class ExecutionContribution:
    """Declarative changes contributed to one Session execution."""

    argv_prefix: tuple[str, ...] = ()
    environment: tuple[tuple[str, str], ...] = ()
    mounts: tuple[SandboxMount, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkspaceFeatureContext:
    """Trusted services made available to an activated feature plugin."""

    store: Any
    executions: Any
    cgroups: Any


class WorkspaceFeature(Protocol):
    """Runtime instance returned by an ``allox.workspace_features`` provider."""

    name: str
    abi: int
    rpc_prefixes: tuple[str, ...]

    def prepare_execution(self, agent_id: str, session_id: str) -> ExecutionContribution: ...

    def dispatch(self, action: str, params: dict[str, Any]) -> Any: ...

    def close(self) -> None: ...


class ActiveFeatureExecutions:
    """Own lifecycle-plugin handles for exactly one managed execution."""

    def __init__(
        self, entries: list[tuple[str, int, FeatureExecutionHandle]]
    ) -> None:
        self._entries = entries
        self._state = "active"
        self._lock = threading.RLock()

    @property
    def enabled(self) -> bool:
        return bool(self._entries)

    def status(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            result: dict[str, dict[str, Any]] = {}
            for name, _, handle in self._entries:
                value = handle.status()
                if not isinstance(value, Mapping):
                    raise WorkspaceError(
                        f'feature plugin "{name}" returned invalid execution status'
                    )
                result[name] = dict(value)
            return result

    def assert_healthy(self) -> None:
        with self._lock:
            if self._state != "active":
                raise WorkspaceError("feature execution lifecycle is not active")
            for _, _, handle in self._entries:
                handle.assert_healthy()

    def agent_started(self) -> None:
        """Commit ABI-v2 pre-release transitions while the run is frozen."""
        with self._lock:
            if self._state != "active":
                raise WorkspaceError("feature execution lifecycle is not active")
            for _, lifecycle_abi, handle in self._entries:
                if lifecycle_abi == 2:
                    handle.agent_started()

    def finish(
        self, *, outcome: str, quiescence: ExecutionQuiescence
    ) -> dict[str, dict[str, Any]]:
        with self._lock:
            if self._state != "active":
                raise WorkspaceError("feature execution lifecycle is not active")
            self._state = "finalizing"
            results: dict[str, dict[str, Any]] = {}
            try:
                for name, _, handle in reversed(self._entries):
                    value = handle.finish(outcome=outcome, quiescence=quiescence)
                    if not isinstance(value, Mapping):
                        raise WorkspaceError(
                            f'feature plugin "{name}" returned an invalid final result'
                        )
                    results[name] = dict(value)
            except BaseException:
                for _, _, handle in reversed(self._entries):
                    with suppress(Exception):
                        handle.close_unverified()
                self._state = "closed"
                raise
            self._state = "closed"
            return results

    def close_unverified(self) -> None:
        with self._lock:
            if self._state == "closed":
                return
            self._state = "closed"
            for _, _, handle in reversed(self._entries):
                with suppress(Exception):
                    handle.close_unverified()


def _installed_feature_factory(name: str):
    points = entry_points(group=PLUGIN_ENTRY_POINT_GROUP)
    matches = [point for point in points if point.name == name]
    if not matches:
        raise WorkspaceError(
            f'Allox feature plugin "{name}" is enabled but not installed'
        )
    if len(matches) != 1:
        raise WorkspaceError(f'multiple Allox feature plugins provide "{name}"')
    return matches[0].load()


class FeatureManager:
    """Load enabled plugins and compose their reversible runtime effects."""

    def __init__(
        self,
        context: WorkspaceFeatureContext,
        specifications: tuple[tuple[str, Mapping[str, Any]], ...] = (),
        *,
        factories: Mapping[str, Any] | None = None,
    ) -> None:
        self._features: list[WorkspaceFeature] = []
        self._routes: dict[str, WorkspaceFeature] = {}
        factories = factories or {}
        try:
            for name, configuration in specifications:
                factory = factories.get(name) or _installed_feature_factory(name)
                feature = factory(context=context, config=dict(configuration))
                # Own it before validation so a rejected ABI or route still
                # unwinds resources created during plugin activation.
                self._features.append(feature)
                if feature.name != name:
                    raise WorkspaceError(
                        f'feature plugin "{name}" returned mismatched name "{feature.name}"'
                    )
                if feature.abi != PLUGIN_ABI:
                    raise WorkspaceError(
                        f'feature plugin "{name}" ABI {feature.abi} != required ABI {PLUGIN_ABI}'
                    )
                lifecycle_abi = getattr(feature, "execution_lifecycle_abi", None)
                lifecycle_start = getattr(feature, "start_execution", None)
                if (
                    lifecycle_abi is not None or lifecycle_start is not None
                ) and (
                    lifecycle_abi not in SUPPORTED_EXECUTION_LIFECYCLE_ABIS
                    or not callable(lifecycle_start)
                ):
                    raise WorkspaceError(
                        f'feature plugin "{name}" has an incompatible execution lifecycle ABI'
                    )
                for prefix in feature.rpc_prefixes:
                    if not prefix or not prefix.endswith("."):
                        raise WorkspaceError(f'feature plugin "{name}" has an invalid RPC prefix')
                    if prefix in self._routes:
                        raise WorkspaceError(f'duplicate feature RPC prefix: {prefix}')
                    self._routes[prefix] = feature
        except BaseException:
            self.close()
            raise

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(feature.name for feature in self._features)

    @property
    def execution_lifecycle_enabled(self) -> bool:
        return any(
            getattr(feature, "execution_lifecycle_abi", None)
            in SUPPORTED_EXECUTION_LIFECYCLE_ABIS
            for feature in self._features
        )

    @property
    def recovery_lifecycle_enabled(self) -> bool:
        return any(
            getattr(feature, "execution_lifecycle_abi", None) == 2
            and getattr(feature, "supports_recovery", False) is True
            for feature in self._features
        )

    def prepare_execution(self, agent_id: str, session_id: str) -> ExecutionContribution:
        prefixes: list[str] = []
        environment: dict[str, str] = {}
        mounts: list[SandboxMount] = []
        metadata: dict[str, Any] = {}
        targets: set[str] = set()
        for feature in self._features:
            contribution = feature.prepare_execution(agent_id, session_id)
            prefixes.extend(contribution.argv_prefix)
            for key, value in contribution.environment:
                if key in environment and environment[key] != value:
                    raise WorkspaceError(f"feature plugins conflict on environment variable {key}")
                environment[key] = value
            for mount in contribution.mounts:
                if mount.target in targets:
                    raise WorkspaceError(f"feature plugins conflict on mount target {mount.target}")
                targets.add(mount.target)
                mounts.append(mount)
            for key, value in contribution.metadata.items():
                if key in metadata and metadata[key] != value:
                    raise WorkspaceError(f"feature plugins conflict on run metadata {key}")
                metadata[key] = value
        return ExecutionContribution(
            argv_prefix=tuple(prefixes),
            environment=tuple(environment.items()),
            mounts=tuple(mounts),
            metadata=metadata,
        )

    def start_execution(self, context: ExecutionStartContext) -> ActiveFeatureExecutions:
        """Start opt-in hooks after Core has stopped, attached and frozen the run."""
        entries: list[tuple[str, int, FeatureExecutionHandle]] = []
        try:
            for feature in self._features:
                start = getattr(feature, "start_execution", None)
                if start is None:
                    continue
                lifecycle_abi = getattr(feature, "execution_lifecycle_abi", None)
                handle = start(context)
                required = ("status", "assert_healthy", "finish", "close_unverified")
                if lifecycle_abi == 2:
                    required = (*required, "agent_started")
                if handle is None or any(
                    not callable(getattr(handle, method, None)) for method in required
                ):
                    close_unverified = getattr(handle, "close_unverified", None)
                    if callable(close_unverified):
                        with suppress(Exception):
                            close_unverified()
                    raise WorkspaceError(
                        f'feature plugin "{feature.name}" returned an invalid lifecycle handle'
                    )
                entries.append((feature.name, lifecycle_abi, handle))
            active = ActiveFeatureExecutions(entries)
            active.assert_healthy()
            return active
        except BaseException:
            for _, _, handle in reversed(entries):
                with suppress(Exception):
                    handle.close_unverified()
            raise

    def dispatch(self, action: str, params: dict[str, Any]) -> Any:
        for prefix, feature in self._routes.items():
            if action.startswith(prefix):
                return feature.dispatch(action, params)
        raise WorkspaceError(f"unknown action: {action}")

    def handles(self, action: str) -> bool:
        return any(action.startswith(prefix) for prefix in self._routes)

    def close(self) -> None:
        for feature in reversed(self._features):
            with suppress(Exception):
                feature.close()
        self._features.clear()
        self._routes.clear()
