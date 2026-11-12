"""Exclusive daemon ownership: admission registries/caches are not multi-writer safe."""
import os
import stat
from contextlib import contextmanager

from allox.workspace.store import WorkspaceError


@contextmanager
def exclusive_owner(path):
    import fcntl

    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise WorkspaceError("recovery ownership lock must be a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WorkspaceError("recovery store/evidence already has an active owner") from exc
        yield
    finally:
        os.close(fd)
