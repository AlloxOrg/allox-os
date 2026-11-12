"""Bounded content-addressed JSONL indexes, rehashed on EVERY query.

No metadata-only cache hits: truncation, symlink replacement and same-size
edits still pass through EvidenceReader and a fresh full SHA256 check.
"""
import hashlib
import json
import os
import threading
from collections import OrderedDict

from allox.workspace.store import WorkspaceError
from allox_trace_correlation.reader import MAX_LINE

MAX_BUFFER = 8 * 1024 * 1024
MAX_INDEX_ENTRIES = 4
MAX_INDEX_ROWS = 200000


class StreamIndex:
    def __init__(self):
        self.lock = threading.Lock()
        self.entries = OrderedDict()
        self.rows = 0

    def page(self, source, run, entry, offset, limit):
        if os.fstat(source.fileno()).st_size > MAX_BUFFER:
            return None
        data = source.read(MAX_BUFFER + 1)
        if len(data) > MAX_BUFFER:
            raise WorkspaceError("event stream changed during query")
        digest = hashlib.sha256(data).hexdigest()
        if digest != entry.get("sha256") or len(data) != entry.get("size_bytes"):
            raise WorkspaceError("event stream digest or length mismatch")
        key = (run, digest, len(data))
        with self.lock:
            offsets = self.entries.get(key)
            if offsets is not None:
                self.entries.move_to_end(key)
        if offsets is None:
            offsets = [0]
            position = 0
            while position < len(data):
                end = data.find(b"\n", position)
                end = len(data) if end < 0 else end + 1
                if end - position > MAX_LINE or len(offsets) > 100000:
                    raise WorkspaceError("event stream exceeds bounded limits")
                self._event(data[position:end], run)
                offsets.append(end)
                position = end
            offsets = tuple(offsets)
            with self.lock:
                if key not in self.entries:
                    self.entries[key] = offsets
                    self.rows += len(offsets)
                    while len(self.entries) > MAX_INDEX_ENTRIES or self.rows > MAX_INDEX_ROWS:
                        _, old = self.entries.popitem(last=False)
                        self.rows -= len(old)
        selected, size = [], 0
        count = len(offsets) - 1
        for index in range(offset, min(count, offset + limit)):
            event = self._event(data[offsets[index]:offsets[index + 1]], run)
            size += len(json.dumps(event, ensure_ascii=True).encode())
            if size > 2 * 1024 * 1024:
                break
            selected.append({"event_index": index, "event": event})
        return count, selected

    @staticmethod
    def _event(raw, run):
        try:
            event = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise WorkspaceError("invalid event stream") from exc
        if not isinstance(event, dict) or ("run_id" in event and event["run_id"] != run):
            raise WorkspaceError("event identity mismatch")
        return event
