"""In-process Btrfs snapshot and deletion on the supported Linux UAPI."""

from __future__ import annotations

import os
import platform
import struct
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# include/uapi/linux/btrfs.h, with the asm-generic _IOW encoding used on
# x86-64 and arm64. Both request layouts are 4096 bytes; keep reserved
# fields zero. Unverified architectures continue to use btrfs-progs.
_SNAP_ARGS = struct.Struct("=qQQ32x4040s")
_DESTROY_ARGS = struct.Struct("=q4088s")
_BTRFS_SUBVOL_RDONLY = 1 << 1


def _iow(number: int, size: int) -> int:
    return (1 << 30) | (size << 16) | (0x94 << 8) | number


_SNAP_CREATE_V2 = _iow(23, _SNAP_ARGS.size)
_SNAP_DESTROY = _iow(15, _DESTROY_ARGS.size)


def supported() -> bool:
    return sys.platform == "linux" and platform.machine() in {"x86_64", "aarch64"}


def _name(path: Path) -> bytes:
    name = os.fsencode(path.name)
    if not name or name in (b".", b"..") or b"/" in name or b"\0" in name or len(name) > 255:
        raise ValueError("Btrfs ioctl requires a valid single-component subvolume name")
    return name


@contextmanager
def _directory(path: Path) -> Iterator[int]:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        yield fd
    finally:
        os.close(fd)


def _ioctl(fd: int, request: int, payload: bytes) -> None:
    import fcntl

    # A mutable buffer is required for arguments larger than fcntl's
    # internal small-buffer limit, including both of these ioctl structs.
    fcntl.ioctl(fd, request, bytearray(payload), True)


def snapshot(source: Path, target: Path, *, readonly: bool) -> None:
    name = _name(target)
    flags = _BTRFS_SUBVOL_RDONLY if readonly else 0
    with _directory(source) as source_fd, _directory(target.parent) as parent_fd:
        _ioctl(parent_fd, _SNAP_CREATE_V2, _SNAP_ARGS.pack(source_fd, 0, flags, name))


def delete_subvolume(path: Path) -> None:
    name = _name(path)
    with _directory(path.parent) as parent_fd:
        _ioctl(parent_fd, _SNAP_DESTROY, _DESTROY_ARGS.pack(0, name))
