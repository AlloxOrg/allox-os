from __future__ import annotations

import errno
import os
import struct

import pytest

from allox.workspace import btrfs_ioctl
from allox.workspace.store import BtrfsBackend, WorkspaceError


@pytest.mark.skipif(not btrfs_ioctl.supported(), reason="Linux native Btrfs ABI")
@pytest.mark.parametrize("readonly", [False, True])
def test_snapshot_uses_source_and_destination_directory_fds(tmp_path, monkeypatch, readonly):
    source = tmp_path / "source"
    parent = tmp_path / "checkpoints"
    source.mkdir()
    parent.mkdir()
    seen = []

    def ioctl(fd, op, payload):
        source_fd, transid, flags, name = struct.unpack("=qQQ32x4040s", payload)
        assert os.path.samestat(os.fstat(fd), parent.stat())
        assert os.path.samestat(os.fstat(source_fd), source.stat())
        assert op == 0x50009417 and transid == 0
        assert flags == (2 if readonly else 0)
        assert name.rstrip(b"\0") == b"target"
        seen.extend([fd, source_fd])

    monkeypatch.setattr(btrfs_ioctl, "_ioctl", ioctl)
    BtrfsBackend().snapshot(source, parent / "target", readonly=readonly)
    assert len(seen) == 2
    for fd in seen:
        with pytest.raises(OSError) as closed:
            os.fstat(fd)
        assert closed.value.errno == errno.EBADF


@pytest.mark.skipif(not btrfs_ioctl.supported(), reason="Linux native Btrfs ABI")
def test_native_delete_names_exact_child_and_never_retries(tmp_path, monkeypatch):
    backend = BtrfsBackend()

    def ioctl(fd, op, payload):
        assert op == 0x5000940F
        assert os.path.samestat(os.fstat(fd), tmp_path.stat())
        assert struct.unpack("=q4088s", payload)[1].rstrip(b"\0") == b"discard"
        raise OSError(errno.EPERM, "denied")

    def no_retry(*args, **kwargs):
        pytest.fail("mutating ioctl failure must not invoke the CLI")

    monkeypatch.setattr(btrfs_ioctl, "_ioctl", ioctl)
    monkeypatch.setattr(backend, "_run", no_retry)
    with pytest.raises(WorkspaceError, match="deletion failed"):
        backend.delete_subvolume(tmp_path / "discard")


@pytest.mark.skipif(not btrfs_ioctl.supported(), reason="Linux native Btrfs ABI")
def test_native_snapshot_refuses_symlink_source(tmp_path):
    source = tmp_path / "source"
    source.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(WorkspaceError, match="snapshot failed"):
        BtrfsBackend().snapshot(source, tmp_path / "target", readonly=True)


def test_unsupported_arch_uses_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(btrfs_ioctl, "supported", lambda: False)
    backend = BtrfsBackend()
    commands = []
    monkeypatch.setattr(backend, "_run", commands.append)
    source, target = tmp_path / "source", tmp_path / "target"
    backend.snapshot(source, target, readonly=True)
    backend.delete_subvolume(target)
    assert commands == [
        ["btrfs", "subvolume", "snapshot", "-r", str(source), str(target)],
        ["btrfs", "subvolume", "delete", str(target)],
    ]
