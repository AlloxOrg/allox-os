"""Allox workspace feature adapter for Session network isolation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from allox.runtime.extensions import ExecutionContribution, SandboxMount
from allox.workspace.store import WorkspaceError
from allox_session_network.networking import SessionNetworkManager


class SessionNetworkFeature:
    name = "session-network"
    abi = 1
    rpc_prefixes = ("network.",)

    def __init__(self, *, context, config: dict[str, Any]) -> None:
        self._context = context
        root = Path(config.get("root", "/run/allox-network"))
        socket_root = Path(config.get("socket_root", "/dev/shm/allox-network"))
        for candidate in (root.resolve(), socket_root.resolve()):
            if candidate == context.store.root or context.store.root in candidate.parents:
                raise WorkspaceError(
                    "Session network state and sockets must be outside the workspace store"
                )
        self._manager = SessionNetworkManager(
            root,
            socket_root=socket_root,
            default_mode=str(config.get("default_mode", "inherit")),
        )
        self._tool = Path(__file__).with_name("tool.py").resolve()

    @staticmethod
    def _required(params: dict[str, Any], key: str) -> str:
        value = params.get(key)
        if not isinstance(value, str) or not value:
            raise WorkspaceError(f"missing string parameter: {key}")
        return value

    def prepare_execution(self, agent_id: str, session_id: str) -> ExecutionContribution:
        launch = self._manager.prepare(agent_id, session_id)
        mounts = ()
        if launch.broker_socket is not None:
            mounts = (
                SandboxMount(launch.broker_socket, "/run/allox/network.sock"),
                SandboxMount(str(self._tool), "/run/allox/network.py"),
            )
        environment = list(launch.environment)
        if launch.broker_socket is not None:
            environment.append(("ALLOX_NETWORK_SOCKET", "/run/allox/network.sock"))
        return ExecutionContribution(
            argv_prefix=launch.argv_prefix,
            environment=tuple(environment),
            mounts=mounts,
            metadata={
                "network_mode": launch.mode,
                "network_namespace_pid": launch.namespace_pid,
            },
        )

    def dispatch(self, action: str, params: dict[str, Any]) -> Any:
        agent_id = self._required(params, "agent_id")
        session_id = self._required(params, "session_id")
        self._context.store.describe(agent_id, session_id)
        if action == "network.status":
            return self._manager.status(agent_id, session_id)
        if action == "network.configure":
            mode = self._required(params, "mode")
            with self._context.executions.mutation(agent_id, session_id):
                return self._manager.configure(agent_id, session_id, mode)
        raise WorkspaceError(f"unknown action: {action}")

    def close(self) -> None:
        self._manager.close()
