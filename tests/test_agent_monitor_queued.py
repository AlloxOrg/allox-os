"""Collector-side group commit, receipt safety and durable lifecycle boundaries."""
import json
import multiprocessing
import os
import threading
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from allox_agent_monitor.plugin import AgentMonitorFeature
from allox_agent_monitor.queued import BoundQueueSink, QueuedTraceWriter
from allox_agent_monitor.sdk import Tracer
from allox_agent_monitor.store import TraceStore
from allox_agent_monitor.transport import SessionQueueTransport
from test_agent_monitor import end, event

from allox.workspace.store import WorkspaceError


@pytest.fixture
def queued(tmp_path):
    store = TraceStore(tmp_path / 'traces')
    writer = QueuedTraceWriter(store, interval=5)
    yield store, writer, BoundQueueSink(writer, 'a', 's')
    try:
        writer.close()
    except WorkspaceError:
        pass
    store.close()


def test_admission_is_not_durable_and_group_commit_reopens(queued):
    store, writer, sink = queued
    t = Tracer(sink, delivery='queued', batch_size=8, strict=True)
    for _ in range(40):
        t.call_tool('sum', sum, [1, 2])
    assert t.health()['enqueued_events'] == 160
    assert t.health()['acknowledged_events'] == 0
    assert not t.health()['complete']
    t.close()
    assert t.health()['complete'] and t.health()['acknowledged_events'] == 160
    assert store.transactions == 1 and writer.status()['group_commits'] == 1
    assert len(store.spans('a', 's')) == 80
    reopened = TraceStore(store.root)
    assert all(s['status'] == 'ok' for s in reopened.spans('a', 's'))
    reopened.close()


def test_admission_does_not_wait_for_commit_but_flush_does(queued, monkeypatch):
    store, writer, sink = queued
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    original = store.ingest_group
    def blocking(requests):
        entered.set()
        assert release.wait(5)
        return original(requests)
    monkeypatch.setattr(store, 'ingest_group', blocking)
    writer.interval = .001
    receipt = sink(event())
    assert receipt['durable'] is False and entered.wait(5)
    thread = threading.Thread(target=lambda: (sink.flush(receipt), done.set()))
    thread.start()
    try:
        assert not done.wait(.05)
    finally:
        release.set()
        thread.join(5)
    assert done.is_set()


def test_frozen_input_foreign_stale_and_future_receipts(queued):
    store, writer, sink = queued
    original = event()
    receipt = sink(original)
    original['name'] = 'changed'
    for extra in ({'stream_id': 'foreign'}, {'collector_id': 'previous-boot'}, {'sequence': 999}, {'sequence': True}):
        with pytest.raises(WorkspaceError, match='receipt'):
            sink.flush({**receipt, **extra})
    other = BoundQueueSink(writer, 'a', 'other')
    other_receipt = other(event())
    with pytest.raises(WorkspaceError, match='receipt'):
        other.flush(receipt)
    sink.flush(receipt)
    other.flush(other_receipt)
    assert store.spans('a', 's')[0]['name'] == 'test'


def test_invalid_channel_atomic_and_other_session_survives(queued):
    store, writer, bad = queued
    receipt = bad({'event': 'batch', 'events': [event('ok'), end('missing', 10)]})
    good = BoundQueueSink(writer, 'b', 's')
    good_receipt = good(event('survives'))
    with pytest.raises(WorkspaceError, match='incomplete'):
        bad.flush(receipt)
    good.flush(good_receipt)
    assert not store.spans('a', 's')
    assert len(store.spans('b', 's')) == 1
    assert writer.status()['failed_channels'] == 1 and not writer.status()['complete']
    with pytest.raises(WorkspaceError, match='failed'):
        bad(event('later'))


@pytest.mark.parametrize('boundary', ['turn', 'context', 'checkpoint', 'close'])
def test_sdk_boundaries_are_durable(queued, boundary):
    store, _, sink = queued
    t = Tracer(sink, delivery='queued', batch_size=64, strict=True)
    if boundary == 'turn':
        with t.span('action', 'turn'):
            t.call_tool('x', lambda: 1)
    elif boundary == 'context':
        with t.span('parent') as span:
            assert span.context['span_id'] in {s['span_id'] for s in store.spans('a', 's')}
        t.barrier()
    elif boundary == 'checkpoint':
        t.call_tool('x', lambda: 1)
        assert t.checkpoint(lambda: len(store.spans('a', 's'))) == 2
    else:
        t.call_tool('x', lambda: 1)
        t.close()
    assert t.health()['complete']


def test_queue_bounds_seal_and_fork(queued, monkeypatch):
    _, writer, sink = queued
    writer.channel_events = 1
    writer.timeout = .01
    receipt = sink(event())
    with pytest.raises(WorkspaceError, match='backpressure'):
        sink(event('second'))
    writer.timeout = 5
    writer.flush(sink.key, receipt, seal=True)
    with pytest.raises(WorkspaceError, match='closed'):
        sink(event('second'))
    monkeypatch.setattr(writer, 'pid', -1)
    with pytest.raises(WorkspaceError, match='fork'):
        writer.submit('a', 's', event())
    monkeypatch.undo()


def test_sdk_sticky_failure_checkpoint_fail_closed_even_soft():
    class Bad:
        def __call__(self, event):
            return {'accepted': True, 'durable': True}
        def flush(self, receipt):
            raise AssertionError('not reached')
    t = Tracer(Bad(), delivery='queued', batch_size=1)
    t.emit(event())
    called = []
    with pytest.raises(RuntimeError):
        t.checkpoint(lambda: called.append(True))
    assert not called and not t.health()['complete']
    with pytest.raises(ValueError, match='receipt-aware'):
        Tracer(lambda e: None, delivery='queued')


def test_no_false_durable_ack(queued):
    _, _, sink = queued
    class Corrupt:
        def __call__(self, event):
            return sink(event)
        def flush(self, receipt):
            return {**sink.flush(receipt), 'stream_id': 'foreign'}
    t = Tracer(Corrupt(), delivery='queued', batch_size=1, strict=True)
    t.emit(event())
    with pytest.raises(RuntimeError, match='durable receipt'):
        t.barrier()
    assert t.health()['acknowledged_events'] == 0 and not t.health()['complete']


def feature(tmp_path, *, enabled=True):
    context = SimpleNamespace(store=SimpleNamespace(root=tmp_path / 'workspace'),
        cgroups=SimpleNamespace(owns=lambda a, s, pid: s == 's', path=lambda a, s: tmp_path),
        executions=SimpleNamespace(share_access=lambda *a, **kw: nullcontext()))
    return AgentMonitorFeature(context=context, config={'root': tmp_path / 'monitor',
        'socket_root': tmp_path / 'sockets', 'async_write': enabled})


def test_real_session_socket_identity_flush_and_execution_end(tmp_path):
    f = feature(tmp_path)
    f.writer.interval = 5
    row = {'agent_id': 'a', 'session_id': 's', 'run_id': 'run', 'state': 'running', 'root_exit_code': 0}
    f.execution_started(row)
    transport = SessionQueueTransport(f._endpoints.endpoint('a', 's'))
    t = Tracer(transport, delivery='queued', batch_size=1, strict=True, inherit_context=False)
    try:
        t.call_tool('real-socket', lambda: 1)
        assert not t.health()['complete']
        t.barrier()
        assert t.health()['complete']
        peer = SessionQueueTransport(f._endpoints.endpoint('a', 'other'))
        with pytest.raises(ValueError, match='belong'):
            peer(event())
        with pytest.raises(ValueError, match='Core-owned'):
            transport(event(kind='process'))
        with pytest.raises(ValueError, match='receipt'):
            transport.flush({'collector_id': 'wrong', 'stream_id': 'wrong', 'sequence': 1})
        t.call_tool('pending-at-exit', lambda: 2)
        f.execution_finished(row)
        assert all(r['status'] == 'ok' for r in f.store.spans('a', 's'))
        assert f.writer.status()['complete']
        with pytest.raises(ValueError, match='active process run'):
            transport(event('late'))
    finally:
        f.close()


def test_mutation_and_export_flush_and_disabled_rejects(tmp_path):
    f = feature(tmp_path)
    params = {'agent_id': 'a', 'session_id': 's'}
    try:
        f.writer.interval = 5
        f.dispatch('monitor.ingest', {**params, 'event': event(), 'delivery': 'queued'})
        f.mutation_started('checkpoint.create', params)
        assert len(f.store.spans('a', 's')) == 1
        f.dispatch('monitor.ingest', {**params, 'event': end('s', 10), 'delivery': 'queued'})
        assert f.dispatch('monitor.trace', params)['spans'][0]['status'] == 'ok'
        assert f.dispatch('monitor.status', {})['write_queue']['complete']
    finally:
        f.close()
    disabled = feature(tmp_path / 'disabled', enabled=False)
    try:
        with pytest.raises(WorkspaceError, match='not enabled'):
            disabled.dispatch('monitor.ingest', {**params, 'event': event(), 'delivery': 'queued'})
    finally:
        disabled.close()


def test_group_rejects_cross_session_before_commit(queued):
    store, _, _ = queued
    batch = {'event': 'batch', 'events': [event()]}
    with pytest.raises(WorkspaceError, match='identity'):
        store.ingest_group([('a', 's', batch, 'framework-sdk', None),
                            ('a', 'other', json.loads(json.dumps(batch)), 'framework-sdk', None)])
    assert store.accepted == store.transactions == 0


def _crash_tail(path, pipe):
    store = TraceStore(path)
    writer = QueuedTraceWriter(store, interval=5)
    sink = BoundQueueSink(writer, 'a', 's')
    receipt = sink(event('lost-tail'))
    pipe.send(receipt)
    pipe.close()
    os._exit(0)  # Deliberately bypass graceful shutdown; no claim of crash durability.


def test_real_collector_crash_drops_unflushed_tail_not_false_durability(tmp_path):
    ctx = multiprocessing.get_context('fork')
    parent, child = ctx.Pipe(False)
    process = ctx.Process(target=_crash_tail, args=(tmp_path, child))
    process.start()
    assert parent.poll(10)
    receipt = parent.recv()
    process.join(10)
    assert process.exitcode == 0 and receipt['durable'] is False
    store = TraceStore(tmp_path)
    writer = QueuedTraceWriter(store, interval=5)
    try:
        assert not store.spans('a', 's')
        writer.submit('a', 's', event('new-boot'))
        with pytest.raises(WorkspaceError, match='receipt'):
            writer.flush(('a', 's', 'framework-sdk', None), receipt)
    finally:
        writer.close()
        store.close()


def test_concurrent_admissions_then_close_drain(queued):
    store, writer, sink = queued
    errors = []
    def send(i):
        try:
            sink(event('span-' + str(i)))
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=send, args=(i,)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    writer.close()
    assert not errors and len(store.spans('a', 's')) == 20 and writer.status()['complete']
    with pytest.raises(WorkspaceError, match='closed'):
        sink(event('late'))
