"""Read only Core-selected Session cgroup counters; no arbitrary RPC file reader."""

from __future__ import annotations

from pathlib import Path


def snapshot(path: Path) -> dict[str, int]:
    counters: dict[str, int] = {}
    try:
        cpu = dict(line.split() for line in (path / "cpu.stat").read_text().splitlines())
        counters["cpu_usage_usec"] = int(cpu["usage_usec"])
    except (OSError, KeyError, ValueError):
        pass
    try:
        counters["memory_peak_bytes"] = int((path / "memory.peak").read_text())
    except (OSError, ValueError):
        pass
    try:
        read = written = 0
        for line in (path / "io.stat").read_text().splitlines():
            fields = dict(item.split("=", 1) for item in line.split()[1:])
            read += int(fields.get("rbytes", 0))
            written += int(fields.get("wbytes", 0))
        counters.update(io_read_bytes=read, io_write_bytes=written)
    except (OSError, ValueError):
        pass
    return counters


def delta(before: dict, after: dict) -> dict:
    result = {}
    for key, value in after.items():
        if key == "memory_peak_bytes":
            result["cgroup." + key] = value
        elif key in before and value >= before[key]:
            result["cgroup." + key] = value - before[key]
    return result
