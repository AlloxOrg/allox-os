"""Real Guest, Btrfs, cgroups, Observer and Codex/Qwen integration oracle."""
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from allox_agent_monitor import Tracer
from allox_trace_correlation.correlate import Correlator

from allox.workspace.daemon import WorkspaceService
from allox.workspace.store import WorkspaceError, WorkspaceStore

OUT = Path('/results')
SOURCE = Path('/allox')
spec = importlib.util.spec_from_file_location('existing_proxy', SOURCE / 'examples/qwen_codex_117/guest_smoke.py')
proxy_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy_module)


def command(*args):
    return subprocess.check_output(args, text=True).strip()


class Fixture(BaseHTTPRequestHandler):
    calls = 0
    def log_message(self, *_):
        pass
    def do_GET(self):
        type(self).calls += 1
        self.send_response(200)
        self.send_header('Content-Length', '4')
        self.end_headers()
        self.wfile.write(b'pong')


def prepare_btrfs():
    Path('/state').mkdir(exist_ok=True)
    image = Path('/dev/shm/e2e.btrfs')
    with image.open('wb') as stream:
        stream.truncate(768*1024*1024)
    subprocess.run(['/fixtures/bin/mkfs.btrfs', '-q', '-f', str(image)], check=True)
    if not Path('/dev/loop-control').exists():
        subprocess.run(['mknod', '/dev/loop-control', 'c', '10', '237'], check=True)
    for i in range(8):
        if not Path('/dev/loop'+str(i)).exists():
            subprocess.run(['mknod', '/dev/loop'+str(i), 'b', '7', str(i)], check=True)
    loop = command('losetup', '--find', '--show', str(image))
    subprocess.run(['mount', '-t', 'btrfs', loop, '/state'], check=True)
    if not Path('/sys/kernel/security/lsm').exists():
        subprocess.run(['mount', '-t', 'securityfs', 'securityfs', '/sys/kernel/security'], check=True)
    tracing = Path('/sys/kernel/tracing')
    if not (tracing / 'events/raw_syscalls/sys_enter/id').exists():
        subprocess.run(['mount', '-t', 'tracefs', 'tracefs', str(tracing)], check=True)
    assert (tracing / 'events/raw_syscalls/sys_enter/id').is_file()
    return loop


def effects_config():
    root = Path('/opt/observer')
    return {'observer_path': str(root / 'deployment-observe'),
            'verifier_path': str(root / 'deployment-observe-verify'),
            'recorder_object_path': str(root / 'system_effects.bpf.o'),
            'recorder_object_sha256': hashlib.sha256((root / 'system_effects.bpf.o').read_bytes()).hexdigest(),
            'lifecycle_object_path': str(root / 'process_lifecycle.bpf.o'),
            'lifecycle_object_sha256': hashlib.sha256((root / 'process_lifecycle.bpf.o').read_bytes()).hexdigest(),
            'profile': 'balanced', 'object_resolution_mode': 'KERNEL_OBJECT_EXPANDED_PARTIAL',
            'poll_interval_ms': 5, 'ready_timeout': 60, 'finalize_timeout': 120}


def collect(correlator, agent, session, run_id, stream):
    events = []
    offset = 0
    while True:
        page = correlator.run(agent, session, run_id, stream=stream, offset=offset, limit=200)
        if page['status'] != 'correlated':
            raise RuntimeError('uncorrelated evidence: '+page['status'])
        events.extend(page['events'])
        if page['next_offset'] is None:
            return {k: v for k, v in page.items() if k != 'events'}, events
        offset = page['next_offset']


def run(label, mode, fixture, *, llm=False, agent='qwen', session='s1', ambiguous=False):
    output = OUT / label
    output.mkdir(mode=0o700)
    storage = Path('/state') / label
    storage.mkdir(mode=0o700)
    store = WorkspaceStore(storage)
    store.initialize()
    store.create_session(agent, session)
    current = store.current(agent, session)
    shutil.copy2(SOURCE / 'examples/monitor_e2e/action_task.py', current / 'task.py')
    enabled = mode != 'off'
    service = WorkspaceService(store, monitor_root=output / 'monitor' if enabled else None,
        monitor_async_write=enabled, process_audit_root=output / 'audit',
        process_cgroup_root=Path('/sys/fs/cgroup/allox-e2e') / label,
        kill_session_processes_on_rollback=True,
        system_effects_config=effects_config() if enabled else None)
    tracer = Tracer(lambda event: service.dispatch('monitor.ingest', {
        'agent_id': agent, 'session_id': session, 'event': event}), enabled=enabled, strict=True)
    proxy = None
    result = {'label': label, 'mode': mode, 'real_llm': llm, 'checks': {}}
    try:
        environment = {'E2E_MODE': mode, 'E2E_PORT': str(fixture.server_port), 'E2E_STEPS': '12',
                       'E2E_AMBIGUOUS': '1' if ambiguous else '0'}
        argv = ['python3', 'task.py']
        if llm:
            proxy = ThreadingHTTPServer(('127.0.0.1', 0), proxy_module.ModelProxy)
            proxy.tracer, proxy.requests, proxy.parent_id = tracer, [], None
            proxy.service = service
            threading.Thread(target=proxy.serve_forever, daemon=True).start()
            home = current / '.codex'
            home.mkdir()
            (home / 'config.toml').write_text('model = '+json.dumps(os.environ['ALLOX_QWEN_MODEL'])+'\n'
                'model_provider="allox_lab"\napproval_policy="never"\nsandbox_mode="danger-full-access"\n'
                'model_context_window=16384\n[model_providers.allox_lab]\nname="Owned lab bridge"\n'
                'base_url="http://127.0.0.1:'+str(proxy.server_port)+'/v1"\nwire_api="responses"\n'
                'requires_openai_auth=false\nrequest_max_retries=0\nstream_max_retries=0\n')
            environment.update(CODEX_HOME='/workspace/.codex', LD_LIBRARY_PATH='/usr/local/lib/qwen-test')
            argv = ['/usr/local/bin/qwen-codex-exec', '--json', '--skip-git-repo-check',
                "Use the exec_command tool to execute exactly python3 task.py in /workspace. "
                "Use cmd as a string, not an array. Execute a real tool call, then report whether it succeeded. "
                "Do not run any other commands, inspect credentials, configuration, environment or session logs."]
        before = time.perf_counter_ns()
        with tracer.span('experiment', 'run', semantic_path=['e2e']):
            with tracer.span('agent-turn', 'turn', turn_id='agent-turn-1') as turn:
                tracer.turn_id = 'agent-turn-1'  # Explicit context for the test-only model proxy thread.
                if proxy:
                    proxy.parent_id = turn.span_id
                row = service.processes.start(agent, session, argv, env=environment, timeout=300,
                    trace_context=turn.context if enabled else None)
                if not service.processes._runs[row['run_id']]['done'].wait(420):
                    raise RuntimeError('agent or observer finalization timed out')
                row = service.processes.get(agent, session, row['run_id'])
        tracer.close()
        result.update(wall_ns=time.perf_counter_ns()-before, run_id=row['run_id'], process=row)
        if row.get('root_exit_code') != 0 or row['state'] != 'completed':
            raise RuntimeError('tracked execution failed; inspect owned audit and receipt')
        proof = json.loads((current / 'proof.json').read_text())
        result['proof'] = proof
        checks = result['checks']
        checks.update(workload=proof['steps'] == 12 and len(proof['rows']) == 12,
                      identity=proof['agent'] == agent and proof['session'] == session,
                      no_management_token=proof['management_token_absent'],
                      sdk_complete=proof['health']['complete'])
        if proxy:
            result['model_requests'] = proxy.requests
            checks['real_model_calls'] = bool(proxy.requests) and all(r['http_status'] == 200 for r in proxy.requests)
            events = [json.loads(line) for line in (Path(row['audit_directory']) / 'stdout.log').read_text().splitlines()
                      if line.startswith('{')]
            commands = [e['item'] for e in events if e.get('type') == 'item.completed'
                        and e.get('item', {}).get('type') == 'command_execution']
            result['agent_command_count'] = len(commands)
            checks['real_agent_tool_call'] = bool(commands)
        if enabled:
            spans = service.dispatch('monitor.trace', {'agent_id': agent, 'session_id': session})['spans']
            (output / 'trace.json').write_text(json.dumps(spans, indent=2))
            profile = service.dispatch('monitor.profile', {'agent_id': agent, 'session_id': session})
            result['profile'] = profile
            checks.update(all_received_spans_ended=all(s['status'] != 'running' for s in spans),
                          profile_complete=profile['complete'],
                          one_trace=len({s['trace_id'] for s in spans}) == 1,
                          one_clock=len({s['clock_id'] for s in spans}) == 1,
                          sdk_event_count=proof['health']['acknowledged_events'] == 72,
                          session_sdk=sum(s['source'] == 'session-sdk' for s in spans) == 36,
                          sdk_run_binding=all(s['process_run_id'] == row['run_id'] for s in spans if s['source'] == 'session-sdk'))
            correlator = Correlator(output / 'monitor', output / 'audit')
            observation = json.loads((Path(row['audit_directory']) / 'system-effects/deployment-observation-report.json').read_text())
            effects_health = observation['cgroup_outcomes'][0]['health']
            lifecycle_health = observation['lifecycle_outcomes'][0]['health']
            result['evidence_health'] = {'effects': effects_health, 'lifecycle': lifecycle_health,
                                         'overall_status': observation['overall_evidence_status']}
            result.setdefault('coverage_gaps', []).extend(lifecycle_health['limitations'])
            summaries = {}
            for stream in ('system-effects.jsonl', 'process-lifecycle.jsonl'):
                summary, event_rows = collect(correlator, agent, session, row['run_id'], stream)
                summaries[stream] = summary
                checks[stream+'_nonempty'] = bool(event_rows)
                checks[stream+'_turn'] = summary['turn']['status'] == ('ambiguous' if ambiguous else 'declared_parent_turn')
                health = effects_health if stream == 'system-effects.jsonl' else lifecycle_health
                checks[stream+'_sampled_hooks_no_known_loss'] = (
                    health['event_attempted'] == health['event_published'] == health['events_persisted'] == summary['event_count']
                    and health['event_dropped'] == 0 and health['userspace_failures'] == 0
                    and health['task_read_failed'] == 0 and health['reasons'] == []
                    and health['producer_barrier_complete'] and health['drain_completed'] and health['wal_durable'])
                checks[stream+'_conservative_status_preserved'] = (
                    summary['observer_evidence_status'] == observation['overall_evidence_status'] == 'INCOMPLETE'
                    and lifecycle_health['status'] == 'PARTIAL')
                original = Path(row['audit_directory']) / 'system-effects' / stream
                checks[stream+'_provenance_digest'] = summary['provenance']['stream_sha256'] == hashlib.sha256(original.read_bytes()).hexdigest()
                if stream == 'system-effects.jsonl':
                    calls = Counter(r['event'].get('syscall_name', '') for r in event_rows)
                    summary['syscalls'] = dict(calls)
                    summary['domains'] = dict(Counter(r['event'].get('domain', '') for r in event_rows))
                    summary['kinds'] = dict(Counter(r['event'].get('kind', '') for r in event_rows))
                    checks['file_and_network_syscalls'] = {'openat', 'write', 'connect', 'bind'} <= calls.keys()
                    summary['read_syscalls_covered'] = 'read' in calls
                    if 'read' not in calls:
                        result.setdefault('coverage_gaps', []).append(
                            'read syscall is absent from the current side-effect policy; file-open/SDK timing is not read coverage')
                else:
                    kinds = Counter(r['event'].get('kind', '') for r in event_rows)
                    summary['kinds'] = dict(kinds)
                    checks['fork_exec_exit'] = {'FORK', 'EXEC', 'EXIT'} <= kinds.keys()
                (output / ('correlated-'+stream)).write_text(''.join(json.dumps(r)+'\n' for r in event_rows))
            result['correlation'] = summaries
            checks['wrong_agent_rejected'] = False
            try:
                correlator.run('foreign', session, row['run_id'])
            except WorkspaceError:
                checks['wrong_agent_rejected'] = True
            tampered = output / 'tampered-audit'
            tampered.mkdir()
            shutil.copytree(Path(row['audit_directory']), tampered / row['run_id'])
            target = tampered / row['run_id'] / 'system-effects/system-effects.jsonl'
            with target.open('ab') as stream:
                stream.write(b'\n')
            checks['tampered_stream_rejected'] = False
            try:
                Correlator(output / 'monitor', tampered).run(agent, session, row['run_id'])
            except WorkspaceError:
                checks['tampered_stream_rejected'] = True
            result['collector'] = service.dispatch('monitor.status', {})
        result['passed'] = all(checks.values())
    except Exception as exc:
        (output / 'failure-detail.txt').write_text(str(exc))
        result.update(passed=False, error_type=type(exc).__name__, error=str(exc)[:200])
    finally:
        if proxy:
            result['model_requests'] = proxy.requests
            proxy.shutdown()
            proxy.server_close()
        try:
            service.close()
        except Exception as exc:
            result.update(passed=False, close_error_type=type(exc).__name__)
        (output / 'report.json').write_text(json.dumps(result, indent=2))
        print('CASE', label, json.dumps({'passed': result['passed'], 'checks': result['checks'],
                                       'error': result.get('error')}), flush=True)
    return result


def main():
    loop = prepare_btrfs()
    environment = {'kernel': os.uname().release, 'btf': Path('/sys/kernel/btf/vmlinux').is_file(),
                   'lsm': Path('/sys/kernel/security/lsm').read_text().strip(),
                   'cgroup_v2_freeze': Path('/sys/fs/cgroup/cgroup.freeze').exists(),
                   'filesystem': command('stat', '-f', '-c', '%T', '/state')}
    (OUT / 'guest-environment.json').write_text(json.dumps(environment, indent=2))
    assert environment['btf'] and 'bpf' in environment['lsm'].split(',')
    assert environment['filesystem'] == 'btrfs'
    fixture = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()
    report = {'environment': environment, 'cases': [], 'scope': 'owned Guest; reset/resume not tested'}
    try:
        if os.environ.get('ALLOX_E2E_BENCHMARK_ONLY') == '1':
            report['scope'] = '10 paired deterministic 12-action runs; full-monitor off/batch; NO model calls'
            for index in range(10):
                order = ('off', 'batch') if index % 2 == 0 else ('batch', 'off')
                for mode in order:
                    report['cases'].append(run('pair-'+str(index)+'-'+mode, mode, fixture))
            report['passed'] = all(case['passed'] for case in report['cases'])
            return 0 if report['passed'] else 1
        # First deterministic integration probe, before spending real inference requests.
        probe = run('probe', 'batch', fixture)
        report['cases'].append(probe)
        if not probe['passed']:
            raise RuntimeError('deterministic eBPF integration probe failed')
        modes = ('batch',) if os.environ.get('ALLOX_E2E_LOCAL_CPU') == '1' else ('off', 'strict', 'batch')
        for mode in modes:
            report['cases'].append(run('agent-'+mode, mode, fixture, llm=True))
        report['cases'].append(run('other-session', 'batch', fixture, session='s2'))
        report['cases'].append(run('other-agent', 'batch', fixture, agent='other-agent'))
        report['cases'].append(run('multi-turn', 'batch', fixture, ambiguous=True))
        report['passed'] = all(c['passed'] for c in report['cases'])
    except Exception as exc:
        report.update(passed=False, error_type=type(exc).__name__, error=str(exc)[:200])
    finally:
        fixture.shutdown()
        fixture.server_close()
        report['fixture_requests'] = Fixture.calls
        (OUT / 'report.json').write_text(json.dumps(report, indent=2))
        print('ALLOX_E2E_REPORT', json.dumps({'passed': report['passed'], 'error': report.get('error')}), flush=True)
        subprocess.run(['umount', '/state'], check=False)
        subprocess.run(['losetup', '-d', loop], check=False)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
