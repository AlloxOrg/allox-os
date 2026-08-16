"""Optional Agent-framework integrations for workspace lifecycle events."""

from __future__ import annotations

from allox.integrations.base import AlloxPlugin, PluginRegistry
from allox.integrations.langchain import LangChainTurnCheckpointPlugin


def builtin_registry(*, discover_external: bool = False) -> PluginRegistry:
    """Return a registry containing Allox's built-in runtime plugins."""
    registry = PluginRegistry()
    registry.register(LangChainTurnCheckpointPlugin)
    if discover_external:
        registry.discover()
    return registry


__all__ = [
    "AlloxPlugin",
    "LangChainTurnCheckpointPlugin",
    "PluginRegistry",
    "builtin_registry",
]
