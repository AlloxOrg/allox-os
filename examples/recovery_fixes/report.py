"""Join recorded baseline/fixed samples without running another workload."""
import json
import statistics
from pathlib import Path

ROOT = Path("/data/hy/Allox/recovery-fixes-20261112")
AUDIT = Path("/data/hy/Allox/perf-bug-audit-20261112")
lines = (ROOT / "guest-orphan.log").read_text().splitlines()
paths = [line.split(" ", 1)[1] for line in lines if line.startswith("REPORT_PATH ")]
assert len(paths) == 1
path = Path(paths[0]).resolve()
assert ROOT in path.parents
fixed = json.loads(path.read_text())
baseline = json.loads((AUDIT / "guest-7yrs0y3v/report.json").read_text())
comparison = []
for old, new in zip(baseline["performance"], fixed["performance"]):
    assert (old["files"], old["total_bytes"]) == (new["files"], new["total_bytes"])
    assert len(new["pairs"]) == 10
    assert all(pair[mode]["restore_oracle"] for pair in new["pairs"] for mode in ("off", "on"))
    comparison.append({"files": new["files"], "MiB": new["total_bytes"] / (1024 * 1024),
                       "old_checks_on_ms": old["median_ms"]["on"],
                       "fixed_checks_on_ms": new["median_ms"]["on"],
                       "fixed_checks_off_ms": new["median_ms"]["off"],
                       "all_restores_consistent": True})
monitor = {}
for mode in ("sync", "background"):
    old = json.loads((AUDIT / ("monitor-" + mode + ".json")).read_text())
    new = json.loads((ROOT / ("monitor-" + mode + ".json")).read_text())
    assert all(pair["on"]["monitor"]["complete"] for pair in new["pairs"])
    monitor[mode] = {"old_median_pair_overhead_percent": old["median_wall_overhead_percent"],
                     "new_median_pair_overhead_percent": new["median_wall_overhead_percent"],
                     "old_on_wall_ms": statistics.median(p["on"]["wall_ns"] / 1e6 for p in old["pairs"]),
                     "new_on_wall_ms": statistics.median(p["on"]["wall_ns"] / 1e6 for p in new["pairs"]),
                     "new_cpu_ms": statistics.median(p["on"]["cpu_seconds"] * 1000 for p in new["pairs"]),
                     "all_events_acknowledged": True}
validation = (ROOT / "validation-complete.log").read_text().splitlines()
result = {"base_commit": "4ad25c6", "fixed_branch": "fix/recovery-safety-performance",
          "product_source_pushed": False,
          "tests": [line for line in validation if "passed," in line],
          "typecheck": [line for line in validation if line.startswith("0 errors")],
          "guest_report": str(path), "host_cpus": fixed["host_cpus"],
          "diagnostics": fixed["diagnostics"], "orphan_runtime": fixed["orphan_runtime"],
          "recovery_performance": comparison,
          "correlation_before": json.loads((AUDIT / "correlation-perf.json").read_text()),
          "correlation_after": json.loads((ROOT / "correlation-perf.json").read_text()),
          "monitoring_sdk": monitor,
          "limits": ["warm-cache tmpfs loop Btrfs; not physical disk timing",
                     "4 shared hash workers versus serial baseline",
                     "shared host load not controlled; baseline/fixed are separate runs",
                     "no new real LLM or eBPF capture", "FULL durability and full content hashes preserved",
                     "correlation cache improves repeated queries, not first parse"]}
print(json.dumps(result, indent=2))
