"""Run identity/profiling checks in an owned disposable Kata Guest."""
import json
import subprocess
from pathlib import Path

ROOT = Path('/data/hy/Allox/agent-monitoring-main-20261030-v2')
NAME = 'allox-monitor-contract-20261030'


def main():
    result = ROOT/'contract-results'
    result.mkdir(exist_ok=True)
    command = ['docker', 'run', '--rm', '--name', NAME, '--runtime', 'kata-allox-ebpf',
        '--cgroupns', 'host', '--cap-add', 'SYS_ADMIN', '--shm-size', '256m',
        '--security-opt', 'seccomp=unconfined', '--security-opt', 'apparmor=unconfined',
        '-e', 'TMPDIR=/dev/shm', '-e', 'PYTHONPATH=', '-e', 'PYTHONDONTWRITEBYTECODE=1',
        '-v', f'{ROOT}/dist:/dist/core:ro',
        '-v', f'{ROOT}/plugins/agent-monitor/dist:/dist/monitor:ro',
        '-v', f'{ROOT}/examples/qwen_codex_117/guest_contract.py:/opt/guest_contract.py:ro',
        '-v', f'{result}:/results', 'allox-ebpf-test:20260907', 'sh', '-ec',
        ('python3 -m pip install -q --no-deps /dist/core/*.whl /dist/monitor/*.whl; '
        'python3 -m allox.runtime.bootstrap; python3 /opt/guest_contract.py')]
    try:
        with (result/'console.log').open('w') as output:
            status = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, timeout=300, check=False)
        if (result/'contract-report.json').exists():
            report = json.loads((result/'contract-report.json').read_text())
            print(json.dumps({'passed': report['passed'], 'checks': report.get('checks'),
                              'error': report.get('error')}), flush=True)
        else:
            print((result/'console.log').read_text()[-1000:], flush=True)
        return status.returncode
    finally:
        subprocess.run(['docker', 'rm', '-f', NAME], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)


if __name__ == '__main__':
    raise SystemExit(main())
