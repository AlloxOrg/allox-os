"""Ten paired trials through real Bubblewrap/cgroups/session telemetry, no LLM/eBPF."""

import argparse
import json
import os
import resource
import statistics
import subprocess
import sys
import time
from pathlib import Path

from allox.workspace.daemon import WorkspaceService
from allox.workspace.store import WorkspaceStore


class DirectoryBackend:
    def assert_root(self, root):
        pass

    def create_subvolume(self, path):
        path.mkdir()


WORKLOAD = r'''
import json,resource,statistics,sys,time
mode=sys.argv[1]
enabled=mode!='off'
import_started=time.perf_counter_ns()
sdk_loader=None
if enabled:
 from allox_agent_monitor import Tracer
 from allox_agent_monitor.transport import emit_session
 kwargs={'delivery':'background'} if mode in ('background','background64') else {}
 tracer=Tracer(emit_session,strict=True,batch_size=64 if mode=='background64' else 32,**kwargs)
 sdk_loader=type(sys.modules['allox_agent_monitor'].__loader__).__name__
import_ns=time.perf_counter_ns()-import_started
def work():
 result=sum(i*i for i in range(1000))
 time.sleep(.001)
 return result
elapsed=[];checksum=0
before=resource.getrusage(resource.RUSAGE_SELF)
start=time.perf_counter_ns()
for i in range(40):
 step=time.perf_counter_ns()
 checksum+=tracer.call_tool('cpu-and-wait',work) if enabled else work()
 elapsed.append(time.perf_counter_ns()-step)
loop_ns=time.perf_counter_ns()-start
if enabled: tracer.close()
wall=time.perf_counter_ns()-start
after=resource.getrusage(resource.RUSAGE_SELF)
print(json.dumps({'wall_ns':wall,'step_ns':elapsed,'step_p50_ns':statistics.median(elapsed),
 'step_p95_ns':sorted(elapsed)[37],'checksum':checksum,'steps':40,
 'cpu_seconds':after.ru_utime+after.ru_stime-before.ru_utime-before.ru_stime,
 'peak_rss_kib':after.ru_maxrss,'loop_before_flush_ns':loop_ns,
 'sdk_health':tracer.health() if enabled else None,'sdk_import_init_ns':import_ns,'sdk_loader':sdk_loader}))
'''


def cpu_time():
    own = resource.getrusage(resource.RUSAGE_SELF)
    child = resource.getrusage(resource.RUSAGE_CHILDREN)
    return own.ru_utime+own.ru_stime+child.ru_utime+child.ru_stime


def worker(mode, label):
    root = Path('/dev/shm/allox-overhead')/label
    root.mkdir(parents=True)
    data = Path('/results/collector-data')/label
    setup_start = time.perf_counter_ns()
    store = WorkspaceStore(root/'workspaces', DirectoryBackend())
    store.initialize()
    store.create_session('benchmark', 'session')
    (store.current('benchmark', 'session')/'task.py').write_text(WORKLOAD)
    service = WorkspaceService(store, process_audit_root=root/'audit',
        kill_session_processes_on_rollback=True,
        monitor_root=data if mode != 'off' else None,
        process_cgroup_root=Path('/sys/fs/cgroup/allox-overhead')/label)
    setup_ns = time.perf_counter_ns()-setup_start
    try:
        before = cpu_time()
        started = time.perf_counter_ns()
        row = service.processes.start('benchmark', 'session', ['python3', 'task.py', mode], timeout=240)
        if not service.processes._runs[row['run_id']]['done'].wait(270):
            raise RuntimeError('benchmark execution timed out')
        wall_ns = time.perf_counter_ns()-started
        cpu_seconds = cpu_time()-before
        row = service.processes.get('benchmark', 'session', row['run_id'])
        audit = Path(row['audit_directory'])
        if row['state'] != 'completed' or row.get('root_exit_code') != 0:
            raise RuntimeError('worker failed: '+(audit/'stderr.log').read_text()[-700:])
        workload = json.loads((audit/'stdout.log').read_text().splitlines()[-1])
        if workload['steps'] != 40 or workload['checksum'] != 40*sum(i*i for i in range(1000)):
            raise RuntimeError('workload result mismatch')
        result = {'enabled': mode != 'off', 'mode': mode, 'wall_ns': wall_ns, 'cpu_seconds': cpu_seconds,
            'setup_ns': setup_ns, 'daemon_peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            'workload': workload, 'collector': None, 'span_count': 0}
        if mode != 'off':
            spans = service.dispatch('monitor.trace', {'agent_id': 'benchmark', 'session_id': 'session'})['spans']
            result['collector'] = service.dispatch('monitor.status', {})
            result['span_count'] = len(spans)
            result['checks'] = {
                'expected_spans': len(spans) == 81,
                'session_owned': all(s['agent_id'] == 'benchmark' and s['session_id'] == 'session' for s in spans),
                'all_ended': all(s['status'] == 'ok' for s in spans),
                'sdk_accepted': workload['sdk_health']['attempted_events'] == 160 and workload['sdk_health']['failed_events'] == 0,
                'collector_accepted': result['collector']['accepted_events_this_process'] == 162,
                'expected_sdk_loader': workload['sdk_loader'] == 'SourceFileLoader',
            }
            if not all(result['checks'].values()):
                raise RuntimeError('monitor reconciliation failed')
        return result
    finally:
        service.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', choices=('off', 'legacy', 'sync', 'background', 'background64'))
    parser.add_argument('--label')
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args.worker, args.label)), flush=True)
        return
    result = {'scope': 'real Kata/Bubblewrap/cgroup + Session socket + SQLite FULL; no LLM/eBPF',
        'modes': ['off', 'legacy', 'sync', 'background', 'background64'],
        'batch_sizes': {'legacy': 32, 'sync': 32, 'background': 32, 'background64': 64},
        'kernel': os.uname().release, 'pairs': [], 'steps_per_execution': 40,
        'collector_filesystem': subprocess.check_output(['stat', '-f', '-c', '%T', '/results'], text=True).strip(),
        'process_state_filesystem': 'Guest /dev/shm tmpfs',
        'timing': 'wall_ns includes process.start through finalization; excludes service setup/teardown and VM boot',
        'cpu_scope': 'Guest daemon plus reaped children, not host hypervisor CPU',
        'rss_scope': 'separate daemon and workload process maxima, not combined/VM memory',
        'managed_runtime': 'enabled identically in both modes; no rollback performed'}
    attempt = str(time.time_ns())
    modes = ['off', 'legacy', 'sync', 'background', 'background64']
    for index in range(10):
        order = modes[index % len(modes):]+modes[:index % len(modes)]
        pair = {'index': index+1, 'order': order}
        for mode in order:
            started = time.perf_counter_ns()
            environment = {**__import__('os').environ, 'PYTHONPATH': '/opt/legacy' if mode == 'legacy' else ''}
            completed = subprocess.run([sys.executable, __file__, '--worker', mode,
                '--label', f'{attempt}-trial-{index+1}-{mode}'], text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, check=False, timeout=300, env=environment)
            if completed.returncode:
                raise RuntimeError(completed.stderr[-1500:])
            pair[mode] = json.loads(completed.stdout)
            pair[mode]['whole_worker_wall_ns'] = time.perf_counter_ns()-started
        pair['wall_overhead_percent'] = (pair['background']['wall_ns']/pair['off']['wall_ns']-1)*100
        result['pairs'].append(pair)
        Path('/results/guest-benchmark.json').write_text(json.dumps(result, indent=2))
        print(json.dumps({'group': index+1, 'wall_ms': {mode: pair[mode]['wall_ns']/1e6 for mode in modes},
            'background_overhead_percent': pair['wall_overhead_percent']}), flush=True)
    result['median_wall_overhead_percent'] = statistics.median(p['wall_overhead_percent'] for p in result['pairs'])
    result['completed'] = True
    Path('/results/guest-benchmark.json').write_text(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
