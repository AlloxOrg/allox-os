"""Reverify all sealed runs after the Guest/worker exit, using the native CLI."""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path('/data/hy/Allox/monitor-resident-20261115')
OLD = Path('/data/hy/Allox/monitor-e2e-20261114')


def verify():
    result = Path('/results')
    report = json.loads((result / 'report.json').read_text())
    rows = []
    for case in report['cases']:
        if case['mode'] == 'off':
            continue
        assert case['passed']
        audit, = (result / case['label'] / 'audit').glob('*/system-effects')
        process = subprocess.run(['/opt/resident-observer/deployment-observe-verify',
            '--artifacts', str(audit), '--expectation',
            str(audit.parent / 'system-effects-verification-expectation.json')],
            check=True, capture_output=True, text=True, timeout=60)
        verified = json.loads(process.stdout)
        assert verified['verified'] is True, verified
        rows.append({'case': case['label'], 'verification': verified})
    assert len(rows) == 20
    evidence = {'passed': True, 'native_reverified_runs': len(rows),
                'after_guest_exit': True, 'rows': rows}
    # No write access to sealed evidence or its siblings.
    print(json.dumps(evidence), flush=True)


def main():
    if sys.argv[1] == '--inside':
        verify()
        return
    attempt = Path(sys.argv[1]).resolve(strict=True)
    assert attempt.parent == OLD and attempt.name.startswith('attempt-')
    online = set()
    for part in Path('/sys/devices/system/cpu/online').read_text().strip().split(','):
        first, _, last = part.partition('-')
        online.update(range(int(first), int(last or first)+1))
    selected = sorted(online)[-24:]
    assert len(selected) == 24
    os.sched_setaffinity(0, selected)
    cpus = ','.join(map(str, selected))
    args = ['taskset', '-c', cpus, 'docker', 'run', '--rm', '--cpuset-cpus', cpus,
        '-v', str(attempt)+':/results:ro', '-v', str(ROOT / 'bin')+':/opt/resident-observer:ro',
        '-v', str(OLD / 'bin')+':/opt/observer:ro',
        '-v', str(Path(__file__).resolve())+':/check.py:ro',
        '--entrypoint', 'python3', 'allox-ebpf-test:20260907', '/check.py', '--inside']
    process = subprocess.run(args, check=True, capture_output=True, text=True, timeout=300)
    evidence = json.loads(process.stdout)
    evidence['cpus'] = cpus
    (attempt / 'postverification.json').write_text(json.dumps(evidence, indent=2))
    print(json.dumps({key: value for key, value in evidence.items() if key != 'rows'}), flush=True)


if __name__ == '__main__':
    main()
