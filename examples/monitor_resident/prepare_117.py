"""Owned source/build copy for the opt-in resident Observer prototype."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path('/data/hy/Allox/monitor-resident-20261115')
OLD = Path('/data/hy/Allox/monitor-e2e-20261114/observer')
online = set()
for part in Path('/sys/devices/system/cpu/online').read_text().strip().split(','):
    first, _, last = part.partition('-')
    online.update(range(int(first), int(last or first)+1))
selected = sorted(online)[-24:]
assert len(selected) == 24
os.sched_setaffinity(0, selected)
cpus = ','.join(map(str, selected))
ROOT.mkdir(exist_ok=True)
observer = ROOT / 'observer'
if not observer.exists():
    shutil.copytree(OLD, observer, symlinks=True, ignore=shutil.ignore_patterns('.git', 'target', '.output'))
subprocess.run([sys.executable, str(ROOT / 'source/examples/monitor_resident/apply_observer_patch.py'),
                str(observer)], check=True)
command = ['taskset', '-c', cpus, 'docker', 'run', '--rm', '--cpuset-cpus', cpus,
    '-v', str(observer)+':/observer', '-v', '/root/.rustup:/root/.rustup:ro',
    '-v', '/root/.cargo:/root/.cargo', '-v', '/data/act/tools/cargo:/data/act/tools/cargo:ro',
    '-v', '/data/hy/Allox/monitor-e2e-20261114/observer/.output/target-container:/target',
    '-e', 'CARGO_TARGET_DIR=/target', '-e', 'CARGO_BUILD_JOBS=24',
    '-e', 'PATH=/data/act/tools/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
    '-w', '/observer', '--entrypoint', '/bin/sh', 'allox-ebpf-test:20260907', '-ec',
    'apt-get update -qq; apt-get install -y --no-install-recommends pkg-config; '
    'cargo fmt --all; cargo build --release --locked --offline --bin deployment-observe --bin deployment-observe-verify']
print('BUILD_CPUS', cpus, flush=True)
subprocess.run(command, check=True)
binary = ROOT / 'bin'
binary.mkdir(exist_ok=True)
for name in ('deployment-observe', 'deployment-observe-verify'):
    shutil.copy2(Path('/data/hy/Allox/monitor-e2e-20261114/observer/.output/target-container/release') / name, binary / name)
    (binary / name).chmod(0o755)
print('RESIDENT_BUILD_OK', flush=True)
