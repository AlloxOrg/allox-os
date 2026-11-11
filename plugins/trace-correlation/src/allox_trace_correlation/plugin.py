"""Opt-in management-only queries; no collection hooks."""

from pathlib import Path

from allox.runtime.extensions import ExecutionContribution
from allox.workspace.store import WorkspaceError
from allox_trace_correlation.correlate import Correlator, identifier
from allox_trace_correlation.reader import read_spans


class TraceCorrelationFeature:
    name = "trace-correlation"
    abi = 1
    rpc_prefixes = ("correlation.",)

    def __init__(self, *, context, config):
        trace, audit = Path(config["trace_root"]), Path(config["audit_root"])
        workspace = context.store.root.resolve()
        for root in (trace.resolve(), audit.resolve()):
            if root == workspace or workspace in root.parents:
                raise WorkspaceError("correlation inputs must be outside workspace rollback storage")
        self.correlator = Correlator(trace, audit)

    def prepare_execution(self, agent_id, session_id):
        return ExecutionContribution()

    def dispatch(self, action, params):
        if action == "correlation.status":
            return {"enabled": True, "read_only": True, "collection_hooks": False}
        agent = identifier(params.get("agent_id"), "agent")
        session = identifier(params.get("session_id"), "session")
        trace = params.get("trace_id")
        if trace is not None:
            identifier(trace, "trace")
        if action == "correlation.trace":
            if trace is None:
                raise WorkspaceError("trace_id is required")
            spans = read_spans(self.correlator.trace_root, agent, session, trace)
            return {
                "trace_id": trace, "agent_id": agent, "session_id": session,
                "runs": [{"run_id": s["process_run_id"], "process_span_id": s["span_id"],
                          "turn_id": s.get("turn_id")} for s in spans
                         if s.get("source") == "allox-executor" and s.get("kind") == "process"
                         and isinstance(s.get("process_run_id"), str)],
            }
        if action != "correlation.run":
            raise WorkspaceError("unknown correlation action")
        return self.correlator.run(
            agent, session, identifier(params.get("run_id"), "run"), trace_id=trace,
            stream=params.get("stream", "system-effects.jsonl"),
            offset=params.get("offset", 0), limit=params.get("limit", 50),
        )

    def close(self):
        pass
