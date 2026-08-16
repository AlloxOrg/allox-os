"""Wait until the in-VM AIO runtime API is ready."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import click
import httpx
from opensandbox.sync.sandbox import SandboxSync


def make_aio_health_check(
    port: int = 8080,
    *,
    health_path: str = "/v1/shell/sessions",
    max_wait_seconds: float = 30.0,
    poll_interval: float = 0.2,
    verbose: bool = False,
    ready_info: dict[str, Any] | None = None,
) -> Callable[[SandboxSync], bool]:
    """Return a health_check callback for SandboxSync.create."""
    path = health_path if health_path.startswith("/") else f"/{health_path}"

    def check(sbx: SandboxSync) -> bool:
        start = time.perf_counter()
        try:
            endpoint = sbx.get_endpoint(port)
            url = f"http://{endpoint.endpoint}{path}"
            if verbose:
                click.echo(f"[verbose] AIO health check: GET {url} (timeout {max_wait_seconds}s)", err=True)
            while time.perf_counter() - start < max_wait_seconds:
                try:
                    resp = httpx.get(url, timeout=1.0)
                    if verbose:
                        click.echo(f"[verbose]   → {resp.status_code}", err=True)
                    if resp.status_code == 200:
                        elapsed = time.perf_counter() - start
                        if ready_info is not None:
                            ready_info["elapsed_seconds"] = round(elapsed, 2)
                            ready_info["health_url"] = url
                        if verbose:
                            click.echo(f"[verbose] AIO ready in {elapsed:.2f}s", err=True)
                        return True
                except httpx.HTTPError as exc:
                    if verbose:
                        click.echo(f"[verbose]   → HTTP error: {exc}", err=True)
                time.sleep(poll_interval)
        except Exception as exc:  # noqa: BLE001 - user-supplied readiness callbacks vary
            if verbose:
                click.echo(f"[verbose] Health check failed: {exc}", err=True)
            return False
        if ready_info is not None:
            ready_info["elapsed_seconds"] = None
        return False

    return check
