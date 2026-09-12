"""Trusted construction of per-Session Bubblewrap command lines."""

from __future__ import annotations

from pathlib import Path

from allox.workspace.store import WorkspaceError, validate_id

_TMP_SYNC_WRAPPER = """\
set -eu
cp -a /workspace/.allox-tmp/. /tmp/
status=0
"$@" || status=$?
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
    share_socket: str | None = None,
) -> list[str]:
    """Expose only one Agent shared area and one child Session workspace."""
    validate_id("agent", agent_id)
    validate_id("session", session_id)
    if not command:
        raise WorkspaceError("workspace run requires a command after --")
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
        "/usr/local/bin:/usr/bin:/bin",
        "--setenv",
        "HOME",
        "/workspace",
        "--setenv",
        "TMPDIR",
        "/tmp",
        "--setenv",
        "ALLOX_AGENT_ID",
        agent_id,
        "--setenv",
        "ALLOX_AGENT_WORKSPACE",
        "/agent",
        "--setenv",
        "ALLOX_AGENT_SHARED",
        "/agent/shared",
        "--setenv",
        "ALLOX_SESSION_ID",
        session_id,
    ]
    for key, value in environment:
        argv.extend(["--setenv", key, value])
    if share_socket is not None:
        argv.extend([
            "--dir", "/run/allox",
            "--ro-bind", share_socket, "/run/allox/share.sock",
            "--ro-bind", str(Path(__file__).with_name("share_tool.py")), "/run/allox/share.py",
        ])
    argv.extend(
        ["--chdir", "/workspace", "sh", "-c", _TMP_SYNC_WRAPPER, "allox-runtime", *command]
    )
    return argv
