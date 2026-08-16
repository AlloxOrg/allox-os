"""Allox runtime plugin registration and discovery.

Plugins deliberately sit above the workspace daemon: a plugin may decide when
to create a checkpoint, but it cannot implement or bypass workspace isolation,
leases, or rollback itself.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from importlib import metadata
from typing import Any, Protocol

PLUGIN_ENTRY_POINT_GROUP = "allox.plugins"


class AlloxPlugin(Protocol):
    """The small contract required for a runtime integration plugin."""

    plugin_name: str


PluginFactory = Callable[..., AlloxPlugin]


class PluginRegistry:
    """Registry for built-in and separately installed Allox plugins."""

    def __init__(self) -> None:
        self._factories: dict[str, PluginFactory] = {}

    def register(self, factory: PluginFactory, *, name: str | None = None) -> None:
        plugin_name = name or getattr(factory, "plugin_name", None)
        if not isinstance(plugin_name, str) or not plugin_name:
            raise ValueError("Allox plugins must define a non-empty plugin_name")
        if plugin_name in self._factories:
            raise ValueError(f"Allox plugin already registered: {plugin_name}")
        self._factories[plugin_name] = factory

    def create(self, plugin_name: str, *args: Any, **kwargs: Any) -> AlloxPlugin:
        try:
            factory = self._factories[plugin_name]
        except KeyError as exc:
            known = ", ".join(self.names()) or "none"
            raise KeyError(f"Unknown Allox plugin {plugin_name!r}; available: {known}") from exc
        return factory(*args, **kwargs)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))

    def discover(self, *, group: str = PLUGIN_ENTRY_POINT_GROUP) -> tuple[str, ...]:
        """Load third-party plugin factories published through Python entry points."""
        entry_points = metadata.entry_points()
        selected: Iterator[metadata.EntryPoint]
        if hasattr(entry_points, "select"):
            selected = iter(entry_points.select(group=group))
        else:  # pragma: no cover - Python < 3.10 compatibility
            selected = iter(entry_points.get(group, ()))
        for entry_point in selected:
            # A built-in plugin may also be declared as this distribution's
            # entry point.  Keep discovery idempotent in that common case.
            if entry_point.name in self._factories:
                continue
            self.register(entry_point.load(), name=entry_point.name)
        return self.names()
