"""Durable, bounded trace collection outside Session checkpoint trees."""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from allox.workspace.store import WorkspaceError, validate_id

KINDS = {"run", "turn", "llm", "tool", "subagent", "phase", "process"}
PHASES = {
    "approval", "executor_queue", "tool_execution", "transport", "mcp_initialize",
    "mcp_discovery", "mcp_call", "server_queue", "server_ttft", "server_generation",
}


def number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WorkspaceError(f"{label} must be a finite nonnegative number")
    if not math.isfinite(value) or value < 0:
        raise WorkspaceError(f"{label} must be a finite nonnegative number")
    return value


def identifier(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise WorkspaceError(f"{label} must be a string")
    return validate_id(label, value)


class TraceStore:
    def __init__(self, root: Path, *, max_spans: int = 10000) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.RLock()
        self.max_spans = max_spans
        self._db = sqlite3.connect(self.root / "traces.sqlite3", check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS spans ("
            "agent TEXT, session TEXT, trace TEXT, span TEXT, payload TEXT, "
            "PRIMARY KEY(agent, session, trace, span))"
        )
        self._db.commit()
        self.accepted = self.rejected = self.collector_ns = 0

    def ingest(self, agent: str, session: str, event: dict, *, source="framework-sdk",
               process_run_id: str | None = None) -> dict:
        started = time.perf_counter_ns()
        try:
            return self._ingest(agent, session, event, source, process_run_id)
        except Exception:
            with self._lock:
                self.rejected += 1
            raise
        finally:
            with self._lock:
                self.collector_ns += time.perf_counter_ns() - started

    def _ingest(self, agent: str, session: str, event: dict, source: str,
                process_run_id: str | None) -> dict:
        identifier(agent, "agent")
        identifier(session, "session")
        if not isinstance(event, dict):
            raise WorkspaceError("event must be an object")
        try:
            encoded = json.dumps(event, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise WorkspaceError("event must contain finite JSON data") from exc
        if len(encoded.encode()) > 65536:
            raise WorkspaceError("event exceeds 64 KiB")
        trace = identifier(event.get("trace_id"), "trace")
        span = identifier(event.get("span_id"), "span")
        key = (agent, session, trace, span)
        with self._lock, self._db:
            found = self._db.execute(
                "SELECT payload FROM spans WHERE agent=? AND session=? AND trace=? AND span=?", key
            ).fetchone()
            if event.get("event") == "start":
                if found:
                    raise WorkspaceError("duplicate span start")
                count = self._db.execute(
                    "SELECT COUNT(*) FROM spans WHERE agent=? AND session=? AND trace=?", key[:3]
                ).fetchone()[0]
                if count >= self.max_spans:
                    raise WorkspaceError("trace span limit exceeded")
                row = self._validate_start(key, event)
                row["source"] = source
                row["process_run_id"] = process_run_id
                self._db.execute("INSERT INTO spans VALUES (?,?,?,?,?)", (*key, json.dumps(row)))
            elif event.get("event") == "end":
                if not found:
                    raise WorkspaceError("span end has no start")
                row = json.loads(found[0])
                if row["status"] != "running":
                    raise WorkspaceError("duplicate span end")
                if row["source"] != source:
                    raise WorkspaceError("span source mismatch")
                ended = event.get("end_ns")
                if type(ended) is not int or ended < row["start_ns"]:
                    raise WorkspaceError("invalid monotonic end time")
                if event.get("clock_id") != row["clock_id"]:
                    raise WorkspaceError("span clocks differ")
                if not isinstance(event.get("status"), str) or event["status"] not in {
                    "ok", "error", "cancelled"
                }:
                    raise WorkspaceError("invalid span status")
                metrics = event.get("metrics", {})
                if not isinstance(metrics, dict):
                    raise WorkspaceError("metrics must be an object")
                for name, value in metrics.items():
                    if not isinstance(name, str) or not name:
                        raise WorkspaceError("invalid metric name")
                    number(value, name)
                row.update(
                    end_ns=ended, duration_ns=ended-row["start_ns"], status=event["status"],
                    result=event.get("result"), error_type=event.get("error_type"), metrics=metrics,
                )
                self._db.execute(
                    "UPDATE spans SET payload=? WHERE agent=? AND session=? AND trace=? AND span=?",
                    (json.dumps(row), *key),
                )
            else:
                raise WorkspaceError("event must be start or end")
        with self._lock:
            self.accepted += 1  # Count only events whose SQLite transaction committed.
        return {"accepted": True, "trace_id": trace, "span_id": span}

    def _validate_start(self, key: tuple, event: dict) -> dict:
        if not isinstance(event.get("kind"), str) or event["kind"] not in KINDS:
            raise WorkspaceError("invalid span kind")
        if not isinstance(event.get("name"), str) or not 0 < len(event["name"]) <= 256:
            raise WorkspaceError("invalid span name")
        if type(event.get("start_ns")) is not int or event["start_ns"] < 0:
            raise WorkspaceError("invalid monotonic start time")
        identifier(event.get("clock_id"), "clock")
        semantic = event.get("semantic_path", [])
        if not isinstance(semantic, list) or len(semantic) > 32 or any(
            not isinstance(item, str) or not 0 < len(item) <= 128 for item in semantic
        ):
            raise WorkspaceError("invalid semantic_path")
        parent = event.get("parent_id")
        if parent is not None:
            identifier(parent, "parent")
        links = event.get("links", [])
        if not isinstance(links, list) or len(links) > 128:
            raise WorkspaceError("invalid causal links")
        for related in ([parent] if parent else []) + links:
            identifier(related, "related span")
            related_row = self._db.execute(
                "SELECT payload FROM spans WHERE agent=? AND session=? AND trace=? AND span=?",
                (*key[:3], related),
            ).fetchone()
            if related_row is None:
                raise WorkspaceError("parent/link must already exist in this Session trace")
            if related == parent and json.loads(related_row[0])["status"] != "running":
                raise WorkspaceError("parent span has already ended")
        for name in ("turn_id", "parallel_group", "loop_id", "model"):
            value = event.get(name)
            if value is not None and (not isinstance(value, str) or len(value) > 256):
                raise WorkspaceError(f"invalid {name}")
        iteration = event.get("iteration")
        if iteration is not None and (type(iteration) is not int or iteration < 0):
            raise WorkspaceError("invalid iteration")
        if event["kind"] == "phase" and event["name"] not in PHASES:
            raise WorkspaceError("unknown timing phase")
        return {
            "agent_id": key[0], "session_id": key[1],
            **{name: event.get(name) for name in (
                "trace_id", "span_id", "parent_id", "kind", "name", "start_ns", "clock_id",
                "turn_id", "parallel_group", "loop_id", "iteration", "model", "input",
            )},
            "links": links, "semantic_path": semantic, "status": "running",
            "end_ns": None, "duration_ns": None, "metrics": {}, "source": "framework-sdk",
        }

    def spans(self, agent: str, session: str, trace: str | None = None) -> list[dict]:
        identifier(agent, "agent")
        identifier(session, "session")
        with self._lock:
            sql = "SELECT payload FROM spans WHERE agent=? AND session=?"
            args: tuple = (agent, session)
            if trace is not None:
                identifier(trace, "trace")
                sql += " AND trace=?"
                args += (trace,)
            rows = self._db.execute(sql + " ORDER BY rowid LIMIT ?", (*args, self.max_spans+1))
            result = [json.loads(row[0]) for row in rows]
        if len(result) > self.max_spans:
            raise WorkspaceError("query exceeds span limit; select one trace")
        return result

    def status(self) -> dict:
        with self._lock:
            return {
                "accepted_events_this_process": self.accepted,
                "rejected_events_this_process": self.rejected,
                "collector_ns_this_process": self.collector_ns,
                "storage_bytes": sum(p.stat().st_size for p in self.root.glob("traces.sqlite3*")),
                "delivery": "synchronous-acknowledged", "event_loss_rate": None,
                "event_loss_note": "no claim about events never submitted by a producer",
            }

    def close(self) -> None:
        with self._lock:
            self._db.close()
