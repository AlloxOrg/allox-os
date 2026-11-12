"""117-only validation; never run a workload without fresh last-24 affinity."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else '/data/hy/Allox/monitor-async-20261112')
BIN = Path('/data/hy/Allox/agent-monitoring-main-20261030-v2/.validation-venv/bin')
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
os.environ.pop('TMPDIR', None)


def run(args):
    online = set()
    for part in Path('/sys/devices/system/cpu/online').read_text().strip().split(','):
        first, _, last = part.partition('-')
        online.update(range(int(first), int(last or first) + 1))
    selected = sorted(online)[-24:]
    assert len(selected) == 24
    cpus = ','.join(map(str, selected))
    print('AFFINITY', cpus, 'COMMAND', args, flush=True)
    if args[:2] == ['docker', 'run']:
        args = args[:2] + ['--cpuset-cpus', cpus] + args[2:]
    subprocess.run(['taskset', '-c', cpus, *args], cwd=ROOT / 'source', check=True)


run([str(BIN / 'ruff'), 'check', '--select', 'E4,E7,E9,F,I', 'src',
     'plugins/agent-monitor', 'plugins/recovery-checks', 'plugins/trace-correlation',
     'tests/test_agent_monitor_queued.py', 'tests/test_agent_monitor_actions.py'])
run(['docker', 'run', '--rm', '--network', 'none', '-v', str(ROOT / 'source') + ':/work:ro',
     '-v', str(BIN.parent / 'lib/python3.10/site-packages/pyright') + ':/checker:ro', '-w', '/work',
     'node:22-alpine', 'node', '/checker/dist/dist/pyright.js',
     '--typeshedpath', '/checker/dist/dist/typeshed-fallback'])
run([str(BIN / 'python'), '-m', 'pytest', '-q', '--maxfail=5', 'tests', 'plugins/system-effects/tests'])
run([str(BIN / 'python'), '-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation', '--wheel-dir',
     str(ROOT / 'wheels'), '.', './plugins/agent-monitor'])
