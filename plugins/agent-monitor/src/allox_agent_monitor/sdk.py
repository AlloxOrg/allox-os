"""Explicit framework instrumentation; no monkeypatching or model SDK dependency."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import threading
import time
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

from allox_agent_monitor.clock import monotonic_domain

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
            "parent_id": self.parent.span_id if self.parent else tracer.parent_id,
            "name": name, "kind": kind, "clock_id": tracer.clock_id,
            "semantic_path": list(self.parent.fields["semantic_path"] if self.parent else tracer.semantic_path),
            "turn_id": self.parent.fields.get("turn_id") if self.parent else tracer.turn_id,
            **fields,
        }
        self.metrics: dict[str, float] = {}
        self.result: dict | None = None
        self._entered = False
        self._reported_error = None

    def mark_error(self, error_type='RemoteToolError'):
        self._reported_error = error_type

    @property
    def context(self) -> dict:
        self.tracer.barrier()  # Exported references must already exist at the collector.
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
        status = ("error" if self._reported_error else "ok") if exc is None else (
            "cancelled" if isinstance(exc, (GeneratorExit, getattr(
                sys.modules.get('asyncio.exceptions'), 'CancelledError', GeneratorExit))) else "error"
        )
        event = {
            "event": "end", "trace_id": self.tracer.trace_id, "span_id": self.span_id,
            "clock_id": self.tracer.clock_id, "end_ns": ended, "status": status,
            "result": self.result, "error_type": exc_type.__name__ if exc_type else self._reported_error,
            "metrics": self.metrics,
        }
        try:
            self.tracer.emit(event)
            if self.tracer.delivery == 'background' and self.fields['kind'] in {'run', 'turn'}:
                self.tracer.barrier()
        except Exception:
            if exc is None:
                raise
            # Preserve the application exception; telemetry failure remains in health().
        return False


class Tracer:
    def __init__(
        self, emit: Callable[[dict], Any], *, trace_id: str | None = None,
        enabled: bool = True, capture_content: bool = False, strict: bool = False,
        batch_size: int = 1,
        inherit_context: bool = True,
        delivery: str = 'sync',
        queue_capacity: int = 1024,
        queue_bytes: int = 1024*1024,
        flush_interval: float = .02,
        export_timeout: float = 10,
    ) -> None:
        context = {}
        raw = os.environ.get('ALLOX_MONITOR_CONTEXT', '') if inherit_context else ''
        if raw:
            if len(raw) > 8192:
                raise ValueError('monitor context too large')
            context = json.loads(raw)
            if not isinstance(context, dict) or set(context) != {'trace_id', 'parent_id', 'semantic_path', 'turn_id'}:
                raise ValueError('invalid monitor context')
        if trace_id is not None and trace_id != context.get('trace_id'):
            context = {}
        self.trace_id = trace_id or context.get('trace_id') or uuid.uuid4().hex
        self.parent_id = context.get('parent_id')
        self.semantic_path = context.get('semantic_path', [])
        self.turn_id = context.get('turn_id')
        self.clock_id = monotonic_domain()
        self.enabled = enabled
        self.capture_content = capture_content
        self.strict = strict
        self._emit = emit
        self.attempted = self.failed = self.emit_ns = self.wire_bytes = self.metric_errors = 0
        if type(batch_size) is not int or not 1 <= batch_size <= 64:
            raise ValueError("batch_size must be 1..64")
        self.batch_size = batch_size
        self._pending: list[dict] = []
        self._pending_bytes = 0
        self._emit_lock = threading.RLock()
        self._owner_pid = os.getpid()
        self.acknowledged = self.batch_requests = 0
        if delivery not in {'sync', 'background'}:
            raise ValueError('delivery must be sync or background')
        self.delivery = delivery
        self._background = None
        if delivery == 'background' and enabled:
            from allox_agent_monitor.background import BackgroundExporter
            self._background = BackgroundExporter(emit, batch_size=batch_size, capacity=queue_capacity,
                max_bytes=queue_bytes, interval=flush_interval, timeout=export_timeout)

    def emit(self, event: dict) -> None:
        if not self.enabled:
            return
        if self._background is not None:
            if os.getpid() != self._owner_pid:
                raise RuntimeError('create a new Tracer after fork')
            try:
                self._background.submit(event)
            except Exception:
                if self.strict:
                    raise
            return
        if self.batch_size > 1:
            with self._emit_lock:
                if os.getpid() != self._owner_pid:
                    raise RuntimeError("create a new Tracer after fork")
                try:
                    encoded = json.dumps(event, allow_nan=False).encode()
                    if len(encoded) > 60000:
                        raise ValueError("buffered event too large")
                except (TypeError, ValueError):
                    self.attempted += 1
                    self.failed += 1
                    if self.strict:
                        raise
                    return
                if self._pending and self._pending_bytes+len(encoded)+1024 > 65536:
                    self.flush()
                self.attempted += 1
                self.wire_bytes += len(encoded)
                self._pending.append(json.loads(encoded))
                self._pending_bytes += len(encoded)+2
                if len(self._pending) >= self.batch_size:
                    self.flush()
            return
        started = time.perf_counter_ns()
        self.attempted += 1
        try:
            encoded = json.dumps(event, allow_nan=False).encode()
            self.wire_bytes += len(encoded)
            ack = self._emit(event)
            if isinstance(ack, dict) and (ack.get("accepted") is False or ack.get("ok") is False):
                raise RuntimeError("collector rejected event")
            self.acknowledged += 1
        except Exception:
            self.failed += 1
            if self.strict:
                raise
        finally:
            self.emit_ns += time.perf_counter_ns() - started

    def flush(self) -> None:
        """Explicit durability barrier; no automatic retries after ambiguous delivery."""
        if os.getpid() != self._owner_pid:
            raise RuntimeError('create a new Tracer after fork')
        if self._background is not None:
            try:
                self._background.flush()
            except Exception:
                if self.strict:
                    raise
            return
        with self._emit_lock:
            if os.getpid() != self._owner_pid:
                raise RuntimeError("create a new Tracer after fork")
            if not self._pending:
                return
            events, self._pending = self._pending, []
            self._pending_bytes = 0
            started = time.perf_counter_ns()
            self.batch_requests += 1
            try:
                ack = self._emit({"event": "batch", "events": events})
                if not isinstance(ack, dict) or ack.get("accepted") is not True or ack.get("durable") is not True or ack.get("event_count") != len(events):
                    raise RuntimeError("batch lacks a complete durable acknowledgement")
                self.acknowledged += len(events)
            except Exception:
                self.failed += len(events)
                if self.strict:
                    raise
            finally:
                self.emit_ns += time.perf_counter_ns()-started

    def close(self):
        if os.getpid() != self._owner_pid:
            raise RuntimeError('create a new Tracer after fork')
        if self._background is not None:
            try:
                self._background.close()
            except Exception:
                if self.strict:
                    raise
        else:
            self.flush()

    def barrier(self):
        """Fail-closed durability boundary, including in best-effort profiling mode."""
        if self._background is not None:
            self._background.flush()
        else:
            self.flush()
        if not self.health()['complete']:
            raise RuntimeError('monitoring durability barrier is incomplete')

    def checkpoint(self, create_checkpoint: Callable, *args, **kwargs):
        """Trusted integration helper; never invoke checkpoint creation before durable telemetry."""
        self.barrier()
        return create_checkpoint(*args, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self.close()
        except Exception:
            if exc is None:
                raise
        return False

    def span(self, name: str, kind: str = "tool", **fields) -> Span:
        return Span(self, name, kind, **fields)

    def phase(self, name: str) -> Span:
        return self.span(name, "phase")

    def call_tool(self, name: str, function: Callable, *args, **kwargs):
        """Measure executor-internal time separately from the outer call's end-to-end time."""
        with self.span(name, "tool", input=payload({"args": args, "kwargs": kwargs},
                                                 self.capture_content)) as outer:
            with self.phase("tool_execution") as execution:
                cpu_started = time.thread_time_ns()
                started = time.perf_counter_ns()
                try:
                    result = function(*args, **kwargs)
                finally:
                    execution.metric("executor.execution_ns", time.perf_counter_ns()-started)
                    execution.metric("executor.thread_cpu_ns", time.thread_time_ns()-cpu_started)
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

    def stream_model(self, model, function, *args, **kwargs):
        with self.span(model, 'llm', model=model, input=payload(
            {'args': args, 'kwargs': kwargs}, self.capture_content)) as span:
            started = time.perf_counter_ns()
            count = 0
            stream = None
            try:
                stream = function(*args, **kwargs)
                for chunk in stream:
                    if count == 0:
                        span.metric('client.first_chunk_ns', time.perf_counter_ns()-started)
                    count += 1
                    record_vllm_metrics(span, chunk)
                    yield chunk
            finally:
                span.metric('client.request_ns', time.perf_counter_ns()-started)
                span.metric('client.chunk_count', count)
                close = getattr(stream, 'close', None)
                if close:
                    close()

    async def astream_model(self, model, function, *args, **kwargs):
        with self.span(model, 'llm', model=model, input=payload(
            {'args': args, 'kwargs': kwargs}, self.capture_content)) as span:
            started = time.perf_counter_ns()
            count = 0
            stream = None
            try:
                stream = function(*args, **kwargs)
                if hasattr(stream, '__await__'):
                    stream = await stream
                async for chunk in stream:
                    if count == 0:
                        span.metric('client.first_chunk_ns', time.perf_counter_ns()-started)
                    count += 1
                    record_vllm_metrics(span, chunk)
                    yield chunk
            finally:
                span.metric('client.request_ns', time.perf_counter_ns()-started)
                span.metric('client.chunk_count', count)
                close = getattr(stream, 'aclose', None)
                if close:
                    await close()

    def health(self) -> dict:
        if self._background is not None:
            result = self._background.health()
            return {**result, 'enabled': self.enabled, 'delivery': self.delivery,
                'batch_size': self.batch_size, 'metric_errors': self.metric_errors, 'strict': self.strict,
                'complete': result['complete'] and self.metric_errors == 0}
        return {"enabled": self.enabled, "attempted_events": self.attempted,
                "failed_events": self.failed, "emit_ns": self.emit_ns,
                "serialized_bytes": self.wire_bytes,
                "complete": self.failed == 0 and self.metric_errors == 0 and not self._pending
                            and self.acknowledged == self.attempted,
                "acknowledged_events": self.acknowledged, "pending_events": len(self._pending),
                "batch_requests": self.batch_requests, "batch_size": self.batch_size,
                "metric_errors": self.metric_errors, "strict": self.strict}


def record_vllm_metrics(span: Span, response: Any) -> None:
    """vLLM metrics are provider-reported, never inferred from client elapsed time."""
    if not isinstance(response, dict) and callable(getattr(response, "model_dump", None)):
        response = response.model_dump()
    if not isinstance(response, dict):
        return
    if isinstance(response.get('response'), dict):
        response = response['response']
    metrics = response.get("metrics")
    if isinstance(metrics, dict):
        for name in ("queue_time_ms", "time_to_first_token_ms", "generation_time_ms",
                     "mean_itl_ms", "tokens_per_second"):
            value = metrics.get(name)
            if value is not None:
                _provider_metric(span, "provider." + name, value)
    usage = response.get("usage")
    if isinstance(usage, dict):
        for name in ("prompt_tokens", "completion_tokens", "input_tokens", "output_tokens"):
            if usage.get(name) is not None:
                _provider_metric(span, "provider." + name, usage[name])


def _provider_metric(span: Span, name: str, value: Any) -> None:
    try:
        span.metric(name, value)
    except ValueError:
        span.tracer.metric_errors += 1
        if span.tracer.strict:
            raise
