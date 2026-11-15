"""Native/transport correctness checks; run only on 117, not during timings."""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path('/data/hy/Allox/monitor-resident-20261115')
BIN = Path('/data/hy/Allox/agent-monitoring-main-20261030-v2/.validation-venv/bin')


def run(args):
    online = set()
    for part in Path('/sys/devices/system/cpu/online').read_text().strip().split(','):
        first, _, last = part.partition('-')
        online.update(range(int(first), int(last or first)+1))
    selected = sorted(online)[-24:]
    assert len(selected) == 24
    cpus = ','.join(map(str, selected))
    os.sched_setaffinity(0, selected)
    print('CPUS', cpus, 'CHECK', args, flush=True)
    if args[:2] == ['docker', 'run']:
        args = args[:2] + ['--cpuset-cpus', cpus] + args[2:]
    subprocess.run(['taskset', '-c', cpus, *map(str, args)], cwd=ROOT / 'source', check=True)


run([BIN / 'ruff', 'check', '--select', 'E4,E7,E9,F,I', 'plugins/system-effects'])
run([BIN / 'ruff', 'check', '--select', 'E4,E7,E9,F', 'examples/monitor_resident'])
run(['docker', 'run', '--rm', '--network', 'none',
     '-v', str(ROOT / 'source')+':/work:ro',
     '-v', str(BIN.parent / 'lib/python3.10/site-packages/pyright')+':/checker:ro',
     '-w', '/work', 'node:22-alpine', 'node', '/checker/dist/dist/pyright.js',
     '--typeshedpath', '/checker/dist/dist/typeshed-fallback'])
manifest = json.loads((ROOT / 'source/plugins/system-effects/observer/resident-v1.json').read_text())
with tempfile.TemporaryDirectory(prefix='resident-patch-', dir=ROOT) as scratch:
    for item in manifest['files']:
        destination = Path(scratch) / item['path']
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path('/data/hy/Allox/monitor-e2e-20261114/observer') / item['path'], destination)
    helper = ROOT / 'source/examples/monitor_resident/apply_observer_patch.py'
    run([BIN / 'python', helper, scratch])
    run([BIN / 'python', helper, scratch])  # Idempotent only for exact result hashes.
run([BIN / 'python', '-m', 'pytest', '-q', '--maxfail=5', 'tests', 'plugins/system-effects/tests'])
