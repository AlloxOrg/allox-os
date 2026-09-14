"""Prepare tracefs and eBPF facilities for the optional process-tree plugin."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from allox.runtime.bootstrap import MountRunner, _mounts, prepare_session_runtime
from allox.workspace.store import WorkspaceError


def prepare_process_tracking_kernel(
    *,
    cgroup_root: Path = Path("/sys/fs/cgroup/allox"),
    tracefs_root: Path = Path("/sys/kernel/tracing"),
    mountinfo: Path = Path("/proc/self/mountinfo"),
    runner: MountRunner = subprocess.run,
    platform: str = sys.platform,
) -> dict:
    """Prepare the Core cgroup boundary plus process-tree tracepoints."""

    result = prepare_session_runtime(
        cgroup_root=cgroup_root,
        mountinfo=mountinfo,
        runner=runner,
        platform=platform,
    )
    tracefs_root = tracefs_root.resolve()
    mounts = _mounts(mountinfo)
    tracefs_mounted = any(
        mount == tracefs_root and filesystem == "tracefs"
        for mount, filesystem, _ in mounts
    )
    if not tracefs_mounted:
        tracefs_root.mkdir(parents=True, exist_ok=True)
        runner(["mount", "-t", "tracefs", "tracefs", str(tracefs_root)], check=True)
    required_events = (
        "sched_process_fork",
        "sched_process_exec",
        "sched_process_exit",
    )
    missing = [
        event
        for event in required_events
        if not (tracefs_root / "events" / "sched" / event / "id").is_file()
    ]
    if missing:
        raise WorkspaceError("Allox Guest kernel lacks sched tracepoints: " + ", ".join(missing))
    result.update(tracefs_root=str(tracefs_root), tracefs_mounted=True)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cgroup-root", type=Path, default=Path("/sys/fs/cgroup/allox"))
    parser.add_argument("--tracefs-root", type=Path, default=Path("/sys/kernel/tracing"))
    args = parser.parse_args(argv)
    try:
        result = prepare_process_tracking_kernel(
            cgroup_root=args.cgroup_root,
            tracefs_root=args.tracefs_root,
        )
    except (WorkspaceError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"allox-process-tree-bootstrap: {exc}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
