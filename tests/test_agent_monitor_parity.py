import asyncio
import json

import pytest
from allox_agent_monitor.clock import monotonic_domain
from allox_agent_monitor.export import folded
from allox_agent_monitor.mcp import TracedMCPClient
from allox_agent_monitor.profile import profile
from allox_agent_monitor.sdk import Tracer
from allox_agent_monitor.store import TraceStore
from test_agent_monitor import make_plugin


def test_context_export_flushes_parent_and_child_inherits(monkeypatch, tmp_path):
    store = TraceStore(tmp_path/'db')
    with (
        Tracer(lambda e: store.ingest('a', 's', e), batch_size=32, strict=True) as parent,
        parent.span('turn', 'turn', semantic_path=['repair'], turn_id='t1') as turn,
    ):
        context = turn.context
        assert len(store.spans('a', 's')) == 1
        monkeypatch.setenv('ALLOX_MONITOR_CONTEXT', json.dumps({
            'trace_id': context['trace_id'], 'parent_id': context['span_id'],
            'semantic_path': ['repair'], 'turn_id': 't1'}))
        child = Tracer(lambda e: store.ingest('a', 's', e), strict=True)
        child.call_tool('tool', lambda: 3)
        assert child.clock_id == parent.clock_id or not child.clock_id.startswith('linux-')
    rows = store.spans('a', 's')
    assert len({s['trace_id'] for s in rows}) == 1
    tool = next(s for s in rows if s['kind'] == 'tool')
    assert tool['parent_id'] == context['span_id'] and tool['turn_id'] == 't1'
    assert tool['semantic_path'] == ['repair']
    report = profile(rows)
    if child.clock_id.startswith('linux-'):
        assert report['complete'] and report['runs'] == 1
    store.close()


def test_core_run_environment_is_scoped_and_has_process_parent(tmp_path):
    plugin, _ = make_plugin(tmp_path)
    env = plugin.prepare_run_environment('a', 's', 'run-1', None)
    assert json.loads(env['ALLOX_MONITOR_CONTEXT'])['parent_id'] == 'process-run-1'
    assert json.loads(env['ALLOX_MONITOR_CONTEXT'])['trace_id'] == 'run-1'
    with pytest.raises(Exception, match='Session'):
        plugin.prepare_run_environment('other', 's', 'run-2', {'trace_id': 't', 'span_id': 'foreign'})
    plugin.close()


def test_explicit_trace_does_not_inherit_foreign_parent(monkeypatch):
    monkeypatch.setenv('ALLOX_MONITOR_CONTEXT', json.dumps({'trace_id': 'one', 'parent_id': 'p',
                                                          'semantic_path': [], 'turn_id': None}))
    t = Tracer(lambda e: None, trace_id='other')
    assert t.parent_id is None and t.trace_id == 'other'


def test_stream_usage_first_chunk_and_early_close(tmp_path):
    store = TraceStore(tmp_path)
    t = Tracer(lambda e: store.ingest('a', 's', e), strict=True)
    def stream():
        yield {'choices': [{'delta': {'content': 'hello'}}]}
        yield {'usage': {'prompt_tokens': 3, 'completion_tokens': 1},
               'metrics': {'queue_time_ms': 2, 'generation_time_ms': 4}}
    assert len(list(t.stream_model('lab', stream))) == 2
    iterator = t.stream_model('cancelled', stream)
    next(iterator)
    iterator.close()
    rows = store.spans('a', 's')
    metrics = rows[0]['metrics']
    assert 0 <= metrics['client.first_chunk_ns'] <= metrics['client.request_ns']
    assert metrics['provider.prompt_tokens'] == 3
    assert 'provider.time_to_first_token_ms' not in metrics
    assert rows[1]['status'] == 'cancelled'
    store.close()


def test_async_stream_and_mcp_phases(tmp_path):
    store = TraceStore(tmp_path)
    t = Tracer(lambda e: store.ingest('a', 's', e), strict=True)
    class Client:
        async def initialize(self):
            await asyncio.sleep(.001)
            return {'ready': True}
        async def list_tools(self):
            return {'tools': ['sum']}
        async def call_tool(self, name, arguments=None):
            return {'result': sum(arguments['values'])}
    async def chunks():
        yield {'usage': {'prompt_tokens': 2, 'completion_tokens': 1}}
    async def run():
        client = TracedMCPClient(Client(), t)
        assert await client.initialize() == {'ready': True}
        assert (await client.list_tools())['tools'] == ['sum']
        assert await client.call_tool('sum', arguments={'values': [1, 2]}) == {'result': 3}
        assert len([chunk async for chunk in t.astream_model('lab', chunks)]) == 1
    asyncio.run(run())
    rows = store.spans('a', 's')
    phases = [s for s in rows if s['kind'] == 'phase']
    assert {s['name'] for s in phases} == {'mcp_initialize', 'mcp_discovery', 'mcp_call'}
    assert all('mcp.client_request_ns' in s['metrics'] for s in phases)
    assert all('executor.execution_ns' not in s['metrics'] for s in phases)
    store.close()


def test_folded_export_and_tool_residual(tmp_path):
    store = TraceStore(tmp_path)
    t = Tracer(lambda e: store.ingest('a', 's', e), strict=True)
    t.call_tool('a;b', lambda: sum(range(10000)))
    rows = store.spans('a', 's')
    data = folded(rows)
    assert data['total_weight'] == 2 and '\\x3b' in data['data']
    assert folded(rows, 'self_ns')['complete']
    tool = next(g for g in profile(rows)['groups'] if g['kind'] == 'tool')
    assert tool['timing_samples'][0]['non_execution_ns'] >= 0
    phase = next(s for s in rows if s['kind'] == 'phase')
    assert phase['metrics']['executor.thread_cpu_ns'] >= 0
    store.close()


def test_linux_domain_and_context_bounds(monkeypatch):
    clock = monotonic_domain()
    assert clock
    if clock.startswith('linux-'):
        assert clock == monotonic_domain()
    monkeypatch.setenv('ALLOX_MONITOR_CONTEXT', 'x'*9000)
    with pytest.raises(ValueError, match='large'):
        Tracer(lambda e: None)


def test_mcp_real_http_jsonrpc_subset(tmp_path):
    import http.client
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    methods = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            methods.append(request['method'])
            result = {'protocolVersion': '2025-03-26'} if request['method'] == 'initialize' else (
                {'tools': [{'name': 'sum'}]} if request['method'] == 'tools/list' else {'value': 3})
            body = json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    class Client:
        async def send(self, method, params):
            def request():
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                try:
                    connection.request('POST', '/', json.dumps({'jsonrpc': '2.0', 'id': 1,
                        'method': method, 'params': params}), {'Content-Type': 'application/json'})
                    return json.loads(connection.getresponse().read())['result']
                finally:
                    connection.close()
            return await asyncio.to_thread(request)
        async def initialize(self):
            return await self.send('initialize', {})
        async def list_tools(self):
            return await self.send('tools/list', {})
        async def call_tool(self, name, arguments=None):
            return await self.send('tools/call', {'name': name, 'arguments': arguments})
    store = TraceStore(tmp_path)
    tracer = Tracer(lambda e: store.ingest('a', 's', e), strict=True)
    async def run():
        client = TracedMCPClient(Client(), tracer)
        await client.initialize()
        await client.list_tools()
        assert await client.call_tool('sum', arguments={'a': 1, 'b': 2}) == {'value': 3}
    try:
        asyncio.run(run())
        assert methods == ['initialize', 'tools/list', 'tools/call']
        assert all(r['status'] == 'ok' for r in store.spans('a', 's'))
    finally:
        server.shutdown()
        server.server_close()
        store.close()


def test_awaitable_stream_and_remote_error_status(tmp_path):
    store = TraceStore(tmp_path)
    tracer = Tracer(lambda e: store.ingest('a', 's', e), strict=True)
    async def chunks():
        yield {'type': 'response.completed', 'response': {'usage': {'input_tokens': 7, 'output_tokens': 2}}}
    async def create_stream():
        return chunks()
    class Client:
        async def call_tool(self, name):
            return {'isError': True, 'content': []}
    async def run():
        assert len([x async for x in tracer.astream_model('model', create_stream)]) == 1
        assert (await TracedMCPClient(Client(), tracer).call_tool('bad'))['isError']
    asyncio.run(run())
    rows = store.spans('a', 's')
    assert rows[0]['metrics']['provider.input_tokens'] == 7
    assert next(r for r in rows if r['kind'] == 'tool')['status'] == 'error'
    store.close()


def test_export_preserves_unknown_usage_and_incomplete_spans(tmp_path):
    store = TraceStore(tmp_path)
    tracer = Tracer(lambda e: store.ingest('a', 's', e), strict=True)
    tracer.call_model('unknown', lambda: {})
    assert not folded(store.spans('a', 's'), 'tokens')['complete']
    active = tracer.span('active')
    active.__enter__()
    assert not folded(store.spans('a', 's'), 'self_ns')['complete']
    active.__exit__(None, None, None)
    store.close()


def test_clock_kernel_without_time_namespaces(monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    from allox_agent_monitor import clock

    monkeypatch.setattr(clock.sys, 'platform', 'linux')
    monkeypatch.setattr(clock.time, 'get_clock_info', lambda name: SimpleNamespace(
        implementation='clock_gettime(CLOCK_MONOTONIC)'))
    monkeypatch.setattr(Path, 'read_text', lambda self: '0'*32)
    monkeypatch.setattr(Path, 'iterdir', lambda self: iter(Path(x) for x in ('pid', 'mnt', 'user', 'uts')))
    assert clock.monotonic_domain() == 'linux-'+'0'*32+'-no-timens'
    monkeypatch.setattr(Path, 'iterdir', lambda self: iter([Path('time_for_children')]))
    assert not clock.monotonic_domain().startswith('linux-')
    monkeypatch.setattr(Path, 'iterdir', lambda self: iter([Path('time')]))
    def denied(self):
        raise PermissionError('namespace unavailable')
    monkeypatch.setattr(Path, 'stat', denied)
    assert not clock.monotonic_domain().startswith('linux-')
