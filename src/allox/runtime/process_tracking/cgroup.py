"""cgroup v2 ownership and termination for Session processes."""

from __future__ import annotations

import stat
import time
from pathlib import Path

from allox.workspace.store import WorkspaceError, validate_id

_LEAF_LIMITS = {
    "cgroup.max.depth": "0",
    "cgroup.max.descendants": "0",
}


def _events(path: Path) -> dict[str, str]:
    try:
        values = dict(line.split() for line in (path / "cgroup.events").read_text().splitlines())
    except (OSError, ValueError) as exc:
        raise WorkspaceError(f"failed to read Session cgroup events: {exc}") from exc
    if values.get("populated") not in {"0", "1"} or values.get("frozen") not in {"0", "1"}:
        raise WorkspaceError("invalid Session cgroup populated/frozen state")
    return values


def _seal_leaf(path: Path) -> None:
    """Make a Session cgroup a verified leaf before target code can run."""
    for name, expected in _LEAF_LIMITS.items():
        try:
            (path / name).write_text(expected)
        except OSError as exc:
            raise WorkspaceError(f"failed to seal Session cgroup via {name}: {exc}") from exc
    for name, expected in _LEAF_LIMITS.items():
        try:
            actual = (path / name).read_text().strip()
        except OSError as exc:
            raise WorkspaceError(f"failed to verify Session cgroup {name}: {exc}") from exc
        if actual != expected:
            raise WorkspaceError(f"Session cgroup {name} did not retain required value {expected}")


def _set_frozen(path: Path, frozen: bool, timeout: float = 5.0) -> None:
    expected = "1" if frozen else "0"
    try:
        (path / "cgroup.freeze").write_text(expected)
    except OSError as exc:
        action = "freeze" if frozen else "thaw"
        raise WorkspaceError(f"failed to {action} Session cgroup: {exc}") from exc
    deadline = time.monotonic() + timeout
    while True:
        if _events(path)["frozen"] == expected:
            return
        if time.monotonic() >= deadline:
            action = "freeze" if frozen else "thaw"
            raise WorkspaceError(f"Session cgroup {action} acknowledgement timed out")
        time.sleep(0.01)


class SessionCgroup:
    def __init__(self, root: Path):
        self.root = root.resolve()
        mount = self._find_mount(self.root)
        if mount is None:
            raise WorkspaceError(f"process cgroup root is not below a cgroup v2 mount: {self.root}")
        self.mount = mount
        if self.root == mount or self.root == Path("/sys/fs/cgroup"):
            raise WorkspaceError("use a dedicated cgroup subtree, not the cgroup mount root")
        self.root.mkdir(parents=True, exist_ok=True)
        if not (self.root / "cgroup.kill").exists():
            raise WorkspaceError("Session execution requires cgroup v2 cgroup.kill (Linux 5.14+)")

    @staticmethod
    def _find_mount(path: Path) -> Path | None:
        candidates = []
        try:
            lines = Path("/proc/self/mountinfo").read_text().splitlines()
        except OSError:
            return None
        for line in lines:
            try:
                left, right = line.split(" - ", 1)
                if right.split()[0] != "cgroup2":
                    continue
                raw_mount = left.split()[4]
            except (IndexError, ValueError):
                continue
            mount = Path(
                raw_mount.replace("\\040", " ")
                .replace("\\011", "\t")
                .replace("\\012", "\n")
                .replace("\\134", "\\")
            ).resolve()
            if path == mount or mount in path.parents:
                candidates.append(mount)
        return max(candidates, key=lambda item: len(item.parts), default=None)

    def path(self, agent_id: str, session_id: str) -> Path:
        validate_id("agent", agent_id)
        validate_id("session", session_id)
        return self.root / "agents" / agent_id / "sessions" / session_id

    def attach(self, agent_id: str, session_id: str, pid: int) -> Path:
        if pid <= 0:
            raise WorkspaceError("cannot attach an invalid pid to a Session cgroup")
        target = self.path(agent_id, session_id)
        target.mkdir(parents=True, exist_ok=True)
        if self.populated(agent_id, session_id):
            raise WorkspaceError(
                "Session cgroup already contains processes; recover it before launch"
            )
        _set_frozen(target, False)
        _seal_leaf(target)
        try:
            (target / "cgroup.procs").write_text(str(pid))
        except OSError as exc:
            raise WorkspaceError(f"failed to attach process to Session cgroup: {exc}") from exc
        return target

    def pids(self, agent_id: str, session_id: str) -> list[int]:
        path = self.path(agent_id, session_id) / "cgroup.procs"
        try:
            return [int(value) for value in path.read_text().split()]
        except FileNotFoundError:
            return []

    def populated(self, agent_id: str, session_id: str) -> bool:
        # cgroup.procs omits nested cgroups and cannot prove an entire tree empty.
        path = self.path(agent_id, session_id) / "cgroup.events"
        if not path.exists():
            return False
        values = _events(path.parent)
        return values["populated"] == "1"

    def identity(self, agent_id: str, session_id: str) -> dict[str, int | str]:
        """Return canonical filesystem and mount-relative kernel cgroup identity."""
        target = self.path(agent_id, session_id)
        try:
            metadata = target.lstat()
        except OSError as exc:
            raise WorkspaceError(f"failed to resolve Session cgroup identity: {exc}") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise WorkspaceError("Session cgroup must be a real directory")
        canonical = target.resolve()
        if canonical != target or metadata.st_ino <= 0:
            raise WorkspaceError("Session cgroup has an invalid canonical kernel identity")
        try:
            relative = "/" + canonical.relative_to(self.mount).as_posix()
        except ValueError as exc:
            raise WorkspaceError("Session cgroup escaped its cgroup v2 mount") from exc
        return {"path": str(canonical), "relative_path": relative, "id": metadata.st_ino}

    def freeze(
        self,
        agent_id: str,
        session_id: str,
        *,
        expected_cgroup_id: int,
        require_populated: bool,
        timeout: float = 5.0,
    ) -> dict[str, int | str]:
        identity = self.identity(agent_id, session_id)
        if identity["id"] != expected_cgroup_id:
            raise WorkspaceError("Session cgroup identity changed before freeze")
        target = self.path(agent_id, session_id)
        _set_frozen(target, True, timeout)
        values = _events(target)
        if require_populated and values["populated"] != "1":
            raise WorkspaceError("Session cgroup became empty before Observer readiness")
        final = self.identity(agent_id, session_id)
        if final != identity:
            raise WorkspaceError("Session cgroup identity changed across freeze barrier")
        return final

    def thaw(
        self,
        agent_id: str,
        session_id: str,
        *,
        expected_cgroup_id: int,
        timeout: float = 5.0,
    ) -> None:
        identity = self.identity(agent_id, session_id)
        if identity["id"] != expected_cgroup_id:
            raise WorkspaceError("Session cgroup identity changed before thaw")
        _set_frozen(self.path(agent_id, session_id), False, timeout)
        if self.identity(agent_id, session_id) != identity:
            raise WorkspaceError("Session cgroup identity changed across thaw barrier")

    def quiesce_empty(
        self,
        agent_id: str,
        session_id: str,
        *,
        expected_cgroup_id: int,
        timeout: float = 5.0,
    ) -> dict[str, int | str]:
        """Freeze an empty leaf and verify the exact cgroup cannot still execute."""
        if not self.wait_empty(agent_id, session_id, timeout):
            raise WorkspaceError("Session cgroup did not become empty before finalization")
        identity = self.freeze(
            agent_id,
            session_id,
            expected_cgroup_id=expected_cgroup_id,
            require_populated=False,
            timeout=timeout,
        )
        if _events(self.path(agent_id, session_id))["populated"] != "0":
            raise WorkspaceError("Session cgroup repopulated during finalization")
        return identity

    def kill(self, agent_id: str, session_id: str) -> None:
        target = self.path(agent_id, session_id)
        if not target.exists():
            return
        kill_file = target / "cgroup.kill"
        if not kill_file.exists():
            raise WorkspaceError("Session termination requires cgroup v2 cgroup.kill")
        try:
            kill_file.write_text("1")
        except OSError as exc:
            raise WorkspaceError(f"failed to kill Session cgroup: {exc}") from exc

    def wait_empty(self, agent_id: str, session_id: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.populated(agent_id, session_id):
                return True
            time.sleep(0.02)
        return not self.populated(agent_id, session_id)

    def remove(self, agent_id: str, session_id: str) -> bool:
        target = self.path(agent_id, session_id)
        if not target.exists():
            return True
        if self.populated(agent_id, session_id):
            return False
        try:
            _set_frozen(target, False)
            for child in sorted(
                (p for p in target.rglob("*") if p.is_dir()),
                key=lambda p: len(p.parts),
                reverse=True,
            ):
                child.rmdir()
            target.rmdir()
            target.parent.rmdir()
            target.parent.parent.rmdir()
        except OSError:
            return not target.exists()
        return not target.exists()

    def owns(self, agent_id: str, session_id: str, pid: int) -> bool:
        """Check exact kernel membership before acting on a possibly reused PID."""
        expected = self.path(agent_id, session_id).relative_to(self.mount).as_posix()
        try:
            lines = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
        except FileNotFoundError:
            return False
        return any(line == f"0::/{expected}" for line in lines)
