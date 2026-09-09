"""Prepare kernel facilities owned by the trusted Allox OS guest service."""

from __future__ import annotations

import argparse
import errno
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from allox.workspace.store import WorkspaceError

MountRunner = Callable[..., subprocess.CompletedProcess]


def _unescape_mount_path(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _mounts(mountinfo: Path) -> list[tuple[Path, str, set[str]]]:
    try:
        lines = mountinfo.read_text().splitlines()
    except OSError as exc:
        raise WorkspaceError(f"cannot read Linux mount table: {exc}") from exc
    result = []
    for line in lines:
        try:
            left, right = line.split(" - ", 1)
            left_fields = left.split()
            right_fields = right.split()
            mount = Path(_unescape_mount_path(left_fields[4])).resolve()
            filesystem = right_fields[0]
            options = set(left_fields[5].split(",")) | set(right_fields[2].split(","))
        except (IndexError, ValueError):
            continue
        result.append((mount, filesystem, options))
    return result


def _cgroup2_mount(root: Path, mounts: list[tuple[Path, str, set[str]]]):
    candidates = [
        item for item in mounts if item[1] == "cgroup2" and (root == item[0] or item[0] in root.parents)
    ]
    return max(candidates, key=lambda item: len(item[0].parts), default=None)


def prepare_process_tracking_kernel(
    *,
    cgroup_root: Path = Path("/sys/fs/cgroup/allox"),
    tracefs_root: Path = Path("/sys/kernel/tracing"),
    mountinfo: Path = Path("/proc/self/mountinfo"),
    runner: MountRunner = subprocess.run,
    platform: str = sys.platform,
) -> dict:
    """Prepare cgroup v2 and tracefs before starting the untrusted Agent layer.

    This function is intended for the trusted Allox OS boot/service context in
    the Kata Guest. Agent processes must never be allowed to invoke it.
    """
    if platform != "linux":
        raise WorkspaceError("Allox kernel bootstrap requires Linux")
    cgroup_root = cgroup_root.resolve()
    tracefs_root = tracefs_root.resolve()
    mounts = _mounts(mountinfo)
    cgroup_entry = _cgroup2_mount(cgroup_root, mounts)
    if cgroup_entry is None:
        raise WorkspaceError(f"no cgroup v2 mount contains {cgroup_root}")
    cgroup_mount, _, cgroup_options = cgroup_entry
    if cgroup_root == cgroup_mount:
        raise WorkspaceError("Allox requires a dedicated cgroup subtree")
    remounted = "rw" not in cgroup_options

    def remount_cgroup2() -> None:
        runner(
            ["mount", "-o", "remount,rw", str(cgroup_mount)],
            check=True,
        )

    if remounted:
        remount_cgroup2()
    try:
        cgroup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        # Kata/OCI can apply a read-only mount attribute that is not reflected
        # in the cgroup2 superblock options reported by mountinfo. Probe the
        # operation itself, then remount once in the trusted Guest context.
        if remounted or exc.errno not in {errno.EROFS, errno.EACCES, errno.EPERM}:
            raise WorkspaceError(f"cannot create Allox cgroup subtree: {exc}") from exc
        remount_cgroup2()
        remounted = True
        try:
            cgroup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as retry_exc:
            raise WorkspaceError(
                f"cannot create Allox cgroup subtree after remount: {retry_exc}"
            ) from retry_exc
    if not (cgroup_root / "cgroup.kill").exists():
        raise WorkspaceError("Allox requires cgroup v2 cgroup.kill (Linux 5.14+)")

    tracefs_root.mkdir(parents=True, exist_ok=True)
    tracefs_mounted = any(
        mount == tracefs_root and filesystem == "tracefs"
        for mount, filesystem, _ in mounts
    )
    if not tracefs_mounted:
        runner(
            ["mount", "-t", "tracefs", "tracefs", str(tracefs_root)],
            check=True,
        )
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
    return {
        "cgroup_mount": str(cgroup_mount),
        "cgroup_root": str(cgroup_root),
        "cgroup_remounted_rw": remounted,
        "tracefs_root": str(tracefs_root),
        "tracefs_mounted": True,
    }


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
        parser.exit(1, f"allox-guest-bootstrap: {exc}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
