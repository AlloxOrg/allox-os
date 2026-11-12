"""Use the existing isolated Kata test kernel, never change global runtime config."""
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path("/data/hy/Allox/recovery-checks-20261112")
FIXTURES = Path("/data/hy/Allox/joint-rollback-kata-btrfs-20261110")


def command(*args, **kwargs):
    return subprocess.check_output(args, universal_newlines=True, **kwargs).strip()


def main():
    online = set()
    for part in Path("/sys/devices/system/cpu/online").read_text().strip().split(","):
        first, _, last = part.partition("-")
        online.update(range(int(first), int(last or first) + 1))
    selected = sorted(online)[-24:]
    if len(selected) != 24:
        raise RuntimeError("need 24 online logical CPUs")
    os.sched_setaffinity(0, selected)
    cpus = ",".join(map(str, selected))
    run = Path(tempfile.mkdtemp(prefix="guest-", dir=str(ROOT)))
    config = Path("/data/hy/Allox/kata-ebpf-runtime/configuration.toml").read_text()
    kernel = FIXTURES / "linux-6.1.62/arch/x86/boot/bzImage"
    assert kernel.is_file()
    config, count = re.subn(r'^kernel = "[^"]+"$', 'kernel = "{}"'.format(kernel), config, flags=re.M)
    assert count == 1
    config = re.sub(r'^block_device_aio = "[^"]+"$', 'block_device_aio = "threads"', config, flags=re.M)
    (run / "kata.toml").write_text(config)
    name = "allox-recovery-" + run.name
    container = None
    try:
        container = command(
            "docker", "create", "--name", name, "--pull", "never", "--runtime", "kata-allox-ebpf",
            "--annotation", "io.katacontainers.config_path={}".format(run / "kata.toml"),
            "--cpuset-cpus", cpus, "--cgroupns", "host", "--network", "none",
            "--cap-add", "SYS_ADMIN", "--cap-add", "SYS_PTRACE", "--cap-add", "MKNOD",
            "--security-opt", "seccomp=unconfined", "--security-opt", "apparmor=unconfined",
            "--device-cgroup-rule", "b 7:* rwm", "--device-cgroup-rule", "c 10:237 rwm",
            "--shm-size", "1g", "-v", "{}:/experiment:ro".format(ROOT),
            "-v", "{}:/allox:ro".format(ROOT / "source"),
            "-v", "{}:/fixtures:ro".format(FIXTURES),
            "-v", "{}:/gate:ro".format(run),
            "--entrypoint", "/bin/sh", "allox-ebpf-test:20260907", "/experiment/guest-mount.sh")
        command("docker", "start", container, timeout=90)
        pinned = []
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                args = (proc / "cmdline").read_bytes()
            except (OSError, PermissionError):
                continue
            if container.encode() in args and (b"qemu-system" in args or b"containerd-shim-kata" in args):
                command("taskset", "-apc", cpus, proc.name)
                pinned.append(int(proc.name))
        assert pinned
        (run / "start").touch()
        with (run / "console.log").open("w") as output:
            subprocess.run(["timeout", "180", "docker", "logs", "-f", name], stdout=output,
                           stderr=subprocess.STDOUT, check=True)
        logs = (run / "console.log").read_text()
        print(logs, flush=True)
        assert int(command("docker", "wait", name, timeout=20)) == 0
        rows = [json.loads(line.split(" ", 1)[1]) for line in logs.splitlines()
                if line.startswith("ALLOX_RECOVERY_REPORT ")]
        assert len(rows) == 1 and rows[0]["passed"]
        rows[0].update(host_cpus=selected, host_processes=pinned, runtime="kata-allox-ebpf",
                       backing="Guest tmpfs loop Btrfs; not physical-disk/power-loss validation")
        (run / "report.json").write_text(json.dumps(rows[0], indent=2))
        print("REPORT_PATH", run / "report.json", flush=True)
    finally:
        if container:
            subprocess.run(["docker", "rm", "-f", container], timeout=30,
                           stdout=subprocess.DEVNULL, check=False)


if __name__ == "__main__":
    main()
