"""Trusted construction of per-Session Bubblewrap command lines."""

from __future__ import annotations

from pathlib import PurePosixPath

from allox.runtime.extensions import SandboxMount
from allox.workspace.store import WorkspaceError, validate_id

_TMP_SYNC_WRAPPER = """\
set -eu
barrier_enabled=$1
shift
cp -a /workspace/.allox-tmp/. /tmp/
if [ "$barrier_enabled" = 1 ]; then
    kill -STOP "$$"
elif [ "$barrier_enabled" != 0 ]; then
    exit 125
fi
status=0
/usr/bin/env -i -- "$@" || status=$?
find /tmp -xdev \\( -type s -o -type p -o -type b -o -type c \\) -delete
rm -rf /workspace/.allox-tmp.new
install -d -m 700 /workspace/.allox-tmp.new
cp -a /tmp/. /workspace/.allox-tmp.new/
rm -rf /workspace/.allox-tmp
mv /workspace/.allox-tmp.new /workspace/.allox-tmp
exit "$status"
"""


def build_bwrap_argv(
    workspace_path: str,
    agent_shared_path: str,
    agent_id: str,
    session_id: str,
    command: tuple[str, ...],
    environment: tuple[tuple[str, str], ...] = (),
    *,
    mounts: tuple[SandboxMount, ...] = (),
    launch_barrier: bool = False,
    run_id: str | None = None,
) -> list[str]:
    """Expose only one Agent shared area and one child Session workspace."""
    validate_id("agent", agent_id)
    validate_id("session", session_id)
    if not command:
        raise WorkspaceError("workspace run requires a command after --")
    if "=" in command[0]:
        raise WorkspaceError("sandbox executable name cannot contain '='")
    if run_id is not None:
        validate_id("run", run_id)
    if any(
        not key or "=" in key or "\0" in key + value
        for key, value in environment
    ):
        raise WorkspaceError("invalid sandbox environment")
    if not isinstance(launch_barrier, bool):
        raise WorkspaceError("invalid trusted launch barrier")
    argv = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--tmpfs",
        "/",
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/sbin",
        "/sbin",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--ro-bind",
        "/etc",
        "/etc",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--bind",
        workspace_path,
        "/workspace",
        "--dir",
        "/agent",
        "--bind",
        agent_shared_path,
        "/agent/shared",
        "--tmpfs",
        "/tmp",
        "--cap-drop",
        "ALL",
        "--clearenv",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
        "--setenv",
        "HOME",
        "/",
        "--setenv",
        "TMPDIR",
        "/tmp",
    ]
    agent_environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        **dict(environment),
        "HOME": "/workspace",
        "TMPDIR": "/tmp",
        "ALLOX_AGENT_ID": agent_id,
        "ALLOX_AGENT_WORKSPACE": "/agent",
        "ALLOX_AGENT_SHARED": "/agent/shared",
        "ALLOX_SESSION_ID": session_id,
    }
    if run_id is not None:
        agent_environment["ALLOX_RUN_ID"] = run_id
    created_directories: set[str] = set()
    for mount in mounts:
        parent = str(PurePosixPath(mount.target).parent)
        if parent != "/" and parent not in created_directories:
            argv.extend(["--dir", parent])
            created_directories.add(parent)
        argv.extend([
            "--ro-bind" if mount.read_only else "--bind",
            mount.source,
            mount.target,
        ])
    argv.extend(
        [
            "--chdir",
            "/workspace",
            "/bin/sh",
            "-c",
            _TMP_SYNC_WRAPPER,
            "allox-runtime",
            "1" if launch_barrier else "0",
            *(
                f"{key}={value}"
                for key, value in agent_environment.items()
            ),
            *command,
        ]
    )
    return argv
