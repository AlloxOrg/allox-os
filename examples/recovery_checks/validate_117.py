"""Re-read CPU topology before EACH workload; run only on 117."""
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path("/data/hy/Allox/recovery-checks-20261112")
BIN = Path("/data/hy/Allox/agent-monitoring-main-20261030-v2/.validation-venv/bin")


def run(args):
    cpus = set()
    for part in Path("/sys/devices/system/cpu/online").read_text().strip().split(","):
        first, _, last = part.partition("-")
        cpus.update(range(int(first), int(last or first) + 1))
    selected = sorted(cpus)[-24:]
    if len(selected) != 24:
        raise RuntimeError("need 24 online logical CPUs")
    print("AFFINITY", selected, "COMMAND", args, flush=True)
    return subprocess.run(["taskset", "-c", ",".join(map(str, selected)), *args],
                          cwd=ROOT / "source", check=True)


os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
run([str(BIN / "ruff"), "check", "--select", "E4,E7,E9,F,I", "src/allox/runtime/extensions.py",
     "src/allox/workspace/daemon.py", "plugins/recovery-checks", "tests/test_recovery_checks.py"])
run([str(BIN / "python"), "-m", "pytest", "-q", "--maxfail=3", "tests/test_recovery_checks.py"])
run([str(BIN / "python"), "-m", "pytest", "-q", "tests", "plugins/system-effects/tests"])
if shutil.which("node"):
    run([str(BIN / "pyright")])
else:
    print("PYRIGHT_NOT_RUN: no host Node.js; do not download or weaken TLS verification", flush=True)
run([str(BIN / "python"), "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
     "--wheel-dir", str(ROOT / "wheels"), ".", "./plugins/recovery-checks"])
run([str(BIN / "python"), "-m", "venv", str(ROOT / "package-env")])
run([str(ROOT / "package-env/bin/python"), "-m", "pip", "install", "--no-deps", "--force-reinstall",
     *map(str, sorted((ROOT / "wheels").glob("*.whl")))])
os.environ.pop("PYTHONPATH", None)
run([str(ROOT / "package-env/bin/python"), str(ROOT / "source/examples/recovery_checks/package_smoke.py")])
