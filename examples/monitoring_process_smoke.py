"""Controlled Python workload in real cgroups/Bubblewrap; no model or BPF claims."""

import json
import os
import time
from pathlib import Path

from allox_agent_monitor.sdk import Tracer

from allox.workspace.daemon import WorkspaceService
from allox.workspace.store import WorkspaceStore


class DirectoryBackend:
    def assert_root(self, root):
        pass

    def create_subvolume(self, path):
        path.mkdir()


TASK = '''import json, os, socket, subprocess, sys
from pathlib import Path
from allox_agent_monitor import Tracer
from allox_agent_monitor.transport import emit_session
def work():
    child = subprocess.check_output([sys.executable, '-c', 'print(55)'], text=True).strip()
    listener = socket.socket(socket.AF_UNIX)
    listener.bind('/tmp/proof.sock')
    listener.listen(1)
    listener.close()
    result = {'agent':os.environ['ALLOX_AGENT_ID'], 'session':os.environ['ALLOX_SESSION_ID'],
              'child_result':child, 'socket_created':Path('/tmp/proof.sock').is_socket()}
    Path('proof.json').write_text(json.dumps(result))
    return result
with Tracer(emit_session, strict=True, batch_size=32, delivery='background') as t:
    t.call_tool('subprocess-and-socket', work)
print(json.dumps(t.health()))
'''


def main():
    root = Path('/dev/shm/allox-process-monitor-smoke')
    root.mkdir()
    store = WorkspaceStore(root/'workspaces', DirectoryBackend())
    store.initialize()
    service = WorkspaceService(store, monitor_root=root/'monitor',
        process_audit_root=root/'audit',
        process_cgroup_root=Path('/sys/fs/cgroup/allox-process-monitor-smoke'))
    output = Path('/results')
    report = {'passed': False, 'kernel': os.uname().release,
              'scope': 'Kata + Bubblewrap + cgroup + session socket; controlled Python, no LLM/BPF/rollback',
              'sessions': []}
    report['clock_environment'] = {
        'implementation': time.get_clock_info('perf_counter').implementation,
        'namespace_names': sorted(p.name for p in Path('/proc/self/ns').iterdir()),
        'boot_id_readable': os.access('/proc/sys/kernel/random/boot_id', os.R_OK),
    }
    try:
        for agent, session in [('a', 's1'), ('a', 's2'), ('b', 's1')]:
            store.create_session(agent, session)
            current = store.current(agent, session)
            (current/'task.py').write_text(TASK)
            tracer = Tracer(lambda event, a=agent, s=session: service.dispatch('monitor.ingest',
                {'agent_id': a, 'session_id': s, 'event': event}), strict=True, batch_size=32,
                delivery='background')
            with (
                tracer,
                tracer.span('controlled-agent', 'run', semantic_path=['smoke']),
                tracer.span('action', 'turn', turn_id='t1') as turn,
            ):
                row = service.processes.start(agent, session, ['python3', 'task.py'],
                    env={'ALLOX_MONITOR_CONTEXT': '{"trace_id":"forged"}'},
                    trace_context=turn.context, timeout=60)
                if not service.processes._runs[row['run_id']]['done'].wait(90):
                    raise RuntimeError('controlled workload timed out')
                row = service.processes.get(agent, session, row['run_id'])
                if row.get('root_exit_code') != 0:
                    raise RuntimeError((Path(row['audit_directory'])/'stderr.log').read_text()[-1200:])
            spans = service.dispatch('monitor.trace', {'agent_id': agent, 'session_id': session})['spans']
            summary = service.dispatch('monitor.profile', {'agent_id': agent, 'session_id': session})
            proof = json.loads((current/'proof.json').read_text())
            sdk_health = json.loads((Path(row['audit_directory'])/'stdout.log').read_text().splitlines()[-1])
            core = [s for s in spans if s['source'] == 'allox-executor']
            sdk = [s for s in spans if s['source'] == 'session-sdk']
            checks = {
                'identity': all(s['agent_id'] == agent and s['session_id'] == session for s in spans),
                'one_trace': len({s['trace_id'] for s in spans}) == 1 and spans[0]['trace_id'] != 'forged',
                'one_clock': len({s['clock_id'] for s in spans}) == 1 and spans[0]['clock_id'].startswith('linux-'),
                'core_parent': len(core) == 1 and any(s['parent_id'] == core[0]['span_id'] for s in sdk),
                'sdk_run_bound': bool(sdk) and all(s['process_run_id'] == row['run_id'] for s in sdk),
                'all_ended': all(s['status'] == 'ok' for s in spans),
                'profile_complete': summary['complete'] and summary['runs'] == 1,
                'semantic_inherited': all(s['semantic_path'] == ['smoke'] for s in spans),
                'actual_subprocess_socket': proof['child_result'] == '55' and proof['socket_created'],
                'resource_cpu': bool(core) and 'cgroup.cpu_usage_usec' in core[0]['metrics'],
                'background_drained': sdk_health['complete'] and not sdk_health['worker_alive'],
            }
            report['sessions'].append({'agent': agent, 'session': session, 'checks': checks,
                                       'span_count': len(spans), 'profile': summary})
            (output/f'{agent}-{session}-trace.json').write_text(json.dumps(spans, indent=2))
        report['passed'] = all(all(s['checks'].values()) for s in report['sessions'])
    finally:
        (output/'report.json').write_text(json.dumps(report, indent=2))
        service.close()
    print(json.dumps({'passed': report['passed'], 'sessions': [s['checks'] for s in report['sessions']]}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
