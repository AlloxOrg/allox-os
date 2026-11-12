"""Bounded, no-follow filesystem assertions inspired by ZDTM's test oracle.

Compares observable semantics, not inode numbers, timestamps or raw OS bytes.
Reads are descriptor-relative: a symlink in a restored tree cannot escape it.
"""
import hashlib
import os
import stat
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from allox.workspace.store import WorkspaceError

MAX_ENTRIES = 10000
MAX_BYTES = 256 * 1024 * 1024


def tree_manifest(root, *, scrub_runtime=False, executor=None):
    rows, hardlinks = {}, {}
    remaining = MAX_BYTES
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY

    def hash_batch(batch):
        results = []
        for index, (child, info, path) in enumerate(batch):
            try:
                opened = os.fstat(child)
                if (not stat.S_ISREG(opened.st_mode)
                        or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mode,
                            opened.st_uid, opened.st_gid) !=
                        (info.st_dev, info.st_ino, info.st_size, info.st_mode, info.st_uid, info.st_gid)):
                    raise WorkspaceError("file changed during assertion")
                digest = hashlib.sha256()
                size = 0
                while chunk := os.read(child, 65536):
                    size += len(chunk)
                    if size > info.st_size:
                        raise WorkspaceError("file changed during assertion")
                    digest.update(chunk)
                after = os.fstat(child)
                if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
                        after.st_size, after.st_mtime_ns, after.st_ctime_ns) or size != info.st_size:
                    raise WorkspaceError("file changed during assertion")
                results.append((path, size, digest.hexdigest()))
            except BaseException:
                for leftover, _, _ in batch[index + 1:]:
                    os.close(leftover)
                raise
            finally:
                os.close(child)
        return results

    pending, batch = deque(), []
    workers = max(1, min(4, len(os.sched_getaffinity(0))))
    pool = executor or ThreadPoolExecutor(max_workers=workers, thread_name_prefix="allox-assert")

    def collect():
        for path, size, digest in pending.popleft().result():
            rows[path].update(size=size, sha256=digest)

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
            item: dict[str, Any] = {"mode": stat.S_IMODE(mode), "uid": info.st_uid, "gid": info.st_gid}
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
                remaining -= info.st_size
                if remaining < 0:
                    raise WorkspaceError("restore assertion byte limit exceeded")
                child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                item["type"] = "file"
                inode = (info.st_dev, info.st_ino)
                item["link_group"] = hardlinks.setdefault(inode, path)
                batch.append((child, info, path))
            elif stat.S_ISSOCK(mode):
                item["type"] = "socket-node"
            elif stat.S_ISFIFO(mode):
                item["type"] = "fifo-node"
            else:
                raise WorkspaceError("unsupported special file in assertion scope")
            if len(rows) >= MAX_ENTRIES:
                raise WorkspaceError("restore assertion entry limit exceeded")
            rows[path] = item
            if len(batch) == 8:
                pending.append(pool.submit(hash_batch, batch[:]))
                batch.clear()
                if len(pending) >= workers:
                    collect()

    fd = os.open(root, flags)
    try:
        visit(fd)
        if batch:
            pending.append(pool.submit(hash_batch, batch[:]))
            batch.clear()
        while pending:
            collect()
    finally:
        os.close(fd)
        for child, _, _ in batch:
            os.close(child)
        if executor is None:
            pool.shutdown(wait=True)
        else:
            # A failed scan must drain its submitted jobs before returning;
            # their FDs belong to those jobs, not to the caller's next scan.
            for future in pending:
                try:
                    future.result()
                except Exception:
                    pass
    return rows


def compare(expected, actual):
    keys = sorted(expected.keys() | actual.keys())
    changed = [k for k in keys if expected.get(k) != actual.get(k)]
    return {"status": "INCONSISTENT_WITHIN_SCOPE" if changed else "CONSISTENT_WITHIN_SCOPE",
            "changed_count": len(changed), "changed_paths": changed[:100]}
