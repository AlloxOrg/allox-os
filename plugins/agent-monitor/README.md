# Agent monitoring and profiling

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

### 可选批量模式

默认 `batch_size=1` 保留逐事件同步确认。显式启用批量模式可减少 SQLite FULL 提交和 Session socket 请求：

```python
with Tracer(emit_session, strict=True, batch_size=32) as tracer:
    for item in work_items:
        tracer.call_tool("work", process, item)
# 正常退出 context 时 flush；也可以显式 tracer.flush() / tracer.close()
```

`flush()` 成功后才确认整批持久化，服务端仍使用 WAL/FULL，一批任一事件非法会整体回滚。
SDK 缓冲最多 64 个事件、单批不超过 64 KiB；没有周期性自动 flush。交出跨进程 parent context、
查询结果、结束 turn 或 checkpoint 前必须先 flush。未 flush 事件不是 durable ACK，进程被杀时可能丢失；
`health()` 明确报告 pending/acknowledged，pending 或未确认时 complete=false。
strict 模式在批量发送/flush 时报告错误，不保证每次入队立即获知远端故障。没有自动重试不确定的 ACK，
避免重复执行/重复 span。需要每一步立刻可见、逐事件崩溃证据的场景应继续用默认同步模式。

### 可选后台 profiling 模式

```python
with Tracer(emit_session, strict=True, batch_size=64,
            delivery="background", queue_capacity=1024,
            queue_bytes=1024 * 1024, flush_interval=0.02) as tracer:
    tracer.call_tool("work", process)
    tracer.barrier()  # 等待之前已提交事件取得 FULL commit 的 ACK
```

仅普通应用层 profiling 可选用此模式；默认 `delivery="sync"` 和 Core/安全记录的可靠路径不变。
后台 worker 按批量阈值或最长等待间隔尝试导出，不在每次 action 中等待磁盘。
时间间隔不是最大持久化延迟承诺：慢磁盘、排队和调度仍可能延后 ACK。
队列上限同时包含排队及在途事件的条数、编码字节；不是整个进程 RSS 上限。
满队列施加背压，`export_timeout`（默认 10 秒）到期明确计失败，不静默丢事件。
发送失败不自动重试不确定 ACK，流进入失败状态，防止后续 end 越过丢失的 start。

`health()` 区分 pending、acknowledged、failed、在途字节、队列峰值、背压等待和 worker 状态；
入队成功不等于持久化成功。进程崩溃/SIGKILL 可丢失内存队列，服务端无法统计从未收到的数据。
`close()`/context 正常退出排空并停止线程；必须先等待应用自己的并行子任务，禁止复用 fork 前的 Tracer。

`barrier()` 即使在 best-effort 模式也会在不完整/失败时抛错；跨进程 `.context` 和后台模式的
run/turn span 结束均使用此屏障。受信任编排层可调用 `tracer.checkpoint(create_checkpoint, ...)`，
只有该 Tracer 先前已提交事件确认完成才调用 checkpoint 函数。它**不是全局自动拦截器**，
不能替其他 Tracer、其他进程或未接入 SDK 的 Agent 排空缓存，也不会为 Agent 增加管理权限。


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
MCP 初始化和工具发现可使用下面的显式 adapter；不自动拦截或替换 MCP SDK。

### 流式模型与 MCP

```python
from allox_agent_monitor.mcp import TracedMCPClient

client = TracedMCPClient(existing_async_mcp_client, tracer)
await client.initialize()
await client.list_tools()
result = await client.call_tool("sum", arguments={"a": 1, "b": 2})

async for chunk in tracer.astream_model("qwen", create_async_stream, **request):
    consume(chunk)
# 同步流使用 stream_model；提前停止时调用 close()/aclose()，正常结束后 flush。
```

MCP wrapper 记录初始化、发现、调用的客户端请求时间；不把客户端时间标成远端工具执行时间。
`isError=True` 的 MCP 返回值保留原样，tool span 标记 error。这里不是完整 ProMCP 六阶段编排器。
流式 wrapper 记录 `client.first_chunk_ns`、流的生命周期与最终 usage/metrics；首个 chunk 可能是元数据，
不是服务端首 token。生命周期包含消费者暂停时间，不等于纯推理时间。缺失服务端排队/推理指标仍 unknown。

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

`process.start` 通过可选 feature hook 向沙箱注入 `ALLOX_MONITOR_CONTEXT`；SDK 默认继承
Core process span、trace、turn 和语义路径。可信变量覆盖调用者传入的同名环境变量；parent 必须是
当前 Agent/Session 内仍在运行的 span。`action.context` 在导出引用前 flush，确保 collector 已有父记录。
`Tracer(inherit_context=False)` 可关闭继承，显式不同 trace_id 不继承原父节点。

`monitor.export` 增加 `measure=calls|self_ns|tokens` 的 folded stack 导出（`data` 字段）。
这是显式 semantic_path 的火焰图输入，不是自动语义识别或 pprof protobuf。缺失 token usage、
未结束 span、不足以计算 self time 的时钟关系会报告 skipped/incomplete，不补零。

### 可选 collector 异步批量落盘

daemon 增加 `--monitor-async-write`（须同时启用 `--monitor-root`），SDK 显式选用：

```python
from allox_agent_monitor import Tracer
from allox_agent_monitor.transport import SessionQueueTransport

with Tracer(SessionQueueTransport(), delivery="queued", batch_size=32,
            strict=True) as tracer:
    with tracer.span("action", "turn"):
        tracer.call_tool("work", process)
```

这与 `background` 不同：旧模式在 SDK 后台逐批等待 FULL ACK；新模式由 collector
先返回 `accepted=true,durable=false` 的入队回执，再把**同一 Agent/Session/source/run**
的多个批次合并成一个 FULL 事务。SDK 不再增加发送线程，单次请求仍有 socket/入队成本。
不同 channel 不共用事务，一个 channel 的非法事件不会回滚另一 Session 的数据。
默认同步路径、Core process 记录和 system-effects/恢复安全日志均未改为异步。

回执绑定 collector 实例、channel 随机 ID 和序号；只有 `flush` 返回匹配的 durable watermark
后，SDK 才增加 acknowledged。重启、错误回执、失败提交、超时均不能变为成功 ACK，也不重试
不确定投递。`health()` 中 enqueued 不等于 acknowledged；失败后流保持 incomplete。
`run/turn` 结束、parent context 导出、`tracer.checkpoint` 和正常退出均等待落盘；
collector 在 Session execution 结束时封闭该 run 入队并排空，在 Core mutation hook 和
trace/profile/export 查询前也排空已接收的对应 Session 数据。**这不能排空其他进程尚未发送的 SDK 缓冲**。

collector 默认最多 4096 个在途事件/8 MiB 编码数据、单 channel 1024 事件、1024 channel；
满队列背压，5 秒仍不能接纳则拒绝。channel 元数据保留到 collector 关闭，达到数量上限会明确拒绝，
不是无限长运行保证。worker 最长等待 20ms 尝试合并，单事务最多 512 事件/1 MiB；
慢盘和调度可能延后提交，20ms **不是持久化时限**。这些是数据容量限制，不是进程 RSS 限制。
SDK 本地仍最多 64 事件/64 KiB，按 batch_size 或显式边界发送，没有本地计时器；
`queue_capacity/queue_bytes/flush_interval/export_timeout` 是旧 background 模式参数，不控制 queued collector。

异步队列在内存中：SIGKILL/断电可能丢失**所有尚未取得 durable 回执的尾部**，不能把它用于要求逐条
崩溃证据的审计。旧回执不能跨 collector 重启复用。频繁每 action 强制 flush 时合并空间很小，
不承诺比严格批量模式更快。性能须包含最后一次 flush，不能只报前台返回速度。

### 可选多 action / 时间窗口策略

在 queued 模式上显式增加以下参数，放宽**普通 profiling** 的逐 turn 持久化要求：

```python
with Tracer(SessionQueueTransport(), delivery="queued", batch_size=32,
            action_batch_size=8, action_flush_interval=0.05,
            strict=True) as tracer:
    for item in work_items:
        with tracer.span("action", "turn"):
            tracer.call_tool("work", process, item)
```

- 默认仍不启用此策略。开启后，`turn` span 结束只报告一个完成的 action，不等待磁盘。
  子 tool/phase 不重复计数；没有 turn span 的适配器可显式调用 `tracer.action_finished()`，不要两处都调用。
- 每个 Tracer 累计 8 个尚未确认的完成 action，**或最早未确认事件等待到 50ms**，先到即通知后台提交。
  定时器不依赖下一次 action，不被持续的新事件无限重置；一个长 action 中的事件也可定时落盘。
- 专用后台 worker 执行入队与 FULL flush，不持有生产者/health 的锁等待磁盘。
  本地队列沿用 `queue_capacity/queue_bytes/export_timeout`，容量计入排队及在途数据；满时背压并提前请求提交。
  wire batch 上限和 collector 自己的计时器也可能让数据更早提交，因此“8/50ms”是触发策略，不是每批恰好 8 个 action。
- `tracer.flush()/barrier()`、`tracer.checkpoint(...)`、parent context 导出、run span 结束、正常 close
  都覆盖此策略并等待 FULL durable ACK。collector 的 mutation/export/execution-end 屏障保持不变，
  **仍只能排空其已接收的记录**；外部管理调用不能读取另一个进程尚未发送的 SDK 缓冲。
- `health()` 增加 completed_actions/durable_actions、action_batch_size、action_flush_interval、
  flush_requests 和 worker 状态；只有已收到持久化回执才增加 acknowledged，失败保持 incomplete。
  50ms 是尝试开始提交的等待窗口，**不是实际持久化时限或最大崩溃丢失窗口**。
- 如果每个 action 内仍调用 checkpoint/context/barrier，仍然会每次 flush，无法靠这个选项合并。
  此策略不合并 workspace checkpoint，也不改变 system-effects/恢复安全日志。

这明确改变了监控持久化语义：允许多个 action 的记录尚未落盘。进程或 collector 崩溃可能丢失尾部，
不能用速度改善声称与“每 action 都已持久化”的原模式有相同崩溃保证。

## 可信度与尚未完成的部分

- 框架记录标为 `framework-sdk`，执行器记录标为 `allox-executor`。前者不是内核证据。
  HTTP ingest 不能伪造或结束 Core process span。
- 沙箱 socket 上报标为 `session-sdk`，`process_run_id` 由 collector 绑定到唯一活跃的
  Core run，不采信 Agent 自报。默认 trace context 将 SDK span 挂到对应 Core process span；
  同一 Session 的后续 run 不能结束先前 run 的 SDK span。
- 进程记录包含 run ID；启用 process-tree 后可用 `process.events` 查看 fork/exec/exit。
  文件读写/网络对象级事件尚未接入本插件；后续通过 system-effects adapter 关联。
- 资源计数覆盖整个 Session cgroup，不把它重复分配给并行的模型/工具子步骤。
  memory peak 使用 max 聚合；block I/O 不等于全部文件访问字节，page cache 访问另计。
- 不同 clock ID 不做时间戳减法。跨机器父子 span 的 self time 为 unknown；
  时钟分辨率不是实际误差上界，尚未建立跨机器时钟校准。
  Linux 上通过 boot ID + time namespace 区分 CLOCK_MONOTONIC 域；真正 procfs 中两个 time
  namespace 条目均缺失时按该内核未启用 TIME_NS 处理。读取失败或非 Linux 时保守地分配独立 ID。
  依赖正常、未伪造的 procfs；SDK 时间戳仍是应用层证据，不是安全可信的内核时间证明。
- 服务端指标按 [vLLM v0.25 文档](https://docs.vllm.ai/en/v0.25.0/features/per_request_metrics/)
  读取 queue、TTFT、generation；generation 是 decode，不包含 prefill。
  流式 wrapper 自动处理收到的最终 usage/metrics chunk；服务端不返回则仍未知。
- SQLite 提交仍是 WAL/FULL；默认同步有界写入，可显式开启上述异步队列，不宣称零开销。发送失败在 SDK health 暴露；collector
  不知道从未发送的事件，因此 loss rate 是 unknown 而非 0。
  首轮 117 的毫秒级合成负载已暴露 FULL 同步落盘的高延迟；批处理/异步队列不能消除
  必须的持久化边界，本版不应直接作为低开销生产采集器。
  批量入口现已复用整个 envelope 的有限 JSON/64 KiB 编码校验，避免再序列化每条
  event；仍逐字段检查，并保留 FULL 提交与 durable ACK。该优化不消除物理盘提交
  延迟，也没有通过换用 tmpfs、降低同步级别或遗漏最终 flush 来制造低开销结果。
- 正文默认仅长度和 hash；显式 capture 会按常见键脱敏，但无法保证自由文本没有秘密。
  当前无自动语义任务识别，semantic_path 由框架显式提供。
- 可观测性失败：SDK 默认 best-effort，`strict=True` 失败即抛错。Core hooks 则 fail-closed，
  finalization 失败会保留 execution lease，不能把监控失败当成安全完成。
- 本版还没有现成 LangChain callback adapter、UI、完整 eBPF
  overhead/事件丢失实验，也不涉及 rollback/权限策略改动。

## 验证

多 action / 时间窗口策略见 [2026-11-12 后续实测](../../docs/development/monitor-actions-20261112.md)：
8 action / 50ms 策略的总耗时中位数从逐 action background 的 1316ms 降至 221ms，
但允许未落盘尾部跨 action 存在，不具有原逐 action 模式的相同崩溃保证；默认不启用。

collector 异步批量落盘的四场景、各 10 组结果见
[2026-11-12 实测](../../docs/development/monitor-async-20261112.md)：连续调用的总耗时中位数降低约 30%–35%，
但每 action 一个持久化边界的 socket 场景反而变慢约 20%；因此保持可选，不能宣称普遍加速。

合并 system-effects main 后的真实 Kata、Codex/Qwen、跨 Session 身份与开销结果见
[2026-10-30 集成实测](../../docs/development/agent-monitoring-integration-test-20261030.md)。
其中明确区分已验证能力、仍需 SDK 埋点的范围、trace/clock 断点和不完整资源计数。

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
