"""Opt-in multi-action profiling persistence; safety barriers remain synchronous."""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque

from allox_agent_monitor.queue_exporter import QueuedExporter


class ActionBatchExporter:
    def __init__(self, emit, *, batch_size, actions, interval, capacity, max_bytes, timeout):
        if type(actions) is not int or not 1 <= actions <= 1024:
            raise ValueError('action_batch_size must be 1..1024')
        if type(interval) not in (int, float) or not 0 < interval <= 5:
            raise ValueError('action_flush_interval must be 0..5 seconds (exclusive zero)')
        if type(capacity) is not int or not 1 <= capacity <= 65536:
            raise ValueError('queue_capacity must be 1..65536')
        if type(max_bytes) is not int or not 65536 <= max_bytes <= 64*1024*1024:
            raise ValueError('queue_bytes must be 64 KiB..64 MiB')
        if type(timeout) not in (int, float) or not 0 < timeout <= 60:
            raise ValueError('export_timeout must be 0..60 seconds (exclusive zero)')
        self.inner = QueuedExporter(emit, batch_size=batch_size)
        self.actions, self.interval, self.timeout = actions, interval, timeout
        self.capacity, self.max_bytes = capacity, max_bytes
        self.pid = os.getpid()
        self.cv = threading.Condition()
        self.queue = deque()
        self.thread = None
        self.closing = False
        self.error = None
        self.attempted = self.acknowledged = self.failed = self.pending_bytes = 0
        self.completed_actions = self.durable_actions = self.force_target = 0
        self.first_pending = None
        self.wire_bytes = self.emit_ns = self.requests = self.flushes = 0
        self.peak_pending = self.peak_bytes = 0

    def _pid(self):
        if os.getpid() != self.pid:
            raise RuntimeError('create a new Tracer after fork')

    def _fail(self, exc):
        self.error = self.error or exc
        self.failed = self.attempted - self.acknowledged
        self.queue.clear()
        self.pending_bytes = 0
        self.cv.notify_all()

    def submit(self, event):
        self._pid()
        try:
            encoded = json.dumps(event, allow_nan=False).encode()
            if len(encoded) > 60000:
                raise ValueError('buffered event too large')
        except Exception as exc:
            with self.cv:
                self.attempted += 1
                self._fail(exc)
            raise
        deadline = time.monotonic() + self.timeout
        with self.cv:
            while (self.attempted - self.acknowledged - self.failed >= self.capacity
                   or self.pending_bytes + len(encoded) > self.max_bytes):
                if self.closing or self.error:
                    break
                # Capacity is another early submission trigger, never silently drop.
                self.force_target = self.attempted
                self.cv.notify_all()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.attempted += 1
                    exc = TimeoutError('action profiling queue backpressure timed out')
                    self._fail(exc)
                    raise exc
                self.cv.wait(remaining)
            self.attempted += 1
            if self.closing or self.error:
                previous = self.error
                exc = RuntimeError('action exporter closed or failed')
                self._fail(exc)
                raise exc from previous
            if self.first_pending is None:
                self.first_pending = time.monotonic()
            self.queue.append((encoded, time.monotonic()))
            self.pending_bytes += len(encoded)
            self.wire_bytes += len(encoded)
            self.peak_pending = max(self.peak_pending, self.attempted - self.acknowledged)
            self.peak_bytes = max(self.peak_bytes, self.pending_bytes)
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, name='allox-action-export', daemon=True)
                self.thread.start()
            self.cv.notify_all()

    def action_finished(self):
        self._pid()
        with self.cv:
            if self.closing or self.error:
                raise RuntimeError('action exporter closed or failed') from self.error
            self.completed_actions += 1
            self.cv.notify_all()

    def _run(self):
        delivered = 0
        buffered_bytes = 0
        while True:
            with self.cv:
                while True:
                    if self.error:
                        return
                    pending = self.attempted > self.acknowledged
                    due = pending and (self.closing or self.force_target > self.acknowledged
                        or self.completed_actions - self.durable_actions >= self.actions
                        or (self.first_pending is not None
                            and time.monotonic() >= self.first_pending + self.interval))
                    if self.queue or due:
                        break
                    if self.closing:
                        return
                    delay = (max(0, self.first_pending + self.interval - time.monotonic())
                             if pending and self.first_pending is not None else None)
                    self.cv.wait(delay)
                items = list(self.queue)
                self.queue.clear()
                action_target = self.completed_actions
            started = time.perf_counter_ns()
            try:
                for encoded, _ in items:
                    self.inner.submit(json.loads(encoded))
                    delivered += 1
                    buffered_bytes += len(encoded)
                if due:
                    self.inner.flush()
                inner_health = self.inner.health()
            except Exception as exc:
                with self.cv:
                    self._fail(exc)
                return
            with self.cv:
                if self.error:
                    return  # A producer failure during I/O cannot become a complete ACK.
                self.emit_ns += time.perf_counter_ns() - started
                self.requests = inner_health['batch_requests']
                if due:
                    # All events through this snapshot are now FULL-acknowledged.
                    self.acknowledged = delivered
                    self.durable_actions = action_target
                    self.pending_bytes -= buffered_bytes
                    buffered_bytes = 0
                    self.flushes += 1
                    self.first_pending = self.queue[0][1] if self.queue else None
                self.cv.notify_all()

    def flush(self):
        self._pid()
        deadline = time.monotonic() + self.timeout
        with self.cv:
            target = self.attempted
            self.force_target = max(self.force_target, target)
            self.cv.notify_all()
            while self.acknowledged < target and not self.error:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('action profiling flush timed out; durability unconfirmed')
                self.cv.wait(remaining)
            if self.error or self.failed:
                raise RuntimeError('action profiling has failed events; durability unconfirmed') from self.error

    def close(self):
        self._pid()
        with self.cv:
            self.closing = True
            self.cv.notify_all()
        try:
            self.flush()
        finally:
            if self.thread:
                self.thread.join(self.timeout)
                if self.thread.is_alive():
                    raise TimeoutError('action profiling worker did not stop')

    def health(self):
        self._pid()
        with self.cv:
            return {'attempted_events': self.attempted, 'acknowledged_events': self.acknowledged,
                    'failed_events': self.failed,
                    'pending_events': self.attempted - self.acknowledged - self.failed,
                    'pending_bytes': self.pending_bytes, 'peak_pending_events': self.peak_pending,
                    'peak_pending_bytes': self.peak_bytes, 'serialized_bytes': self.wire_bytes,
                    'emit_ns': self.emit_ns, 'batch_requests': self.requests,
                    'worker_alive': bool(self.thread and self.thread.is_alive()),
                    'action_batch_size': self.actions, 'action_flush_interval': self.interval,
                    'completed_actions': self.completed_actions, 'durable_actions': self.durable_actions,
                    'flush_requests': self.flushes, 'closed': self.closing,
                    'complete': not self.error and not self.failed
                        and self.attempted == self.acknowledged}
