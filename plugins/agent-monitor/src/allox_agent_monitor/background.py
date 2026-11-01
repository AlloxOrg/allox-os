"""Opt-in bounded profiling exporter. Enqueue is NOT a durable acknowledgement."""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque


class BackgroundExporter:
    def __init__(self, emit, *, batch_size, capacity, max_bytes, interval, timeout):
        if type(capacity) is not int or not 1 <= capacity <= 65536:
            raise ValueError('queue_capacity must be 1..65536')
        if type(max_bytes) is not int or not 65536 <= max_bytes <= 64*1024*1024:
            raise ValueError('queue_bytes must be 64 KiB..64 MiB')
        if type(interval) not in (int, float) or not 0 < interval <= 5:
            raise ValueError('flush_interval must be 0..5 seconds (exclusive zero)')
        if type(timeout) not in (int, float) or not 0 < timeout <= 60:
            raise ValueError('export_timeout must be 0..60 seconds (exclusive zero)')
        self._emit = emit
        self._batch_size, self._capacity, self._max_bytes = batch_size, capacity, max_bytes
        self._interval, self._timeout = interval, timeout
        self._pid = os.getpid()
        self._condition = threading.Condition()
        self._queue: deque[tuple[bytes, float]] = deque()
        self._thread = None
        self._closing = False
        self._error = None
        self._force_target = 0
        self.attempted = self.acknowledged = self.failed = self.pending_bytes = 0
        self.wire_bytes = self.emit_ns = self.batch_requests = self.queue_wait_ns = 0
        self.peak_pending_events = self.peak_pending_bytes = self.queue_full_waits = 0

    def _check_pid(self):
        if os.getpid() != self._pid:
            raise RuntimeError('create a new Tracer after fork')

    def submit(self, event):
        self._check_pid()
        try:
            encoded = json.dumps(event, allow_nan=False).encode()
            if len(encoded) > 60000:
                raise ValueError('buffered event too large')
        except (ValueError, TypeError):
            with self._condition:
                self.attempted += 1
                self.failed += 1
            raise
        started = time.perf_counter_ns()
        deadline = time.monotonic()+self._timeout
        with self._condition:
            waited = False
            while (self.attempted-self.acknowledged-self.failed >= self._capacity
                   or self.pending_bytes+len(encoded) > self._max_bytes):
                if self._closing or self._error is not None:
                    break
                if not waited:
                    self.queue_full_waits += 1
                    waited = True
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    self.attempted += 1
                    self.failed += 1
                    self.queue_wait_ns += time.perf_counter_ns()-started
                    raise TimeoutError('profiling queue backpressure timed out; event not accepted')
                self._condition.wait(remaining)
            self.queue_wait_ns += time.perf_counter_ns()-started
            self.attempted += 1
            if self._closing or self._error is not None:
                self.failed += 1
                raise RuntimeError('profiling exporter closed or failed') from self._error
            self.wire_bytes += len(encoded)
            self.pending_bytes += len(encoded)
            self._queue.append((encoded, time.monotonic()))
            self.peak_pending_events = max(self.peak_pending_events,
                self.attempted-self.acknowledged-self.failed)
            self.peak_pending_bytes = max(self.peak_pending_bytes, self.pending_bytes)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name='allox-profile-export', daemon=True)
                self._thread.start()
            self._condition.notify_all()

    def _run(self):
        while True:
            with self._condition:
                while not self._queue:
                    if self._closing:
                        return
                    self._condition.wait()
                while (not self._closing and self._force_target <= self.acknowledged+self.failed
                       and len(self._queue) < self._batch_size):
                    delay = self._queue[0][1]+self._interval-time.monotonic()
                    if delay <= 0:
                        break
                    self._condition.wait(delay)
                batch = []
                size = 64
                while self._queue and len(batch) < self._batch_size:
                    encoded, _ = self._queue[0]
                    if batch and size+len(encoded)+2 > 65536:
                        break
                    batch.append(self._queue.popleft()[0])
                    size += len(encoded)+2
                self.batch_requests += 1
            started = time.perf_counter_ns()
            error = None
            try:
                acknowledgement = self._emit({'event': 'batch',
                    'events': [json.loads(e) for e in batch]})
                if (not isinstance(acknowledgement, dict)
                    or acknowledgement.get('accepted') is not True
                    or acknowledgement.get('durable') is not True
                    or type(acknowledgement.get('event_count')) is not int
                    or acknowledgement.get('event_count') != len(batch)):
                    raise RuntimeError('batch lacks a complete durable acknowledgement')
            except Exception as exc:  # no retries after an ambiguous acknowledgement
                error = exc
            with self._condition:
                self.emit_ns += time.perf_counter_ns()-started
                self.pending_bytes -= sum(map(len, batch))
                if error is None:
                    self.acknowledged += len(batch)
                else:
                    # Stop this stream after failure; later ends must not pass missing starts.
                    self.failed += len(batch)+len(self._queue)
                    self.pending_bytes = 0
                    self._queue.clear()
                    self._error = error
                self._condition.notify_all()
                if error is not None:
                    return

    def flush(self):
        """Wait for all events submitted before this barrier, including an in-flight batch."""
        self._check_pid()
        deadline = time.monotonic()+self._timeout
        with self._condition:
            target = self.attempted
            self._force_target = max(self._force_target, target)
            self._condition.notify_all()
            while self.acknowledged+self.failed < target:
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('profiling durability barrier timed out; delivery remains unconfirmed')
                self._condition.wait(remaining)
            if self.failed:
                raise RuntimeError('profiling durability barrier has failed events') from self._error

    def close(self):
        self._check_pid()
        # Fence new submitters before draining, so close cannot race past a new event.
        with self._condition:
            self._closing = True
            self._condition.notify_all()
        try:
            self.flush()
        finally:
            if self._thread is not None:
                self._thread.join(self._timeout)
                if self._thread.is_alive():
                    raise TimeoutError('profiling worker did not stop; pending delivery is not durable')

    def health(self):
        self._check_pid()
        with self._condition:
            pending = self.attempted-self.acknowledged-self.failed
            return {'attempted_events': self.attempted, 'acknowledged_events': self.acknowledged,
                'failed_events': self.failed, 'pending_events': pending, 'pending_bytes': self.pending_bytes,
                'complete': self.failed == 0 and pending == 0,
                'serialized_bytes': self.wire_bytes, 'emit_ns': self.emit_ns,
                'batch_requests': self.batch_requests, 'queue_wait_ns': self.queue_wait_ns,
                'queue_full_waits': self.queue_full_waits, 'queue_capacity': self._capacity,
                'queue_bytes_limit': self._max_bytes, 'peak_pending_events': self.peak_pending_events,
                'peak_pending_bytes': self.peak_pending_bytes,
                'worker_alive': self._thread is not None and self._thread.is_alive(),
                'closed': self._closing, 'export_failed': self._error is not None,
                'durability': 'only acknowledged events; memory queue is not crash durable'}
