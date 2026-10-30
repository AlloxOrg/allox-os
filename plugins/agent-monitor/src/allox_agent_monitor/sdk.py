"""Explicit framework instrumentation; no monkeypatching or model SDK dependency."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

_active: ContextVar[tuple[Any, Any] | None] = ContextVar("allox_monitor_span", default=None)
_SECRET_KEYS = {"authorization", "api_key", "apikey", "password", "token", "secret"}


def payload(value: Any, capture: bool = False) -> dict:
    """Default to metadata/hash; explicit content capture still redacts common secret keys."""
    def redact(item, depth=0):
        if depth > 10:
            return {"truncated_type": type(item).__name__}
        if isinstance(item, dict):
            return {str(k): "[REDACTED]" if str(k).lower() in _SECRET_KEYS else redact(v, depth+1)
                    for k, v in item.items()}
        if isinstance(item, (tuple, list)):
            return [redact(v, depth+1) for v in item]
        if isinstance(item, float) and not math.isfinite(item):
            return {"nonfinite": str(item)}
        if item is None or isinstance(item, (str, bool, int, float)):
            return item
        return {"type": type(item).__name__}

    encoded = json.dumps(redact(value), allow_nan=False, ensure_ascii=False).encode()
    result: dict[str, Any] = {"bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()}
    if capture and len(encoded) <= 8192:
        result["content"] = json.loads(encoded)
    return result


class Span:
    def __init__(self, tracer, name: str, kind: str, **fields) -> None:
        self.tracer = tracer
        self.span_id = uuid.uuid4().hex
        active = _active.get()
        self.parent = active[1] if active and active[0] is tracer else None
        self.fields = {
            "trace_id": tracer.trace_id, "span_id": self.span_id,
            "parent_id": self.parent.span_id if self.parent else None,
            "name": name, "kind": kind, "clock_id": tracer.clock_id,
            "semantic_path": list(self.parent.fields["semantic_path"] if self.parent else ()),
            "turn_id": self.parent.fields.get("turn_id") if self.parent else None,
            **fields,
        }
        self.metrics: dict[str, float] = {}
        self.result: dict | None = None
        self._entered = False

    @property
    def context(self) -> dict:
        return {"trace_id": self.tracer.trace_id, "span_id": self.span_id}

    def __enter__(self):
        if self._entered:
            raise RuntimeError("a span cannot be reused")
        self._entered = True
        self.tracer.emit({"event": "start", **self.fields, "start_ns": time.perf_counter_ns()})
        self._token = _active.set((self.tracer, self))
        return self

    def set_result(self, value: Any) -> None:
        self.result = payload(value, self.tracer.capture_content)

    def metric(self, name: str, value: float) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or (
            not math.isfinite(value) or value < 0
        ):
            raise ValueError("metric must be a finite nonnegative number")
        self.metrics[name] = value

    def __exit__(self, exc_type, exc, tb):
        ended = time.perf_counter_ns()
        _active.reset(self._token)
        status = "ok" if exc is None else (
            "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
        )
        event = {
            "event": "end", "trace_id": self.tracer.trace_id, "span_id": self.span_id,
            "clock_id": self.tracer.clock_id, "end_ns": ended, "status": status,
            "result": self.result, "error_type": exc_type.__name__ if exc_type else None,
            "metrics": self.metrics,
        }
        try:
            self.tracer.emit(event)
        except Exception:
            if exc is None:
                raise
            # Preserve the application exception; telemetry failure remains in health().
        return False


class Tracer:
    def __init__(
        self, emit: Callable[[dict], Any], *, trace_id: str | None = None,
        enabled: bool = True, capture_content: bool = False, strict: bool = False,
    ) -> None:
        self.trace_id = trace_id or uuid.uuid4().hex
        self.clock_id = uuid.uuid4().hex
        self.enabled = enabled
        self.capture_content = capture_content
        self.strict = strict
        self._emit = emit
        self.attempted = self.failed = self.emit_ns = self.wire_bytes = self.metric_errors = 0

    def emit(self, event: dict) -> None:
        if not self.enabled:
            return
        started = time.perf_counter_ns()
        self.attempted += 1
        try:
            encoded = json.dumps(event, allow_nan=False).encode()
            self.wire_bytes += len(encoded)
            ack = self._emit(event)
            if isinstance(ack, dict) and (ack.get("accepted") is False or ack.get("ok") is False):
                raise RuntimeError("collector rejected event")
        except Exception:
            self.failed += 1
            if self.strict:
                raise
        finally:
            self.emit_ns += time.perf_counter_ns() - started

    def span(self, name: str, kind: str = "tool", **fields) -> Span:
        return Span(self, name, kind, **fields)

    def phase(self, name: str) -> Span:
        return self.span(name, "phase")

    def call_tool(self, name: str, function: Callable, *args, **kwargs):
        """Measure executor-internal time separately from the outer call's end-to-end time."""
        with self.span(name, "tool", input=payload({"args": args, "kwargs": kwargs},
                                                 self.capture_content)) as outer:
            with self.phase("tool_execution") as execution:
                started = time.perf_counter_ns()
                try:
                    result = function(*args, **kwargs)
                finally:
                    execution.metric("executor.execution_ns", time.perf_counter_ns()-started)
            outer.set_result(result)
            return result

    async def acall_tool(self, name: str, function: Callable, *args, **kwargs):
        with self.span(name, "tool", input=payload({"args": args, "kwargs": kwargs},
                                                 self.capture_content)) as outer:
            with self.phase("tool_execution") as execution:
                started = time.perf_counter_ns()
                try:
                    result = await function(*args, **kwargs)
                finally:
                    execution.metric("executor.execution_ns", time.perf_counter_ns()-started)
            outer.set_result(result)
            return result

    def call_model(self, model: str, function: Callable, *args, **kwargs):
        with self.span(model, "llm", model=model, input=payload(
            {"args": args, "kwargs": kwargs}, self.capture_content
        )) as span:
            started = time.perf_counter_ns()
            try:
                response = function(*args, **kwargs)
            finally:
                span.metric("client.request_ns", time.perf_counter_ns()-started)
            record_vllm_metrics(span, response)
            span.set_result(response)
            return response

    async def acall_model(self, model: str, function: Callable, *args, **kwargs):
        with self.span(model, "llm", model=model, input=payload(
            {"args": args, "kwargs": kwargs}, self.capture_content
        )) as span:
            started = time.perf_counter_ns()
            try:
                response = await function(*args, **kwargs)
            finally:
                span.metric("client.request_ns", time.perf_counter_ns()-started)
            record_vllm_metrics(span, response)
            span.set_result(response)
            return response

    def health(self) -> dict:
        return {"enabled": self.enabled, "attempted_events": self.attempted,
                "failed_events": self.failed, "emit_ns": self.emit_ns,
                "serialized_bytes": self.wire_bytes,
                "complete": self.failed == 0 and self.metric_errors == 0,
                "metric_errors": self.metric_errors, "strict": self.strict}


def record_vllm_metrics(span: Span, response: Any) -> None:
    """vLLM metrics are provider-reported, never inferred from client elapsed time."""
    if not isinstance(response, dict) and callable(getattr(response, "model_dump", None)):
        response = response.model_dump()
    if not isinstance(response, dict):
        return
    metrics = response.get("metrics")
    if isinstance(metrics, dict):
        for name in ("queue_time_ms", "time_to_first_token_ms", "generation_time_ms"):
            value = metrics.get(name)
            if value is not None:
                _provider_metric(span, "provider." + name, value)
    usage = response.get("usage")
    if isinstance(usage, dict):
        for name in ("prompt_tokens", "completion_tokens"):
            if usage.get(name) is not None:
                _provider_metric(span, "provider." + name, usage[name])


def _provider_metric(span: Span, name: str, value: Any) -> None:
    try:
        span.metric(name, value)
    except ValueError:
        span.tracer.metric_errors += 1
        if span.tracer.strict:
            raise
