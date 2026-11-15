"""Inventory the owned test image with fresh last-24 affinity, no host changes."""
import os
import subprocess
from pathlib import Path

online = set()
for part in Path('/sys/devices/system/cpu/online').read_text().strip().split(','):
    first, _, last = part.partition('-')
    online.update(range(int(first), int(last or first) + 1))
selected = sorted(online)[-24:]
assert len(selected) == 24
os.sched_setaffinity(0, selected)
cpus = ','.join(map(str, selected))
print('CPU', cpus, flush=True)
subprocess.run(['taskset', '-c', cpus, 'docker', 'run', '--rm', '--network', 'none',
    '--cpuset-cpus', cpus, '--entrypoint', '/bin/sh', 'allox-ebpf-test:20260907', '-c',
    'for x in gcc make clang pahole bpftool pkg-config bison flex; do command -v "$x" || true; done; '
    'ls /boot; ls /usr/include/libelf.h; ls /usr/bin/clang*; ls /usr/bin/pahole*; '
    'ls /usr/sbin/bpftool*; cat /etc/os-release'], check=True)
