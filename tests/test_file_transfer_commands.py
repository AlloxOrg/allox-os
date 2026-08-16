"""Unit tests for binary-safe file upload and download commands."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def test_file_upload_streams_binary_file(runner, tmp_path):
    source = tmp_path / "source.bin"
    payload = b"\x00allox\xff\n"
    source.write_bytes(payload)
    sandbox = MagicMock()

    def capture_upload(path, stream, **kwargs):
        assert path == "/tmp/source.bin"
        assert stream.read() == payload
        assert kwargs == {"mode": 600}

    sandbox.files.write_file.side_effect = capture_upload
    with patch("allox.context.ClientContext.connect_sandbox", return_value=sandbox):
        result = runner(
            [
                "file",
                "upload",
                "--mode",
                "600",
                "sbx-upload",
                str(source),
                "/tmp/source.bin",
                "-o",
                "json",
            ]
        )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["bytes"] == len(payload)
    assert data["remote_path"] == "/tmp/source.bin"
    sandbox.close.assert_called_once()


def test_file_upload_rejects_missing_local_file_before_connect(runner, tmp_path):
    connect = MagicMock()
    with patch("allox.context.ClientContext.connect_sandbox", connect):
        result = runner(
            ["file", "upload", "sbx-upload", str(tmp_path / "missing"), "/tmp/file"]
        )

    assert result.exit_code != 0
    assert "Local file not found" in result.output
    connect.assert_not_called()


def test_file_download_streams_to_local_file(runner, tmp_path):
    target = tmp_path / "nested" / "target.bin"
    sandbox = MagicMock()
    sandbox.files.read_bytes_stream.return_value = iter([b"\x00all", b"ox\xff"])

    with patch("allox.context.ClientContext.connect_sandbox", return_value=sandbox):
        result = runner(
            ["file", "download", "sbx-download", "/tmp/source.bin", str(target), "-o", "json"]
        )

    assert result.exit_code == 0, result.output
    assert target.read_bytes() == b"\x00allox\xff"
    assert json.loads(result.output)["bytes"] == 7
    sandbox.files.read_bytes_stream.assert_called_once_with(
        "/tmp/source.bin", chunk_size=64 * 1024
    )
    sandbox.close.assert_called_once()


def test_file_download_refuses_overwrite_without_force(runner, tmp_path):
    target = tmp_path / "existing.bin"
    target.write_bytes(b"keep")
    connect = MagicMock()
    with patch("allox.context.ClientContext.connect_sandbox", connect):
        result = runner(
            ["file", "download", "sbx-download", "/tmp/source.bin", str(target)]
        )

    assert result.exit_code != 0
    assert "Use --force" in result.output
    assert target.read_bytes() == b"keep"
    connect.assert_not_called()


def test_file_download_force_overwrites_and_uses_session(runner, tmp_path, monkeypatch):
    target = tmp_path / "existing.bin"
    target.write_bytes(b"old")
    sandbox = MagicMock()
    sandbox.files.read_bytes_stream.return_value = iter([b"new"])
    session = SimpleNamespace(sandbox_id="session-sbx", aio_url="")
    monkeypatch.setattr("allox.context.get_current_session", lambda: session)

    with patch("allox.context.ClientContext.connect_sandbox", return_value=sandbox) as connect:
        result = runner(
            ["file", "download", "--force", "/tmp/source.bin", str(target)]
        )

    assert result.exit_code == 0, result.output
    assert target.read_bytes() == b"new"
    connect.assert_called_once_with("session-sbx")


def test_file_download_failure_removes_partial_file(runner, tmp_path):
    target = tmp_path / "target.bin"
    sandbox = MagicMock()

    def failing_stream(*args, **kwargs):
        yield b"partial"
        raise RuntimeError("connection lost")

    sandbox.files.read_bytes_stream.side_effect = failing_stream
    with patch("allox.context.ClientContext.connect_sandbox", return_value=sandbox):
        result = runner(
            ["file", "download", "sbx-download", "/tmp/source.bin", str(target)]
        )

    assert result.exit_code != 0
    assert not target.exists()
    assert not list(tmp_path.glob("*.part"))
    sandbox.close.assert_called_once()


def test_file_transfer_requires_two_paths(runner):
    result = runner(["file", "upload", "only-one-argument"])
    assert result.exit_code != 0
    assert "optional leading SANDBOX_ID" in result.output


def test_recursive_upload_preserves_tree_and_empty_directories(runner, tmp_path):
    source = tmp_path / "tree"
    (source / "nested" / "empty").mkdir(parents=True)
    (source / "root.bin").write_bytes(b"root\x00")
    (source / "nested" / "child.bin").write_bytes(b"child\xff")
    sandbox = MagicMock()
    uploaded = {}

    def capture_upload(path, stream, **kwargs):
        uploaded[path] = stream.read()

    sandbox.files.write_file.side_effect = capture_upload
    with patch("allox.context.ClientContext.connect_sandbox", return_value=sandbox):
        result = runner(
            [
                "file",
                "upload",
                "--recursive",
                "sbx-upload",
                str(source),
                "/workspace/tree",
                "-o",
                "json",
            ]
        )

    assert result.exit_code == 0, result.output
    assert uploaded == {
        "/workspace/tree/root.bin": b"root\x00",
        "/workspace/tree/nested/child.bin": b"child\xff",
    }
    directory_entries = sandbox.files.create_directories.call_args.args[0]
    assert {entry.path for entry in directory_entries} == {
        "/workspace/tree",
        "/workspace/tree/nested",
        "/workspace/tree/nested/empty",
    }
    assert {entry.mode for entry in directory_entries} == {755}
    data = json.loads(result.output)
    assert data["files"] == 2
    assert data["directories"] == 3


def test_upload_directory_requires_recursive(runner, tmp_path):
    source = tmp_path / "tree"
    source.mkdir()
    result = runner(["file", "upload", "sbx-upload", str(source), "/tmp/tree"])
    assert result.exit_code != 0
    assert "Use --recursive" in result.output


def test_upload_accepts_octal_mode_spellings(runner, tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"data")
    for spelling in ("644", "0644", "0o644"):
        sandbox = MagicMock()
        with patch("allox.context.ClientContext.connect_sandbox", return_value=sandbox):
            result = runner(
                [
                    "file",
                    "upload",
                    "--mode",
                    spelling,
                    "sbx-upload",
                    str(source),
                    "/tmp/source.bin",
                ]
            )
        assert result.exit_code == 0, result.output
        assert sandbox.files.write_file.call_args.kwargs["mode"] == 644


def test_upload_rejects_invalid_octal_mode(runner, tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"data")
    connect = MagicMock()
    with patch("allox.context.ClientContext.connect_sandbox", connect):
        result = runner(
            [
                "file",
                "upload",
                "--mode",
                "493",
                "sbx-upload",
                str(source),
                "/tmp/source.bin",
            ]
        )
    assert result.exit_code != 0
    assert "must be an octal permission" in result.output
    connect.assert_not_called()


def test_recursive_download_preserves_tree_and_empty_directories(runner, tmp_path):
    target = tmp_path / "tree"
    sandbox = MagicMock()
    sandbox.files.get_file_info.return_value = {
        "/workspace/tree": SimpleNamespace(entry_type="directory")
    }

    listings = {
        "/workspace/tree": [
            SimpleNamespace(path="/workspace/tree/root.bin", entry_type="file"),
            SimpleNamespace(path="/workspace/tree/nested", entry_type="directory"),
        ],
        "/workspace/tree/nested": [
            SimpleNamespace(path="/workspace/tree/nested/child.bin", entry_type="file"),
            SimpleNamespace(path="/workspace/tree/nested/empty", entry_type="directory"),
        ],
        "/workspace/tree/nested/empty": [],
    }
    sandbox.files.list_directory.side_effect = lambda entry: listings[entry.path]
    payloads = {
        "/workspace/tree/root.bin": [b"root", b"\x00"],
        "/workspace/tree/nested/child.bin": [b"child\xff"],
    }
    sandbox.files.read_bytes_stream.side_effect = (
        lambda path, **kwargs: iter(payloads[path])
    )

    with patch("allox.context.ClientContext.connect_sandbox", return_value=sandbox):
        result = runner(
            [
                "file",
                "download",
                "--recursive",
                "sbx-download",
                "/workspace/tree",
                str(target),
                "-o",
                "json",
            ]
        )

    assert result.exit_code == 0, result.output
    assert (target / "root.bin").read_bytes() == b"root\x00"
    assert (target / "nested" / "child.bin").read_bytes() == b"child\xff"
    assert (target / "nested" / "empty").is_dir()
    data = json.loads(result.output)
    assert data["files"] == 2
    assert data["directories"] == 3


def test_recursive_download_rejects_remote_symlink_and_cleans_staging(runner, tmp_path):
    target = tmp_path / "tree"
    sandbox = MagicMock()
    sandbox.files.get_file_info.return_value = {
        "/workspace/tree": SimpleNamespace(entry_type="directory")
    }
    sandbox.files.list_directory.return_value = [
        SimpleNamespace(path="/workspace/tree/link", entry_type="symlink")
    ]

    with patch("allox.context.ClientContext.connect_sandbox", return_value=sandbox):
        result = runner(
            [
                "file",
                "download",
                "--recursive",
                "sbx-download",
                "/workspace/tree",
                str(target),
            ]
        )

    assert result.exit_code != 0
    assert "Symbolic links are not supported" in result.output
    assert not target.exists()
    assert not list(tmp_path.glob("*.part"))


def test_recursive_download_rejects_path_escape(runner, tmp_path):
    sandbox = MagicMock()
    sandbox.files.get_file_info.return_value = {
        "/workspace/tree": SimpleNamespace(entry_type="directory")
    }
    sandbox.files.list_directory.return_value = [
        SimpleNamespace(path="/workspace/escape.bin", entry_type="file")
    ]

    with patch("allox.context.ClientContext.connect_sandbox", return_value=sandbox):
        result = runner(
            [
                "file",
                "download",
                "--recursive",
                "sbx-download",
                "/workspace/tree",
                str(tmp_path / "tree"),
            ]
        )

    assert result.exit_code != 0
    assert "escapes source directory" in result.output
