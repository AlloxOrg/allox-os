"""Opt-in Session network namespace tests for the Allox Kata Guest on 117."""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
from test_process_tracking_kernel import DirectoryBackend

from allox.workspace.daemon import WorkspaceService
from allox.workspace.store import WorkspaceStore

pytestmark = pytest.mark.skipif(
    os.environ.get("ALLOX_NETWORK_TEST") != "1",
    reason="requires a privileged Allox Kata Guest with network namespaces",
)


@pytest.fixture
def service(tmp_path):
    store_root = tmp_path / "store"
    store_root.mkdir()
    store = WorkspaceStore(store_root, DirectoryBackend())
    store.initialize()
    for session in ("a", "b", "proxy"):
        store.create_session("agent", session)
    instance = WorkspaceService(
        store,
        process_tracking="ebpf",
        process_audit_root=tmp_path / "audit",
        process_cgroup_root=Path("/sys/fs/cgroup/allox-network-tests") / tmp_path.name,
        process_tracker_command=os.environ.get(
            "ALLOX_EBPF_COMMAND", "/src/plugins/process-tree/native/allox-process-tracker"
        ),
        session_network="isolated",
        network_root=tmp_path / "network",
        network_socket_root=Path("/dev/shm/allox-network-tests"),
    )
    yield instance
    instance.close()


def wait(service, row, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = service.processes.get("agent", row["session_id"], row["run_id"])
        if result["state"] not in {"starting", "running"}:
            return result
        time.sleep(0.02)
    pytest.fail("network workload did not finish")


def run_command(service, session, argv, timeout=20):
    row = service.processes.start(
        "agent", session, argv, timeout=timeout
    )
    result = wait(service, row, timeout + 10)
    stdout = (Path(result["audit_directory"]) / "stdout.log").read_text()
    stderr = (Path(result["audit_directory"]) / "stderr.log").read_text()
    assert result["root_exit_code"] == 0, (stdout, stderr, result)
    assert result["trace_complete"], result
    return result, stdout


def run(service, session, code, timeout=20):
    return run_command(service, session, [sys.executable, "-c", code], timeout)


def test_namespace_persists_across_workspace_rollback_and_direct_egress_is_blocked(service):
    code = (
        "import json,os,socket; "
        "blocked=False; "
        "s=socket.socket(); s.settimeout(1); "
        "\ntry: s.connect(('121.48.164.135',53434))"
        "\nexcept OSError: blocked=True"
        "\nopen('after-checkpoint','w').write('changed')"
        "\nprint(json.dumps({'netns':os.readlink('/proc/self/ns/net'),'blocked':blocked}))"
    )
    checkpoint = service.store.create_checkpoint("agent", "a", "before-network-run")
    first, first_out = run(service, "a", code)
    service.dispatch(
        "session.rollback",
        {
            "agent_id": "agent",
            "session_id": "a",
            "checkpoint_id": checkpoint["checkpoint_id"],
        },
    )
    assert not (service.store.current("agent", "a") / "after-checkpoint").exists()
    second, second_out = run(service, "a", code)
    one, two = json.loads(first_out), json.loads(second_out)
    assert one["blocked"] and two["blocked"]
    assert one["netns"] == two["netns"]
    assert first["network_namespace_pid"] == second["network_namespace_pid"]


def test_two_sessions_can_bind_the_same_port_without_visibility(service):
    server = (
        "import socket,time; s=socket.socket(); "
        "s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); "
        "s.bind(('127.0.0.1',45678)); s.listen(); print('ready',flush=True); time.sleep(2)"
    )
    rows = [
        service.processes.start("agent", session, [sys.executable, "-c", server], timeout=10)
        for session in ("a", "b")
    ]
    results = [wait(service, row) for row in rows]
    assert all(result["root_exit_code"] == 0 for result in results)
    assert results[0]["network_namespace_pid"] != results[1]["network_namespace_pid"]


def test_one_sessions_loopback_service_is_not_visible_to_another(service):
    server = (
        "import socket,time; s=socket.socket(); s.bind(('127.0.0.1',45679)); "
        "s.listen(); print('ready',flush=True); time.sleep(2)"
    )
    server_row = service.processes.start(
        "agent", "b", [sys.executable, "-c", server], timeout=10
    )
    server_stdout = Path(server_row["audit_directory"]) / "stdout.log"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if server_stdout.exists() and "ready" in server_stdout.read_text():
            break
        time.sleep(0.02)
    else:
        pytest.fail("Session B loopback server did not start")
    _, output = run(
        service,
        "a",
        "import socket; s=socket.socket(); s.settimeout(1); "
        "\ntry: s.connect(('127.0.0.1',45679)); print('visible')"
        "\nexcept OSError: print('blocked')",
    )
    assert output.strip() == "blocked"
    assert wait(service, server_row)["root_exit_code"] == 0


def test_proxy_mode_allows_brokered_http_but_not_direct_connection(service):
    service.dispatch(
        "network.configure",
        {"agent_id": "agent", "session_id": "proxy", "mode": "proxy"},
    )
    code = (
        "import json,os,socket,urllib.error,urllib.request; "
        "direct=False; s=socket.socket(); s.settimeout(1); "
        "\ntry: s.connect(('121.48.164.135',53434)); direct=True"
        "\nexcept OSError: pass"
        "\ntry:"
        "\n r=urllib.request.urlopen('http://121.48.164.135:53434/v1/models',timeout=10)"
        "\n status=r.status"
        "\nexcept urllib.error.HTTPError as e: status=e.code"
        "\nprint(json.dumps({'direct':direct,'proxy_status':status,'http_proxy':os.environ.get('HTTP_PROXY')}))"
    )
    result, output = run(service, "proxy", code)
    proof = json.loads(output)
    assert not proof["direct"]
    assert 100 <= proof["proxy_status"] <= 599
    assert proof["http_proxy"] == "http://127.0.0.1:3128"
    assert result["network_mode"] == "proxy"
    network = service.dispatch(
        "network.status", {"agent_id": "agent", "session_id": "proxy"}
    )
    assert network["active"]


def test_generic_connector_forwards_opaque_tcp_to_guest_root_namespace(service):
    service.dispatch(
        "network.configure",
        {"agent_id": "agent", "session_id": "proxy", "mode": "proxy"},
    )
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def echo():
        stream, _ = listener.accept()
        request = bytearray()
        while True:
            chunk = stream.recv(4096)
            if not chunk:
                break
            request.extend(chunk)
        stream.sendall(b"root-reply:" + request)
        stream.close()

    thread = threading.Thread(target=echo)
    thread.start()
    command = (
        f"printf opaque-session-bytes | "
        f"python3 /run/allox/network.py connect 127.0.0.1 {port}"
    )
    _, output = run_command(
        service,
        "proxy",
        [
            "/bin/sh",
            "-ec",
            command,
        ],
    )
    thread.join(timeout=2)
    listener.close()
    assert output == "root-reply:opaque-session-bytes"


def test_generic_connector_reaches_a_real_ssh_service(service):
    service.dispatch(
        "network.configure",
        {"agent_id": "agent", "session_id": "proxy", "mode": "proxy"},
    )
    command = (
        "printf 'SSH-2.0-Allox-Test\\r\\n' | "
        "timeout 15 python3 /run/allox/network.py connect ssh.github.com 443 | "
        "head -n 1 >/tmp/ssh.banner; "
        "grep -q '^SSH-2.0-' /tmp/ssh.banner; cat /tmp/ssh.banner"
    )
    _, output = run_command(
        service,
        "proxy",
        [
            "/bin/sh",
            "-ec",
            command,
        ],
        timeout=30,
    )
    assert output.startswith("SSH-2.0-")
