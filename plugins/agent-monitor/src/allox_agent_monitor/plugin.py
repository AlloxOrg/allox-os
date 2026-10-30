"""Optional trusted collector and Core execution observer, Plugin API 1."""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any

from allox.runtime.extensions import ExecutionContribution, SandboxMount
from allox.workspace.store import WorkspaceError
from allox_agent_monitor.endpoint import SessionEndpoints
from allox_agent_monitor.profile import profile
from allox_agent_monitor.resources import delta, snapshot
from allox_agent_monitor.store import TraceStore, identifier


class AgentMonitorFeature:
    name = "agent-monitor"
    abi = 1
    rpc_prefixes = ("monitor.",)

    def __init__(self, *, context, config: dict[str, Any]) -> None:
        root = Path(config["root"]).resolve()
        workspace = context.store.root.resolve()
        if root == workspace or workspace in root.parents:
            raise WorkspaceError("monitor root must be outside workspace rollback storage")
        self.store = TraceStore(root)
        self.cgroups = context.cgroups
        self.clock = uuid.uuid4().hex
        self._runs: dict[str, dict] = {}
        self._run_lock = threading.RLock()
        self._endpoints = SessionEndpoints(
            context, self, Path(config.get("socket_root", "/dev/shm/allox-monitor"))
        )

    def prepare_execution(self, agent_id: str, session_id: str) -> ExecutionContribution:
        # Expose only Session-bound append-only telemetry, never the DB/admin API.
        endpoint = self._endpoints.endpoint(agent_id, session_id)
        return ExecutionContribution(
            environment=(("ALLOX_MONITOR_SOCK", "/run/allox/monitor.sock"),
                         ("PYTHONPATH", "/run/allox/monitor-sdk")),
            mounts=(SandboxMount(endpoint, "/run/allox/monitor.sock"),
                    SandboxMount(str(Path(__file__).resolve().parents[1]),
                                 "/run/allox/monitor-sdk")),
            metadata={"agent_monitor": True},
        )

    def dispatch(self, action: str, params: dict[str, Any]) -> Any:
        if action == "monitor.status":
            return self.store.status()
        agent = identifier(params.get("agent_id"), "agent")
        session = identifier(params.get("session_id"), "session")
        if action == "monitor.ingest":
            event = params.get("event")
            if not isinstance(event, dict) or event.get("kind") == "process":
                raise WorkspaceError("framework event required; process spans are Core-owned")
            return self.store.ingest(agent, session, event)
        sessions = params.get("session_ids", [session])
        if not isinstance(sessions, list) or not 0 < len(sessions) <= 100:
            raise WorkspaceError("session_ids must contain 1..100 Session IDs")
        if len({identifier(s, "session") for s in sessions}) != len(sessions):
            raise WorkspaceError("duplicate Session ID")
        if action == "monitor.trace" and len(sessions) != 1:
            raise WorkspaceError("monitor.trace selects exactly one Session")
        rows = []
        for selected in sessions:
            rows.extend(self.store.spans(agent, selected, params.get("trace_id")))
            if len(rows) > self.store.max_spans:
                raise WorkspaceError("query exceeds span limit; narrow selection")
        if action == "monitor.trace":
            return {"spans": rows}
        if action == "monitor.profile":
            return profile(rows)
        raise WorkspaceError(f"unknown monitor action: {action}")

    def active_run_id(self, identity: tuple[str, str]) -> str:
        with self._run_lock:
            matches = [run_id for run_id, run in self._runs.items()
                       if (run["row"]["agent_id"], run["row"]["session_id"]) == identity
                       and run["row"]["state"] == "running"]
        if len(matches) != 1:
            raise WorkspaceError("Session has no unique active process run")
        return matches[0]

    def ingest_session(self, identity: tuple[str, str], event: dict, expected_run_id: str) -> dict:
        """Peer cgroup has been verified; bind telemetry to the Core-owned active run."""
        run_id = self.active_run_id(identity)
        if run_id != expected_run_id:
            raise WorkspaceError("process run changed while telemetry was pending")
        if event.get("kind") == "process":
            raise WorkspaceError("process spans are Core-owned")
        return self.store.ingest(*identity, event, source="session-sdk", process_run_id=run_id)

    def execution_started(self, row: dict) -> None:
        """Called after attribution, before SIGCONT. No action can run before this ack."""
        parent = row.get("trace_context") or {}
        trace = parent.get("trace_id", uuid.uuid4().hex)
        span = "process-" + row["run_id"]
        agent, session = row["agent_id"], row["session_id"]
        parent_fields = {}
        if parent:
            parents = [s for s in self.store.spans(agent, session, trace)
                       if s["span_id"] == parent["span_id"]]
            if not parents:
                raise WorkspaceError("trace parent does not belong to this Session")
            parent_fields = parents[0]
        self.store.ingest(agent, session, {
            "event": "start", "trace_id": trace, "span_id": span,
            "parent_id": parent.get("span_id"), "kind": "process", "name": "allox.execution",
            "clock_id": self.clock, "start_ns": time.perf_counter_ns(),
            "semantic_path": parent_fields.get("semantic_path", ["system-execution"]),
            "turn_id": parent_fields.get("turn_id"),
        }, source="allox-executor", process_run_id=row["run_id"])
        cgroup = self.cgroups.path(agent, session)
        with self._run_lock:
            self._runs[row["run_id"]] = {
                "trace": trace, "span": span, "before": snapshot(cgroup), "path": cgroup, "row": row,
            }
        row["monitor_trace_id"] = trace
        row["monitor_span_id"] = span

    def execution_finished(self, row: dict) -> None:
        with self._run_lock:
            run = self._runs[row["run_id"]]
        metrics = delta(run["before"], snapshot(run["path"]))
        self.store.ingest(row["agent_id"], row["session_id"], {
            "event": "end", "trace_id": run["trace"], "span_id": run["span"],
            "clock_id": self.clock, "end_ns": time.perf_counter_ns(),
            "status": "ok" if row.get("root_exit_code") == 0 else "error",
            "metrics": metrics,
            "result": {"run_id": row["run_id"], "root_exit_code": row.get("root_exit_code"),
                       "resource_scope": "whole Session cgroup, not one nested span",
                       "events": "query process.events when process-tree is enabled",
                       "unavailable_counters": sorted({"cgroup.cpu_usage_usec",
                           "cgroup.memory_peak_bytes", "cgroup.io_read_bytes",
                           "cgroup.io_write_bytes"} - metrics.keys())},
        }, source="allox-executor")
        with self._run_lock:
            del self._runs[row["run_id"]]

    def close(self) -> None:
        self._endpoints.close()
        self.store.close()
