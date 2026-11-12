"""Opt-in collector queue. Accepted != durable; only flush proves persistence."""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections import deque

from allox.workspace.store import WorkspaceError
from allox_agent_monitor.store import identifier


class QueuedTraceWriter:
    def __init__(self, store, *, interval=.02, max_events=4096, max_bytes=8*1024*1024,
                 channel_events=1024, max_channels=1024, timeout=5):
        if (not 0 < interval <= 5 or not 1 <= max_events <= 65536
                or not 65536 <= max_bytes <= 64*1024*1024 or not 1 <= channel_events <= max_events
                or not 1 <= max_channels <= 4096 or not 0 < timeout <= 60):
            raise ValueError("invalid queued collector bounds")
        self.store, self.interval, self.timeout = store, interval, timeout
        self.max_events, self.max_bytes = max_events, max_bytes
        self.channel_events, self.max_channels = channel_events, max_channels
        self.instance = uuid.uuid4().hex
        self.pid = os.getpid()
        self.cv = threading.Condition()
        self.queue = deque()
        self.channels = {}
        self.pending_events = self.pending_bytes = 0
        self.closing = False
        self.worker = None
        self.commits = 0

    def _pid(self):
        if os.getpid() != self.pid:
            raise WorkspaceError("collector queue cannot be reused after fork")

    def submit(self, agent, session, event, *, source="framework-sdk", process_run_id=None):
        self._pid()
        identifier(agent, "agent")
        identifier(session, "session")
        if not isinstance(event, dict):
            raise WorkspaceError("monitor event must be an object")
        envelope = event if event.get("event") == "batch" else {"event": "batch", "events": [event]}
        events, _ = self.store._batch_events(envelope)
        encoded = json.dumps(envelope, allow_nan=False).encode()
        key = (agent, session, source, process_run_id)
        count = len(events)
        if count > min(self.max_events, self.channel_events):
            raise WorkspaceError('batch exceeds collector queue event capacity')
        deadline = time.monotonic() + self.timeout
        with self.cv:
            if key not in self.channels:
                if len(self.channels) >= self.max_channels:
                    raise WorkspaceError("collector channel limit exceeded")
                self.channels[key] = {"id": uuid.uuid4().hex, "submitted": 0, "durable": 0,
                                      "pending": 0, "error": None, "sealed": False, "force": 0}
            channel = self.channels[key]
            while (self.pending_events + count > self.max_events
                   or self.pending_bytes + len(encoded) > self.max_bytes
                   or channel["pending"] + count > self.channel_events):
                self._ready(channel)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkspaceError("collector queue backpressure timeout; not accepted")
                self.cv.wait(remaining)
            self._ready(channel)
            channel["submitted"] += 1
            sequence = channel["submitted"]
            channel["pending"] += count
            self.pending_events += count
            self.pending_bytes += len(encoded)
            self.queue.append((key, sequence, encoded, count, time.monotonic()))
            if self.worker is None:
                self.worker = threading.Thread(target=self._run, name="allox-monitor-writer", daemon=True)
                self.worker.start()
            self.cv.notify_all()
            return {"accepted": True, "durable": False, "collector_id": self.instance,
                    "stream_id": channel["id"], "sequence": sequence, "event_count": count}

    def _ready(self, channel):
        if self.closing or channel["sealed"] or channel["error"]:
            raise WorkspaceError("collector channel is closed or failed")

    def _run(self):
        while True:
            with self.cv:
                while not self.queue:
                    if self.closing:
                        return
                    self.cv.wait()
                while not self.closing and not any(c["force"] > c["durable"] and not c['error']
                                                   for c in self.channels.values()):
                    delay = self.queue[0][4] + self.interval - time.monotonic()
                    if delay <= 0 or sum(item[3] for item in self.queue) >= 512:
                        break
                    self.cv.wait(delay)
                    if not self.queue:
                        break
                groups, count, size = {}, 0, 0
                while self.queue:
                    item = self.queue[0]
                    if count + item[3] > 512 or size + len(item[2]) > 1024*1024:
                        break
                    self.queue.popleft()
                    groups.setdefault(item[0], []).append(item)
                    count += item[3]
                    size += len(item[2])
            for key, group in groups.items():
                error = None
                try:
                    requests = [(key[0], key[1], json.loads(item[2]), key[2], key[3]) for item in group]
                    self.store.ingest_group(requests)
                except Exception as exc:
                    error = type(exc).__name__  # No payload/secret in channel health.
                with self.cv:
                    channel = self.channels[key]
                    for item in group:
                        channel["pending"] -= item[3]
                        self.pending_events -= item[3]
                        self.pending_bytes -= len(item[2])
                    if error is None:
                        channel["durable"] = group[-1][1]
                        self.commits += 1
                    else:
                        channel["error"] = error
                        remaining = deque()
                        for item in self.queue:
                            if item[0] == key:
                                channel["pending"] -= item[3]
                                self.pending_events -= item[3]
                                self.pending_bytes -= len(item[2])
                            else:
                                remaining.append(item)
                        self.queue = remaining
                    self.cv.notify_all()

    def flush(self, key, receipt=None, *, seal=False):
        self._pid()
        deadline = time.monotonic() + self.timeout
        with self.cv:
            channel = self.channels.get(key)
            if channel is None:
                if receipt is not None:
                    raise WorkspaceError("unknown collector receipt")
                return {"durable": True, "durable_sequence": 0}
            if receipt is not None and (
                not isinstance(receipt, dict) or receipt.get("collector_id") != self.instance
                or receipt.get("stream_id") != channel["id"]
                or type(receipt.get("sequence")) is not int
                or not 1 <= receipt["sequence"] <= channel["submitted"]
            ):
                raise WorkspaceError("stale or foreign collector receipt")
            if seal:
                channel["sealed"] = True
            target = channel["submitted"] if receipt is None else receipt["sequence"]
            channel["force"] = max(channel["force"], target)
            self.cv.notify_all()
            while channel["durable"] < target and not channel["error"]:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkspaceError("collector flush timed out; durability unconfirmed")
                self.cv.wait(remaining)
            if channel["error"]:
                raise WorkspaceError("collector flush failed; channel is incomplete")
            return {"durable": True, "collector_id": self.instance, "stream_id": channel["id"],
                    "durable_sequence": channel["durable"]}

    def flush_session(self, agent, session):
        with self.cv:
            keys = [key for key in self.channels if key[:2] == (agent, session)]
        for key in keys:
            self.flush(key)

    def status(self):
        with self.cv:
            failed = sum(c["error"] is not None for c in self.channels.values())
            return {"collector_id": self.instance, "pending_events": self.pending_events,
                    "pending_bytes": self.pending_bytes, "failed_channels": failed,
                    "complete": self.pending_events == 0 and failed == 0,
                    "group_commits": self.commits, "channels": len(self.channels),
                    "crash_durable": "only flush-confirmed sequences; memory queue is not durable"}

    def close(self):
        self._pid()
        with self.cv:
            self.closing = True
            keys = list(self.channels)
            self.cv.notify_all()
        errors = []
        for key in keys:
            try:
                self.flush(key)
            except WorkspaceError as exc:
                errors.append(exc)
        if self.worker:
            self.worker.join(self.timeout)
            if self.worker.is_alive():
                raise WorkspaceError("collector writer did not stop; durability unconfirmed")
        if errors:
            raise errors[0]


class BoundQueueSink:
    """Trusted in-process adapter with the same receipt protocol as the socket transport."""
    def __init__(self, writer, agent, session, *, source="framework-sdk", process_run_id=None):
        self.writer = writer
        self.key = (agent, session, source, process_run_id)

    def __call__(self, event):
        return self.writer.submit(self.key[0], self.key[1], event,
                                  source=self.key[2], process_run_id=self.key[3])

    def flush(self, receipt):
        return self.writer.flush(self.key, receipt)
