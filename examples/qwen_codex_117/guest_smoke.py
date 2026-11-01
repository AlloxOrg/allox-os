"""Real Codex/Qwen inside Kata + Bubblewrap; provider credentials remain on host."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from allox_agent_monitor.sdk import Tracer, payload, record_vllm_metrics

from allox.workspace.daemon import WorkspaceService
from allox.workspace.store import WorkspaceStore

MODEL = os.environ.get("ALLOX_QWEN_MODEL", "Qwen3.5-4B-CPU-Test")

class DirectoryBackend:
    """Execution-only fixture. This test does not claim Btrfs rollback coverage."""

    def assert_root(self, root):
        pass

    def create_subvolume(self, path):
        path.mkdir()


class ModelProxy(BaseHTTPRequestHandler):
    """Transparent unary Responses timing; no token or content logging."""

    def log_message(self, *args):
        pass

    def do_POST(self):
        server = self.server
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size < 2 * 1024 * 1024:
                raise ValueError("invalid model request size")
            request = json.loads(self.rfile.read(size))
            streaming = bool(request.get("stream"))
            request["model"] = MODEL
            request["stream"] = False
            request["max_output_tokens"] = min(request.get("max_output_tokens") or 2048, 2048)
            request["chat_template_kwargs"] = {"enable_thinking": False}
            active = [r for r in server.service.processes.list_runs('qwen', 's1')
                      if r['state'] == 'running']
            parent_id = active[0].get('monitor_span_id', server.parent_id) if len(active) == 1 else server.parent_id
            with server.tracer.span(
                MODEL, "llm", model=MODEL,
                parent_id=parent_id, semantic_path=["codex-qwen-smoke"],
                input=payload(request),
            ) as span:
                target = urlsplit(os.environ["ALLOX_QWEN_UPSTREAM"])
                conn = http.client.HTTPConnection(target.hostname, target.port, timeout=300)
                started = time.perf_counter_ns()
                conn.request("POST", target.path.rstrip("/") + "/responses",
                             json.dumps(request).encode(), {"Content-Type": "application/json",
                             "Authorization": "Bearer "+os.environ.get("ALLOX_GATEWAY_TOKEN", "")})
                response = conn.getresponse()
                data = response.read(4 * 1024 * 1024)
                span.metric("client.request_ns", time.perf_counter_ns()-started)
                value = json.loads(data)
                record_vllm_metrics(span, value)
                for name in ("input_tokens", "output_tokens"):
                    if isinstance(value.get("usage", {}).get(name), (int, float)):
                        span.metric("provider."+name, value["usage"][name])
                span.set_result({"status": response.status, "response": value})
                server.requests.append({"http_status": response.status,
                    "input_tokens": value.get("usage", {}).get("input_tokens"),
                    "output_tokens": value.get("usage", {}).get("output_tokens"),
                    "input_bytes": size, "output_bytes": len(data)})
                conn.close()
                if response.status >= 400:
                    raise RuntimeError("model upstream rejected request")
            self.send_response(response.status)
            if streaming:
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                events = [{"type": "response.created", "response": {**value, "output": [],
                           "status": "in_progress"}}]
                for index, item in enumerate(value.get("output", [])):
                    events.append({"type": "response.output_item.added", "output_index": index,
                                   "item": item})
                    events.append({"type": "response.output_item.done", "output_index": index,
                                   "item": item})
                events.append({"type": "response.completed", "response": value})
                for event in events:
                    self.wfile.write(("event: "+event["type"]+"\ndata: "+json.dumps(event)+"\n\n").encode())
                self.wfile.flush()
            else:
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except Exception as exc:  # noqa: BLE001 - do not expose prompts or infrastructure errors
            data = json.dumps({"error": {"message": "model proxy: " + type(exc).__name__}}).encode()
            self.send_response(502)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)


TASK = '''import json, os, subprocess, sys
from pathlib import Path
from allox_agent_monitor import Tracer
from allox_agent_monitor.transport import emit_session
t=Tracer(emit_session, strict=True)
def work():
    child=int(subprocess.check_output([sys.executable,'-c','import os;print(os.getpid())']))
    value={'sum':sum(range(1,11)),'pid':os.getpid(),'child_pid':child,
           'agent':os.environ['ALLOX_AGENT_ID'],'session':os.environ['ALLOX_SESSION_ID']}
    Path('proof.json').write_text(json.dumps(value));print(json.dumps(value));return value
t.call_tool('sum-and-child-process',work)
'''

RESUME_TASK = '''import json,sys
from pathlib import Path
from allox_agent_monitor import Tracer
from allox_agent_monitor.transport import emit_session
t=Tracer(emit_session,strict=True)
def work():
    original=json.loads(Path('proof.json').read_text())
    result={'sum':original['sum']+1,'marker':sys.argv[1]}
    Path('resumed.json').write_text(json.dumps(result));print(json.dumps(result));return result
t.call_tool('resume-and-write',work)
'''


def main():
    runtime = Path("/dev/shm/allox-qwen-monitor")
    runtime.mkdir()
    store = WorkspaceStore(runtime / "workspace", DirectoryBackend())
    store.initialize()
    store.create_session("qwen", "s1")
    current = store.current("qwen", "s1")
    (current / "task.py").write_text(TASK)
    (current / "resume_task.py").write_text(RESUME_TASK)
    home = current / ".codex"
    home.mkdir()
    service = WorkspaceService(store, monitor_root=runtime / "monitor",
        process_audit_root=runtime / "process-audit",
        process_cgroup_root=Path("/sys/fs/cgroup/allox-qwen-monitor"))
    tracer = Tracer(lambda event: service.dispatch("monitor.ingest", {
        "agent_id": "qwen", "session_id": "s1", "event": event}), strict=True)
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), ModelProxy)
    proxy.tracer, proxy.requests, proxy.parent_id = tracer, [], None
    proxy.service = service
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    config = f'''model = "{MODEL}"
model_provider = "allox_qwen_cpu"
approval_policy = "never"
sandbox_mode = "danger-full-access"
model_context_window = 16384
[model_providers.allox_qwen_cpu]
name = "Allox laboratory bridge"
base_url = "http://127.0.0.1:{proxy.server_port}/v1"
wire_api = "responses"
requires_openai_auth = false
request_max_retries = 0
stream_max_retries = 0
'''
    (home / "config.toml").write_text(config)
    environment = {"CODEX_HOME": "/workspace/.codex",
                   "LD_LIBRARY_PATH": "/usr/local/lib/qwen-test"}
    report = {"passed": False, "kernel": os.uname().release,
              "snapshot_backend": "directory fixture; no rollback tested",
              "execution_boundary": "real Kata + Bubblewrap + cgroup v2",
              "model": MODEL, "rounds": []}
    output = Path("/results")
    try:
        with tracer.span("codex-qwen-real-agent", "run", semantic_path=["codex-qwen-smoke"]):
            thread_id = None
            for index, prompt in enumerate((
                ("Use the exec_command tool with cmd='python3 task.py', workdir='/workspace' now. "
                "The argument name is cmd, a string; never use a command array or a tool named shell. "
                "Use a real tool call, not XML or plain text describing a call. "
                "Then read proof.json and report the sum. Do not merely describe a tool call. "
                "Remember the conversation-only marker JADE_CRANE_926. Do not write this marker "
                "to any file. Do not inspect environment, credentials, config or session logs."),
                ("Continue the previous task. Use the exec_command tool with cmd to run `python3 resume_task.py "
                "<marker>` using the conversation-only marker I gave you earlier as its argument. "
                "Read resumed.json. Do not read config, session logs or any unrelated files."),
            )):
                with tracer.span(f"turn-{index+1}", "turn", turn_id=f"turn-{index+1}") as turn:
                    proxy.parent_id = turn.span_id
                    args = ["/usr/local/bin/qwen-codex-exec"]
                    if thread_id:
                        args += ["resume"]
                    args += ["--json", "--skip-git-repo-check"]
                    if thread_id:
                        args += [thread_id]
                    args += [prompt]
                    row = service.processes.start("qwen", "s1", args, env=environment,
                                                  timeout=420, trace_context=turn.context)
                    run = service.processes._runs[row["run_id"]]
                    if not run["done"].wait(450):
                        raise RuntimeError("real Agent timed out")
                    row = service.processes.get("qwen", "s1", row["run_id"])
                    stdout = (Path(row["audit_directory"]) / "stdout.log").read_text()
                    stderr = (Path(row["audit_directory"]) / "stderr.log").read_text()
                    (output / f"turn-{index+1}.jsonl").write_text(stdout)
                    (output / f"turn-{index+1}.stderr.txt").write_text(stderr)
                    events = []
                    for line in stdout.splitlines():
                        try:
                            events.append(json.loads(line))
                        except ValueError:
                            pass
                    ids = [e["thread_id"] for e in events if e.get("type") == "thread.started"]
                    if ids:
                        if thread_id is not None and thread_id != ids[-1]:
                            raise RuntimeError("resume changed conversation identity")
                        thread_id = ids[-1]
                    commands = [e["item"] for e in events if e.get("type") == "item.completed"
                                and e.get("item", {}).get("type") == "command_execution"]
                    item = {"turn": index+1, "run_id": row["run_id"], "thread_id": thread_id,
                            "exit_code": row.get("root_exit_code"), "commands": commands,
                            "state": row["state"]}
                    report["rounds"].append(item)
                    print(json.dumps({"turn": index+1, "exit_code": item["exit_code"],
                                      "command_count": len(commands)}), flush=True)
                    if row.get("root_exit_code") != 0 or not commands or not thread_id:
                        raise RuntimeError("Codex did not finish with a real shell action")
        proof = json.loads((current / "proof.json").read_text())
        resumed = json.loads((current / "resumed.json").read_text())
        spans = service.dispatch("monitor.trace", {"agent_id": "qwen", "session_id": "s1"})["spans"]
        report["checks"] = {
            "sum_55": proof["sum"] == 55, "resumed_sum_56": resumed["sum"] == 56,
            "memory_marker": resumed["marker"] == "JADE_CRANE_926",
            "real_model_requests": len(proxy.requests) >= 2,
            "sandbox_sdk_events": any(s["source"] == "session-sdk" for s in spans),
            "core_process_spans": sum(s["source"] == "allox-executor" for s in spans) == 2,
            "executor_internal_timing": any("executor.execution_ns" in s["metrics"] for s in spans),
            "all_received_spans_ended": all(s["status"] != "running" for s in spans),
            "real_cgroup_cpu": all("cgroup.cpu_usage_usec" in s["metrics"]
                for s in spans if s["source"] == "allox-executor"),
            "single_trace": len({s['trace_id'] for s in spans}) == 1,
            "same_clock_domain": len({s['clock_id'] for s in spans}) == 1,
            "profile_complete": service.dispatch('monitor.profile', {
                'agent_id': 'qwen', 'session_id': 's1'})['complete'],
        }
        (output / "trace.json").write_text(json.dumps(spans, indent=2))
        report["proof"], report["resumed"] = proof, resumed
        report["passed"] = all(report["checks"].values())
    except Exception as exc:  # noqa: BLE001 - record failure without dumping configuration
        report["error_type"] = type(exc).__name__
        report["error"] = str(exc)[:300]
    finally:
        report["model_requests"] = proxy.requests
        report["sdk_health"] = tracer.health()
        report["profile"] = service.dispatch("monitor.profile", {"agent_id": "qwen", "session_id": "s1"})
        (output / "report.json").write_text(json.dumps(report, indent=2))
        service.close()
        proxy.shutdown()
        proxy.server_close()
        shutil.copytree(runtime / "monitor", output / "monitor", dirs_exist_ok=True)
        if (current / "proof.json").exists():
            shutil.copy2(current / "proof.json", output / "proof.json")
        print(json.dumps({"passed": report["passed"], "error": report.get("error"),
                          "checks": report.get("checks")}), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
