"""Run ten-pair baselines sequentially on 117, preserving raw samples and storage provenance."""

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path('/data/hy/Allox/monitoring-background-20261101-v1')
SOURCE = ROOT/'source'
NAME = 'allox-monitor-background-bench-20261101'


def tail_cpus():
    cpus = []
    for part in Path('/sys/devices/system/cpu/online').read_text().strip().split(','):
        bounds = [int(x) for x in part.split('-')]
        cpus.extend(range(bounds[0], bounds[-1]+1))
    return sorted(set(cpus))[-24:]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--guest-only', action='store_true')
    args = parser.parse_args()
    cpus = tail_cpus()
    os.sched_setaffinity(0, cpus)
    if Path.cwd().resolve() != ROOT:
        raise SystemExit('use the dedicated 117 experiment directory')
    results = ROOT/'results-v3'
    results.mkdir(exist_ok=True)
    artifacts = [SOURCE/'examples/monitoring_benchmark.py', SOURCE/'dist/allox_os-0.1.0-py3-none-any.whl',
        SOURCE/'plugins/agent-monitor/dist/allox_agent_monitor-0.1.0-py3-none-any.whl']
    artifacts += list(sorted((ROOT/'baseline').glob('*.whl')))
    metadata = {'source_base': 'baseline parity + background exporter', 'batch_size': 32,
        'modes': ['off', 'legacy sync32', 'new sync32', 'new background32', 'new background64'],
        'durability': 'FULL commit before batch ACK; explicit flush timed; unflushed SDK buffer not durable',
        'repetitions': 10, 'steps': 40,
        'host_kernel': os.uname().release, 'online_cpus': Path('/sys/devices/system/cpu/online').read_text().strip(),
        'affinity': sorted(os.sched_getaffinity(0)), 'affinity_policy': 'last 24 online host CPUs',
        'micro_temp_filesystem': subprocess.check_output(['stat', '-f', '-c', '%T', '/tmp'], text=True).strip(),
        'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'artifact_sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in artifacts}}
    (results/'environment.json').write_text(json.dumps(metadata, indent=2))
    if not args.guest_only:
        for delivery in ('sync', 'background'):
            os.sched_setaffinity(0, tail_cpus())
            print('Starting 10-pair SDK '+delivery, flush=True)
            with (results/f'micro-{delivery}.json').open('w') as output:
                subprocess.run(['/data/hy/Allox/agent-monitoring-main-20261030-v2/.validation-venv/bin/python',
                    str(artifacts[0]), '--pairs', '10', '--steps', '40', '--batch-size', '32',
                    '--delivery', delivery], cwd=SOURCE,
                    env={**os.environ, 'PYTHONPATH': str(SOURCE/'src')+':'+str(SOURCE/'plugins/agent-monitor/src')},
                    stdout=output, check=True, timeout=600)
    print('Starting real Guest four-arm baseline', flush=True)
    guest = results/'guest'
    guest.mkdir(exist_ok=True)
    if (guest/'console.log').exists():
        (guest/'console.log').rename(guest/f'console-attempt-{time.time_ns()}.log')
    command = ['docker', 'run', '--rm', '--name', NAME, '--runtime', 'kata-allox-ebpf',
        '--cpuset-cpus', ','.join(map(str, tail_cpus())),
        '--cgroupns', 'host', '--cap-add', 'SYS_ADMIN', '--shm-size', '256m',
        '--security-opt', 'seccomp=unconfined', '--security-opt', 'apparmor=unconfined',
        '-e', 'PYTHONPATH=', '-e', 'PYTHONDONTWRITEBYTECODE=1', '-e', 'TMPDIR=/dev/shm',
        '-v', f'{SOURCE}/dist:/dist/core:ro',
        '-v', f'{SOURCE}/plugins/agent-monitor/dist:/dist/monitor:ro',
        '-v', f'{ROOT}/baseline:/dist/legacy:ro',
        '-v', f'{ROOT}/guest_bench.py:/opt/guest_bench.py:ro', '-v', f'{guest}:/results',
        'allox-ebpf-test:20260907', 'sh', '-ec',
        ('python3 -m pip install -q --no-deps /dist/core/*.whl /dist/monitor/*.whl; '
         'mkdir -p /opt/legacy; for wheel in /dist/legacy/*.whl; do '
         'python3 -m zipfile -e "$wheel" /opt/legacy; done; '
         'python3 -m allox.runtime.bootstrap; python3 -u /opt/guest_bench.py')]
    try:
        with (guest/'console.log').open('w') as output:
            status = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, check=False, timeout=1500)
        metadata['guest_exit_code'] = status.returncode
        print((guest/'console.log').read_text()[-4000:], flush=True)
        if status.returncode:
            raise RuntimeError('Guest baseline failed; no incomplete data counted as success')
    finally:
        subprocess.run(['docker', 'rm', '-f', NAME], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
        metadata['temporary_container_removed'] = True
        (results/'environment.json').write_text(json.dumps(metadata, indent=2))
    metadata['completed'] = True
    (results/'environment.json').write_text(json.dumps(metadata, indent=2))


if __name__ == '__main__':
    main()
