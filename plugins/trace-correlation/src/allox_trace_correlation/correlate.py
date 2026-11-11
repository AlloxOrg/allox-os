"""Exact-run joins, with conservative turn attribution and sealed artifact checks."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from allox.workspace.store import WorkspaceError, validate_id
from allox_trace_correlation.reader import MAX_LINE, MAX_STREAM, EvidenceReader, read_spans

STREAMS = ("system-effects.jsonl", "process-lifecycle.jsonl")


def identifier(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise WorkspaceError(f"{label} must be a string")
    return validate_id(label, value)


class Correlator:
    def __init__(self, trace_root: Path, audit_root: Path):
        self.trace_root = trace_root.resolve(strict=True)
        self.reader = EvidenceReader(audit_root)

    def run(
        self, agent: str, session: str, run_id: str, *, trace_id: str | None = None,
        stream: str = "system-effects.jsonl", offset: int = 0, limit: int = 50,
    ) -> dict:
        for value, label in ((agent, "agent"), (session, "session"), (run_id, "run")):
            identifier(value, label)
        if trace_id is not None:
            identifier(trace_id, "trace")
        if stream not in STREAMS:
            raise WorkspaceError("unsupported evidence stream")
        if type(offset) is not int or not 0 <= offset <= 100000:
            raise WorkspaceError("offset must be in 0..100000")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise WorkspaceError("limit must be in 1..200")
        spans = read_spans(self.trace_root, agent, session, trace_id)
        owners = [s for s in spans if s.get("kind") == "process"
                  and s.get("source") == "allox-executor" and s.get("process_run_id") == run_id]
        if len(owners) != 1:
            raise WorkspaceError("expected one Core-owned process span for this run; narrow trace_id")
        owner = owners[0]
        trace = owner["trace_id"]
        related = [s for s in spans if s["trace_id"] == trace and s.get("process_run_id") == run_id]
        turns = {s["turn_id"] for s in related if isinstance(s.get("turn_id"), str) and s["turn_id"]}
        turn = owner.get("turn_id")
        assignment = "declared_parent_turn" if isinstance(turn, str) and turn and turns == {turn} else (
            "ambiguous" if turns else "unknown"
        )
        result = {
            "schema": "allox.trace-correlation/v1",
            "agent_id": agent, "session_id": session, "run_id": run_id,
            "trace_id": trace, "process_span_id": owner["span_id"],
            "parent_span_id": owner.get("parent_id"),
            "span_refs": [{"span_id": s["span_id"], "kind": s.get("kind")} for s in related],
            "turn": {"status": assignment, "turn_id": turn if assignment == "declared_parent_turn" else None,
                     "candidates": sorted(turns), "basis": "recorded context, not kernel semantic proof"},
            "status": "missing_evidence", "events": [], "recorded_verification": None,
            "integrity": "unchecked", "observer_evidence_status": None,
        }
        try:
            state = self.reader.json(run_id, "state.json")
        except FileNotFoundError:
            result["status"] = "missing_core_record"
            return result
        self._identity(state, agent, session, run_id)
        for field, expected in (("monitor_trace_id", trace), ("monitor_span_id", owner["span_id"])):
            if field in state and state[field] != expected:
                raise WorkspaceError("Core process metadata does not match the trace owner")
        if state.get("state") in ("starting", "running"):
            result["status"] = "run_not_finalized"
            return result
        try:
            receipt = self.reader.json(run_id, "system-effects-receipt.json")
        except FileNotFoundError:
            return result
        self._identity(receipt, agent, session, run_id)
        if (type(state.get("run_epoch")) is not int or state["run_epoch"] < 1
                or type(receipt.get("run_epoch")) is not int
                or receipt.get("run_epoch") != state["run_epoch"]
                or receipt.get("status") != "FINISHED"):
            raise WorkspaceError("Observer receipt epoch or finalization mismatch")
        protocol = receipt.get("protocol_version")
        if type(protocol) is not int or protocol not in (2, 3):
            raise WorkspaceError("unsupported Observer receipt protocol")
        manifest_name = "artifact-manifest.json" if protocol == 2 else "recovery-artifact-manifest.json"
        manifest_bytes = self.reader.read(run_id, "system-effects", manifest_name)
        if hashlib.sha256(manifest_bytes).hexdigest() != receipt.get("artifact_manifest_sha256"):
            raise WorkspaceError("artifact manifest digest mismatch")
        try:
            manifest = json.loads(manifest_bytes)
        except (ValueError, RecursionError) as exc:
            raise WorkspaceError("invalid artifact manifest") from exc
        if (not isinstance(manifest, dict) or manifest.get("run_id") != run_id
                or manifest.get("protocol_version") != protocol):
            raise WorkspaceError("artifact manifest run mismatch")
        entries = manifest.get("artifacts")
        if not isinstance(entries, list) or len(entries) > 4096 or any(not isinstance(x, dict) for x in entries):
            raise WorkspaceError("invalid bounded artifact list")
        matches = [x for x in entries if x.get("relative_path") == stream]
        if len(matches) > 1:
            raise WorkspaceError("ambiguous duplicate stream in artifact manifest")
        result.update(recorded_verification=receipt.get("offline_verification"),
                      observer_evidence_status=receipt.get("observer_evidence_status"),
                      recovery_verdict=receipt.get("recovery_verdict"))
        if len(matches) != 1:
            result["status"] = "stream_not_committed"
            return result
        entry = matches[0]
        digest, count, selected, nbytes = hashlib.sha256(), 0, [], 0
        selected_bytes, page_full = 0, False
        with self.reader.open((run_id, "system-effects", stream), MAX_STREAM) as source:
            while raw := source.readline(MAX_LINE + 1):
                if len(raw) > MAX_LINE or count >= 100000:
                    raise WorkspaceError("event stream exceeds bounded limits")
                digest.update(raw)
                nbytes += len(raw)
                try:
                    event = json.loads(raw)
                except (ValueError, RecursionError) as exc:
                    raise WorkspaceError("invalid event stream") from exc
                if not isinstance(event, dict) or (
                    "run_id" in event and event["run_id"] != run_id
                ):
                    raise WorkspaceError("event identity mismatch")
                if offset <= count and not page_full:
                    output_size = len(json.dumps(event, ensure_ascii=True).encode())
                    if len(selected) == limit or selected_bytes + output_size > 2 * 1024 * 1024:
                        page_full = True
                    else:
                        selected.append({"event_index": count, "event": event})
                        selected_bytes += output_size
                count += 1
        if digest.hexdigest() != entry.get("sha256") or nbytes != entry.get("size_bytes"):
            raise WorkspaceError("event stream digest or length mismatch")
        result.update(status="correlated", integrity="matched_recorded_sha256",
                      stream=stream, event_count=count, events=selected,
                      next_offset=offset + len(selected) if offset + len(selected) < count else None,
                      provenance={"receipt": f"{run_id}/system-effects-receipt.json",
                                  "manifest": f"{run_id}/system-effects/{manifest_name}",
                                  "stream": f"{run_id}/system-effects/{stream}",
                                  "stream_sha256": digest.hexdigest()},
                      warning="Recorded hashes are not signatures; independent verifier is not rerun. "
                              "Run attribution does not prove delegated effects or a precise tool span.")
        return result

    @staticmethod
    def _identity(record, agent, session, run_id):
        if (record.get("agent_id"), record.get("session_id"), record.get("run_id")) != (
            agent, session, run_id
        ):
            raise WorkspaceError("process/receipt identity mismatch")
