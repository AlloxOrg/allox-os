"""CLI output helpers: table / JSON / YAML / raw."""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from typing import Any

import click
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

_STATUS_STYLES: dict[str, tuple[str, str]] = {
    "running": ("bold green", "●"),
    "ready": ("bold green", "●"),
    "pending": ("bold yellow", "◐"),
    "creating": ("bold yellow", "◐"),
    "paused": ("bold blue", "⏸"),
    "stopped": ("dim", "○"),
    "terminated": ("dim", "○"),
    "killed": ("dim", "○"),
    "error": ("bold red", "✗"),
    "failed": ("bold red", "✗"),
}


class OutputFormatter:
    def __init__(self, fmt: str, *, color: bool = True) -> None:
        self.fmt = fmt
        self.color = color
        self.console = Console(stderr=False, color_system="auto" if color else None)

    @contextmanager
    def spinner(self, message: str):
        if self.fmt in ("json", "yaml", "raw"):
            yield
            return
        with self.console.status(message):
            yield

    def success_panel(self, data: dict[str, Any], *, title: str = "OK") -> None:
        if self.fmt == "json":
            click.echo(json.dumps(data, indent=2, default=str))
            return
        if self.fmt == "yaml":
            self._print_yaml(data)
            return
        if self.fmt == "raw":
            for v in data.values():
                click.echo(v)
            return
        lines = "\n".join(f"[bold]{k}[/]: {v}" for k, v in data.items())
        self.console.print(Panel(lines, title=title, border_style="green"))

    def error_panel(self, message: str, *, title: str = "Error") -> None:
        if self.fmt == "json":
            click.echo(json.dumps({"error": message}, indent=2))
            return
        if self.fmt == "yaml":
            self._print_yaml({"error": message})
            return
        self.console.print(Panel(message, title=title, border_style="red"))

    def print_rows(
        self,
        rows: list[dict[str, Any]],
        columns: list[str],
        *,
        title: str | None = None,
    ) -> None:
        if self.fmt == "json":
            click.echo(json.dumps(rows, indent=2, default=str))
            return
        if self.fmt == "yaml":
            self._print_yaml(rows)
            return
        if self.fmt == "raw":
            for row in rows:
                click.echo("\t".join(str(row.get(c, "")) for c in columns))
            return
        self._print_table(rows, columns, title=title)

    def _print_table(
        self,
        rows: list[dict[str, Any]],
        columns: list[str],
        *,
        title: str | None = None,
    ) -> None:
        table = Table(
            title=title,
            show_header=True,
            header_style="bold magenta",
            box=box.ROUNDED,
            border_style="bright_black",
            padding=(0, 1),
        )
        for col in columns:
            style = "bold cyan" if col == "id" else ""
            table.add_column(col.upper(), style=style, no_wrap=(col == "id"))
        for row in rows:
            cells: list[Text | str] = []
            for col in columns:
                val = str(row.get(col, "-"))
                if col == "state":
                    style, icon = _STATUS_STYLES.get(val.lower(), ("", ""))
                    cells.append(Text(f"{icon} {val}", style=style) if style else val)
                else:
                    cells.append(val)
            table.add_row(*cells)
        self.console.print(table)

    def _print_yaml(self, data: Any) -> None:
        if yaml is None:
            click.secho("PyYAML not installed. Use -o json instead.", fg="red", err=True)
            sys.exit(1)
        click.echo(yaml.dump(data, default_flow_style=False, allow_unicode=True).rstrip())
