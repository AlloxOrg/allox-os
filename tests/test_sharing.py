"""Share authorization, path confinement, persistence and mutation barriers."""

import base64
import json
import os
import shutil
from pathlib import Path

import pytest

from allox.workspace.daemon import WorkspaceService
from allox.workspace.sharing import MAX_BYTES
from allox.workspace.store import WorkspaceError, WorkspaceStore

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Linux filesystem sharing")


class DirectoryBackend:
    def assert_root(self, root):
        pass

    def create_subvolume(self, path):
        path.mkdir()

    def snapshot(self, source, target, *, readonly):
        shutil.copytree(source, target)

    def delete_subvolume(self, path):
        shutil.rmtree(path)


@pytest.fixture
def service(tmp_path):
    store = WorkspaceStore(tmp_path, DirectoryBackend())
    store.initialize()
    for agent in ("a", "b", "c"):
        store.create_session(agent, "s")
        (store.current(agent, "s") / "output").mkdir()
    result = WorkspaceService(store)
    yield result
    result.close()


def share(service, action, caller="a", target="b", **params):
    return service.dispatch("share." + action, {
        "agent_id": caller, "session_id": "s", "target_agent_id": target,
        "target_session_id": "s", **params,
    })


def enable_both(service, permission="read"):
    share(service, "enable")
    share(service, "enable", caller="b", scope="output", permission=permission)


def test_both_opt_in_and_disable_takes_effect_immediately(service):
    assert not share(service, "status")["enabled"]
    for caller_enabled, target_enabled in ((False, False), (True, False), (False, True)):
        share(service, "enable" if caller_enabled else "disable")
        share(service, "enable" if target_enabled else "disable", caller="b")
        with pytest.raises(WorkspaceError, match="both Sessions"):
            share(service, "list")
    enable_both(service)
    assert share(service, "list")["entries"] == []
    share(service, "disable", caller="b")
    with pytest.raises(WorkspaceError, match="both Sessions"):
        share(service, "read", path="anything")


def test_read_write_scope_binary_and_audit(service):
    enable_both(service)
    data = base64.b64encode(b"\0\xffhello").decode()
    with pytest.raises(WorkspaceError, match="read-only"):
        share(service, "write", path="blob", data_base64=data)
    share(service, "enable", caller="b", scope="output", permission="write")
    assert share(service, "write", path="blob", data_base64=data) == {"written": 7}
    assert share(service, "read", path="blob")["data_base64"] == data
    assert share(service, "list")["entries"][0]["name"] == "blob"
    assert (service.store.current("b", "s") / "output/blob").read_bytes() == b"\0\xffhello"
    assert not (service.store.current("a", "s") / "blob").exists()
    records = [json.loads(line) for line in service.store._event_path("b", "s").read_text().splitlines()]
    writes = [r for r in records if r["op"] == "share.write_prepared"]
    assert writes[-1]["caller_agent_id"] == "a"
    assert writes[-1]["caller_session_id"] == "s"
    assert "data_base64" not in writes[-1]


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "x/../../secret", "a//b", "./x", "a\\b", "x\0"])
def test_rejects_escaping_paths(service, path):
    enable_both(service, "write")
    for action in ("read", "list", "write"):
        with pytest.raises(WorkspaceError):
            share(service, action, path=path, data_base64="")


def test_rejects_symlinks_hardlinks_and_special_files(service, tmp_path):
    enable_both(service, "write")
    output = service.store.current("b", "s") / "output"
    secret = tmp_path / "secret"
    secret.write_text("private")
    (output / "link").symlink_to(secret)
    (output / "dirlink").symlink_to(tmp_path, target_is_directory=True)
    os.link(secret, output / "hard")
    os.mkfifo(output / "fifo")
    for path in ("link", "dirlink/secret", "hard", "fifo"):
        for action in ("read", "write"):
            with pytest.raises(WorkspaceError):
                share(service, action, path=path, data_base64="")
    assert secret.read_text() == "private"


def test_limits_and_invalid_data(service):
    enable_both(service, "write")
    (service.store.current("b", "s") / "output/big").write_bytes(b"x" * (MAX_BYTES + 1))
    with pytest.raises(WorkspaceError, match="256 KiB"):
        share(service, "read", path="big")
    with pytest.raises(WorkspaceError, match="base64"):
        share(service, "write", path="bad", data_base64="invalid!")


def test_live_reads_but_idle_target_required_for_writes(service):
    enable_both(service, "write")
    lease = service.executions.acquire("b", "s")
    assert share(service, "list")["entries"] == []
    with pytest.raises(WorkspaceError, match="active executions"):
        share(service, "write", path="new", data_base64="")
    service.executions.release(lease["lease_token"])
    share(service, "write", path="new", data_base64="")


def test_share_and_reset_mutually_exclude(service):
    enable_both(service, "write")
    with service.executions.share_access("b", "s", write=False):
        with pytest.raises(WorkspaceError, match="active share reads"):
            service.executions.begin_runtime_reset("b", "s")
        with (
            pytest.raises(WorkspaceError, match="active share reads"),
            service.executions.mutation("b", "s"),
        ):
            pass
    reset = service.executions.begin_runtime_reset("b", "s")
    for action in ("read", "write", "list"):
        with pytest.raises(WorkspaceError, match="restored|mutation"):
            share(service, action, path="new", data_base64="")
    service.executions.complete_runtime_reset(reset["reset_token"], success=True)
    share(service, "write", path="new", data_base64="")


def test_rollback_reopens_current_and_preserves_share_policy(service):
    enable_both(service, "write")
    path = service.store.current("b", "s") / "output/result"
    path.write_text("before")
    service.store.create_checkpoint("b", "s", "cp")
    share(service, "write", path="result", data_base64=base64.b64encode(b"after").decode())
    service.dispatch("session.rollback", {"agent_id": "b", "session_id": "s", "checkpoint_id": "cp"})
    assert base64.b64decode(share(service, "read", path="result")["data_base64"]) == b"before"
    share(service, "disable", caller="b")
    service.dispatch("session.rollback", {"agent_id": "b", "session_id": "s", "checkpoint_id": "cp"})
    restarted = WorkspaceService(service.store)
    assert not share(restarted, "status", caller="b")["enabled"]
    with pytest.raises(WorkspaceError, match="both Sessions"):
        share(restarted, "read", path="result")


def test_endpoint_rejects_wrong_cgroup_and_spoofed_identity(service, tmp_path):
    from allox.runtime.share_endpoint import SessionShareEndpoints
    from allox.runtime.share_tool import call

    class Cgroups:
        allowed = False

        def owns(self, agent, session, pid):
            return self.allowed and (agent, session, pid) == ("a", "s", os.getpid())

    cgroups = Cgroups()
    # Linux Unix socket address length is limited to 108 bytes.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="share-test-") as root:
        endpoints = SessionShareEndpoints(service.shares, cgroups, Path(root))
        try:
            endpoint = endpoints.endpoint("a", "s")
            with pytest.raises(ValueError, match="does not belong"):
                call("enable", {}, endpoint)
            cgroups.allowed = True
            with pytest.raises(ValueError, match="identity"):
                call("enable", {"agent_id": "b"}, endpoint)
            assert call("enable", {}, endpoint)["session_id"] == "s"
            assert share(service, "status")["enabled"]
            assert not share(service, "status", caller="b")["enabled"]
        finally:
            endpoints.close()
