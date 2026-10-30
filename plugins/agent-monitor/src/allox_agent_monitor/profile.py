"""Single/cross-run semantic profiling without double-counting parallel children."""

from __future__ import annotations

import math
from collections import defaultdict


def union_ns(intervals: list[tuple[int, int]]) -> int:
    end = total = 0
    for left, right in sorted(intervals):
        total += max(0, right - max(left, end))
        end = max(end, right)
    return total


def percentile(values: list[int], q: float) -> int | None:
    return sorted(values)[max(0, math.ceil(len(values)*q)-1)] if values else None


def profile(spans: list[dict]) -> dict:
    def trace_key(row):
        return (row["agent_id"], row["session_id"], row["trace_id"])

    children: dict[tuple, list] = defaultdict(list)
    for row in spans:
        children[(*trace_key(row), row["parent_id"])].append(row)
    groups: dict[tuple, list] = defaultdict(list)
    self_times = {}
    warnings = []
    for row in spans:
        key = (*trace_key(row), row["span_id"])
        if row["end_ns"] is None:
            warnings.append({"span_id": row["span_id"], "reason": "unfinished"})
            continue
        intervals = []
        valid = True
        for child in children[key]:
            if child["clock_id"] != row["clock_id"] or child["end_ns"] is None:
                valid = False
                continue
            if child["start_ns"] < row["start_ns"] or child["end_ns"] > row["end_ns"]:
                valid = False
            intervals.append((max(row["start_ns"], child["start_ns"]),
                              min(row["end_ns"], child["end_ns"])))
        self_times[key] = row["duration_ns"] - union_ns(intervals) if valid else None
        if not valid:
            warnings.append({"span_id": row["span_id"], "reason": "child timing incomplete"})
        groups[(tuple(row["semantic_path"]), row["kind"], row["name"], row["model"])].append(row)
    results = []
    for (semantic, kind, name, model), rows in sorted(groups.items(), key=lambda item: str(item[0])):
        durations = [row["duration_ns"] for row in rows]
        exclusive = [self_times[(*trace_key(row), row["span_id"])] for row in rows]
        metrics: dict[str, list] = defaultdict(list)
        for row in rows:
            for metric, value in row["metrics"].items():
                metrics[metric].append(value)
        results.append({
            "semantic_path": list(semantic), "kind": kind, "name": name, "model": model,
            "calls": len(rows), "runs": len({trace_key(row) for row in rows}),
            "errors": sum(row["status"] != "ok" for row in rows),
            "inclusive_ns": sum(durations),
            "self_ns": sum(exclusive) if all(v is not None for v in exclusive) else None,
            "p50_ns": percentile(durations, .5), "p95_ns": percentile(durations, .95),
            "metrics": {name: {("max" if "peak" in name else "sum"):
                               max(values) if "peak" in name else sum(values),
                               "samples": len(values)}
                        for name, values in metrics.items()},
        })
    return {
        "runs": len({trace_key(row) for row in spans}), "spans": len(spans),
        "complete": not warnings, "warnings": warnings, "groups": results,
        "completeness_scope": "received spans only; reconcile producer health separately",
        "time_semantics": "inclusive sums are work, not elapsed wall time; self subtracts child union",
        "unobserved": ["transport unless instrumented", "server timings unless provider reports"],
    }
