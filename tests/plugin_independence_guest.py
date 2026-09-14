"""Real Guest checks proving share/network plugins do not require process tracing."""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path

from allox.workspace.daemon import WorkspaceService
from allox.workspace.store import WorkspaceStore


class DirectoryBackend:
    def assert_root(self, root):
        pass

    def create_subvolume(self, path):
        path.mkdir()

    def snapshot(self, source, target, *, readonly):
        shutil.copytree(source, target)

    def delete_subvolume(self, path):
        shutil.rmtree(path)


def wait(service, agent, session, row):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = service.processes.get(agent, session, row["run_id"])
        if result["state"] not in {"starting", "running"}:
            stdout = (Path(result["audit_directory"]) / "stdout.log").read_bytes()
            stderr = (Path(result["audit_directory"]) / "stderr.log").read_text()
            assert result["root_exit_code"] == 0, (result, stderr)
            assert result["backend"] == "disabled"
            assert result["trace_complete"] is False
            assert result["lifecycle_complete"] is True
            return stdout
        time.sleep(0.02)
    raise RuntimeError("Session execution timed out")


def run(service, agent, session, command):
    row = service.processes.start(agent, session, ["/bin/sh", "-ec", command], timeout=20)
    return wait(service, agent, session, row)


def store_at(root):
    store_root = root / "store"
    store_root.mkdir()
    store = WorkspaceStore(store_root, DirectoryBackend())
    store.initialize()
    return store


def share_only(root):
    assert importlib.util.find_spec("allox_session_share") is not None
    assert importlib.util.find_spec("allox_session_network") is None
    assert importlib.util.find_spec("allox_process_tree") is None
    store = store_at(root)
    for agent in ("a", "b"):
        store.create_session(agent, "s")
    service = WorkspaceService(
        store,
        process_audit_root=root / "audit",
        process_cgroup_root=Path("/sys/fs/cgroup/allox-plugin-share-only"),
        share_tools=True,
        share_socket_root=Path("/dev/shm/allox-plugin-share-only"),
    )
    try:
        run(
            service,
            "b",
            "s",
            "mkdir output; printf plugin-only > output/report; "
            "python3 /run/allox/share.py enable --scope output",
        )
        output = run(
            service,
            "a",
            "s",
            "python3 /run/allox/share.py enable >/dev/null; "
            "python3 /run/allox/share.py read b/s report",
        )
        assert output == b"plugin-only"
        assert service.feature_names == ("session-share",)
        assert service.dispatch("process.status", {})["enabled"] is False
    finally:
        service.close()
    print("SHARE_ONLY_PASS")


def network_only(root):
    assert importlib.util.find_spec("allox_session_network") is not None
    assert importlib.util.find_spec("allox_session_share") is None
    assert importlib.util.find_spec("allox_process_tree") is None
    store = store_at(root)
    store.create_session("agent", "s")
    service = WorkspaceService(
        store,
        process_audit_root=root / "audit",
        process_cgroup_root=Path("/sys/fs/cgroup/allox-plugin-network-only"),
        session_network="proxy",
        network_root=root / "network",
        network_socket_root=Path("/dev/shm/allox-plugin-network-only"),
    )
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def echo():
        stream, _ = listener.accept()
        data = stream.recv(1024)
        stream.sendall(b"network-plugin:" + data)
        stream.close()

    thread = threading.Thread(target=echo)
    thread.start()
    try:
        output = run(
            service,
            "agent",
            "s",
            "printf independent | python3 /run/allox/network.py connect 127.0.0.1 "
            + str(port),
        )
        assert output == b"network-plugin:independent"
        status = service.dispatch(
            "network.status", {"agent_id": "agent", "session_id": "s"}
        )
        assert status["active"] and status["mode"] == "proxy"
        assert service.feature_names == ("session-network",)
        assert service.dispatch("process.status", {})["enabled"] is False
    finally:
        service.close()
        listener.close()
        thread.join(timeout=2)
    print("NETWORK_ONLY_PASS")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("share", "network"))
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="allox-plugin-independent-") as directory:
        if args.mode == "share":
            share_only(Path(directory))
        else:
            network_only(Path(directory))


if __name__ == "__main__":
    main()
