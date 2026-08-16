"""Optional end-to-end tests (require OpenSandbox server + Docker).

Run explicitly:
  uv run pytest -m integration tests/test_integration_e2e.py -v
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def require_server(require_opensandbox_server):
    if shutil.which("docker") and subprocess.run(
        ["docker", "info"],
        capture_output=True,
        timeout=10,
    ).returncode != 0:
        pytest.skip("Docker daemon not running")


@pytest.fixture
def sandbox_id(runner, require_server):
    """Create a sandbox and always attempt cleanup, even after a failed assertion."""
    create = runner(["sandbox", "create", "-o", "json", "--timeout", "5m"])
    assert create.exit_code == 0, create.output
    sandbox_id = json.loads(create.output)["id"]
    assert sandbox_id
    try:
        yield sandbox_id
    finally:
        kill = runner(["sandbox", "kill", sandbox_id, "-o", "json"])
        assert kill.exit_code == 0, kill.output


def test_e2e_sandbox_lifecycle(runner, sandbox_id, tmp_path):
    """ROADMAP 1.5: create -> exec -> screenshot -> file transfer -> kill."""
    out_png = tmp_path / "test.png"

    exec_r = runner(["aio", "exec", sandbox_id, "echo", "hello"])
    assert exec_r.exit_code == 0, exec_r.output
    assert "hello" in exec_r.output

    shot = runner(["aio", "screenshot", sandbox_id, "-f", str(out_png)])
    assert shot.exit_code == 0, shot.output
    assert out_png.exists() and out_png.stat().st_size > 0

    jupyter = runner(
        ["aio", "jupyter", "run", sandbox_id, "-c", "print(2+2)", "-o", "json"]
    )
    assert jupyter.exit_code == 0, jupyter.output
    jdata = json.loads(jupyter.output)
    assert jdata.get("status") == "ok"

    browser = runner(["aio", "browser", "info", sandbox_id, "-o", "json"])
    assert browser.exit_code == 0, browser.output
    bdata = json.loads(browser.output)
    assert bdata.get("cdp_url")

    local_source = tmp_path / "upload.bin"
    local_download = tmp_path / "download.bin"
    payload = b"\x00allox-transfer\xff\n"
    local_source.write_bytes(payload)
    upload = runner(
        [
            "file",
            "upload",
            sandbox_id,
            str(local_source),
            "/tmp/allox-transfer.bin",
            "-o",
            "json",
        ]
    )
    assert upload.exit_code == 0, upload.output

    download = runner(
        [
            "file",
            "download",
            sandbox_id,
            "/tmp/allox-transfer.bin",
            str(local_download),
            "-o",
            "json",
        ]
    )
    assert download.exit_code == 0, download.output
    assert local_download.read_bytes() == payload

    local_tree = tmp_path / "upload-tree"
    downloaded_tree = tmp_path / "download-tree"
    (local_tree / "nested" / "empty").mkdir(parents=True)
    (local_tree / "root.txt").write_text("root", encoding="utf-8")
    (local_tree / "nested" / "data.bin").write_bytes(payload)
    recursive_upload = runner(
        [
            "file",
            "upload",
            "--recursive",
            sandbox_id,
            str(local_tree),
            "/tmp/allox-transfer-tree",
            "-o",
            "json",
        ]
    )
    assert recursive_upload.exit_code == 0, recursive_upload.output
    recursive_download = runner(
        [
            "file",
            "download",
            "--recursive",
            sandbox_id,
            "/tmp/allox-transfer-tree",
            str(downloaded_tree),
            "-o",
            "json",
        ]
    )
    assert recursive_download.exit_code == 0, recursive_download.output
    assert (downloaded_tree / "root.txt").read_text(encoding="utf-8") == "root"
    assert (downloaded_tree / "nested" / "data.bin").read_bytes() == payload
    assert (downloaded_tree / "nested" / "empty").is_dir()
