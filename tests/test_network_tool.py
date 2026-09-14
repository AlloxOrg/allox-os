"""Generic stdin/stdout network connector tests."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="requires Unix-domain sockets")
def test_connector_forwards_opaque_bytes_in_both_directions():
    with tempfile.TemporaryDirectory() as temporary:
        path = str(Path(temporary) / "broker.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
        listener.listen(1)
        observed = {}

        def serve():
            stream, _ = listener.accept()
            control = bytearray()
            while not control.endswith(b"\n"):
                control.extend(stream.recv(1))
            observed["control"] = json.loads(control)
            stream.sendall(b'{"ok":true}\n')
            observed["payload"] = stream.recv(1024)
            stream.sendall(b"reply:\x00\xff" + observed["payload"])
            stream.close()

        thread = threading.Thread(target=serve)
        thread.start()
        environment = dict(os.environ)
        source = str(Path(__file__).parents[1] / "plugins" / "session-network" / "src")
        core = str(Path(__file__).parents[1] / "src")
        environment["PYTHONPATH"] = (
            source + os.pathsep + core + os.pathsep + environment.get("PYTHONPATH", "")
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "allox_session_network.tool",
                "connect",
                "example.internal",
                "2222",
                "--socket",
                path,
            ],
            input=b"request:\x00\xff",
            capture_output=True,
            env=environment,
            timeout=5,
            check=False,
        )
        thread.join(timeout=2)
        listener.close()

        assert result.returncode == 0, result.stderr
        assert observed["control"] == {"host": "example.internal", "port": 2222}
        assert observed["payload"] == b"request:\x00\xff"
        assert result.stdout == b"reply:\x00\xffrequest:\x00\xff"
