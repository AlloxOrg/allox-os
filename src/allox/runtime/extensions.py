"""Stable extension surface for independently distributed Allox features."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from allox.workspace.store import WorkspaceError

PLUGIN_ABI = 1
PLUGIN_ENTRY_POINT_GROUP = "allox.workspace_features"


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
