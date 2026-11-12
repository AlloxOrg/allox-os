"""Bounded, no-follow filesystem assertions inspired by ZDTM's test oracle.

Compares observable semantics, not inode numbers, timestamps or raw OS bytes.
Reads are descriptor-relative: a symlink in a restored tree cannot escape it.
"""
import hashlib
import os
import stat

from allox.workspace.store import WorkspaceError

MAX_ENTRIES = 10000
MAX_BYTES = 256 * 1024 * 1024


def tree_manifest(root, *, scrub_runtime=False):
    rows, hardlinks = {}, {}
    remaining = MAX_BYTES
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY

    def visit(fd, prefix=""):
        nonlocal remaining
        names = os.listdir(fd)
        if len(rows) + len(names) > MAX_ENTRIES:
            raise WorkspaceError("restore assertion entry limit exceeded")
        for name in sorted(names):
            path = f"{prefix}/{name}" if prefix else name
            if path.count("/") > 64 or len(path) > 4096:
                raise WorkspaceError("restore assertion path limit exceeded")
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            mode = info.st_mode
            # Explicit cleanup policy: only sockets/FIFOs under Session tmp.
            if (scrub_runtime and path.startswith(".allox-tmp/")
                    and (stat.S_ISSOCK(mode) or stat.S_ISFIFO(mode))):
                continue
            item = {"mode": stat.S_IMODE(mode), "uid": info.st_uid, "gid": info.st_gid}
            if stat.S_ISDIR(mode):
                item["type"] = "directory"
                child = os.open(name, flags, dir_fd=fd)
                try:
                    visit(child, path)
                finally:
                    os.close(child)
            elif stat.S_ISLNK(mode):
                item.update(type="symlink", target=os.readlink(name, dir_fd=fd))
            elif stat.S_ISREG(mode):
                child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                try:
                    opened = os.fstat(child)
                    if (not stat.S_ISREG(opened.st_mode)
                            or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)):
                        raise WorkspaceError("file changed during assertion")
                    digest = hashlib.sha256()
                    size = 0
                    while True:
                        chunk = os.read(child, min(65536, max(1, remaining + 1)))
                        if not chunk:
                            break
                        size += len(chunk)
                        remaining -= len(chunk)
                        if remaining < 0:
                            raise WorkspaceError("restore assertion byte limit exceeded")
                        digest.update(chunk)
                    after = os.fstat(child)
                    if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
                            after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                        raise WorkspaceError("file changed during assertion")
                    item.update(type="file", size=size, sha256=digest.hexdigest())
                    inode = (info.st_dev, info.st_ino)
                    item["link_group"] = hardlinks.setdefault(inode, path)
                finally:
                    os.close(child)
            elif stat.S_ISSOCK(mode):
                item["type"] = "socket-node"
            elif stat.S_ISFIFO(mode):
                item["type"] = "fifo-node"
            else:
                raise WorkspaceError("unsupported special file in assertion scope")
            if len(rows) >= MAX_ENTRIES:
                raise WorkspaceError("restore assertion entry limit exceeded")
            rows[path] = item

    fd = os.open(root, flags)
    try:
        visit(fd)
    finally:
        os.close(fd)
    return rows


def compare(expected, actual):
    keys = sorted(expected.keys() | actual.keys())
    changed = [k for k in keys if expected.get(k) != actual.get(k)]
    return {"status": "INCONSISTENT_WITHIN_SCOPE" if changed else "CONSISTENT_WITHIN_SCOPE",
            "changed_count": len(changed), "changed_paths": changed[:100]}
