"""Final validation on 117; obtain last 24 online CPUs immediately before execution."""
import json
import argparse
import os
import subprocess
from pathlib import Path

ROOT = Path('/data/hy/Allox/monitoring-parity-20261031-v1/source')
VENV = Path('/data/hy/Allox/agent-monitoring-main-20261030-v2/.validation-venv')


def online_tail():
    text = Path('/sys/devices/system/cpu/online').read_text().strip()
    ids = []
    for part in text.split(','):
        ends = [int(x) for x in part.split('-')]
        ids.extend(range(ends[0], ends[-1]+1))
    return sorted(set(ids))[-24:]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--process-only', action='store_true')
    args = parser.parse_args()
    cpus = online_tail()
    os.sched_setaffinity(0, cpus)
    result = ROOT/'final-validation'
    result.mkdir(exist_ok=True)
    (result/'affinity.json').write_text(json.dumps({'online': Path('/sys/devices/system/cpu/online').read_text().strip(),
        'host_cpu_ids': cpus, 'policy': 'last 24 online, host affinity and Kata container cpuset'}, indent=2))
    if not args.process_only:
        validate_host(result)
    for name, script in [('unit', (
        'python3 -m pytest -q -s -p no:cacheprovider tests/test_agent_monitor.py '
        'tests/test_agent_monitor_bridge.py tests/test_agent_monitor_batch.py '
        'tests/test_agent_monitor_parity.py '
        'tests/test_process_tracking.py::ProcessTrackingTests::test_lifecycle_feature_runs_between_frozen_inner_gate_and_thaw')),
        ('process', 'python3 examples/monitoring_process_smoke.py')]:
        if args.process_only and name != 'process':
            continue
        run_guest(name, script, result)


def validate_host(result):
    subprocess.run([str(VENV/'bin/ruff'), 'check', 'src/allox/runtime/extensions.py',
        'src/allox/runtime/process_tracking/service.py', 'plugins/agent-monitor/src',
        'tests/test_agent_monitor_parity.py', 'examples/monitoring_process_smoke.py',
        '--select', 'E9,F,I'], cwd=ROOT, check=True)
    with (result/'host-pytest.log').open('w') as log:
        subprocess.run([str(VENV/'bin/python'), '-m', 'pytest', '-q', 'tests', 'plugins/system-effects/tests'], cwd=ROOT,
            stdout=log, stderr=subprocess.STDOUT, timeout=300, check=True)
    print((result/'host-pytest.log').read_text()[-800:], flush=True)
    for folder in (ROOT, ROOT/'plugins/agent-monitor'):
        subprocess.run([str(VENV/'bin/python'), '-m', 'build', '--wheel', '--no-isolation'],
            cwd=folder, stdout=subprocess.DEVNULL, check=True)


def run_guest(name, script, result):
        cpus = online_tail()
        guest_result = result/name
        guest_result.mkdir(exist_ok=True)
        command = ['docker', 'run', '--rm', '--name', 'allox-parity-final-'+name,
            '--runtime', 'kata-allox-ebpf', '--cpuset-cpus', ','.join(map(str, cpus)),
            '--cgroupns', 'host', '--cap-add', 'SYS_ADMIN', '--shm-size', '256m',
            '--security-opt', 'seccomp=unconfined', '--security-opt', 'apparmor=unconfined',
            '-e', 'TMPDIR=/dev/shm', '-e', 'PYTHONDONTWRITEBYTECODE=1',
            '-e', 'PYTHONPATH=/dev/shm/source/src:/dev/shm/source/plugins/agent-monitor/src:'
                '/dev/shm/source/plugins/process-tree/src:/dev/shm/source/plugins/session-share/src:'
                '/dev/shm/source/plugins/session-network/src:/dev/shm/source/plugins/system-effects/src:'
                '/deps/lib/python3.10/site-packages',
            '-v', f'{ROOT}:/work:ro', '-v', f'{VENV}:/deps:ro', '-v', f'{guest_result}:/results',
            'allox-ebpf-test:20260907', 'sh', '-ec',
            'mkdir -p /dev/shm/source; cp -a /work/src /work/plugins /work/tests /work/examples '
            '/work/pyproject.toml /dev/shm/source/; cd /dev/shm/source; '
            'python3 -m allox.runtime.bootstrap; '+script+' > /results/test.log 2>&1']
        with (guest_result/'console.log').open('w') as log:
            status = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=300, check=False)
        print(name, (guest_result/'test.log').read_text()[-4000:] if (guest_result/'test.log').exists()
              else (guest_result/'console.log').read_text()[-1500:], flush=True)
        if status.returncode:
            raise RuntimeError(name+' validation failed')


if __name__ == '__main__':
    main()
