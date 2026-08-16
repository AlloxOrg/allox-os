"""Shared CLI context for the outer VM and inner workspace control planes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import click
from agent_sandbox import Sandbox as AioSandboxClient
from opensandbox.config.connection_sync import ConnectionConfigSync
from opensandbox.sync.manager import SandboxManagerSync
from opensandbox.sync.sandbox import SandboxSync

from allox.cli.output import OutputFormatter
from allox.vm.selection import get_current_session
from allox.workspace.client import WorkspaceClient


@dataclass
class ClientContext:
    resolved_config: dict[str, Any]
    config_path: Path
    verbose: bool = False
    output: OutputFormatter = field(init=False)
    _connection_config: ConnectionConfigSync | None = field(default=None, init=False, repr=False)
    _manager: SandboxManagerSync | None = field(default=None, init=False, repr=False)
    _workspace_client: WorkspaceClient | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.output = OutputFormatter("table", color=self.resolved_config.get("color", True))

    @property
    def connection_config(self) -> ConnectionConfigSync:
        if self._connection_config is None:
            cfg = self.resolved_config
            self._connection_config = ConnectionConfigSync(
                api_key=cfg.get("api_key"),
                domain=cfg.get("domain"),
                protocol=cfg.get("protocol", "http"),
                request_timeout=timedelta(seconds=cfg.get("request_timeout", 30)),
                use_server_proxy=cfg.get("use_server_proxy", False),
            )
        return self._connection_config

    def get_manager(self) -> SandboxManagerSync:
        if self._manager is None:
            self._manager = SandboxManagerSync.create(self.connection_config)
        return self._manager

    def get_workspace_client(self) -> WorkspaceClient:
        if self._workspace_client is None:
            cfg = self.resolved_config
            self._workspace_client = WorkspaceClient(
                str(cfg.get("workspace_daemon_url", "http://127.0.0.1:8092")),
                token=str(cfg.get("workspace_token", "")),
                timeout=float(cfg.get("workspace_request_timeout", 30)),
            )
        return self._workspace_client

    def resolve_sandbox_id(self, sandbox_id: str | None) -> str:
        """Use explicit id or fall back to current session."""
        if sandbox_id:
            return sandbox_id
        session = get_current_session()
        if session:
            return session.sandbox_id
        raise click.ClickException(
            "No sandbox_id and no current session. "
            "Run: allox sandbox create  OR  allox session use <id>"
        )

    def connect_sandbox(self, sandbox_id: str, *, skip_health_check: bool = True) -> SandboxSync:
        resolved = self.resolve_sandbox_id(sandbox_id)
        return SandboxSync.connect(
            resolved,
            connection_config=self.connection_config,
            skip_health_check=skip_health_check,
        )

    def aio_port(self) -> int:
        return int(self.resolved_config.get("aio_port", 8080))

    def aio_base_url(self, sandbox_id: str) -> str:
        sbx = self.connect_sandbox(sandbox_id)
        try:
            endpoint = sbx.get_endpoint(self.aio_port())
            return f"http://{endpoint.endpoint}"
        finally:
            sbx.close()

    def aio_client(self, sandbox_id: str) -> AioSandboxClient:
        resolved = self.resolve_sandbox_id(sandbox_id)
        try:
            base_url = self.aio_base_url(resolved)
            if self.verbose:
                click.echo(f"[verbose] AIO client: {base_url}", err=True)
            return AioSandboxClient(base_url=base_url)
        except Exception as exc:
            raise click.ClickException(
                f"Failed to connect to AIO sandbox '{resolved}': {exc}\n"
                "Hints:\n"
                f"  • Check endpoint: allox sandbox endpoint {resolved}\n"
                "  • Ensure OpenSandbox server is reachable and firewall allows the port\n"
                "  • Verify AIO health: GET /v1/shell/sessions returns 200"
            ) from exc

    def make_output(self, fmt: str) -> OutputFormatter:
        formatter = OutputFormatter(fmt, color=self.resolved_config.get("color", True))
        self.output = formatter
        return formatter

    def close(self) -> None:
        if self._workspace_client is not None:
            self._workspace_client.close()
            self._workspace_client = None
        if self._manager is not None:
            self._manager.close()
            self._manager = None
        if self._connection_config is not None:
            self._connection_config.close_transport_if_owned()
            self._connection_config = None
