"""Trusted Allox Guest kernel bootstrap tests."""

from __future__ import annotations

import errno
import subprocess
from pathlib import Path

import pytest

from allox.runtime.bootstrap import prepare_process_tracking_kernel
from allox.workspace.store import WorkspaceError


def _mountinfo(cgroup_mount: Path, tracefs_root: Path, *, read_only: bool) -> str:
    mode = "ro" if read_only else "rw"
    return (
        f"20 1 0:20 / {cgroup_mount} {mode},nosuid - cgroup2 cgroup {mode}\n"
        f"21 1 0:21 / {tracefs_root} rw,nosuid - tracefs tracefs rw\n"
    )


def _kernel_tree(tmp_path: Path, *, read_only: bool = False):
    cgroup_mount = tmp_path / "cgroup"
    cgroup_root = cgroup_mount / "allox"
    tracefs_root = tmp_path / "tracing"
    cgroup_root.mkdir(parents=True)
    (cgroup_root / "cgroup.kill").touch()
    for event in ("sched_process_fork", "sched_process_exec", "sched_process_exit"):
        path = tracefs_root / "events" / "sched" / event
        path.mkdir(parents=True, exist_ok=True)
        (path / "id").write_text("1")
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(_mountinfo(cgroup_mount, tracefs_root, read_only=read_only))
    return cgroup_mount, cgroup_root, tracefs_root, mountinfo


def test_bootstrap_accepts_ready_guest_kernel(tmp_path):
    _, cgroup_root, tracefs_root, mountinfo = _kernel_tree(tmp_path)
    calls = []

    result = prepare_process_tracking_kernel(
        cgroup_root=cgroup_root,
        tracefs_root=tracefs_root,
        mountinfo=mountinfo,
        runner=lambda argv, **kwargs: calls.append((argv, kwargs)),
        platform="linux",
    )

    assert result["cgroup_root"] == str(cgroup_root)
    assert not result["cgroup_remounted_rw"]
    assert calls == []


def test_bootstrap_remounts_read_only_cgroup2(tmp_path):
    cgroup_mount, cgroup_root, tracefs_root, mountinfo = _kernel_tree(
        tmp_path, read_only=True
    )
    calls = []

    result = prepare_process_tracking_kernel(
        cgroup_root=cgroup_root,
        tracefs_root=tracefs_root,
        mountinfo=mountinfo,
        runner=lambda argv, **kwargs: calls.append((argv, kwargs)),
        platform="linux",
    )

    assert result["cgroup_remounted_rw"]
    assert calls == [
        (["mount", "-o", "remount,rw", str(cgroup_mount)], {"check": True})
    ]


def test_bootstrap_retries_when_oci_mount_attribute_is_read_only(tmp_path, monkeypatch):
    cgroup_mount, cgroup_root, tracefs_root, mountinfo = _kernel_tree(tmp_path)
    calls = []
    original_mkdir = Path.mkdir
    failed = False

    def mkdir(path, *args, **kwargs):
        nonlocal failed
        if path == cgroup_root and not failed:
            failed = True
            raise OSError(errno.EROFS, "read-only OCI mount")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    result = prepare_process_tracking_kernel(
        cgroup_root=cgroup_root,
        tracefs_root=tracefs_root,
        mountinfo=mountinfo,
        runner=lambda argv, **kwargs: calls.append((argv, kwargs)),
        platform="linux",
    )

    assert result["cgroup_remounted_rw"]
    assert calls == [
        (["mount", "-o", "remount,rw", str(cgroup_mount)], {"check": True})
    ]


def test_bootstrap_rejects_missing_tracepoint(tmp_path):
    _, cgroup_root, tracefs_root, mountinfo = _kernel_tree(tmp_path)
    (tracefs_root / "events" / "sched" / "sched_process_exec" / "id").unlink()

    with pytest.raises(WorkspaceError, match="sched_process_exec"):
        prepare_process_tracking_kernel(
            cgroup_root=cgroup_root,
            tracefs_root=tracefs_root,
            mountinfo=mountinfo,
            runner=subprocess.run,
            platform="linux",
        )
