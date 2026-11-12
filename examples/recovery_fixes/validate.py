"""All workloads run on 117 with freshly derived last-24 affinity."""
import os
import subprocess
from pathlib import Path

ROOT = Path("/data/hy/Allox/recovery-fixes-20261112")
BIN = Path("/data/hy/Allox/agent-monitoring-main-20261030-v2/.validation-venv/bin")
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
os.environ.pop("TMPDIR", None)  # Short AF_UNIX paths in legacy unit fixtures.


def run(args):
    cpus = set()
    for part in Path("/sys/devices/system/cpu/online").read_text().strip().split(","):
        first, _, last = part.partition("-")
        cpus.update(range(int(first), int(last or first) + 1))
    selected = sorted(cpus)[-24:]
    assert len(selected) == 24
    print("AFFINITY", selected, "COMMAND", args, flush=True)
    if args[:2] == ["docker", "run"]:
        args = args[:2] + ["--cpuset-cpus", ",".join(map(str, selected))] + args[2:]
    subprocess.run(["taskset", "-c", ",".join(map(str, selected)), *args], cwd=ROOT / "source", check=True)


run([str(BIN / "ruff"), "check", "--select", "E4,E7,E9,F,I", "src", "plugins/recovery-checks",
     "plugins/trace-correlation", "tests/test_recovery_checks.py", "tests/test_trace_correlation.py"])
run(["docker", "run", "--rm", "--network", "none", "-v", str(ROOT / "source") + ":/work:ro",
     "-v", str(BIN.parent / "lib/python3.10/site-packages/pyright") + ":/checker:ro", "-w", "/work",
     "node:22-alpine", "node", "/checker/dist/dist/pyright.js",
     "--typeshedpath", "/checker/dist/dist/typeshed-fallback"])
run([str(BIN / "python"), "-m", "pytest", "-q", "--maxfail=5", "tests", "plugins/system-effects/tests"])
run([str(BIN / "python"), "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--wheel-dir",
     str(ROOT / "wheels"), ".", "./plugins/recovery-checks", "./plugins/trace-correlation", "./plugins/agent-monitor"])
