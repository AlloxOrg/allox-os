"""Sequential verification: fresh Kata workload, then host query/SDK benchmarks."""
import os
import subprocess
from pathlib import Path

ROOT = Path("/data/hy/Allox/recovery-fixes-20261112")
AUDIT = Path("/data/hy/Allox/perf-bug-audit-20261112")
BIN = Path("/data/hy/Allox/agent-monitoring-main-20261030-v2/.validation-venv/bin")
paths = [ROOT / "source/src", ROOT / "source/tests"]
paths += list((ROOT / "source/plugins").glob("*/src"))
(ROOT / "tmp").mkdir(exist_ok=True)
os.environ.update(PYTHONPATH=":".join(map(str, paths)), TMPDIR=str(ROOT / "tmp"), PYTHONDONTWRITEBYTECODE="1")


def run(args, output):
    online = set()
    for part in Path("/sys/devices/system/cpu/online").read_text().strip().split(","):
        first, _, last = part.partition("-")
        online.update(range(int(first), int(last or first) + 1))
    selected = sorted(online)[-24:]
    assert len(selected) == 24
    print("AFFINITY", selected, "TASK", output, flush=True)
    with (ROOT / output).open("w") as stream:
        subprocess.run(["taskset", "-c", ",".join(map(str, selected)), *args],
                       stdout=stream, stderr=subprocess.STDOUT, check=True)


run(["python3", "-u", str(ROOT / "run_guest.py")], "guest-fixed.log")
run([str(BIN / "python"), str(AUDIT / "correlation_perf.py")], "correlation-perf.json")
for mode in ("sync", "background"):
    run([str(BIN / "python"), str(ROOT / "source/examples/monitoring_benchmark.py"),
         "--pairs", "10", "--steps", "40", "--batch-size", "32", "--delivery", mode],
        "monitor-" + mode + ".json")
