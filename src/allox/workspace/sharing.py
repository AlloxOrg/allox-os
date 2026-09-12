"""ID-based Session sharing, served only by the trusted workspace daemon."""

from __future__ import annotations

import base64
import binascii
import json
import os
import stat
import threading
import uuid
from contextlib import contextmanager

from allox.workspace.store import WorkspaceError, validate_id

MAX_BYTES = 256 * 1024
MAX_ENTRIES = 1000


def relative_parts(path):
    if not isinstance(path, str) or not path or "\0" in path or "\\" in path:
        raise WorkspaceError("path must be a relative POSIX path")
    if path == ".":
        return []
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise WorkspaceError("absolute paths and dot components are not allowed")
    return parts


class ShareService:
    """Opt-in mutual sharing. No bearer keys; caller identity is supplied by the transport."""

    def __init__(self, store, executions):
        self.store = store
        self.executions = executions
        self._lock = threading.RLock()

    def _policy_path(self, identity):
        agent, session = identity
        if not isinstance(agent, str) or not isinstance(session, str):
            raise WorkspaceError("Agent and Session IDs must be strings")
        validate_id("agent", agent)
        validate_id("session", session)
        return self.store.root / ".allox" / "shares" / agent / (session + ".json")

    def _policy(self, identity):
        path = self._policy_path(identity)
        self.store.describe(*identity)
        if not path.exists():
            return {"enabled": False, "scope": ".", "permission": "read"}
        return json.loads(path.read_text(encoding="utf-8"))

    def dispatch(self, caller, action, params):
        # Serializes configure/disable with the complete access operation.
        with self._lock:
            policy = self._policy(caller)
            if action == "status":
                return {"agent_id": caller[0], "session_id": caller[1], **policy}
            if action in {"enable", "disable"}:
                if action == "enable":
                    scope = params.get("scope", ".")
                    permission = params.get("permission", "read")
                    relative_parts(scope)
                    if permission not in {"read", "write"}:
                        raise WorkspaceError("permission must be read or write")
                    with (
                        self.store._session_lock(*caller),
                        self._open(caller, relative_parts(scope), directory=True),
                    ):
                        pass
                    policy = {"enabled": True, "scope": scope, "permission": permission}
                else:
                    policy["enabled"] = False
                self.store._atomic_write_json(self._policy_path(caller), policy)
                self.store._append_event({"op": "share." + action, **policy}, *caller)
                return {"agent_id": caller[0], "session_id": caller[1], **policy}
            if action not in {"list", "read", "write"}:
                raise WorkspaceError("unknown share action")
            target = (params.get("target_agent_id"), params.get("target_session_id"))
            self._policy_path(target)
            path = params.get("path", ".")
            relative_parts(path)
            audit = {
                "op": "share." + action,
                "caller_agent_id": caller[0], "caller_session_id": caller[1],
                "path": path,
            }
            try:
                target_policy = self._policy(target)
                if not policy["enabled"] or not target_policy["enabled"]:
                    raise WorkspaceError("both Sessions must enable share")
                if action == "write" and target_policy["permission"] != "write":
                    raise WorkspaceError("target share is read-only")
                # Reads may observe a live writer, but cannot overlap a workspace swap.
                # Writes additionally require the target to have no active execution.
                with (
                    self.executions.share_access(*target, write=action == "write"),
                    self.store._session_lock(*target),
                ):
                    result = self._access(target, target_policy, action, params, audit)
                audit["result"] = "success"
                self.store._append_event(audit, *target)
                return result
            except (OSError, WorkspaceError) as exc:
                audit["result"] = "denied_or_failed"
                self.store._append_event(audit, *target)
                if isinstance(exc, WorkspaceError):
                    raise
                raise WorkspaceError("shared file unavailable or unsupported") from exc

    @contextmanager
    def _open(self, target, parts, *, directory=False):
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
            raise WorkspaceError("share file access requires Linux")
        # No symlink traversal, including intermediate components. Directory FDs
        # avoid check-then-open path substitution; all agents lack mount privileges.
        fd = os.open(self.store.current(*target), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            device = os.fstat(fd).st_dev
            for index, part in enumerate(parts):
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if directory or index < len(parts) - 1:
                    flags |= os.O_DIRECTORY
                next_fd = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = next_fd
                if os.fstat(fd).st_dev != device:
                    raise WorkspaceError("cross-filesystem sharing is not supported")
            yield fd
        finally:
            os.close(fd)

    def _access(self, target, policy, action, params, audit):
        parts = relative_parts(policy["scope"]) + relative_parts(params.get("path", "."))
        if action == "list":
            with self._open(target, parts, directory=True) as fd:
                entries = []
                with os.scandir(fd) as iterator:
                    for entry in iterator:
                        if len(entries) == MAX_ENTRIES:
                            raise WorkspaceError("directory exceeds 1000 entries; list a subdirectory")
                        info = entry.stat(follow_symlinks=False)
                        kind = ("directory" if stat.S_ISDIR(info.st_mode) else
                                "file" if stat.S_ISREG(info.st_mode) else "unsupported")
                        entries.append({"name": entry.name, "type": kind, "size": info.st_size})
                return {"entries": sorted(entries, key=lambda item: item["name"])}
        if not relative_parts(params.get("path", ".")):
            raise WorkspaceError("read/write requires a file path")
        if action == "read":
            with self._open(target, parts) as fd:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise WorkspaceError("only regular files without hard links can be shared")
                if info.st_size > MAX_BYTES:
                    raise WorkspaceError("file exceeds 256 KiB")
                with os.fdopen(os.dup(fd), "rb") as stream:
                    data = stream.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise WorkspaceError("file exceeds 256 KiB")
            return {"data_base64": base64.b64encode(data).decode("ascii"), "size": len(data)}
        encoded = params.get("data_base64")
        if not isinstance(encoded, str) or len(encoded) > (MAX_BYTES + 2) // 3 * 4:
            raise WorkspaceError("write requires base64 data up to 256 KiB")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise WorkspaceError("invalid base64 data") from exc
        if len(data) > MAX_BYTES:
            raise WorkspaceError("file exceeds 256 KiB")
        with self._open(target, parts[:-1], directory=True) as parent:
            try:
                old = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                old = None
            if old and (not stat.S_ISREG(old.st_mode) or old.st_nlink != 1):
                raise WorkspaceError("cannot replace a link, directory or special file")
            temporary = ".allox-share-" + uuid.uuid4().hex
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                self.store._append_event({
                    **audit, "op": "share.write_prepared", "size": len(data),
                }, *target)
                os.replace(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
        return {"written": len(data)}
