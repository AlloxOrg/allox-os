"""Synthetic paired overhead benchmark. No real LLM or kernel isolation claim.

Run on 117 with taskset; workers inherit the launcher's CPU affinity.
"""

from __future__ import annotations

import argparse
import json
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from allox_agent_monitor.profile import profile
from allox_agent_monitor.sdk import Tracer
from allox_agent_monitor.store import TraceStore


def worker(enabled: bool, steps: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="allox-monitor-bench-") as tmp:
        store = TraceStore(Path(tmp)) if enabled else None
        tracer = Tracer(lambda e: store.ingest("benchmark", "session", e), strict=True)
        elapsed = []
        before = resource.getrusage(resource.RUSAGE_SELF)
        started = time.perf_counter_ns()
        for _ in range(steps):
            step_start = time.perf_counter_ns()
            if enabled:
                tracer.call_tool("cpu-and-wait", workload)
            else:
                workload()
            elapsed.append(time.perf_counter_ns() - step_start)
        total = time.perf_counter_ns()-started
        after = resource.getrusage(resource.RUSAGE_SELF)
        result = {
            "enabled": enabled, "wall_ns": total,
            "cpu_seconds": after.ru_utime+after.ru_stime-before.ru_utime-before.ru_stime,
            "peak_rss_kib": after.ru_maxrss, "step_p50_ns": statistics.median(elapsed),
            "step_p95_ns": sorted(elapsed)[max(0, int(len(elapsed)*.95)-1)],
            "monitor": tracer.health() if enabled else None,
            "collector": store.status() if store else None,
            "profile": profile(store.spans("benchmark", "session")) if store else None,
        }
        if store:
            store.close()
        return result


def workload():
    value = sum(i*i for i in range(1000))
    time.sleep(.001)
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", choices=("on", "off"))
    parser.add_argument("--steps", type=int, default=40)
    parser.add_argument("--pairs", type=int, default=5)
    args = parser.parse_args()
    if args.steps < 1 or args.pairs < 1:
        parser.error("steps and pairs must be positive")
    if args.worker:
        print(json.dumps(worker(args.worker == "on", args.steps)))
        return
    trials = []
    for index in range(args.pairs):
        pair = {}
        for mode in (("off", "on") if index % 2 == 0 else ("on", "off")):
            output = subprocess.check_output([
                sys.executable, __file__, "--worker", mode, "--steps", str(args.steps)
            ], text=True)
            pair[mode] = json.loads(output)
        pair["wall_overhead_percent"] = (pair["on"]["wall_ns"]/pair["off"]["wall_ns"]-1)*100
        trials.append(pair)
    print(json.dumps({
        "scope": "synthetic SDK + SQLite synchronous durability; not Agent/eBPF overhead",
        "pairs": trials,
        "median_wall_overhead_percent": statistics.median(
            pair["wall_overhead_percent"] for pair in trials
        ),
        "clock_resolution_seconds": time.get_clock_info("perf_counter").resolution,
        "clock_error_bound_ns": None,
        "clock_note": "resolution is not measured accuracy or cross-host synchronization",
    }, indent=2))


if __name__ == "__main__":
    main()
