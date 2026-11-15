"""Opt-in serial native worker. Each invocation retains the v2 verifier contract.

Only a validated, nonce/hash-bound IDLE ACK authorizes reuse. Unexpected output,
EOF or an unfinished invocation poisons the worker; no implicit restart occurs.
"""
from __future__ import annotations

import atexit
import copy
import json
import os
import signal
import subprocess
import threading
import time
from collections import deque

from allox.workspace.store import WorkspaceError

LIMIT = 64 * 1024
ACK_KEYS = {"resident_protocol", "status", "run_id", "epoch", "finish_nonce",
            "report_sha256", "artifact_manifest_sha256", "sequence",
            "kernel_objects_reused", "resident_resources", "resident_objects"}


class _Input:
    def __init__(self, invocation):
        self.invocation, self.closed, self.pending = invocation, False, b""

    def write(self, value):
        if self.closed or self.pending or len(value) > LIMIT:
            raise OSError("resident invocation stdin is closed or unbounded")
        self.pending = bytes(value)
        return len(value)

    def flush(self):
        value, self.pending = self.pending, b""
        if value:
            self.invocation.owner.finish(self.invocation, value)

    def close(self):
        self.closed = True
        self.pending = b""


class ResidentInvocation:
    """Per-run streams: wait() means a verified IDLE seal ACK, NOT worker exit."""
    def __init__(self, owner, run_id, epoch):
        self.owner, self.run_id, self.epoch = owner, run_id, epoch
        self.pid, self.returncode = owner.process.pid, None
        self.done = threading.Event()
        self.ready = self.terminal = None
        self.finish_sent = False
        self.evidence = None
        read, write = os.pipe()
        self.stdout, self._stdout = os.fdopen(read, "rb"), os.fdopen(write, "wb", buffering=0)
        read, write = os.pipe()
        self.stderr, self._stderr = os.fdopen(read, "rb"), os.fdopen(write, "wb", buffering=0)
        self.stdin = _Input(self)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if not self.done.wait(timeout):
            raise subprocess.TimeoutExpired("resident run", timeout)
        return self.returncode

    def terminate(self):
        self.owner.process.terminate()

    def kill(self):
        self.owner.process.kill()

    def complete(self, status, terminal=None):
        if self.done.is_set():
            return
        if terminal is not None:
            self._stdout.write(json.dumps(terminal).encode()+b"\n")
        self.returncode = status
        self._stdout.close()
        self._stderr.close()
        self.done.set()


class ResidentObserver:
    def __init__(self, config):
        self.owner_pid = os.getpid()
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.active = None
        self.poisoned = None
        self.closing = False
        self.closed = threading.Event()
        self.sequence = 0
        self.identity = None
        self.diagnostics = deque(maxlen=64)
        self.process = subprocess.Popen([str(config.observer_path), "--resident-stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            close_fds=True, start_new_session=True, bufsize=0,
            env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})
        self.reader = threading.Thread(target=self._read, daemon=True, name="allox-resident-protocol")
        self.errors = threading.Thread(target=self._errors, daemon=True, name="allox-resident-stderr")
        self.reader.start()
        self.errors.start()

    def _send(self, value):
        if not value.endswith(b"\n") or len(value) > LIMIT:
            raise WorkspaceError("resident request exceeds protocol bounds")
        with self.write_lock:
            assert self.process.stdin is not None
            self.process.stdin.write(value)
            self.process.stdin.flush()

    def begin(self, path, binding):
        with self.lock:
            if os.getpid() != self.owner_pid:
                raise WorkspaceError("resident worker cannot be used after fork")
            if self.poisoned or self.closing or self.process.poll() is not None:
                raise WorkspaceError("resident worker unavailable; explicit shutdown/restart required")
            if self.active is not None:
                raise WorkspaceError("resident worker already owns a run")
            invocation = ResidentInvocation(self, binding.run_id, binding.run_epoch)
            self.active = invocation
        try:
            self._send(json.dumps({"command": "START", "config_path": str(path)}).encode()+b"\n")
        except Exception:
            self._fail("resident START delivery failed")
            raise
        return invocation

    def finish(self, invocation, value):
        with self.lock:
            message = json.loads(value)
            if self.active is not invocation or invocation.ready is None or invocation.finish_sent:
                raise WorkspaceError("resident FINISH is out of order")
            if (set(message) != {"protocol_version", "command", "run_id", "finish_nonce"}
                or message.get("protocol_version") != 2 or message.get("command") != "FINISH"
                or message.get("run_id") != invocation.run_id
                or message.get("finish_nonce") != invocation.ready["finish_nonce"]):
                raise WorkspaceError("resident FINISH binding mismatch")
            invocation.finish_sent = True
        self._send(value)

    def _read(self):
        try:
            assert self.process.stdout is not None
            while True:
                line = self.process.stdout.readline(LIMIT+1)
                if not line:
                    if self.closed.is_set():
                        return
                    raise WorkspaceError("resident worker unexpected EOF")
                if len(line) > LIMIT or not line.endswith(b"\n"):
                    raise WorkspaceError("resident worker response exceeds bounds")
                self._route(json.loads(line))
        except Exception as exc:
            if not (self.closing and self.closed.is_set()):
                self._fail("resident protocol failure: "+type(exc).__name__)

    def _route(self, message):
        if not isinstance(message, dict):
            raise WorkspaceError("resident response is not an object")
        with self.lock:
            if self.closing and message == {"resident_protocol": 1, "status": "CLOSED"}:
                self.closed.set()
                return
            run = self.active
            if run is None:
                raise WorkspaceError("unsolicited resident response")
            status = message.get("status")
            if status == "READY" and run.ready is None and not run.finish_sent:
                if (message.get("run_id") != run.run_id or type(message.get("epoch")) is not int
                    or message.get("epoch") != run.epoch):
                    raise WorkspaceError("resident READY identity mismatch")
                run.ready = message
                run._stdout.write(json.dumps(message).encode()+b"\n")
            elif status == "FINISHED" and run.finish_sent and run.terminal is None:
                if message.get("run_id") != run.run_id:
                    raise WorkspaceError("resident terminal identity mismatch")
                run.terminal = message
            elif status == "IDLE" and run.terminal is not None:
                self._acknowledge(run, message)
            else:
                raise WorkspaceError("out-of-order resident response")

    def _acknowledge(self, run, ack):
        if (set(ack) != ACK_KEYS or type(ack["resident_protocol"]) is not int
            or ack["resident_protocol"] != 1 or ack["run_id"] != run.run_id
            or type(ack["epoch"]) is not int or ack["epoch"] != run.epoch
            or ack["finish_nonce"] != run.ready["finish_nonce"]
            or ack["report_sha256"] != run.terminal["report_sha256"]
            or ack["artifact_manifest_sha256"] != run.terminal["artifact_manifest_sha256"]
            or type(ack["sequence"]) is not int or ack["sequence"] != self.sequence+1
            or type(ack["kernel_objects_reused"]) is not bool
            or ack["kernel_objects_reused"] != (self.sequence > 0)
            or type(ack["resident_resources"]) is not int or ack["resident_resources"] != 2):
            raise WorkspaceError("resident IDLE seal binding mismatch")
        objects = ack["resident_objects"]
        if not isinstance(objects, list) or len(objects) != 2:
            raise WorkspaceError("resident object identity is missing")
        for item in objects:
            if not isinstance(item, dict) or set(item) != {"program_ids", "map_ids", "link_ids"}:
                raise WorkspaceError("resident object identity schema mismatch")
            for ids in item.values():
                if (not isinstance(ids, list) or not 0 < len(ids) <= 4096
                    or any(type(value) is not int or value <= 0 for value in ids)
                    or len(ids) != len(set(ids))):
                    raise WorkspaceError("resident object ID list is invalid")
        if self.identity is not None and objects != self.identity:
            raise WorkspaceError("resident kernel objects were replaced between runs")
        self.identity = copy.deepcopy(objects)
        self.sequence = ack["sequence"]
        run.evidence = {"worker_pid": self.process.pid, **copy.deepcopy(ack)}
        self.active = None
        # The v2 runner sees terminal/EOF only after the native IDLE seal ACK.
        run.complete(0, run.terminal)

    def _errors(self):
        try:
            assert self.process.stderr is not None
            while chunk := self.process.stderr.read(4096):
                with self.lock:
                    self.diagnostics.append(chunk[:4096])
                    if self.active is not None:
                        self.active._stderr.write(chunk[:4096])
        except (OSError, ValueError):
            pass

    def _fail(self, reason):
        with self.lock:
            if self.poisoned:
                return
            self.poisoned = reason
            run = self.active
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        with self.lock:
            if run is not None:
                run.complete(-1)
                self.active = None
        self.closed.set()

    def close(self):
        if os.getpid() != self.owner_pid:
            return
        before = time.perf_counter_ns()
        with self.lock:
            unfinished = self.active is not None
            self.closing = True
        if unfinished:
            self._fail("resident shutdown with unfinished run")
        elif self.process.poll() is None and not self.poisoned:
            self._send(b'{"command":"SHUTDOWN"}\n')
            if not self.closed.wait(120):
                self._fail("resident shutdown deadline")
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._fail("resident worker did not exit")
        for thread in (self.reader, self.errors):
            if thread is not threading.current_thread():
                thread.join(timeout=5)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()
        return {"worker_pid": self.process.pid, "runs": self.sequence,
                "shutdown_ns": time.perf_counter_ns()-before, "poisoned": self.poisoned,
                "exit_code": self.process.returncode}


_registry_lock = threading.Lock()
_registry_pid = os.getpid()
_worker = None
_key = None


def resident_worker(config):
    global _worker, _key
    if os.getpid() != _registry_pid:
        raise WorkspaceError("resident registry cannot be used after fork")
    metadata = config.observer_path.stat()
    key = (str(config.observer_path), metadata.st_ino, metadata.st_ctime_ns,
           str(config.recorder_object_path), config.recorder_object_sha256,
           str(config.lifecycle_object_path), config.lifecycle_object_sha256,
           config.profile, config.object_resolution_mode, str(config.host_lock_path),
           os.stat('/proc/self/ns/mnt').st_ino, os.stat('/proc/self/ns/net').st_ino)
    with _registry_lock:
        if _worker is None:
            _worker, _key = ResidentObserver(config), key
        if _key != key:
            raise WorkspaceError("resident immutable configuration differs; explicit shutdown required")
        return _worker


def shutdown_resident_observers():
    global _worker, _key
    if os.getpid() != _registry_pid:
        return None
    with _registry_lock:
        result = _worker.close() if _worker is not None else None
        if _worker is not None and _worker.process.poll() is None:
            raise WorkspaceError("resident worker has not exited; registry remains fenced")
        _worker = _key = None
        return result


def resident_observer_status():
    if os.getpid() != _registry_pid:
        return {"present": False, "forked": True}
    with _registry_lock:
        if _worker is None:
            return {"present": False}
        return {"present": True, "worker_pid": _worker.process.pid,
                "runs": _worker.sequence, "poisoned": _worker.poisoned,
                "alive": _worker.process.poll() is None}


atexit.register(shutdown_resident_observers)
