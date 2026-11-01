"""117-only validation and builds; no credentials, refresh CPU affinity for each command."""
import json
import subprocess
from pathlib import Path

BASE = Path('/data/hy/Allox/monitoring-background-20261101-v1')
ROOT = BASE/'source'
DEPS = Path('/data/hy/Allox/agent-monitoring-main-20261030-v2/.validation-venv')


def cpus():
    ids = []
    for part in Path('/sys/devices/system/cpu/online').read_text().strip().split(','):
        bounds = [int(x) for x in part.split('-')]
        ids.extend(range(bounds[0], bounds[-1]+1))
    return ','.join(map(str, sorted(set(ids))[-24:]))


def run(argv, log, cwd=ROOT):
    affinity = cpus()
    with log.open('w') as output:
        status = subprocess.run(['taskset', '-c', affinity, *map(str, argv)], cwd=cwd,
            stdout=output, stderr=subprocess.STDOUT, timeout=600, check=False)
    print(json.dumps({'log': str(log), 'cpu_ids': affinity, 'exit_code': status.returncode}), flush=True)
    print(log.read_text()[-4000:], flush=True)
    if status.returncode:
        raise RuntimeError('validation failed: '+log.name)


def main():
    results = BASE/'validation'
    results.mkdir(exist_ok=True)
    run([DEPS/'bin/ruff', 'check', '--select', 'E9,F,I', 'plugins/agent-monitor/src',
         'tests/test_agent_monitor_background.py'], results/'ruff.log')
    run([DEPS/'bin/python', '-m', 'pytest', '-q', 'tests', 'plugins/system-effects/tests'],
        results/'host-pytest.log')
    for path, label in [(ROOT, 'core'), (ROOT/'plugins/agent-monitor', 'plugin')]:
        run([DEPS/'bin/python', '-m', 'build', '--wheel', '--no-isolation'], results/(label+'-build.log'), path)
    name = 'allox-monitor-bg-validation-20261101'
    guest_results = results/'guest'
    guest_results.mkdir(exist_ok=True)
    command = ['docker', 'run', '--rm', '--name', name, '--runtime', 'kata-allox-ebpf',
        '--cpuset-cpus', cpus(), '--cgroupns', 'host', '--cap-add', 'SYS_ADMIN',
        '--shm-size', '256m', '--security-opt', 'seccomp=unconfined',
        '--security-opt', 'apparmor=unconfined', '-e', 'TMPDIR=/dev/shm',
        '-e', 'PYTHONDONTWRITEBYTECODE=1',
        '-e', 'PYTHONPATH=/dev/shm/source/src:/dev/shm/source/plugins/agent-monitor/src:'
        '/dev/shm/source/plugins/process-tree/src:/dev/shm/source/plugins/session-share/src:'
        '/dev/shm/source/plugins/session-network/src:/deps/lib/python3.10/site-packages',
        '-v', f'{ROOT}:/work:ro', '-v', f'{DEPS}:/deps:ro', '-v', f'{guest_results}:/results',
        'allox-ebpf-test:20260907', 'sh', '-ec',
        'mkdir -p /dev/shm/source; cp -a /work/src /work/plugins /work/tests /work/examples '
        '/work/pyproject.toml /dev/shm/source/; cd /dev/shm/source; '
        'python3 -m allox.runtime.bootstrap; python3 -m pytest -q -p no:cacheprovider '
        'tests/test_agent_monitor.py tests/test_agent_monitor_batch.py '
        'tests/test_agent_monitor_parity.py tests/test_agent_monitor_background.py '
        '> /results/pytest.log 2>&1; python3 examples/monitoring_process_smoke.py > /results/process.log 2>&1']
    try:
        run(command, results/'guest-console.log')
        print((guest_results/'pytest.log').read_text()[-2000:], flush=True)
        print((guest_results/'process.log').read_text()[-2000:], flush=True)
    finally:
        subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
