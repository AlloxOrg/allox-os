"""Refresh the previous qwen-codex test using a CPU-affined replica and disposable Kata VM."""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path("/data/hy/Allox/agent-monitoring-20261030-v1")


def affinity():
    values = []
    for piece in Path("/sys/devices/system/cpu/online").read_text().strip().split(","):
        ends = [int(value) for value in piece.split("-")]
        values.extend(range(ends[0], ends[-1]+1))
    selected = sorted(set(values))[-24:]
    if len(selected) != 24:
        raise RuntimeError("117 has fewer than 24 online logical CPUs")
    os.sched_setaffinity(0, selected)
    return ",".join(map(str, selected)), selected


def output(argv, **kwargs):
    return subprocess.check_output(argv, text=True, **kwargs).strip()


def inspect(container):
    return json.loads(output(["docker", "inspect", container]))[0]


def main():
    if not ROOT.is_dir() or not str(Path.cwd()).startswith("/data/hy/Allox"):
        raise SystemExit("run only in the 117 Allox experiment workspace")
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    name = "allox-qwen-monitor-"+stamp
    result = ROOT / "real-agent-results" / name
    result.mkdir(parents=True, mode=0o700)
    print("RESULT_DIR", result, flush=True)
    model_container = agent_container = None
    metadata = {"passed": False, "result_dir": str(result), "model": "Qwen3.5-4B-CPU-Test"}
    try:
        original = inspect("myhc-ai-test-local-llm-1")
        model_image = original["Config"]["Image"]
        cpus, selected = affinity()
        help_text = output(["taskset", "-c", cpus, "docker", "run", "--rm",
            "--cpuset-cpus", cpus, "--network", "none", model_image, "--help"],
            stderr=subprocess.STDOUT, timeout=40)
        args = list(original["Config"]["Cmd"])
        for flag, value in (("-c", "16384"), ("-t", "24"), ("--threads-batch", "24")):
            args[args.index(flag)+1] = value
        if "--jinja" in help_text:
            args += ["--jinja"]
        if "--chat-template-kwargs" in help_text:
            args += ["--chat-template-kwargs", '{"enable_thinking":false}']
        cpus, selected = affinity()
        command = ["taskset", "-c", cpus, "docker", "run", "-d", "--name", name+"-llm",
                   "--cpuset-cpus", cpus, "--memory", "12g", "--network", "bridge"]
        # This particular deployment embeds weights in the immutable image.
        if original["Mounts"]:
            raise RuntimeError("model mounts changed; review them before replicating")
        command += [model_image, *args]
        model_container = output(command, timeout=60)
        address = inspect(model_container)["NetworkSettings"]["Networks"]["bridge"]["IPAddress"]
        metadata.update(host_cpu_ids=selected, model_container=model_container,
                        original_model_service_unchanged=True, model_image=model_image,
                        model_context_window=16384)
        print("MODEL_REPLICA_STARTED", model_container, "CPU", cpus, flush=True)
        deadline = time.monotonic()+180
        while time.monotonic() < deadline:
            try:
                conn = http.client.HTTPConnection(address, 8083, timeout=3)
                conn.request("GET", "/v1/models")
                response = conn.getresponse()
                payload = json.loads(response.read())
                conn.close()
                if response.status == 200:
                    assert any(m.get("id") == "Qwen3.5-4B-CPU-Test" for m in payload["data"])
                    break
            except (OSError, ValueError):
                pass
            time.sleep(1)
        else:
            raise RuntimeError("CPU model replica did not become ready")
        cpus, selected = affinity()
        command = ["taskset", "-c", cpus, "docker", "run", "-d", "--name", name,
            "--runtime", "kata-allox-ebpf", "--cpuset-cpus", cpus, "--cgroupns", "host",
            "--cap-add", "SYS_ADMIN", "--security-opt", "seccomp=unconfined",
            "--security-opt", "apparmor=unconfined", "-e", "PYTHONPATH=",
            "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", f"ALLOX_QWEN_UPSTREAM=http://{address}:8083/v1"]
        for source, target in (
            (ROOT / "dist", "/dist/core"),
            (ROOT / "plugins/agent-monitor/dist", "/dist/monitor"),
            (Path(__file__).with_name("guest_smoke.py"), "/opt/guest_smoke.py"),
            (Path("/data/act/bin/qwen-codex-exec"), "/usr/local/bin/qwen-codex-exec"),
            (Path("/opt/openssl-1.1.1q/libssl.so.1.1"), "/usr/local/lib/qwen-test/libssl.so.1.1"),
            (Path("/opt/openssl-1.1.1q/libcrypto.so.1.1"), "/usr/local/lib/qwen-test/libcrypto.so.1.1"),
        ):
            if not source.exists():
                raise RuntimeError("required experiment artifact is missing")
            command += ["-v", f"{source}:{target}:ro"]
        command += ["-v", f"{result}:/results", "allox-ebpf-test:20260907", "sh", "-ec",
            "sleep 10; python3 -m pip install -q --no-deps /dist/core/*.whl /dist/monitor/*.whl; "
            "python3 -m allox.runtime.bootstrap; python3 /opt/guest_smoke.py"]
        agent_container = output(command, timeout=90)
        for pattern in ("qemu-system.*sandbox-"+agent_container,
                        "containerd-shim-kata-v2-patched.*-id "+agent_container):
            pids = output(["pgrep", "-f", pattern]).split()
            for pid in pids:
                subprocess.run(["taskset", "-apc", cpus, pid], check=True,
                               stdout=subprocess.DEVNULL)
        metadata["agent_container"] = agent_container
        metadata["runtime"] = "kata-allox-ebpf"
        (result / "runtime.json").write_text(json.dumps(metadata, indent=2))
        print("KATA_STARTED", agent_container, flush=True)
        with (result / "console.log").open("w") as log:
            follow = subprocess.Popen(["docker", "logs", "-f", agent_container],
                                      stdout=log, stderr=subprocess.STDOUT)
            try:
                status = output(["docker", "wait", agent_container], timeout=1050)
            finally:
                follow.wait(timeout=15)
        report_path = result / "report.json"
        if report_path.exists():
            report = json.loads(report_path.read_text())
            metadata["passed"] = report.get("passed", False)
            print(json.dumps({"passed": metadata["passed"], "checks": report.get("checks"),
                              "error": report.get("error")}), flush=True)
        else:
            print((result / "console.log").read_text()[-3000:], flush=True)
        metadata["container_exit_code"] = int(status)
        if metadata["passed"]:
            (Path("/data/hy/Allox") / "qwen-codex-current.json").write_text(json.dumps({
                "binary": "/data/act/bin/qwen-codex-exec", "model": "Qwen3.5-4B-CPU-Test",
                "wire_api": "responses", "requires_key": False,
                "launcher": str(Path(__file__).resolve()), "last_success": str(result),
                "runtime": "Kata + Bubblewrap + cgroup v2", "cpu_policy": "last 24 online CPUs",
            }, indent=2))
    finally:
        for container in (agent_container, model_container):
            if container:
                subprocess.run(["docker", "rm", "-f", container], check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        metadata["test_containers_removed"] = True
        (result / "runtime.json").write_text(json.dumps(metadata, indent=2))
    return 0 if metadata["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
