"""Dedicated Guest; same pinned kernel, loop Btrfs and audit inputs as baseline."""
import importlib.util
from pathlib import Path

ROOT = Path("/data/hy/Allox/recovery-fixes-20261112")
spec = importlib.util.spec_from_file_location("existing_launcher", ROOT / "source/examples/recovery_checks/run_guest_117.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.ROOT = ROOT
original = module.command


def command(*args, **kwargs):
    if args[:2] == ("docker", "create"):
        args = (*args[:2], "-v", "/data/hy/Allox/perf-bug-audit-20261112:/audit:ro", *args[2:])
    return original(*args, **kwargs)


module.command = command
module.main()
