"""117-only wheel build in the independent resident experiment directory."""
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

ROOT = Path('/data/hy/Allox/monitor-resident-20261115')
source = ROOT / 'source'
if not source.exists():
    shutil.copytree('/data/hy/Allox/monitor-e2e-20261114/source', source,
                    ignore=shutil.ignore_patterns('__pycache__', '.git'))
with tarfile.open(ROOT / 'python.tar.gz') as archive:
    for entry in archive.getmembers():
        assert not Path(entry.name).is_absolute() and '..' not in Path(entry.name).parts
    archive.extractall(source)
online = set()
for part in Path('/sys/devices/system/cpu/online').read_text().strip().split(','):
    first, _, last = part.partition('-')
    online.update(range(int(first), int(last or first)+1))
selected = sorted(online)[-24:]
assert len(selected) == 24
os.sched_setaffinity(0, selected)
cpus = ','.join(map(str, selected))
subprocess.run(['taskset', '-c', cpus,
    '/data/hy/Allox/agent-monitoring-main-20261030-v2/.validation-venv/bin/python',
    '-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation', '-w', str(ROOT / 'wheels'),
    '.', './plugins/agent-monitor', './plugins/system-effects', './plugins/trace-correlation'],
    cwd=source, check=True)
print('PYTHON_BUILD_OK', cpus, flush=True)
