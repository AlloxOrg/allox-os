"""Owned Kata Guest + memory-only Qwen relay; run on 117 via SSH stdin."""
import importlib.util
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path('/data/hy/Allox/monitor-e2e-20261114')
SOURCE = ROOT / 'source'
IMAGE = 'allox-ebpf-test:20260907'


def affinity():
    online = set()
    for part in Path('/sys/devices/system/cpu/online').read_text().strip().split(','):
        first, _, last = part.partition('-')
        online.update(range(int(first), int(last or first) + 1))
    cpus = sorted(online)[-24:]
    assert len(cpus) == 24
    os.sched_setaffinity(0, cpus)
    return cpus


def output(*args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def pin(container, cpus):
    found = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            cmd = (proc / 'cmdline').read_bytes()
        except OSError:
            continue
        exe = cmd.split(b'\0', 1)[0]
        if container.encode() in cmd and (b'qemu-system' in exe or b'containerd-shim-kata' in exe
                                          or b'virtiofsd' in exe):
            output('taskset', '-apc', cpus, proc.name)
            found.append({'pid': int(proc.name), 'qemu': b'qemu-system' in exe,
                          'virtiofsd': b'virtiofsd' in exe})
    assert any(p['qemu'] for p in found)
    return found


def main():
    if Path.cwd().resolve() != ROOT:
        raise SystemExit('run only in the owned 117 experiment directory')
    local_cpu = '--local-cpu-llm' in sys.argv
    if local_cpu:
        address = output('docker', 'inspect', '--format',
            '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}', 'myhc-ai-test-local-llm-1').split()[0]
        config = {'base_url': 'http://'+address+':8083/v1', 'key': 'local-fixture-no-auth',
                  'model': 'Qwen3.5-4B-CPU-Test', 'auth_header': 'Authorization', 'timeout': 300,
                  'compact_system_prompt': 'You are a coding agent in /workspace. Use the advertised shell tools to fulfill the user request. Execute real tools, do not describe imaginary calls. Do not inspect credentials, environment, configuration, session logs, or unrelated files. Follow the user task exactly.',
                  'max_output_tokens': 512}
    else:
        config = json.load(sys.stdin)
    cpus = affinity()
    selected = ','.join(map(str, cpus))
    result = ROOT / ('attempt-'+secrets.token_hex(5))
    result.mkdir(mode=0o700)
    print('RESULT_DIR', result, 'HOST_CPUS', selected, flush=True)
    wrapper = result / 'qemu-console'
    shutil.copy2(Path(__file__).with_name('qemu_console.py'), wrapper)
    wrapper.chmod(0o755)
    text = Path('/data/hy/Allox/kata-ebpf-runtime/configuration.toml').read_text()
    for name, value in (('path', '"'+str(wrapper)+'"'),
                        ('kernel', '"'+str(ROOT / 'linux-6.12.107/arch/x86/boot/bzImage')+'"'),
                        ('image', '"'+str(ROOT / 'kata-ubuntu-3.10-monitor.image')+'"'),
                        ('shared_fs', '"virtio-9p"'),
                        ('block_device_aio', '"threads"'), ('default_memory', '4096'), ('default_vcpus', '4')):
        text, count = re.subn(r'^'+name+r'\s*=.*$', name+' = '+value, text, flags=re.M)
        assert count == 1, name
    (result / 'kata.toml').write_text(text)
    spec = importlib.util.spec_from_file_location('compat_gateway', SOURCE / 'examples/qwen_codex_117/run_lab_on_117.py')
    compat = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(compat)
    network = json.loads(output('docker', 'network', 'inspect', 'bridge'))[0]
    address = network['IPAM']['Config'][0]['Gateway']
    gateway = ThreadingHTTPServer((address, 0), compat.Gateway)
    gateway.config, gateway.gateway_token, gateway.requests = config, secrets.token_urlsafe(32), []
    threading.Thread(target=gateway.serve_forever, daemon=True).start()
    container = None
    metadata = {'passed': False, 'source_base': 'd81c7ac', 'runtime': 'kata-allox-ebpf',
                'model': config['model'], 'host_cpus': cpus, 'provider_key_in_guest': False,
                'provider_kind': 'cached-local-cpu' if local_cpu else 'lab-key-gateway',
                'prompt_mode': 'test-only compact system prompt for 4096-token CPU fixture' if local_cpu else 'original Codex system prompt',
                'kernel': str(ROOT / 'linux-6.12.107/arch/x86/boot/bzImage'),
                'guest_image': str(ROOT / 'kata-ubuntu-3.10-monitor.image'),
                'shared_fs': 'virtio-9p; owned per-Guest override',
                'scope': 'real BPF + real Agent; no snapshot/resume or power-loss claim'}
    try:
        command = ['docker', 'create', '--pull', 'never', '--name', 'allox-e2e-'+result.name,
            '--runtime', 'kata-allox-ebpf', '--annotation', 'io.katacontainers.config_path='+str(result / 'kata.toml'),
            '--cpuset-cpus', selected, '--cgroupns', 'host', '--shm-size', '2g',
            '--cap-add', 'SYS_ADMIN', '--cap-add', 'SYS_PTRACE', '--cap-add', 'MKNOD',
            '--cap-add', 'SYS_RESOURCE', '--cap-add', 'NET_ADMIN', '--ulimit', 'memlock=-1:-1',
            '--security-opt', 'seccomp=unconfined', '--security-opt', 'apparmor=unconfined',
            '--device-cgroup-rule', 'b 7:* rwm', '--device-cgroup-rule', 'c 10:237 rwm',
            '-e', 'PYTHONPATH=', '-e', 'PYTHONDONTWRITEBYTECODE=1',
            '-e', 'ALLOX_QWEN_MODEL='+config['model'],
            '-e', 'ALLOX_E2E_LOCAL_CPU='+('1' if local_cpu else '0'),
            '-e', 'ALLOX_E2E_BENCHMARK_ONLY='+('1' if '--benchmark-only' in sys.argv else '0'),
            '-e', 'ALLOX_QWEN_UPSTREAM=http://'+address+':'+str(gateway.server_port)+'/v1',
            '-e', 'ALLOX_GATEWAY_TOKEN='+gateway.gateway_token]
        for source, target, ro in (
            (SOURCE, '/allox', True), (ROOT / 'wheels', '/dist', True), (ROOT / 'bin', '/opt/observer', True),
            (Path('/data/hy/Allox/joint-rollback-kata-btrfs-20261110'), '/fixtures', True),
            (Path('/data/act/bin/qwen-codex-exec'), '/usr/local/bin/qwen-codex-exec', True),
            (Path('/opt/openssl-1.1.1q/libssl.so.1.1'), '/usr/local/lib/qwen-test/libssl.so.1.1', True),
            (Path('/opt/openssl-1.1.1q/libcrypto.so.1.1'), '/usr/local/lib/qwen-test/libcrypto.so.1.1', True),
            (result, '/results', False)):
            assert source.exists(), str(source)
            command += ['-v', str(source)+':'+target+(':ro' if ro else '')]
        command += ['--entrypoint', '/bin/sh', IMAGE, '-ec',
            'while [ ! -f /results/start ]; do sleep .05; done; '
            'export PATH=/fixtures/bin:$PATH; export LD_LIBRARY_PATH=/fixtures/bin/lib; '
            'python3 -m pip install -q --no-deps /dist/*.whl; '
            'python3 -m allox.runtime.bootstrap; '
            'python3 -u /allox/examples/monitor_e2e/guest.py']
        container = output(*command, timeout=60)
        starter = subprocess.Popen(['docker', 'start', container], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        boot_deadline = time.monotonic()+120
        while starter.poll() is None:
            try:
                metadata['boot_pids'] = pin(container, selected)
                break
            except AssertionError:
                if time.monotonic() >= boot_deadline:
                    break
                time.sleep(.2)
        try:
            stdout, stderr = starter.communicate(timeout=150)
        except subprocess.TimeoutExpired:
            starter.terminate()
            starter.communicate(timeout=10)
            raise RuntimeError('owned Kata start exceeded deadline') from None
        if starter.returncode:
            logs = []
            for process in metadata.get('boot_pids', []):
                read = subprocess.run(['journalctl', '--no-pager', '-o', 'cat', '_PID='+str(process['pid'])],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
                logs.append(read.stdout.decode('utf-8', 'replace'))
            diagnostics = '\n'.join(logs).replace(config['key'], '[REDACTED]').replace(gateway.gateway_token, '[REDACTED]')
            (result / 'boot-journal.log').write_text(diagnostics)
            (result / 'start-error.log').write_text(stderr.decode('utf-8', 'replace')
                .replace(config['key'], '[REDACTED]').replace(gateway.gateway_token, '[REDACTED]'))
            print('BOOT_FAILURE', result / 'boot-journal.log', flush=True)
            raise RuntimeError('Kata Guest failed to reach agent; inspect owned boot journal')
        # Re-read online IDs before releasing this actual workload.
        cpus = affinity()
        selected = ','.join(map(str, cpus))
        metadata['vmm_affinity'] = pin(container, selected)
        (result / 'start').touch()
        print('KATA_STARTED', container, flush=True)
        with (result / 'console.log').open('w') as stream:
            follower = subprocess.Popen(['docker', 'logs', '-f', container], stdout=stream, stderr=subprocess.STDOUT)
            try:
                status = int(output('docker', 'wait', container, timeout=1500))
            finally:
                follower.wait(timeout=20)
        metadata['container_exit_code'] = status
        if (result / 'report.json').exists():
            report = json.loads((result / 'report.json').read_text())
            metadata['passed'] = bool(report['passed']) and status == 0
            print('REPORT', json.dumps({'passed': metadata['passed'], 'error': report.get('error'),
                  'cases': [{'label': c['label'], 'passed': c['passed'], 'error': c.get('error')}
                            for c in report.get('cases', [])]}), flush=True)
        else:
            print('GUEST_EARLY_FAILURE', (result / 'console.log').read_text(errors='replace')[-3000:], flush=True)
    except Exception as exc:
        # Do not let CalledProcessError print docker arguments (relay token).
        metadata['host_error_type'] = type(exc).__name__
        print('HOST_FAILURE', type(exc).__name__, flush=True)
    finally:
        if container:
            subprocess.run(['docker', 'rm', '-f', container], timeout=60, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False)
        gateway.shutdown()
        gateway.server_close()
        metadata.update(removed_owned_container=True, gateway_requests=gateway.requests)
        text = json.dumps(metadata, indent=2).replace(config['key'], '[REDACTED]')
        (result / 'runtime.json').write_text(text)
        print('FINISHED', result, 'PASS', metadata['passed'], flush=True)
    return 0 if metadata['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
