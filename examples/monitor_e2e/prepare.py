"""117-only isolated assets. Never mutate cached kernels/Observer or global Kata config."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path('/data/hy/Allox/monitor-e2e-20261114')
OLD_KERNEL = Path('/data/hy/Allox/joint-rollback-kata-btrfs-20261110/linux-6.1.62')
OLD_OBSERVER = Path('/data/hy/Allox/system-effects-test-20261030-v1/observer')
PYTHON = Path('/data/hy/Allox/agent-monitoring-main-20261030-v2/.validation-venv/bin/python')
IMAGE = 'allox-ebpf-test:20260907'


def affinity():
    online = set()
    for part in Path('/sys/devices/system/cpu/online').read_text().strip().split(','):
        first, _, last = part.partition('-')
        online.update(range(int(first), int(last or first) + 1))
    cpus = sorted(online)[-24:]
    assert len(cpus) == 24
    os.sched_setaffinity(0, cpus)
    return ','.join(map(str, cpus))


def run(args, name, **kwargs):
    cpus = affinity()
    print('BUILD', name, 'CPUS', cpus, flush=True)
    if args[:2] == ['docker', 'run']:
        args = args[:2] + ['--cpuset-cpus', cpus] + args[2:]
    with (ROOT / (name + '.log')).open('w') as stream:
        result = subprocess.run(['taskset', '-c', cpus, *map(str, args)], stdout=stream,
                                stderr=subprocess.STDOUT, **kwargs)
    print((ROOT / (name + '.log')).read_text(errors='replace')[-5000:], flush=True)
    if result.returncode:
        raise SystemExit(result.returncode)


mode = sys.argv[1]
affinity()
if mode == 'kernel':
    kernel = ROOT / 'kernel'
    if not kernel.exists():
        # Full copy, NOT hardlinks: generated files/config never affect cached assets.
        shutil.copytree(OLD_KERNEL, kernel, symlinks=True)
    enable = ['MODULES', 'BPF', 'BPF_SYSCALL', 'BPF_JIT', 'BPF_JIT_ALWAYS_ON', 'BPF_EVENTS',
              'BPF_LSM', 'CGROUP_BPF', 'SECURITY', 'SECURITYFS', 'SECURITY_PATH',
              'FTRACE', 'FUNCTION_TRACER', 'DYNAMIC_FTRACE', 'KPROBES', 'KPROBE_EVENTS',
              'FTRACE_SYSCALLS', 'PERF_EVENTS', 'DEBUG_KERNEL', 'DEBUG_INFO',
              'DEBUG_INFO_DWARF4', 'DEBUG_INFO_BTF', 'BTRFS_FS', 'BTRFS_FS_POSIX_ACL']
    args = [str(kernel / 'scripts/config'), '--file', str(kernel / '.config'),
            '--disable', 'DEBUG_INFO_NONE', '--disable', 'DEBUG_INFO_REDUCED',
            '--disable', 'DEBUG_INFO_SPLIT', '--disable', 'DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT',
            '--disable', 'DEBUG_INFO_DWARF5', '--disable', 'SECURITY_SELINUX',
            '--set-str', 'LOCALVERSION', '-allox-monitor-e2e', '--set-str', 'LSM', 'capability,bpf']
    for value in enable:
        args += ['--enable', value]
    run(args, 'kernel-config')
    run(['docker', 'run', '--rm', '--network', 'none', '-v', str(kernel)+':/kernel',
         '-w', '/kernel', '--entrypoint', '/bin/sh', IMAGE, '-ec',
         'make olddefconfig; make -j24 bzImage'], 'kernel-build')
    shutil.copy2(kernel / '.config', ROOT / 'kernel.config')
    for required in ('CONFIG_BPF_LSM=y', 'CONFIG_DEBUG_INFO_BTF=y', 'CONFIG_BTRFS_FS=y'):
        assert required in (ROOT / 'kernel.config').read_text().splitlines(), required
elif mode in ('observer', 'observer-bpf'):
    observer = ROOT / 'observer'
    if not observer.exists():
        shutil.copytree(OLD_OBSERVER, observer, ignore=shutil.ignore_patterns('.output', '.git'))
        # Mechanical newline normalization of the copied source only.
        for path in observer.rglob('*'):
            if path.is_file() and (path.suffix in {'.rs', '.h', '.c', '.toml', '.lock', '.sh'}
                                   or path.name == 'Makefile'):
                raw = path.read_bytes()
                if b'\r' in raw:
                    path.write_bytes(raw.replace(b'\r', b''))
    bpf_source = observer / 'bpf/system_effects.bpf.c'
    if 'static __always_inline long fill_socket_state(' not in bpf_source.read_text():
        run(['patch', '-p1', '-i', Path(__file__).with_name('observer-stack.patch').resolve()],
            'observer-stack-patch', cwd=observer)
    if 'effect_socket_state_scratch SEC' not in bpf_source.read_text():
        run(['patch', '-p1', '-i', Path(__file__).with_name('observer-scratch.patch').resolve()],
            'observer-scratch-patch', cwd=observer)
    output = observer / '.output'
    output.mkdir(exist_ok=True)
    kernel = OLD_KERNEL
    compile_bpf = ('clang-19 -g -O2 -target bpf -D__TARGET_ARCH_x86 -Wall -Wextra -Werror '
        '-I/kernel/tools/include/uapi -I/kernel/tools/lib -I.output/bpf-include '
        '-I/usr/include/x86_64-linux-gnu ')
    run(['docker', 'run', '--rm', '--network', 'none', '-v', str(observer)+':/observer',
         '-v', str(kernel)+':/kernel:ro', '-w', '/observer', '--entrypoint', '/bin/sh', IMAGE,
         '-ec', 'mkdir -p .output/bpf-include; python3 /kernel/scripts/bpf_doc.py --header '
         '> .output/bpf-include/bpf_helper_defs.h; '
         + compile_bpf+'-c bpf/system_effects.bpf.c -o .output/system_effects.bpf.o; '
         + compile_bpf+'-c bpf/process_lifecycle.bpf.c -o .output/process_lifecycle.bpf.o'], 'observer-bpf')
    # The shared host's glibc 2.17 lacks statx. Build for the actual Debian Guest,
    # using cached Rust crates, rather than patching semantics or host libraries.
    if mode == 'observer':
        run(['docker', 'run', '--rm', '-v', str(observer)+':/observer',
         '-v', '/root/.rustup:/root/.rustup:ro', '-v', '/root/.cargo:/root/.cargo',
         '-v', '/data/act/tools/cargo:/data/act/tools/cargo:ro',
         '-e', 'CARGO_TARGET_DIR=/observer/.output/target-container', '-e', 'CARGO_BUILD_JOBS=24',
         '-e', 'PATH=/data/act/tools/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
         '-w', '/observer', '--entrypoint', '/bin/sh', IMAGE, '-ec',
         'apt-get update -qq; apt-get install -y --no-install-recommends pkg-config; '
         'cargo build --release --locked --offline --bin deployment-observe --bin deployment-observe-verify'],
        'observer-rust-container')
    install = ROOT / 'bin'
    install.mkdir(exist_ok=True, mode=0o755)
    for name in ('deployment-observe', 'deployment-observe-verify'):
        shutil.copy2(output / 'target-container/release' / name, install / name)
        (install / name).chmod(0o755)
    for name in ('system_effects.bpf.o', 'process_lifecycle.bpf.o'):
        shutil.copy2(output / name, install / name)
        (install / name).chmod(0o644)
    (ROOT / 'observer-artifacts.json').write_text(json.dumps({
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in install.iterdir()}, indent=2))
elif mode == 'wheels':
    run([PYTHON, '-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation', '--wheel-dir', ROOT / 'wheels',
         '.', './plugins/agent-monitor', './plugins/system-effects', './plugins/trace-correlation'],
        'wheels', cwd=ROOT / 'source')
else:
    raise SystemExit('expected kernel / observer / observer-bpf / wheels')
