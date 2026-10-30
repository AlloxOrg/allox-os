"""Real cgroup/Unix-peer identity and profiling checks; no LLM or fake cgroups."""

import json
import os
from pathlib import Path

from allox.workspace.daemon import WorkspaceService
from allox.workspace.store import WorkspaceStore


class DirectoryBackend:
    def assert_root(self, root):
        pass

    def create_subvolume(self, path):
        path.mkdir()


PROBE = r'''
import json,os,subprocess,sys
from pathlib import Path
from allox_agent_monitor import Tracer
from allox_agent_monitor.transport import emit_session

t=Tracer(emit_session,trace_id='shared-trace',strict=True)
with t.span('contract-run','run',semantic_path=['contract']):
 def work():
  existed=Path('identity.json').exists()
  child=subprocess.check_output([sys.executable,'-c','print(sum(range(250000)))']).decode().strip()
  identity={'agent':os.environ['ALLOX_AGENT_ID'],'session':os.environ['ALLOX_SESSION_ID'],
            'previous_identity_visible':existed,'child_result':int(child),
            'management_token_absent':'ALLOX_WORKSPACE_TOKEN' not in os.environ}
  Path('identity.json').write_text(json.dumps(identity))
  return identity
 result=t.call_tool('probe',work)
 try:
  with t.span('expected-tool-error','tool'):
   raise ValueError('intentional contract failure')
 except ValueError:
  pass
 try:
  emit_session({'event':'start','agent_id':'foreign','session_id':'foreign'})
  result['forged_identity_rejected']=False
 except Exception:
  result['forged_identity_rejected']=True
result['sdk_health']=t.health()
print(json.dumps(result))
'''


def main():
    root = Path('/dev/shm/allox-monitor-contract')
    root.mkdir()
    store = WorkspaceStore(root/'workspace', DirectoryBackend())
    store.initialize()
    service = WorkspaceService(store, monitor_root=root/'monitor',
        process_audit_root=root/'process-audit',
        process_cgroup_root=Path('/sys/fs/cgroup/allox-monitor-contract'))
    report = {'passed': False, 'kernel': os.uname().release,
              'snapshot_backend': 'directory fixture; no rollback tested', 'runs': []}
    try:
        launched = []
        for agent, session in [('agent-a', 's1'), ('agent-a', 's2'), ('agent-b', 's1')]:
            store.create_session(agent, session)
            (store.current(agent, session)/'probe.py').write_text(PROBE)
            row = service.processes.start(agent, session, ['python3', 'probe.py'], timeout=60)
            launched.append((agent, session, row['run_id']))
        checks = {}
        all_spans = []
        for agent, session, run_id in launched:
            if not service.processes._runs[run_id]['done'].wait(90):
                raise RuntimeError('contract process timed out')
            row = service.processes.get(agent, session, run_id)
            stdout = (Path(row['audit_directory'])/'stdout.log').read_text()
            stderr = (Path(row['audit_directory'])/'stderr.log').read_text()
            if row.get('root_exit_code') != 0:
                raise RuntimeError('contract process failed: '+stderr[-500:])
            result = json.loads(stdout.splitlines()[-1])
            spans = service.dispatch('monitor.trace', {'agent_id': agent, 'session_id': session})['spans']
            all_spans.extend(spans)
            core = [s for s in spans if s['source'] == 'allox-executor']
            sdk = [s for s in spans if s['source'] == 'session-sdk']
            label = agent+'/'+session
            checks[label] = all([
                row['state'] == 'completed', row.get('lifecycle_complete') is True,
                row.get('session_fenced') is False,
                result['agent'] == agent, result['session'] == session,
                not result['previous_identity_visible'], result['management_token_absent'],
                result['forged_identity_rejected'], result['sdk_health']['failed_events'] == 0,
                result['child_result'] == sum(range(250000)), len(core) == 1, len(sdk) >= 4,
                all(s['process_run_id'] == run_id for s in sdk),
                all(s['agent_id'] == agent and s['session_id'] == session for s in spans),
                all(s['status'] != 'running' for s in spans),
                any(s['name'] == 'expected-tool-error' and s['status'] == 'error' for s in sdk),
                any('executor.execution_ns' in s['metrics'] for s in sdk),
                core[0]['metrics'].get('cgroup.cpu_usage_usec', 0) > 0,
            ])
            report['runs'].append({'agent': agent, 'session': session, 'run_id': run_id,
                'state': row['state'], 'probe': result, 'span_count': len(spans),
                'core_metrics': core[0]['metrics'] if core else {}})
        combined = service.dispatch('monitor.profile', {'agent_id': 'agent-a', 'session_id': 's1',
            'session_ids': ['s1', 's2'], 'trace_id': 'shared-trace'})
        probes = [g for g in combined['groups'] if g['kind'] == 'tool' and g['name'] == 'probe']
        checks['cross_session_profile'] = combined['runs'] == 2 and len(probes) == 1 and probes[0]['calls'] == 2
        checks['cross_session_profile_complete'] = combined['complete']
        report.update(checks=checks, combined_profile=combined, passed=all(checks.values()))
        Path('/results/contract-trace.json').write_text(json.dumps(all_spans, indent=2))
    except Exception as exc:  # noqa: BLE001 - test report, no provider configuration present
        report['error'] = type(exc).__name__+': '+str(exc)[:500]
    finally:
        service.close()
        Path('/results/contract-report.json').write_text(json.dumps(report, indent=2))
        print(json.dumps({'passed': report['passed'], 'checks': report.get('checks'),
                          'error': report.get('error')}), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
