# Agent monitoring and profiling (first slice)

独立 wheel：`allox-agent-monitor`，默认不安装、不启用。基于 Plugin API 1 扩展，
不依赖 LangChain、特定模型 SDK 或 system-effects 实验分支。

## 已实现

- `run/turn/llm/tool/subagent/phase/process` span；父子、因果 links、并行组、循环迭代。
- 参数/结果摘要、状态、异常类型与单调时钟计时；默认不保存 prompt、凭据或输出正文。
- 工具端到端与内部执行时间；`executor.execution_ns` 在执行器实现两侧直接计时，
  不包含 begin/end 事件发送；模型请求同理记录 `client.request_ns`。
  span inclusive 时间包含启用监控后的实际路径开销，不能冒充纯内部执行时长。
  显式 phase 覆盖审批、执行器排队、传输、MCP 初始化/发现/调用。
- vLLM 响应中的真实 `metrics` 与 token usage；缺失值不补零、不推测网络时延。
- Core 进程启动/结束 hooks，将 run ID 与调用 span 关联；结束前读取整个 Session
  cgroup 的 CPU、内存峰值、block I/O。缺失计数器明确列出。
- Session 内单次/跨运行语义路径聚合、调用数、错误数、P50/P95、inclusive/self 时间。
  并行子 span 使用时间区间并集扣减；不能把 inclusive 累计工作量当作墙钟时间。
- SQLite WAL/FULL 持久化，存储在 Session rollback 范围外；崩溃未结束的 span 保留为 incomplete。
- 同步确认、拒绝/发送失败计数、采集耗时和存储/序列化字节数；提供监控开关的配对 benchmark。

## 使用

```bash
pip install ./plugins/agent-monitor
export ALLOX_WORKSPACE_TOKEN='<management-only token>'
allox-workspace-daemon --root /var/lib/allox/workspaces \
  --process-audit-root /var/log/allox/processes \
  --monitor-root /var/log/allox/agent-monitor
```

`--monitor-root` 启用 collector 与受管理执行，要求 cgroup v2 和管理 API 认证。
管理 token 和 DB 不会挂进 Bubblewrap。Agent 在沙箱内可使用只追加的 Session telemetry
socket；服务端使用 `SO_PEERCRED` + cgroup 校验身份，不能指定其他 Session、查询其他
记录或发起管理操作。SDK 以只读方式挂入，并通过 `PYTHONPATH` 提供：

```python
from allox_agent_monitor import Tracer
from allox_agent_monitor.transport import emit_session

tracer = Tracer(emit_session)
result = tracer.call_tool("test", run_tests)
```

框架在可信编排层时，也可以通过管理 RPC 提交事件；不要把管理 token 交给 Agent。

```python
from allox_agent_monitor import Tracer

# rpc 是可信应用已有的管理 API 客户端；不是 Agent 自己持有的管理权限。
tracer = Tracer(lambda event: rpc("monitor.ingest", {
    "agent_id": "a", "session_id": "s", "event": event,
}), strict=True)

with tracer.span("fix-test", "run", semantic_path=["coding", "repair"]):
    with tracer.span("turn-1", "turn", turn_id="turn-1"):
        response = tracer.call_model("qwen", invoke_model)
        result = tracer.call_tool("test", run_tests)
```

框架有独立队列/审批环节时，在外层 tool span 内用 `tracer.phase("approval")`、
`tracer.phase("executor_queue")`；在执行器内部用 `phase("tool_execution")`。
`call_tool` 只适合真正执行工具实现的 callable，不能用包住 RPC 客户端的时长冒充远端执行时间。
MCP 初始化和工具发现同样需要在对应客户端操作周围明确埋点；本版不会自动拦截 MCP SDK。

与 Allox 命令关联时，在工具 span 内调用：

```python
with tracer.span("test", "tool") as action:
    run = rpc("process.start", {"agent_id": "a", "session_id": "s",
        "argv": ["python3", "-m", "pytest"], "trace_context": action.context})
    wait_until_completed(run["run_id"])  # 不要启动后立刻结束父 span
```

`monitor.trace` 返回 span，`monitor.profile` 返回聚合结果；参数包含 `agent_id`、
`session_id`，可选 `trace_id`。省略 trace ID 时聚合该 Session 内所有运行，相同
semantic_path/name/model 合并。当前查询最多 10,000 个 span；超限显式报错，不静默截断。
`monitor.profile` 可提供 `session_ids` 列表聚合同一 Agent 的多条 Session；关联键保留
Session 身份，相同 trace ID 不会串线。跨 Agent 的全局查询尚未提供。

## 可信度与尚未完成的部分

- 框架记录标为 `framework-sdk`，执行器记录标为 `allox-executor`。前者不是内核证据。
  HTTP ingest 不能伪造或结束 Core process span。
- 沙箱 socket 上报标为 `session-sdk`，`process_run_id` 由 collector 绑定到唯一活跃的
  Core run，不采信 Agent 自报。Agent 自己建立的 trace 与 Core trace 可通过此 ID 连接。
- 进程记录包含 run ID；启用 process-tree 后可用 `process.events` 查看 fork/exec/exit。
  文件读写/网络对象级事件尚未接入本插件；后续通过 system-effects adapter 关联。
- 资源计数覆盖整个 Session cgroup，不把它重复分配给并行的模型/工具子步骤。
  memory peak 使用 max 聚合；block I/O 不等于全部文件访问字节，page cache 访问另计。
- 不同 clock ID 不做时间戳减法。跨机器父子 span 的 self time 为 unknown；
  时钟分辨率不是实际误差上界，尚未建立跨机器时钟校准。
- 服务端指标按 [vLLM v0.25 文档](https://docs.vllm.ai/en/v0.25.0/features/per_request_metrics/)
  读取 queue、TTFT、generation；generation 是 decode，不包含 prefill。
  流式响应需框架把最终 usage/metrics chunk 交给 `record_vllm_metrics`。
- SQLite 是同步有界事件写入，不宣称零开销。发送失败在 SDK health 暴露；collector
  不知道从未发送的事件，因此 loss rate 是 unknown 而非 0。
  首轮 117 的毫秒级合成负载已暴露 FULL 同步落盘的高延迟，下一步需要批处理/异步
  exporter，并明确崩溃丢失边界；本版不应直接作为低开销生产采集器。
- 正文默认仅长度和 hash；显式 capture 会按常见键脱敏，但无法保证自由文本没有秘密。
  当前无自动语义任务识别，semantic_path 由框架显式提供。
- 可观测性失败：SDK 默认 best-effort，`strict=True` 失败即抛错。Core hooks 则 fail-closed，
  finalization 失败会保留 execution lease，不能把监控失败当成安全完成。
- 本版还没有现成 LangChain/MCP callback adapter、UI、完整 eBPF
  overhead/事件丢失实验，也不涉及 rollback/权限策略改动。

## 验证

在 117 的独立目录运行 `python3.10 scripts/validate_agent_monitor.py`：每个验证任务
重新读取在线 CPU 并用 taskset 绑定最后 24 个逻辑核。输出位于 `validation-results/`。
benchmark 是合成 CPU+等待负载，只测 SDK/SQLite 监控开销，不代表真实模型或 eBPF 开销。
首轮结果及已验证/未验证边界见
[2026-10-30 验证记录](../../docs/development/agent-monitoring-first-slice.md)。

需求参考：
[TraceLab](https://arxiv.org/html/2606.30560)、
[AgentSight](https://arxiv.org/html/2508.02736v2)、
[AgentPProf](https://arxiv.org/html/2609.20301v1)、
[ProMCP](https://aclanthology.org/2026.findings-acl.1967/)。
