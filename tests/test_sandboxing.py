"""Allox OS in-Guest Agent sandbox boundary tests."""

from allox.runtime.sandboxing import build_bwrap_argv


def test_tracked_sandbox_has_session_view_and_no_capabilities():
    current = "/var/lib/allox/workspaces/agents/a/workspace/sessions/s/current"
    shared = "/var/lib/allox/workspaces/agents/a/workspace/shared"

    argv = build_bwrap_argv(current, shared, "a", "s", ("sh", "-c", "pwd"))

    assert argv[argv.index("--bind") + 1 : argv.index("--bind") + 3] == [
        current,
        "/workspace",
    ]
    assert argv[argv.index(shared) : argv.index(shared) + 2] == [shared, "/agent/shared"]
    assert argv[argv.index("/tmp") - 1 : argv.index("/tmp") + 1] == ["--tmpfs", "/tmp"]
    assert argv[argv.index("/proc") - 1 : argv.index("/proc") + 1] == ["--proc", "/proc"]
    assert "--unshare-pid" in argv
    assert "--unshare-user" not in argv
    cap_index = argv.index("--cap-drop")
    assert argv[cap_index : cap_index + 2] == ["--cap-drop", "ALL"]
    assert "/sys/fs/cgroup" not in argv
    assert "/sys/kernel/tracing" not in argv
