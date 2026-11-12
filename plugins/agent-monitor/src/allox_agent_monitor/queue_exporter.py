"""Bounded producer batching with explicit collector durability receipts.

No second producer thread: sending a full batch waits only for admission, not
fsync. The collector owns batching/commit scheduling. Failures are sticky.
"""
from __future__ import annotations

import json
import os
import threading
import time


class QueuedExporter:
    def __init__(self, emit, *, batch_size=32):
        if not callable(getattr(emit, 'flush', None)):
            raise ValueError('queued delivery requires a receipt-aware flush transport')
        self.emit = emit
        self.batch_size = batch_size
        self.lock = threading.RLock()
        self.pid = os.getpid()
        self.pending = []
        self.nbytes = 0
        self.attempted = self.enqueued = self.acknowledged = self.failed = 0
        self.requests = self.emit_ns = self.wire_bytes = 0
        self.receipt = None
        self.error = False
        self.closed = False

    def _pid(self):
        if os.getpid() != self.pid:
            raise RuntimeError('create a new Tracer after fork')

    def submit(self, event):
        self._pid()
        with self.lock:
            self.attempted += 1
            try:
                if self.closed or self.error:
                    raise RuntimeError('queued exporter closed or failed')
                encoded = json.dumps(event, allow_nan=False).encode()
                if len(encoded) > 60000:
                    raise ValueError('buffered event too large')
                if self.pending and self.nbytes + len(encoded) + 1024 > 65536:
                    self._send()
                self.pending.append(json.loads(encoded))
                self.nbytes += len(encoded) + 2
                self.wire_bytes += len(encoded)
                if len(self.pending) >= self.batch_size:
                    self._send()
            except Exception:
                self.error = True
                self.failed += 1
                raise

    def _send(self):
        if not self.pending:
            return
        batch, self.pending = self.pending, []
        self.nbytes = 0
        started = time.perf_counter_ns()
        self.requests += 1
        try:
            ack = self.emit({'event': 'batch', 'events': batch})
            if (not isinstance(ack, dict) or ack.get('accepted') is not True
                    or ack.get('durable') is not False or ack.get('event_count') != len(batch)
                    or not isinstance(ack.get('collector_id'), str) or not ack['collector_id']
                    or not isinstance(ack.get('stream_id'), str) or not ack['stream_id']
                    or type(ack.get('sequence')) is not int or ack['sequence'] < 1):
                raise RuntimeError('collector lacks a complete admission receipt')
            if self.receipt and (ack['collector_id'] != self.receipt['collector_id']
                    or ack['stream_id'] != self.receipt['stream_id']
                    or ack['sequence'] <= self.receipt['sequence']):
                raise RuntimeError('collector receipt changed or regressed')
            self.receipt = {k: ack[k] for k in ('collector_id', 'stream_id', 'sequence')}
            self.enqueued += len(batch)
        except Exception:
            self.error = True
            raise
        finally:
            self.emit_ns += time.perf_counter_ns() - started

    def _flush(self):
        if self.error:
            raise RuntimeError('queued exporter has failed events; durability unconfirmed')
        try:
            self._send()
            if self.receipt is not None and self.enqueued != self.acknowledged:
                ack = self.emit.flush(dict(self.receipt))
                if (not isinstance(ack, dict) or ack.get('durable') is not True
                        or ack.get('collector_id') != self.receipt['collector_id']
                        or ack.get('stream_id') != self.receipt['stream_id']
                        or type(ack.get('durable_sequence')) is not int
                        or ack['durable_sequence'] < self.receipt['sequence']):
                    raise RuntimeError('collector lacks a complete durable receipt')
                self.acknowledged = self.enqueued
        except Exception:
            self.error = True
            raise

    def flush(self):
        self._pid()
        with self.lock:
            self._flush()

    def close(self):
        self._pid()
        with self.lock:
            self.closed = True
            self._flush()

    def health(self):
        self._pid()
        with self.lock:
            return {'attempted_events': self.attempted, 'enqueued_events': self.enqueued,
                    'acknowledged_events': self.acknowledged, 'failed_events': self.failed,
                    'pending_events': self.attempted - self.acknowledged,
                    'batch_requests': self.requests, 'emit_ns': self.emit_ns,
                    'serialized_bytes': self.wire_bytes, 'closed': self.closed,
                    'complete': not self.error and self.attempted == self.acknowledged}
