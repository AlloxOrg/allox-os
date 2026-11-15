"""Pinned official 6.12 Guest kernel: expanded Observer needs file_post_open.

Only an owned build directory/image is changed. The tarball hash is from
https://cdn.kernel.org/pub/linux/kernel/v6.x/sha256sums.asc (HTTPS, not a GPG verification claim).
"""
import hashlib
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path('/data/hy/Allox/monitor-e2e-20261114')
NAME = 'linux-6.12.107'
SHA256 = 'a5f8c5be3fde2d6d9ca14e9631642cf1f44487143f11059da730dcd5892e307a'
URL = 'https://cdn.kernel.org/pub/linux/kernel/v6.x/'+NAME+'.tar.xz'

if len(sys.argv) > 1 and sys.argv[1] == 'fetch':
    archive = Path('/build') / (NAME+'.tar.xz')
    if not archive.exists():
        with urllib.request.urlopen(URL, timeout=120) as source, archive.open('wb') as target:
            shutil.copyfileobj(source, target, length=1024*1024)
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == SHA256, 'tarball SHA256 mismatch'
    print('FETCHED', NAME, SHA256, flush=True)
    raise SystemExit(0)

# prepare.py has a CLI main body, so use the tiny affinity runner explicitly.
online = set()
for part in Path('/sys/devices/system/cpu/online').read_text().strip().split(','):
    first, _, last = part.partition('-')
    online.update(range(int(first), int(last or first) + 1))
selected = sorted(online)[-24:]
assert len(selected) == 24
cpus = ','.join(map(str, selected))


def run(args):
    subprocess.run(['taskset', '-c', cpus, *map(str, args)], check=True)


run(['docker', 'run', '--rm', '--cpuset-cpus', cpus, '-v', str(ROOT)+':/build',
     '-v', str(Path(__file__).resolve())+':/fetch.py:ro', '--entrypoint', 'python3',
     'allox-ebpf-test:20260907', '/fetch.py', 'fetch'])
archive = ROOT / (NAME+'.tar.xz')
assert hashlib.sha256(archive.read_bytes()).hexdigest() == SHA256
kernel = ROOT / NAME
if not kernel.exists():
    with tarfile.open(archive) as bundle:
        for entry in bundle.getmembers():
            assert entry.name == NAME or entry.name.startswith(NAME+'/')
            assert '..' not in Path(entry.name).parts and not entry.name.startswith('/')
    run(['tar', '-xJf', archive, '-C', ROOT])
if not (kernel / '.config').exists():
    shutil.copy2(ROOT / 'kernel.config', kernel / '.config')
assert 'file_post_open' in (kernel / 'include/linux/lsm_hook_defs.h').read_text()
if 'SKB_WITH_OVERHEAD(1024 * 8)' not in (kernel / 'include/linux/virtio_vsock.h').read_text():
    subprocess.run(['patch', '-p1', '-i', str(Path(__file__).with_name('vsock-legacy-rx.patch').resolve())],
                   cwd=kernel, check=True)
if 'trace_sched_process_exit(tsk, group_dead)' not in (kernel / 'kernel/exit.c').read_text():
    subprocess.run(['patch', '-p1', '-i', str(Path(__file__).with_name('sched-exit-group-dead.patch').resolve())],
                   cwd=kernel, check=True)
run([kernel / 'scripts/config', '--file', kernel / '.config', '--set-str', 'LOCALVERSION', '-allox-monitor-e2e',
     '--enable', 'MODULES', '--enable', 'BPF_JIT', '--enable', 'BPF_LSM', '--enable', 'DEBUG_INFO_BTF',
     '--enable', 'BTRFS_FS', '--enable', 'SECURITYFS', '--enable', 'SECURITY_PATH'])
print('BUILD_KERNEL', NAME, 'CPUS', cpus, flush=True)
with (ROOT / 'kernel612-build.log').open('w') as output:
    subprocess.run(['taskset', '-c', cpus, 'docker', 'run', '--rm', '--network', 'none',
        '--cpuset-cpus', cpus, '-v', str(kernel)+':/kernel', '-w', '/kernel', '--entrypoint', '/bin/sh',
        'allox-ebpf-test:20260907', '-ec', 'make olddefconfig; make -j24 bzImage'],
        stdout=output, stderr=subprocess.STDOUT, check=True)
shutil.copy2(kernel / '.config', ROOT / 'kernel612.config')
for required in ('CONFIG_BPF_LSM=y', 'CONFIG_DEBUG_INFO_BTF=y', 'CONFIG_BTRFS_FS=y'):
    assert required in (ROOT / 'kernel612.config').read_text().splitlines()
print('KERNEL_READY', kernel / 'arch/x86/boot/bzImage', flush=True)
