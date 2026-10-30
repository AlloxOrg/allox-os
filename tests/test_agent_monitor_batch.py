"""Batches preserve atomic durable ACKs; SDK pending events are not acknowledged."""

import json
import threading

import pytest
from allox_agent_monitor.sdk import Tracer
from allox_agent_monitor.store import TraceStore
from test_agent_monitor import event, make_plugin

from allox.workspace.store import WorkspaceError


def test_batch_flush_is_explicit_atomic_and_durable(tmp_path):
    store = TraceStore(tmp_path)
    t = Tracer(lambda e: store.ingest('a', 's', e), batch_size=32, strict=True)
    assert t.call_tool('tool', lambda: 7) == 7
    assert store.spans('a', 's') == []
    assert t.health()['pending_events'] == 4 and not t.health()['complete']
    t.flush()
    assert t.health()['acknowledged_events'] == 4 and t.health()['complete']
    assert store.status()['committed_transactions_this_process'] == 1
    store.close()
    reopened = TraceStore(tmp_path)
    assert len(reopened.spans('a', 's')) == 2
    assert all(s['status'] == 'ok' for s in reopened.spans('a', 's'))
    reopened.close()


def test_failed_batch_rolls_back_all_events_and_counters(tmp_path):
    store = TraceStore(tmp_path)
    with pytest.raises(WorkspaceError, match='duplicate'):
        store.ingest('a', 's', {'event': 'batch', 'events': [event(), event()]})
    assert store.spans('a', 's') == []
    assert store.accepted == 0 and store.transactions == 0
    store.close()


@pytest.mark.parametrize('events', [[], [event()]*65, [{'event': 'batch'}], [None]])
def test_batch_bounds(tmp_path, events):
    store = TraceStore(tmp_path)
    with pytest.raises(WorkspaceError):
        store.ingest('a', 's', {'event': 'batch', 'events': events})
    assert store.spans('a', 's') == []
    store.close()


def test_batch_rejects_spoofed_identity_and_core_process(tmp_path):
    plugin, _ = make_plugin(tmp_path)
    for bad in (event(kind='process'), event(source='allox-executor'), event(agent_id='foreign')):
        with pytest.raises(WorkspaceError):
            plugin.dispatch('monitor.ingest', {'agent_id': 'a', 'session_id': 's',
                'event': {'event': 'batch', 'events': [bad]}})
    assert plugin.store.accepted == 0
    plugin.close()


def test_ambiguous_ack_is_not_retried_or_reported_complete():
    calls = []
    def emit(e):
        calls.append(e)
        return {'accepted': True}
    t = Tracer(emit, batch_size=32, strict=True)
    t.call_tool('tool', lambda: 1)
    with pytest.raises(RuntimeError, match='acknowledgement'):
        t.flush()
    t.flush()
    assert len(calls) == 1
    assert t.health()['failed_events'] == 4
    assert t.health()['acknowledged_events'] == 0 and not t.health()['complete']


def test_context_flush_and_concurrency(tmp_path):
    store = TraceStore(tmp_path)
    with Tracer(lambda e: store.ingest('a', 's', e), batch_size=32, strict=True) as t:
        threads = [threading.Thread(target=lambda: t.call_tool('thread', lambda: 1)) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    assert store.accepted == 40 and store.transactions == 2
    assert len(store.spans('a', 's')) == 20 and t.health()['complete']
    assert t.health()['failed_events'] == 0
    store.close()


def test_buffer_freezes_input_and_is_size_bounded(tmp_path):
    store = TraceStore(tmp_path)
    t = Tracer(lambda e: store.ingest('a', 's', e), batch_size=32, strict=True)
    original = event()
    t.emit(original)
    original['name'] = 'changed'
    t.flush()
    assert store.spans('a', 's')[0]['name'] == 'test'
    with pytest.raises(ValueError, match='large'):
        t.emit(event('big', input='x'*65536))
    assert len(json.dumps(t.health())) < 2000
    store.close()


def test_health_does_not_report_inflight_batch_complete():
    entered, release = threading.Event(), threading.Event()
    def emit(e):
        entered.set()
        assert release.wait(5)
        return {'accepted': True, 'durable': True, 'event_count': len(e['events'])}
    t = Tracer(emit, batch_size=32, strict=True)
    t.call_tool('tool', lambda: 1)
    thread = threading.Thread(target=t.flush)
    thread.start()
    assert entered.wait(5)
    try:
        assert not t.health()['complete'] and t.health()['acknowledged_events'] == 0
    finally:
        release.set()
        thread.join(5)
    assert t.health()['complete']


def test_batch_socket_checks_inner_events(tmp_path):
    import os

    from allox_agent_monitor.transport import emit_session
    plugin, context = make_plugin(tmp_path)
    context.cgroups.owns = lambda a, s, pid: pid == os.getpid()
    plugin._runs['run'] = {'row': {'agent_id': 'a', 'session_id': 's', 'state': 'running'}}
    try:
        endpoint = plugin._endpoints.endpoint('a', 's')
        with Tracer(lambda e: emit_session(e, endpoint), batch_size=32, strict=True) as t:
            t.call_tool('socket', lambda: 2)
        assert plugin.store.accepted == 4 and plugin.store.transactions == 1
        for bad in (event(kind='process'), event(agent_id='foreign')):
            with pytest.raises(ValueError):
                emit_session({'event': 'batch', 'events': [bad]}, endpoint)
        assert plugin.store.accepted == 4
    finally:
        plugin.close()


def test_buffered_soft_encoding_failure_is_visible():
    t = Tracer(lambda e: None, batch_size=32)
    t.emit({'bad': object()})
    assert t.health()['failed_events'] == 1 and not t.health()['complete']
