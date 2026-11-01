"""Durability, bounded backpressure and lifecycle tests; never rely on timing speedups."""

import json
import threading

import pytest
from allox_agent_monitor.sdk import Tracer
from allox_agent_monitor.store import TraceStore
from test_agent_monitor_batch import event


def ack(batch):
    return {'accepted': True, 'durable': True, 'event_count': len(batch['events'])}


def test_background_eventual_timer_flush_and_shutdown(tmp_path):
    store = TraceStore(tmp_path)
    received = threading.Event()
    def emit(batch):
        result = store.ingest('a', 's', batch)
        received.set()
        return result
    tracer = Tracer(emit, delivery='background', batch_size=64, flush_interval=.01, strict=True)
    tracer.call_tool('work', lambda: 55)
    assert received.wait(5)
    tracer.close()
    assert len(store.spans('a', 's')) == 2
    assert tracer.health()['complete'] and not tracer.health()['worker_alive']
    assert tracer.health()['acknowledged_events'] == 4
    store.close()


def test_background_hot_path_does_not_wait_for_commit_and_flush_does():
    entered, release, flushed = threading.Event(), threading.Event(), threading.Event()
    def emit(batch):
        entered.set()
        assert release.wait(5)
        return ack(batch)
    tracer = Tracer(emit, delivery='background', batch_size=4, strict=True)
    try:
        assert tracer.call_tool('work', lambda: 55) == 55
        assert entered.wait(5)
        health = tracer.health()
        assert health['pending_events'] == 4 and health['acknowledged_events'] == 0
        assert not health['complete']
        thread = threading.Thread(target=lambda: (tracer.barrier(), flushed.set()))
        thread.start()
        assert not flushed.wait(.05)
        release.set()
        assert flushed.wait(5)
        thread.join(5)
        assert tracer.health()['complete']
    finally:
        release.set()
        tracer.close()


def test_background_freezes_input_and_context_is_durable(tmp_path):
    store = TraceStore(tmp_path)
    with Tracer(lambda e: store.ingest('a', 's', e), delivery='background',
                batch_size=64, flush_interval=5, strict=True) as tracer:
        original = event()
        tracer.emit(original)
        original['name'] = 'mutated'
        with tracer.span('parent') as parent:
            exported = parent.context
            assert exported['span_id'] in {s['span_id'] for s in store.spans('a', 's')}
        assert store.spans('a', 's')[0]['name'] == 'test'
    store.close()


def test_background_queue_bounds_backpressure_and_no_silent_loss():
    entered, release = threading.Event(), threading.Event()
    def emit(batch):
        entered.set()
        assert release.wait(5)
        return ack(batch)
    tracer = Tracer(emit, delivery='background', batch_size=1, queue_capacity=1,
                    export_timeout=.05, strict=True)
    try:
        tracer.emit(event('one'))
        assert entered.wait(5)
        with pytest.raises(TimeoutError, match='backpressure'):
            tracer.emit(event('two'))
        health = tracer.health()
        assert health['peak_pending_events'] == 1 and health['failed_events'] == 1
        assert health['queue_full_waits'] == 1 and not health['complete']
        release.set()
        with pytest.raises(RuntimeError, match='failed events'):
            tracer.barrier()
    finally:
        release.set()
        with pytest.raises(RuntimeError, match='failed events'):
            tracer.close()


@pytest.mark.parametrize('bad_ack', [None, {}, {'accepted': True, 'event_count': 1}])
def test_background_failure_is_sticky_and_checkpoint_is_fail_closed(bad_ack):
    called = []
    tracer = Tracer(lambda batch: bad_ack, delivery='background', batch_size=1)
    tracer.emit(event())
    tracer.flush()  # best-effort does not turn failure into an application exception
    with pytest.raises(RuntimeError, match='failed events'):
        tracer.checkpoint(lambda: called.append('unsafe-checkpoint'))
    assert not called
    tracer.emit(event('later'))
    tracer.close()
    health = tracer.health()
    assert health['attempted_events'] == 2 and health['failed_events'] == 2
    assert health['acknowledged_events'] == 0 and not health['complete']


def test_background_turn_and_checkpoint_barriers(tmp_path):
    store = TraceStore(tmp_path)
    with Tracer(lambda e: store.ingest('a', 's', e), delivery='background', batch_size=64,
                flush_interval=5, strict=True) as tracer:
        with tracer.span('action', 'turn'):
            tracer.call_tool('work', lambda: 1)
        assert len(store.spans('a', 's')) == 3 and tracer.health()['complete']
        def checkpoint():
            assert tracer.health()['complete']
            return 'C1'
        assert tracer.checkpoint(checkpoint) == 'C1'
    store.close()


def test_background_concurrent_producers_and_large_envelopes(tmp_path):
    store = TraceStore(tmp_path)
    batches = []
    def emit(batch):
        assert len(json.dumps(batch).encode()) <= 65536
        batches.append(batch)
        return store.ingest('a', 's', batch)
    with Tracer(emit, delivery='background', batch_size=64, strict=True) as tracer:
        threads = [threading.Thread(target=lambda: tracer.call_tool('thread', lambda: 1)) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        for index in range(4):
            tracer.emit(event('large'+str(index), input={'value': 'x'*30000}))
    assert tracer.health()['acknowledged_events'] == 84
    assert len(store.spans('a', 's')) == 44
    assert len(batches) >= 3
    store.close()


def test_background_invalid_data_and_fork_guard(monkeypatch):
    tracer = Tracer(ack, delivery='background', batch_size=64)
    tracer.emit({'bad': object()})
    assert tracer.health()['failed_events'] == 1
    import os
    monkeypatch.setattr(os, 'getpid', lambda: tracer._owner_pid+1)
    with pytest.raises(RuntimeError, match='after fork'):
        tracer.emit(event())
    with pytest.raises(RuntimeError, match='after fork'):
        tracer.flush()


def test_background_close_fences_new_events():
    tracer = Tracer(ack, delivery='background', batch_size=64, strict=True)
    tracer.close()
    with pytest.raises(RuntimeError, match='closed'):
        tracer.emit(event())
    assert tracer.health()['failed_events'] == 1 and not tracer.health()['complete']


def test_background_lost_ack_is_not_retried(tmp_path):
    store = TraceStore(tmp_path)
    calls = []
    def lost_ack(batch):
        calls.append(batch)
        store.ingest('a', 's', batch)
        raise ConnectionError('ack lost after commit')
    tracer = Tracer(lost_ack, delivery='background', batch_size=32)
    tracer.call_tool('work', lambda: 1)
    tracer.close()
    assert len(calls) == 1 and store.accepted == 4
    assert tracer.health()['failed_events'] == 4 and tracer.health()['acknowledged_events'] == 0
    assert not tracer.health()['complete']
    store.close()


def test_background_full_queue_resumes_after_ack():
    entered, release, submitted = threading.Event(), threading.Event(), threading.Event()
    batches = []
    def emit(batch):
        entered.set()
        assert release.wait(5)
        batches.append(batch)
        return ack(batch)
    tracer = Tracer(emit, delivery='background', batch_size=1, queue_capacity=1, strict=True)
    tracer.emit(event('one'))
    assert entered.wait(5)
    producer = threading.Thread(target=lambda: (tracer.emit(event('two')), submitted.set()))
    producer.start()
    try:
        assert not submitted.wait(.05)
        release.set()
        assert submitted.wait(5)
        producer.join(5)
        tracer.close()
        assert tracer.health()['complete'] and tracer.health()['acknowledged_events'] == 2
        assert [b['events'][0]['span_id'] for b in batches] == ['one', 'two']
    finally:
        release.set()
        producer.join(5)
        tracer.close()


def test_background_byte_limit_includes_inflight_batch():
    entered, release = threading.Event(), threading.Event()
    def emit(batch):
        entered.set()
        assert release.wait(5)
        return ack(batch)
    tracer = Tracer(emit, delivery='background', batch_size=1, queue_bytes=65536,
                    export_timeout=.05, strict=True)
    try:
        tracer.emit(event('one', input='x'*30000))
        assert entered.wait(5)
        tracer.emit(event('two', input='x'*30000))
        with pytest.raises(TimeoutError, match='backpressure'):
            tracer.emit(event('three', input='x'*30000))
        health = tracer.health()
        assert health['pending_bytes'] <= 65536 and health['peak_pending_bytes'] <= 65536
        assert health['pending_events'] == 2 and health['failed_events'] == 1
    finally:
        release.set()
        with pytest.raises(RuntimeError, match='failed events'):
            tracer.close()
