"""Package only an owned copy of the cached 9p-compatible Guest image."""
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path('/data/hy/Allox/monitor-e2e-20261114')
OLD = Path('/data/hy/Allox/kata-agent-3.10-device-fix')
online = set()
for part in Path('/sys/devices/system/cpu/online').read_text().strip().split(','):
    first, _, last = part.partition('-')
    online.update(range(int(first), int(last or first) + 1))
selected = sorted(online)[-24:]
assert len(selected) == 24
os.sched_setaffinity(0, selected)
cpus = ','.join(map(str, selected))
image = ROOT / 'kata-ubuntu-3.10-monitor.image'
rootfs = ROOT / 'monitor-rootfs.ext4'
agent = ROOT / 'agent-output/usr/bin/kata-agent'
assert agent.is_file()
assert not image.exists() and not rootfs.exists(), 'do not overwrite packaged images'
shutil.copy2(OLD / 'kata-ubuntu-3.10-device-fix.image', image)
shutil.copy2(OLD / 'rootfs.ext4', rootfs)
subprocess.run(['taskset', '-c', cpus, 'docker', 'run', '--rm', '--cpuset-cpus', cpus,
    '-v', str(ROOT)+':/work', 'allox-ebpf-test:20260907', 'bash', '-ec',
    'debugfs -w -R "rm /usr/bin/kata-agent" /work/monitor-rootfs.ext4; '
    'debugfs -w -R "write /work/agent-output/usr/bin/kata-agent /usr/bin/kata-agent" /work/monitor-rootfs.ext4; '
    'debugfs -w -R "set_inode_field /usr/bin/kata-agent mode 0100755" /work/monitor-rootfs.ext4; '
    'debugfs -R "dump /usr/bin/kata-agent /work/packaged-kata-agent" /work/monitor-rootfs.ext4'], check=True)
def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


assert sha(ROOT / 'packaged-kata-agent') == sha(agent)
partition_offset = 3145728
assert rootfs.stat().st_size == 265289728
with image.open('r+b') as target, rootfs.open('rb') as source:
    target.seek(partition_offset)
    shutil.copyfileobj(source, target)
report = {'agent_sha256': sha(agent), 'image_sha256': sha(image),
          'partition_offset': partition_offset, 'host_cpus': selected,
          'patch': 'ttrpc 0.8.1 non-INET ReusePort compatibility; cached device fix retained'}
(ROOT / 'guest-package.json').write_text(json.dumps(report, indent=2))
print(json.dumps(report), flush=True)
