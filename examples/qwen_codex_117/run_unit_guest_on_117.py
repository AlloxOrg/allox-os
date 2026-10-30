"""Exercise monitoring and the merged pidfd lifecycle test in a real Kata kernel."""
import subprocess
from pathlib import Path

ROOT = Path('/data/hy/Allox/agent-monitoring-main-20261030-v2')
NAME = 'allox-monitor-unit-guest-20261030'


def main():
    result = ROOT/'guest-unit-results'
    result.mkdir(exist_ok=True)
    pythonpath = ('/dev/shm/source/src:/dev/shm/source/plugins/agent-monitor/src:'
        '/dev/shm/source/plugins/process-tree/src:/dev/shm/source/plugins/session-share/src:'
        '/dev/shm/source/plugins/session-network/src:/dev/shm/source/plugins/system-effects/src:'
        '/work/.validation-venv/lib/python3.10/site-packages')
    command = ['docker', 'run', '--rm', '--name', NAME, '--runtime', 'kata-allox-ebpf',
        '--cgroupns', 'host', '--cap-add', 'SYS_ADMIN', '--shm-size', '256m',
        '--security-opt', 'seccomp=unconfined', '--security-opt', 'apparmor=unconfined',
        '-e', 'TMPDIR=/dev/shm', '-e', 'PYTHONPATH='+pythonpath, '-e', 'PYTHONDONTWRITEBYTECODE=1',
        '-v', f'{ROOT}:/work:ro', '-v', f'{result}:/results',
        'allox-ebpf-test:20260907', 'sh', '-ec',
        ('mkdir -p /dev/shm/source; cp -a /work/src /work/plugins /work/tests /work/examples '
        '/work/pyproject.toml /dev/shm/source/; cd /dev/shm/source; '
        'python3 -m pytest -q -s -p no:cacheprovider tests/test_agent_monitor.py '
        'tests/test_agent_monitor_bridge.py '
        'tests/test_process_tracking.py::ProcessTrackingTests::test_lifecycle_feature_runs_between_frozen_inner_gate_and_thaw '
        '> /results/pytest.log 2>&1')]
    try:
        with (result/'console.log').open('w') as output:
            status = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, timeout=300, check=False)
        log = result/'pytest.log'
        print(log.read_text()[-1800:] if log.exists() else 'Guest produced no pytest log', flush=True)
        return status.returncode
    finally:
        subprocess.run(['docker', 'rm', '-f', NAME], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)


if __name__ == '__main__':
    raise SystemExit(main())
