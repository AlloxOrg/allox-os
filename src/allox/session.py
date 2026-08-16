"""Local session state persisted in ~/.allox/sessions.json."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from allox.config import DEFAULT_CONFIG_DIR

DEFAULT_SESSIONS_PATH = DEFAULT_CONFIG_DIR / "sessions.json"


@dataclass
class Session:
    sandbox_id: str
    aio_url: str
    created_at: str

    @classmethod
    def now(cls, sandbox_id: str, aio_url: str) -> Session:
        ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        return cls(sandbox_id=sandbox_id, aio_url=aio_url, created_at=ts)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        return cls(
            sandbox_id=str(data["sandbox_id"]),
            aio_url=str(data["aio_url"]),
            created_at=str(data["created_at"]),
        )

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def _ensure_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def load_sessions(path: Path | None = None) -> dict[str, Any]:
    """Load raw sessions document; returns empty dict if missing or invalid."""
    file_path = path or DEFAULT_SESSIONS_PATH
    if not file_path.exists():
        return {}
    try:
        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_sessions(data: dict[str, Any], path: Path | None = None) -> Path:
    """Persist sessions document atomically."""
    file_path = path or DEFAULT_SESSIONS_PATH
    _ensure_dir(file_path)
    tmp = file_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(file_path)
    return file_path


def get_current_session(path: Path | None = None) -> Session | None:
    """Return the active session, or None if unset."""
    raw = load_sessions(path).get("current")
    if not isinstance(raw, dict):
        return None
    try:
        return Session.from_dict(raw)
    except KeyError:
        return None


def set_current_session(
    sandbox_id: str,
    aio_url: str,
    *,
    path: Path | None = None,
    created_at: str | None = None,
) -> Session:
    """Write or replace the current session."""
    session = (
        Session(sandbox_id=sandbox_id, aio_url=aio_url, created_at=created_at)
        if created_at
        else Session.now(sandbox_id, aio_url)
    )
    data = load_sessions(path)
    data["current"] = session.to_dict()
    save_sessions(data, path)
    return session


def clear_current_session(path: Path | None = None) -> bool:
    """Remove the current session. Returns True if one existed."""
    data = load_sessions(path)
    if "current" not in data:
        return False
    del data["current"]
    if data:
        save_sessions(data, path)
    else:
        file_path = path or DEFAULT_SESSIONS_PATH
        if file_path.exists():
            file_path.unlink()
    return True
