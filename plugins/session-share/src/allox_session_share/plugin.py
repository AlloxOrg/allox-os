"""Allox workspace feature adapter for Session file sharing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from allox.runtime.extensions import ExecutionContribution, SandboxMount
from allox.workspace.store import WorkspaceError
from allox_session_share.endpoint import SessionShareEndpoints
from allox_session_share.sharing import ShareService


class SessionShareFeature:
    name = "session-share"
    abi = 1
    rpc_prefixes = ("share.",)

    def __init__(self, *, context, config: dict[str, Any]) -> None:
        socket_root = Path(config.get("socket_root", "/run/allox-share"))
        self._shares = ShareService(context.store, context.executions)
        self._endpoints = SessionShareEndpoints(self._shares, context.cgroups, socket_root)
        self._tool = Path(__file__).with_name("tool.py").resolve()

    @staticmethod
    def _required(params: dict[str, Any], key: str) -> str:
        value = params.get(key)
        if not isinstance(value, str) or not value:
            raise WorkspaceError(f"missing string parameter: {key}")
        return value

    def prepare_execution(self, agent_id: str, session_id: str) -> ExecutionContribution:
        endpoint = self._endpoints.endpoint(agent_id, session_id)
        return ExecutionContribution(
            mounts=(
                SandboxMount(endpoint, "/run/allox/share.sock"),
                SandboxMount(str(self._tool), "/run/allox/share.py"),
            ),
            metadata={"session_share": True},
        )

    def dispatch(self, action: str, params: dict[str, Any]) -> Any:
        caller = (
            self._required(params, "agent_id"),
            self._required(params, "session_id"),
        )
        return self._shares.dispatch(caller, action.removeprefix("share."), params)

    def close(self) -> None:
        self._endpoints.close()
