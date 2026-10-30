"""117-only validation runner; re-read CPU online list before every workload."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def run(argv: list[str], *, output: Path | None = None) -> int:
    cpus = []
    for part in Path("/sys/devices/system/cpu/online").read_text().strip().split(","):
        bounds = [int(v) for v in part.split("-")]
        cpus.extend(range(bounds[0], bounds[-1]+1))
    affinity = ",".join(str(v) for v in sorted(set(cpus))[-24:])
    print(json.dumps({"cpu_ids": affinity, "argv": argv}), flush=True)
    if output:
        with output.open("w") as stream:
            result = subprocess.run(["taskset", "-c", affinity, *argv], check=False, stdout=stream,
                                    stderr=subprocess.STDOUT)
        print(output.read_text()[-12000:], flush=True)
        return result.returncode
    return subprocess.call(["taskset", "-c", affinity, *argv])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-bootstrap", action="store_true")
    parser.add_argument("--skip-typecheck", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if not str(root).startswith("/data/hy/Allox/"):
        raise SystemExit("run only in the isolated 117 experiment workspace")
    os.chdir(root)
    results = root / "validation-results"
    results.mkdir(exist_ok=True)
    python = root / ".validation-venv/bin/python"
    if not python.exists() and run([sys.executable, "-m", "venv", str(python.parents[1])]):
        return 1
    commands = [
        ([str(python), "-m", "pip", "install", "pip>=23,<27", "pytest>=8,<9", "ruff", "pyright", "build",
          "hatchling", "editables"], "dependencies.log"),
        ([str(python), "-m", "pip", "install", "--no-build-isolation", "-e", ".",
          "-e", "plugins/agent-monitor"], "install.log"),
        ([str(python), "-m", "pytest", "-q"], "pytest.log"),
        ([str(python), "-m", "ruff", "check", "src", "plugins/agent-monitor", "tests/test_agent_monitor.py",
          "examples/monitoring_benchmark.py", "scripts/validate_agent_monitor.py"], "ruff.log"),
        ([str(python), "-m", "pyright"], "pyright.log"),
        ([str(python), "-m", "build", "--wheel", "--no-isolation"], "core-wheel.log"),
        ([str(python), "-m", "build", "--wheel", "--no-isolation", "plugins/agent-monitor"],
         "plugin-wheel.log"),
        ([str(python), "examples/monitoring_benchmark.py", "--pairs", "5", "--steps", "40"],
         "benchmark.json"),
    ]
    outcomes = {}
    for command, filename in commands:
        if args.skip_bootstrap and filename in {"dependencies.log", "install.log"}:
            continue
        if args.skip_typecheck and filename == "pyright.log":
            continue
        outcomes[filename] = run(command, output=results / filename)
        if filename in {"dependencies.log", "install.log"} and outcomes[filename]:
            break
    (results / "summary.json").write_text(json.dumps(outcomes, indent=2))
    return int(any(outcomes.values()))


if __name__ == "__main__":
    raise SystemExit(main())
