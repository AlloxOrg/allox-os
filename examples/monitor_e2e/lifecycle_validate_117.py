"""Lightweight checks for the diagnostic scripts, on 117 with fresh affinity."""
import os
import subprocess
from pathlib import Path

online = set()
for part in Path('/sys/devices/system/cpu/online').read_text().strip().split(','):
    first, _, last = part.partition('-')
    online.update(range(int(first), int(last or first)+1))
selected = sorted(online)[-24:]
assert len(selected) == 24
os.sched_setaffinity(0, selected)
cpus = ','.join(map(str, selected))
subprocess.run(['taskset', '-c', cpus,
    '/data/hy/Allox/agent-monitoring-main-20261030-v2/.validation-venv/bin/ruff',
    'check', '--select', 'E4,E7,E9,F', 'examples/monitor_e2e'],
    cwd='/data/hy/Allox/monitor-e2e-20261114/source', check=True)
print('HOST_CPUS', cpus)
