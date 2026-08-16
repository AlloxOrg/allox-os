"""Client for the workspace daemon inside the outer Kata VM."""

from __future__ import annotations

from typing import Any

import click
import httpx


class WorkspaceClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def rpc(self, action: str, **params: Any) -> Any:
        try:
            response = self._client.post("/v1/rpc", json={"action": action, "params": params})
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise click.ClickException(f"workspace daemon request failed: {exc}") from exc
        if not response.is_success or not payload.get("ok"):
            raise click.ClickException(str(payload.get("error", "workspace operation failed")))
        return payload.get("result")
