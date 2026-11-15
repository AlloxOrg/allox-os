"""In-memory phase timers in an isolated test Guest. Preserve all operations."""
import functools
import inspect
import itertools
import json
import os
import shutil
import threading
import time
from collections import Counter
from pathlib import Path

import guest
from allox_agent_monitor.sdk import Tracer
from allox_agent_monitor.store import TraceStore
from allox_system_effects import runner

from allox.runtime.extensions import FeatureManager
from allox.runtime.process_tracking.cgroup import SessionCgroup
from allox.runtime.process_tracking.service import ProcessTrackingService

events = []
identities = itertools.count()
local = threading.local()
case = ''


def record(name, original):
    @functools.wraps(original)
    def timed(*args, **kwargs):
        stack = getattr(local, 'stack', [])
        local.stack = stack
        identity = next(identities)
        entry = {'id': identity, 'parent': stack[-1] if stack else None,
                 'case': case, 'name': name, 'thread': threading.get_native_id(),
                 'start_ns': time.perf_counter_ns(), 'wall_epoch_ns': time.time_ns()}
        if name == 'observer._receive':
            entry['name'] += '.'+str(args[-1])
        if name == 'observer.write_file':
            entry['file'] = Path(args[0]).name
        events.append(entry)
        stack.append(identity)
        stop, sampler = threading.Event(), None
        snapshots = Counter()
        if entry['name'] == 'observer._receive.FINISHED' and os.environ.get('ALLOX_LIFECYCLE_STACKS') == '1':
            pid = args[0]._process.pid

            def sample():
                while not stop.is_set():
                    try:
                        snapshots[Path('/proc/'+str(pid)+'/stack').read_text()] += 1
                    except OSError as exc:
                        snapshots['unavailable:'+type(exc).__name__] += 1
                    stop.wait(.005)
            sampler = threading.Thread(target=sample, daemon=True)
            sampler.start()
        cpu = time.thread_time_ns()
        try:
            return original(*args, **kwargs)
        finally:
            entry.update(end_ns=time.perf_counter_ns(), cpu_ns=time.thread_time_ns()-cpu)
            if sampler:
                stop.set()
                sampler.join(timeout=1)
                entry['kernel_stacks'] = dict(snapshots)
            stack.pop()
    return timed


def methods(cls, prefix, names):
    for name in names:
        descriptor = inspect.getattr_static(cls, name)
        original = getattr(cls, name)
        wrapper = record(prefix+'.'+name, original)
        setattr(cls, name, staticmethod(wrapper) if isinstance(descriptor, staticmethod) else wrapper)


methods(runner.SystemEffectsRun, 'observer', ['__init__', '_receive', '_validate_ready',
    '_validate_quiescence', 'finish', '_send', '_require_observer_exit',
    '_validate_terminal_artifacts', '_run_verifier', '_validate_offline_verification',
    '_shutdown'])
runner._write_new_file = record('observer.write_file', runner._write_new_file)
methods(ProcessTrackingService, 'core', ['start', '_wait_inner_gate', '_wait_stopped',
    '_save', '_save_durable', '_target_identity'])
methods(SessionCgroup, 'cgroup', ['attach', 'freeze', 'thaw', 'quiesce_empty', 'wait_empty', 'remove'])
methods(FeatureManager, 'features', ['start_execution', 'execution_started', 'execution_finished'])
methods(TraceStore, 'collector', ['ingest', 'ingest_group', 'ingest_batch'])
methods(Tracer, 'framework', ['close'])

real_span = Tracer.span


def marked_span(self, name, *args, **kwargs):
    if name == 'experiment':
        events.append({'id': next(identities), 'case': case, 'name': 'timed_boundary',
                       'thread': threading.get_native_id(), 'start_ns': time.perf_counter_ns()})
    return real_span(self, name, *args, **kwargs)


Tracer.span = marked_span
real_fsync = os.fsync


def synced(fd):
    try:
        path = os.readlink('/proc/self/fd/'+str(fd))
    except OSError:
        path = 'unavailable'
    before, cpu = time.perf_counter_ns(), time.thread_time_ns()
    try:
        return real_fsync(fd)
    finally:
        events.append({'id': next(identities), 'parent': (getattr(local, 'stack', None) or [None])[-1],
            'case': case, 'name': 'python.fsync', 'file': path,
            'thread': threading.get_native_id(), 'start_ns': before,
            'end_ns': time.perf_counter_ns(), 'cpu_ns': time.thread_time_ns()-cpu})


os.fsync = synced
if os.environ.get('ALLOX_LIFECYCLE_STRACE') == '1':
    for name in ('observer', 'verifier'):
        target = Path('/opt/diagnostic-'+name)
        shutil.copy2(Path(__file__).with_name('native_trace.py'), target)
        target.chmod(0o755)
    original_config = guest.effects_config

    def config():
        result = original_config()
        result.update(observer_path='/opt/diagnostic-observer', verifier_path='/opt/diagnostic-verifier')
        return result
    guest.effects_config = config


def run(*args, **kwargs):
    global case
    case = args[0]
    result = original_run(*args, **kwargs)
    selected = [entry for entry in events if entry['case'] == case]
    (Path('/results') / case / 'phases.json').write_text(json.dumps(selected, indent=2))
    return result


original_run = guest.run
guest.run = run
# Reuse the unchanged business workload, checks and off/on ordering.
original_main = guest.main


def main():
    global case
    loop = guest.prepare_btrfs()
    fixture = guest.ThreadingHTTPServer(('127.0.0.1', 0), guest.Fixture)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()
    report = {'passed': False, 'cases': [], 'scope': 'lifecycle diagnostics; no LLM', 'instrumentation': 'in-memory wall/thread CPU timers',
              'strace': os.environ.get('ALLOX_LIFECYCLE_STRACE') == '1'}
    try:
        for index in range(int(os.environ.get('ALLOX_LIFECYCLE_PAIRS', '10'))):
            for mode in (('off', 'batch') if index % 2 == 0 else ('batch', 'off')):
                report['cases'].append(run('pair-'+str(index)+'-'+mode, mode, fixture))
        report['passed'] = all(item['passed'] for item in report['cases'])
    finally:
        (Path('/results') / 'report.json').write_text(json.dumps(report, indent=2))
        fixture.shutdown()
        fixture.server_close()
        guest.subprocess.run(['umount', '/state'], check=False)
        guest.subprocess.run(['losetup', '-d', loop], check=False)
    return 0 if report['passed'] else 1


raise SystemExit(main())
