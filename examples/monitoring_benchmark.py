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
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

from allox_agent_monitor.plugin import AgentMonitorFeature
from allox_agent_monitor.profile import profile
from allox_agent_monitor.queued import BoundQueueSink, QueuedTraceWriter
from allox_agent_monitor.sdk import Tracer
from allox_agent_monitor.store import TraceStore
from allox_agent_monitor.transport import SessionQueueTransport, emit_session


def worker(enabled: bool, steps: int, batch_size: int = 1, delivery: str = 'sync',
           turns: bool = False, transport: str = 'direct', action_batch_size: int | None = None,
           action_flush_interval: float = .05) -> dict:
    with tempfile.TemporaryDirectory(prefix="allox-monitor-bench-") as tmp:
        store = TraceStore(Path(tmp)) if enabled else None
        writer = QueuedTraceWriter(store) if enabled and delivery == 'queued' else None
        sink = BoundQueueSink(writer, 'benchmark', 'session') if writer else (
            lambda e: store.ingest('benchmark', 'session', e))
        feature = None
        if enabled and transport == 'socket':
            if store:
                store.close()
            context = SimpleNamespace(store=SimpleNamespace(root=Path(tmp) / 'work'),
                cgroups=SimpleNamespace(path=lambda *a: Path(tmp) / 'absent', owns=lambda *a: True),
                executions=SimpleNamespace(share_access=lambda *a, **kw: nullcontext()))
            # Short socket path; DB remains on the original XFS tmp directory.
            feature = AgentMonitorFeature(context=context, config={'root': Path(tmp) / 'db',
                'socket_root': Path('/tmp'), 'async_write': delivery == 'queued'})
            store, writer = feature.store, feature.writer
            feature.execution_started({'agent_id': 'benchmark', 'session_id': 'session',
                'run_id': 'benchmark-process', 'state': 'running'})
            endpoint = feature._endpoints.endpoint('benchmark', 'session')
            sink = SessionQueueTransport(endpoint) if writer else lambda e: emit_session(e, endpoint)
        tracer = Tracer(sink, strict=True, enabled=enabled,
                        batch_size=batch_size, delivery=delivery,
                        action_batch_size=action_batch_size,
                        action_flush_interval=action_flush_interval)
        elapsed = []
        before = resource.getrusage(resource.RUSAGE_SELF)
        started = time.perf_counter_ns()
        for _ in range(steps):
            step_start = time.perf_counter_ns()
            with tracer.span('action', 'turn') if enabled and turns else nullcontext():
                if enabled:
                    tracer.call_tool("cpu-and-wait", workload)
                else:
                    workload()
            if enabled and turns and action_batch_size is None:
                tracer.barrier()  # Equal durability boundaries in all delivery modes.
            elapsed.append(time.perf_counter_ns() - step_start)
        foreground = time.perf_counter_ns() - started
        if enabled:
            tracer.close()
        if writer:
            writer.close()
        total = time.perf_counter_ns()-started
        after = resource.getrusage(resource.RUSAGE_SELF)
        result = {
            "enabled": enabled, "wall_ns": total, "foreground_ns": foreground,
            "final_flush_ns": total - foreground,
            "cpu_seconds": after.ru_utime+after.ru_stime-before.ru_utime-before.ru_stime,
            "peak_rss_kib": after.ru_maxrss, "step_p50_ns": statistics.median(elapsed),
            "step_p95_ns": sorted(elapsed)[max(0, int(len(elapsed)*.95)-1)],
            "monitor": tracer.health() if enabled else None,
            "collector": store.status() if store else None,
            "profile": profile(store.spans("benchmark", "session")) if store else None,
        }
        expected = steps * (3 if turns else 2)
        if store:
            reopened = TraceStore(store.root)
            rows = [r for r in reopened.spans('benchmark', 'session') if r['kind'] != 'process']
            result['reopen_oracle'] = len(rows) == expected and all(r['status'] == 'ok' for r in rows)
            assert result['reopen_oracle'] and tracer.health()['complete']
            reopened.close()
        if feature:
            feature.close()
            store = None
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
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument('--delivery', choices=('sync', 'background', 'queued'), default='sync')
    parser.add_argument('--turns', action='store_true')
    parser.add_argument('--transport', choices=('direct', 'socket'), default='direct')
    parser.add_argument('--action-batch-size', type=int, default=None)
    parser.add_argument('--action-flush-interval', type=float, default=.05)
    args = parser.parse_args()
    if args.steps < 1 or args.pairs < 1:
        parser.error("steps and pairs must be positive")
    if args.worker:
        print(json.dumps(worker(args.worker == "on", args.steps, args.batch_size, args.delivery,
                                args.turns, args.transport, args.action_batch_size,
                                args.action_flush_interval)))
        return
    trials = []
    for index in range(args.pairs):
        pair = {}
        for mode in (("off", "on") if index % 2 == 0 else ("on", "off")):
            online = set()
            for part in Path('/sys/devices/system/cpu/online').read_text().strip().split(','):
                first, _, last = part.partition('-')
                online.update(range(int(first), int(last or first) + 1))
            selected = sorted(online)[-24:]
            assert len(selected) == 24
            output = subprocess.check_output([
                'taskset', '-c', ','.join(map(str, selected)),
                sys.executable, __file__, "--worker", mode, "--steps", str(args.steps),
                "--batch-size", str(args.batch_size), '--delivery', args.delivery,
                '--transport', args.transport, *(['--turns'] if args.turns else []),
                *(['--action-batch-size', str(args.action_batch_size), '--action-flush-interval',
                    str(args.action_flush_interval)] if args.action_batch_size is not None else [])
            ], text=True)
            pair[mode] = json.loads(output)
        pair["wall_overhead_percent"] = (pair["on"]["wall_ns"]/pair["off"]["wall_ns"]-1)*100
        trials.append(pair)
    print(json.dumps({
        "scope": "synthetic SDK + SQLite FULL durability; not real Agent/eBPF overhead",
        "delivery": args.delivery, "turns": args.turns, "transport": args.transport,
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
