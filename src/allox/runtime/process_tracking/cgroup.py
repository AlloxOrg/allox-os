"""cgroup v2 ownership and termination for Session processes."""

from __future__ import annotations

import time
from pathlib import Path

from allox.workspace.store import WorkspaceError, validate_id


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
            raise WorkspaceError("process tracking requires cgroup v2 cgroup.kill (Linux 5.14+)")

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
        try:
            values = dict(line.split() for line in path.read_text().splitlines())
        except FileNotFoundError:
            return False
        if values.get("populated") not in {"0", "1"}:
            raise WorkspaceError("invalid cgroup.events populated state")
        return values["populated"] == "1"

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

    def remove(self, agent_id: str, session_id: str) -> None:
        target = self.path(agent_id, session_id)
        if not target.exists() or self.populated(agent_id, session_id):
            return
        try:
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
            pass

    def owns(self, agent_id: str, session_id: str, pid: int) -> bool:
        """Check exact kernel membership before acting on a possibly reused PID."""
        expected = self.path(agent_id, session_id).relative_to(self.mount).as_posix()
        try:
            lines = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
        except FileNotFoundError:
            return False
        return any(line == f"0::/{expected}" for line in lines)
