"""Prepare syscall timing tool in a disposable build container, not the host."""
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
Path('/data/hy/Allox/monitor-e2e-20261114/diagnostic-bin').mkdir(exist_ok=True)
subprocess.run(['taskset', '-c', cpus, 'docker', 'run', '--rm', '--cpuset-cpus', cpus,
    '-v', '/data/hy/Allox/monitor-e2e-20261114/diagnostic-bin:/out', 'allox-ebpf-test:20260907',
    'bash', '-ec', 'apt-get update -qq; apt-get install -y -qq --no-install-recommends strace; '
    'cp /usr/bin/strace /out/strace; mkdir -p /out/lib; '
    'for lib in libunwind.so.8 libunwind-x86_64.so.8 libunwind-ptrace.so.0 liblzma.so.5; do '
    'cp -L /usr/lib/x86_64-linux-gnu/$lib /out/lib/; done; '
    'ldd /usr/bin/strace; strace --version | head -1'], check=True)
