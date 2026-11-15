"""Owned copy of cached Kata 3.10 sources; no global runtime/image mutation."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path('/data/hy/Allox/monitor-e2e-20261114')
OLD = Path('/data/hy/Allox/kata-agent-3.10-device-fix/kata-containers')
source = ROOT / 'kata-agent-source'
online = set()
for part in Path('/sys/devices/system/cpu/online').read_text().strip().split(','):
    first, _, last = part.partition('-')
    online.update(range(int(first), int(last or first) + 1))
selected = sorted(online)[-24:]
assert len(selected) == 24
os.sched_setaffinity(0, selected)
cpus = ','.join(map(str, selected))
print('AGENT_PREPARE', cpus, flush=True)
if not source.exists():
    shutil.copytree(OLD, source, symlinks=True, ignore=shutil.ignore_patterns('.git'))
for name in ('agent-registry', 'agent-git', 'agent-output'):
    (ROOT / name).mkdir(exist_ok=True)
mode = sys.argv[1] if len(sys.argv) > 1 else 'fetch'
if mode == 'build':
    crate = source / 'e2e-ttrpc'
    if not crate.exists():
        cached, = (ROOT / 'agent-registry/src').glob('*/ttrpc-0.8.1')
        shutil.copytree(cached, crate)
        subprocess.run(['patch', '-p1', '-i', str(Path(__file__).with_name('ttrpc-vsock.patch'))], cwd=crate, check=True)
        subprocess.run(['patch', '-p1', '-i', str(Path(__file__).with_name('agent-workspace.patch'))], cwd=source, check=True)
script = ('cargo fetch --locked; cargo locate-project --workspace --message-format plain' if mode == 'fetch' else
          'cargo update -p ttrpc --offline; '
          'DESTDIR=/out AGENT_POLICY=yes PULL_TYPE=guest-pull make -j24; '
          'DESTDIR=/out AGENT_POLICY=yes PULL_TYPE=guest-pull make install')
command = ['taskset', '-c', cpus, 'docker', 'run', '--rm', '--cpuset-cpus', cpus,
    '-v', str(source)+':/src', '-v', str(ROOT / 'agent-registry')+':/opt/cargo/registry',
    '-v', str(ROOT / 'agent-git')+':/opt/cargo/git', '-w', '/src/src/agent',
    '-v', str(ROOT / 'agent-output')+':/out',
    '-e', 'LIBSECCOMP_VERSION=2.5.5', '-e', 'LIBSECCOMP_URL=https://github.com/seccomp/libseccomp',
    '-e', 'GPERF_VERSION=3.1', '-e', 'GPERF_URL=https://ftp.gnu.org/gnu/gperf',
    'allox-kata-agent-builder:3.10-device-fix', 'bash', '-ec',
    script]
subprocess.run(command, check=True)
