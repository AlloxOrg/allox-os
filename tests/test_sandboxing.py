"""Allox OS in-Guest Agent sandbox boundary tests."""

import pytest

from allox.runtime.extensions import SandboxMount
from allox.runtime.sandboxing import _TMP_SYNC_WRAPPER, build_bwrap_argv
from allox.workspace.store import WorkspaceError


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


def test_plugin_mount_is_added_declaratively():
    argv = build_bwrap_argv(
        "/store/current",
        "/store/shared",
        "a",
        "s",
        ("true",),
        mounts=(
            SandboxMount(
                "/dev/shm/allox-network/broker.sock",
                "/run/allox/network.sock",
            ),
        ),
    )
    assert argv[argv.index("/dev/shm/allox-network/broker.sock") :][:2] == [
        "/dev/shm/allox-network/broker.sock",
        "/run/allox/network.sock",
    ]


def test_launch_barrier_runs_inside_final_sandbox_before_workload():
    command = ("python3", "-c", "print('started')")

    argv = build_bwrap_argv(
        "/store/current",
        "/store/shared",
        "a",
        "s",
        command,
        launch_barrier=True,
    )

    shell = argv.index("/bin/sh")
    assert argv[shell : shell + 5] == [
        "/bin/sh",
        "-c",
        _TMP_SYNC_WRAPPER,
        "allox-runtime",
        "1",
    ]
    assert argv[shell + 5 : shell + 12] == [
        "PATH=/usr/local/bin:/usr/bin:/bin",
        "HOME=/workspace",
        "TMPDIR=/tmp",
        "ALLOX_AGENT_ID=a",
        "ALLOX_AGENT_WORKSPACE=/agent",
        "ALLOX_AGENT_SHARED=/agent/shared",
        "ALLOX_SESSION_ID=s",
    ]
    assert argv[shell + 12 :] == list(command)
    assert _TMP_SYNC_WRAPPER.index('kill -STOP "$$"') < _TMP_SYNC_WRAPPER.index('/usr/bin/env -i')
    assert "printf" not in _TMP_SYNC_WRAPPER
    assert ".allox-launch-" not in " ".join(argv)


def test_untrusted_environment_is_not_active_before_launch_barrier():
    argv = build_bwrap_argv(
        "/store/current",
        "/store/shared",
        "a",
        "s",
        ("agent-command",),
        (("PATH", "/workspace"), ("LD_PRELOAD", "/workspace/evil.so")),
        launch_barrier=True,
        run_id="run-1",
    )

    shell = argv.index("/bin/sh")
    wrapper_options = argv[:shell]
    assert "/workspace" not in [
        wrapper_options[index + 2]
        for index, value in enumerate(wrapper_options[:-2])
        if value == "--setenv"
    ]
    assert "LD_PRELOAD" not in wrapper_options
    delayed = argv[shell + 5 : -1]
    assert "PATH=/workspace" == delayed[0]
    assert "LD_PRELOAD=/workspace/evil.so" in delayed
    assert "ALLOX_RUN_ID=run-1" == delayed[-1]
    assert argv[-1] == "agent-command"


def test_execve_environment_names_are_not_interpreted_by_the_shell():
    argv = build_bwrap_argv(
        "/store/current",
        "/store/shared",
        "a",
        "s",
        ("agent-command",),
        (("A-B", "hyphen"), ("UID", "caller-value")),
        launch_barrier=True,
    )

    shell = argv.index("/bin/sh")
    delayed = argv[shell + 5 : -1]
    assert "A-B=hyphen" in delayed
    assert "UID=caller-value" in delayed
    assert "export" not in _TMP_SYNC_WRAPPER


def test_executable_name_that_env_would_treat_as_an_assignment_is_rejected():
    with pytest.raises(WorkspaceError, match="executable name cannot contain"):
        build_bwrap_argv(
            "/store/current",
            "/store/shared",
            "a",
            "s",
            ("name=not-a-command",),
        )


@pytest.mark.parametrize(
    "launch_barrier",
    [None, 1, "yes", ("path", "token")],
)
def test_invalid_launch_barrier_is_rejected(launch_barrier):
    with pytest.raises(WorkspaceError, match="invalid trusted launch barrier"):
        build_bwrap_argv(
            "/store/current",
            "/store/shared",
            "a",
            "s",
            ("true",),
            launch_barrier=launch_barrier,
        )
