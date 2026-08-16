"""Integration tests for custom AIO image (ROADMAP phase 3).

Requires:
  - OpenSandbox server + Docker
  - Runtime image built: cd images/aio-runtime && ./build.sh
  - ALLOX_CUSTOM_IMAGE env (default: allox/aio-runtime:v2)

Run:
  uv run pytest -m integration tests/test_integration_custom_image.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.integration

CUSTOM_IMAGE = os.environ.get("ALLOX_CUSTOM_IMAGE", "allox/aio-runtime:v2")


def _image_available(image: str) -> bool:
    if not shutil.which("docker"):
        return False
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
        check=False,
        timeout=15,
    )
    return result.returncode == 0


@pytest.fixture(scope="module")
def require_custom_image(require_opensandbox_server):
    if shutil.which("docker") and subprocess.run(
        ["docker", "info"],
        capture_output=True,
        check=False,
        timeout=10,
    ).returncode != 0:
        pytest.skip("Docker daemon not running")
    if not _image_available(CUSTOM_IMAGE):
        pytest.skip(
            f"Custom image {CUSTOM_IMAGE} not found. "
            "Build with: cd images/aio-runtime && ./build.sh"
        )


def test_custom_image_create_and_health(runner, require_custom_image):
    """Create sandbox with custom image; AIO health_check passes."""
    create = runner(
        ["sandbox", "create", "-o", "json", "--timeout", "5m", "--image", CUSTOM_IMAGE]
    )
    assert create.exit_code == 0, create.output
    data = json.loads(create.output)
    sandbox_id = data["id"]
    assert sandbox_id
    assert CUSTOM_IMAGE in data.get("image", CUSTOM_IMAGE)

    try:
        exec_r = runner(["aio", "exec", sandbox_id, "cat", "/opt/allox/image-version.txt"])
        assert exec_r.exit_code == 0, exec_r.output
        assert "allox-aio-runtime:v2" in exec_r.output

        jq_r = runner(["aio", "exec", sandbox_id, "jq", "--version"])
        assert jq_r.exit_code == 0

        health_r = runner(
            [
                "aio",
                "exec",
                sandbox_id,
                "curl",
                "-sf",
                "http://127.0.0.1:8080/allox-health",
            ]
        )
        assert health_r.exit_code == 0, health_r.output
        assert "allox-custom" in health_r.output

        shot = runner(["aio", "screenshot", sandbox_id, "-f", "/tmp/allox-custom-test.png"])
        assert shot.exit_code == 0
    finally:
        kill = runner(["sandbox", "kill", sandbox_id, "-o", "json"])
        assert kill.exit_code == 0
