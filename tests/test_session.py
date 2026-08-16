"""Unit tests for ~/.allox/sessions.json storage."""

from __future__ import annotations

import json

from allox.vm.selection import (
    clear_current_session,
    get_current_session,
    load_sessions,
    set_current_session,
)


def test_sessions_file_roundtrip(tmp_path):
    path = tmp_path / "sessions.json"
    assert get_current_session(path) is None

    session = set_current_session("sbx-abc", "http://127.0.0.1:12345", path=path)
    assert session.sandbox_id == "sbx-abc"
    assert session.aio_url == "http://127.0.0.1:12345"
    assert session.created_at  # ISO timestamp

    loaded = get_current_session(path)
    assert loaded is not None
    assert loaded.sandbox_id == "sbx-abc"
    assert loaded.aio_url == "http://127.0.0.1:12345"

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == {
        "current": {
            "sandbox_id": "sbx-abc",
            "aio_url": "http://127.0.0.1:12345",
            "created_at": session.created_at,
        }
    }


def test_clear_current_session(tmp_path):
    path = tmp_path / "sessions.json"
    set_current_session("sbx-1", "http://localhost:8080", path=path)
    assert clear_current_session(path) is True
    assert get_current_session(path) is None
    assert not path.exists()

    assert clear_current_session(path) is False


def test_load_sessions_missing_file(tmp_path):
    path = tmp_path / "missing.json"
    assert load_sessions(path) == {}
    assert get_current_session(path) is None


def test_load_sessions_invalid_json(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text("not json", encoding="utf-8")
    assert load_sessions(path) == {}
    assert get_current_session(path) is None


def test_set_current_session_replaces(tmp_path):
    path = tmp_path / "sessions.json"
    first = set_current_session("old-id", "http://old", path=path)
    second = set_current_session("new-id", "http://new", path=path)
    assert second.sandbox_id == "new-id"
    assert second.created_at != first.created_at or second.sandbox_id != first.sandbox_id
    current = get_current_session(path)
    assert current is not None
    assert current.sandbox_id == "new-id"
