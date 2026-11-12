"""Multi-action/time triggers may defer profiling, never an explicit safety barrier."""
import threading

import pytest
from allox_agent_monitor.queued import BoundQueueSink, QueuedTraceWriter
from allox_agent_monitor.sdk import Tracer
from allox_agent_monitor.store import TraceStore
from test_agent_monitor import event
from test_agent_monitor_queued import feature

from allox.workspace.store import WorkspaceError


@pytest.fixture
def setup(tmp_path):
    store = TraceStore(tmp_path / 'traces')
    writer = QueuedTraceWriter(store, interval=5)
    sink = BoundQueueSink(writer, 'a', 's')
    tracers = []
    def create(**kw):
        tracer = Tracer(sink, delivery='queued', batch_size=64, action_batch_size=8,
                        action_flush_interval=5, **kw)
        tracers.append(tracer)
        return tracer
    yield store, writer, create
    for tracer in tracers:
        try:
            tracer.close()
        except (RuntimeError, TimeoutError):
            pass
    try:
        writer.close()
    except WorkspaceError:
        pass
    store.close()


def action(tracer):
    with tracer.span('action', 'turn'):
        tracer.call_tool('work', lambda: 1)


def wait_durable(tracer):
    exporter = tracer._background
    with exporter.cv:
        assert exporter.cv.wait_for(lambda: exporter.acknowledged == exporter.attempted
                                   or exporter.error, timeout=5)
    assert tracer.health()['complete']


def test_eight_actions_trigger_one_commit_not_seven(setup):
    store, _, create = setup
    tracer = create(strict=True)
    for _ in range(7):
        action(tracer)
    assert not tracer.health()['complete'] and store.transactions == 0
    assert tracer.health()['completed_actions'] == 7
    action(tracer)
    wait_durable(tracer)
    assert store.transactions == 1 and len(store.spans('a', 's')) == 24
    assert tracer.health()['acknowledged_events'] == 48


def test_timer_flushes_idle_tail_without_another_action(setup):
    store, _, create = setup
    tracer = create(strict=True)
    tracer._background.interval = .02
    action(tracer)
    wait_durable(tracer)
    assert store.transactions == 1 and tracer.health()['completed_actions'] == 1
    assert tracer.health()['durable_actions'] == 1


def test_timer_flushes_without_any_completed_action(setup):
    store, _, create = setup
    tracer = create(strict=True)
    tracer._background.interval = .02
    tracer.call_tool('standalone', lambda: 1)
    wait_durable(tracer)
    assert store.transactions == 1 and tracer.health()['completed_actions'] == 0


def test_slow_commit_does_not_hold_producer_or_health_lock(setup, monkeypatch):
    store, _, create = setup
    tracer = create(strict=True)
    entered, release, produced = threading.Event(), threading.Event(), threading.Event()
    original = store.ingest_group
    def block(requests):
        entered.set()
        assert release.wait(5)
        return original(requests)
    monkeypatch.setattr(store, 'ingest_group', block)
    for _ in range(8):
        action(tracer)
    assert entered.wait(5)
    def produce():
        action(tracer)
        assert not tracer.health()['complete']
        produced.set()
    thread = threading.Thread(target=produce)
    thread.start()
    try:
        assert produced.wait(2)
    finally:
        release.set()
        thread.join(5)
    tracer.barrier()
    assert tracer.health()['acknowledged_events'] == 54


@pytest.mark.parametrize('boundary', ['context', 'checkpoint', 'run', 'close', 'flush'])
def test_explicit_boundaries_override_batch_threshold(setup, boundary):
    store, _, create = setup
    tracer = create(strict=True)
    action(tracer)
    if boundary == 'context':
        with tracer.span('parent') as span:
            assert span.context['span_id'] in {s['span_id'] for s in store.spans('a', 's')}
        tracer.barrier()
    elif boundary == 'checkpoint':
        assert tracer.checkpoint(lambda: len(store.spans('a', 's'))) == 3
    elif boundary == 'run':
        with tracer.span('task', 'run'):
            pass
    elif boundary == 'close':
        tracer.close()
        assert not tracer.health()['worker_alive']
    else:
        tracer.flush()
    assert tracer.health()['complete']


def test_manual_action_marker_and_no_double_count_nested_tools(setup):
    _, _, create = setup
    tracer = create(strict=True)
    with tracer.span('action', 'turn'):
        tracer.call_tool('first', lambda: 1)
        tracer.call_tool('second', lambda: 2)
    assert tracer.health()['completed_actions'] == 1
    tracer.action_finished()
    assert tracer.health()['completed_actions'] == 2
    tracer.close()
    with pytest.raises(RuntimeError, match='closed'):
        tracer.action_finished()


def test_freezes_input_and_rejects_nonfinite_data(setup):
    store, _, create = setup
    tracer = create(strict=True)
    original = event('frozen')
    tracer.emit(original)
    original['name'] = 'mutated'
    tracer.barrier()
    assert store.spans('a', 's')[0]['name'] == 'test'
    with pytest.raises(ValueError):
        tracer.emit(event('invalid', input=float('nan')))
    assert not tracer.health()['complete'] and tracer.health()['failed_events'] == 1


def test_action_socket(tmp_path):
    from allox_agent_monitor.transport import SessionQueueTransport
    f = feature(tmp_path)
    row = {'agent_id': 'a', 'session_id': 's', 'run_id': 'action-run',
           'state': 'running', 'root_exit_code': 0}
    f.execution_started(row)
    transport = SessionQueueTransport(f._endpoints.endpoint('a', 's'))
    try:
        with Tracer(transport, delivery='queued', action_batch_size=8,
                    action_flush_interval=.05, batch_size=32, strict=True) as tracer:
            for _ in range(9):
                action(tracer)
        assert tracer.health()['complete'] and tracer.health()['acknowledged_events'] == 54
        rows = [r for r in f.store.spans('a', 's') if r['kind'] != 'process']
        assert len(rows) == 27
        assert all(r['source'] == 'session-sdk' and r['process_run_id'] == 'action-run' for r in rows)
        f.execution_finished(row)
    finally:
        f.close()


def test_failed_ack_stays_incomplete_and_checkpoint_never_runs():
    class Bad:
        def __call__(self, batch):
            return {'accepted': True, 'durable': False}
        def flush(self, receipt):
            raise AssertionError('unreachable')
    tracer = Tracer(Bad(), delivery='queued', batch_size=64, action_batch_size=8)
    action(tracer)
    ran = []
    with pytest.raises(RuntimeError, match='failed events'):
        tracer.checkpoint(lambda: ran.append(True))
    assert not ran and not tracer.health()['complete']
    tracer.close()  # soft close preserves failure in health
    assert not tracer.health()['worker_alive']


def test_capacity_backpressure_failure_is_sticky_and_fork_rejected(setup, monkeypatch):
    store, _, create = setup
    entered, release = threading.Event(), threading.Event()
    original = store.ingest_group
    def block(requests):
        entered.set()
        assert release.wait(5)
        return original(requests)
    monkeypatch.setattr(store, 'ingest_group', block)
    tracer = create(strict=True, queue_capacity=1, export_timeout=.05)
    tracer.emit(event('one'))
    try:
        with pytest.raises(TimeoutError, match='backpressure'):
            tracer.emit(event('two'))
        assert entered.is_set() and not tracer.health()['complete']
        monkeypatch.setattr(tracer._background, 'pid', -1)
        with pytest.raises(RuntimeError, match='fork'):
            tracer.emit(event('forked'))
        monkeypatch.setattr(tracer._background, 'pid', tracer._owner_pid)
    finally:
        release.set()
        tracer._background.thread.join(5)
    with pytest.raises(RuntimeError, match='failed events'):
        tracer.barrier()


@pytest.mark.parametrize('kw', [
    {'action_batch_size': 0}, {'action_batch_size': True}, {'action_batch_size': 1025},
    {'action_batch_size': 8, 'action_flush_interval': 0},
    {'action_batch_size': 8, 'action_flush_interval': float('nan')},
    {'action_batch_size': 8, 'queue_capacity': 0},
])
def test_invalid_policy_rejected(kw):
    class Sink:
        def __call__(self, batch):
            pass
        def flush(self, receipt):
            pass
    with pytest.raises(ValueError):
        Tracer(Sink(), delivery='queued', **kw)
    with pytest.raises(ValueError, match='requires queued'):
        Tracer(Sink(), delivery='background', action_batch_size=8)
