# 观测与 profiling：基线对照、补齐与验证

日期：2026-10-31（客户端日期）。基于 `0e60d77`，独立工作树 `feat/monitoring-baseline-parity`；实验期间未修改原有 dirty main。后续发布状态以 Git 历史为准。

本文前半部分保留 10 月 31 日历史数据；11 月 1 日的后台导出改进与复测见文末。

## 结论

当前实现不是低开销生产监控器。批量提交降低了同步落盘次数，但短工具调用仍明显受监控影响。
论文中只有部分数字是“启用监控相对于关闭监控”的额外延迟，不能把全部 baseline 数字放在同一排行榜。
以下论文结果来自原文核查，**未在 117 复现这些外部系统**。

## MD 中的基线

| 基线 | 原文中的成本口径 | 与 Allox 的关系 |
|---|---|---|
| [OpenAI Agents tracing](https://developers.openai.com/api/docs/guides/agents-api/tracing) | 官方文档提供模型、工具、handoff 等 trace 与导出能力；所查文档没有统一监控开关延迟百分比 | 借鉴父子 trace、状态与导出；不能声称比它快或慢 |
| [AgentSight §5.1](https://arxiv.org/html/2508.02736v2) | 真实 Claude Code 工作流，每种条件重复 3 次；论文平均 2.9%，三个任务分别 3.4%、4.9%、0.4% | 是最相关的低扰动监控参照；平台、工作量和采集范围均不同，不可直接横向排名 |
| [TraceLab §6.3](https://arxiv.org/html/2606.30560) | 工具端到端减 runner 内部执行，残差均值 1.11 秒、P50 0.13 秒 | 包括审批、调度、shell/输出等，不是 tracer 自身开销；我们新增同类残差样本，但不称作网络时延 |
| [AgentPProf §5.4](https://arxiv.org/html/2609.20301v1) | 405 条轨迹一次性语义分段 37 分钟；固定标注后 27,765 个 operation 的 union 构建 1.16 秒，峰值 RSS 465 MiB | 是离线分段/重放成本，不是在线埋点开销；本版只有显式语义路径聚合与 folded export |
| [ProMCP](https://aclanthology.org/2026.findings-acl.1967.pdf) | 六阶段协议流水线，区分初始化、工具发现、规划/调用/最终综合等成本 | 其协议成本不是监控器额外开销；本版补三种 MCP 客户端操作，尚非完整六阶段编排器 |
| [vLLM per-request metrics](https://docs.vllm.ai/en/v0.25.0/features/per_request_metrics/) | 服务端显式启用指标，文档提示高并发下 CPU 成本可能不可忽略，没有统一百分比 | 按服务端实际返回值读取 queue、TTFT、decode；未返回则 unknown，不由客户端残差倒推 |

注意：AgentSight 并非每个任务都低于 3%。vLLM 的 TTFT 从被调度开始计时，不含排队；generation 是首输出到末输出的 decode，不等于完整推理。

## 最终代码：10 组开关配对实测

117 后 24 个在线逻辑核（24–47）；Guest Linux 6.1.62 / Kata / Bubblewrap / cgroup v2。
每轮相同的 40 次 CPU + 1 ms 等待，交替 off/on 顺序，显式 `batch_size=32`，最终 flush 计入时间；
collector 在真实 XFS 上使用 WAL/FULL。没有 LLM 等待，也没有实际 eBPF 采集。
默认 `batch_size=1` 未改，**这些数字不是默认逐事件同步模式的数字**。

| 测量范围 | 无监控均值 | 有监控均值 | 额外延迟 | 均值比增幅 |
|---|---:|---:|---:|---:|
| SDK → SQLite，40 次工具批次 | 49.73 ms | 188.11 ms | 138.38 ms | +278.2% |
| Guest 内 40 次工具循环，含 Session socket/flush | 49.52 ms | 289.35 ms | 239.83 ms | +484.3% |
| Session process.start → 终结 | 1,114.28 ms | 1,585.78 ms | 471.50 ms | +42.3% |
| 整个 Guest worker，含初始化/关闭 | 1,985.32 ms | 3,130.35 ms | 1,145.04 ms | +57.7% |

Session 配对差值的近似 t 95% CI 为 **388.56–554.44 ms**，单组增幅范围 17.3%–63.5%。
10 个样本较少，非独占机器、不能解释为稳定生产 SLA。不能把 Session 的百分比当成每个工具调用的百分比。
Session 窗口不含 daemon 初始化/关闭、VM 启动；整 worker 数字补上前两者，仍不含 VM 启动。

同一 Session 测量窗口 CPU 从 196.29 → 240.41 ms（+22.5%）；daemon 进程峰值 RSS 增加约 1.80 MiB，
工作负载进程峰值 RSS 增加约 3.81 MiB。两者是独立进程最大值，不相加冒充 VM 内存。
每轮确认 162 个事件 / 81 个 span，10 轮共确认 1,620 个事件，校验均通过；只能证明已提交事件得到确认，不能证明全系统 loss=0。

历史参考：上一版批量模式 Session 额外约 932 ms，本轮约 472 ms；但 CPU 亲和性和运行时环境有变化，
这约 49% 的差值缩小只是历史对照，**不是单一优化的受控消融结论**。本轮前一快照未额外绑核时，
同类 Session 结果为 952.69 → 1,441.58 ms（+51.3%），说明结果受环境影响，不能择优汇报。
SDK 前一快照的均值受长尾影响且没有相对旧批量版改善，也不能笼统声称所有热路径都已提速。

原始证据：[汇总](evidence/monitoring-baseline-parity-20261031/summary.json)、
[10 组 Guest](evidence/monitoring-baseline-parity-20261031/guest-benchmark.json)、
[10 组 SDK](evidence/monitoring-baseline-parity-20261031/micro-benchmark.json)、
[环境与 wheel SHA256](evidence/monitoring-baseline-parity-20261031/environment.json)。
脚本 `run_on_117.py`、`guest_bench.py`、`final_validation.py` 也在该目录，固定使用独立远端实验目录；
先将源码放到其 `source/`，构建 wheel，将 Guest 脚本放到根目录的 `guest_bench.py`，再从实验根目录运行。

## 已落地的补齐

- 可选 Core feature hook 传播 process run 的 trace context；SDK 自动继承对应 process parent、turn、语义路径。跨进程传递引用前 flush，拒绝当前 Session 外或已结束的父 span。
- 同一 Linux boot/time namespace 使用可比较的 CLOCK_MONOTONIC 域，读取失败仍保守返回独立域，不假设跨机器时钟同步。
- 同步/异步模型流：客户端首 chunk、生命周期、chunk 数、最终 usage/metrics，以及提前关闭/取消状态。首 chunk 不冒充服务端首 token，生命周期可能包含消费者暂停。
- 可插拔 `TracedMCPClient`：initialize、list_tools、call_tool，记录真实客户端请求时间；远端 `isError` 标记错误而不改变原返回值。
- 同步工具实现的 thread CPU 时间；工具端到端减单一内部执行 phase 的非执行残差。异步任务、子进程 CPU 不硬算到当前线程。
- `monitor.export`：calls/self_ns/tokens 的 folded stack；unknown usage 与不完整 span 不补零。不是自动语义识别，也不是完整 pprof protobuf。
- SDK 去掉不必要的顶层 asyncio 导入；保留 `batch_size=1` 默认语义，显式批量模式仍 FULL commit 后才 ACK。

跨进程实测还发现 Kata Guest 没有 time namespace 条目，早期实现误把每个进程分配成不同 clock ID。
已兼容此内核配置，并增加“读取失败不误判为同一时钟”的回归测试。真正 procfs 中两个 time namespace 条目由
[Linux CONFIG_TIME_NS](https://raw.githubusercontent.com/torvalds/linux/v6.1/fs/proc/namespaces.c) 控制；该逻辑假设正常未伪造的 procfs。
SDK 时间戳不是安全可信的内核证据。

## 验证范围

- 117，经跳板；实验根目录 `/data/hy/Allox/monitoring-parity-20261031-v1`。
- Host 全量：**227 passed / 20 skipped**，包括 system-effects 插件测试；Host 缺失能力对应的 skip 不算通过。
- 真 Kata Guest：**66 passed**；另有 3 个受管理执行场景（a/s1、a/s2、b/s1）全部通过。
- 场景实际使用 Bubblewrap、cgroup、Session telemetry socket，执行 Python 子进程、普通文件写入和 Unix socket 创建；这是受控 Python 工作负载，**不是本轮真实 LLM Agent 复测**，也不是文件/网络 eBPF 事件覆盖验证。
- 三个场景均验证身份、单一 trace、可比较 clock、SDK → Core process 父子关联、run ID 绑定、完整已接收 span、语义继承和 cgroup CPU。
- MCP 验证包含真实本地 HTTP/JSON-RPC 子集交互；不是官方 MCP SDK/完整协议互操作认证。
- Ruff（E9/F/I）、Pyright 无报错，Core 与插件 wheel 构建成功。

证据：[Host 日志](evidence/monitoring-baseline-parity-20261031/host-pytest.log)、
[Guest 日志](evidence/monitoring-baseline-parity-20261031/guest-pytest.log)、
[进程场景报告](evidence/monitoring-baseline-parity-20261031/process-report.json)、
[示例 trace](evidence/monitoring-baseline-parity-20261031/a-s1-trace.json)、
[独立 cProfile](evidence/monitoring-baseline-parity-20261031/batch-sdk-profile.txt)。

Qwen 未复测：本地密钥文件存在两个不同候选，原读取器无法安全解析；未自动猜选，未把密钥写入工程或输出。
Guest 是 Linux 6.1.62，缺少当前 system-effects BPF 所需的内核能力；本轮没有实际加载其完整 BPF 观测链，也没有验证 Btrfs rollback。

## 剩余瓶颈与改进优先级

单次独立 cProfile（不是 10 组均值）显示：worker 约 318 ms，5 次 SQLite 事务退出约 88 ms；
collector 初始化约 65 ms；565 次 JSON dumps 约 22 ms（cumulative 时间有嵌套，不能相加当占比）。
说明批量后仍有同步持久化等待、冷启动和重复序列化；不是主要慢在业务计算或模型推理。

1. 复用长驻 daemon/collector 与会话 endpoint，分别测冷启动、热路径；不把初始化成本悄悄移出统计口径。
2. 维持同步 durable 模式；另设计显式低扰动队列/后台 exporter 模式，定义背压、flush 屏障、崩溃丢失窗口与未确认状态，再测 10 组。尚未实现该异步模式。
3. 减少重复 JSON 序列化和逐事件 SQL 查询；不可通过信任 Agent 自报身份或关闭校验来提速。
4. 语义分段、火焰图构建等放到离线查询路径；避免在每个 Agent action 的热路径调用分析模型。
5. 对齐真实 Agent 工作负载复现 AgentSight，分开报告墙钟、CPU、RSS、存储、事件确认/丢失与完整采集范围。不能仅用更长的模型等待稀释百分比。

## 对需求六项的覆盖

| 需求 | 状态与缺口 |
|---|---|
| 行为 trace | 已有模型/工具/子 Agent span、父子/因果/并行/循环字段；仍需框架显式接入，缺 LangChain 自动 callback |
| 模型耗时分解 | 请求/流式客户端计时与服务端 metrics 解析已做；真实 provider 排队/推理缺值仍未知，网络不能独立分解 |
| 工具/MCP 耗时 | E2E、内部执行、显式 phase、三类 MCP 操作已做；没有自动覆盖所有审批与六阶段编排 |
| 系统活动/资源 | process run 关联、Session cgroup CPU、同步 thread CPU 已有；文件/网络对象级 eBPF 与 action 关联、并发细粒度资源归属未完成 |
| 自身开销 | 有 10 组开关对照、CPU/RSS、序列化/存储字节、确认/失败/pending；未发送事件、内核 ring loss、完整 BPF 开销仍未知 |
| 单次/跨次 profiling | 显式语义路径聚合、并行区间去重、folded 导出；缺自动语义分段、完整 pprof 与跨 Agent 全局查询 |

本轮没有改动 rollback/权限策略，不把观测补齐说成状态一致性、安全防护或全部 TODO 已完成。

## 2026-11-01：后台 profiling 优化

这次借鉴旁路采集思路，但未移植 AgentSight 代码，也未改 Core 的同步可靠记录、安全审计或 rollback 语义。
默认仍为 `delivery="sync"`，低扰动 profiling 显式选择 `delivery="background"`。

实现：

- 独立 SDK 导出线程，按批量阈值/最长等待间隔尝试导出；业务线程不逐批等待磁盘，空闲时用条件变量等待，不周期轮询。
- 有界队列：默认 1,024 条 / 1 MiB 编码字节，包含在途批次。满队列背压，超时明确计失败；没有静默丢弃或不确定 ACK 的自动重试。
- collector 仍 WAL/FULL、事务原子性和 Session 身份校验；入队不计 ACK，只有收到持久化确认才增加 acknowledged。
- `barrier()` 在 best-effort 下也 fail-closed；导出 parent context、后台 run/turn 结束自动经过屏障，context 关闭排空并停止线程。
- `tracer.checkpoint(create_checkpoint, ...)` 先确认当前 Tracer 之前提交的记录，再调用可信 checkpoint 函数。不是全局拦截器，不替其他生产者排空，也不赋予 Agent 管理权限。
- endpoint 关闭主动唤醒 selector，消除原先最多约 500 ms 的固定等待；不增加常驻轮询频率。

边界：内存队列仍不是崩溃持久化日志；SIGKILL/崩溃可丢失从未提交的记录。未使用 SDK context/屏障的调用者不能获得其保证。
此模式只用于可观测性，不能把尚未 ACK 的 profiling 数据当成可靠回退日志。全部实验没有改变 SQLite synchronous 设置。

负面结果同样保留：只做后台化的首轮 10 组中，Session 均值 1.657 → 1.652 秒，未观察到明显整体改善；
工具循环含 flush 为 321.67 → 243.75 ms，但部分等待转移到末尾。随后试过只读 SDK zip；
在 10 组配对中导入/初始化反而从目录版约 116.56 ms 到 zip 版 127.23 ms，Session 差值 CI 跨零。
该打包方案已移除，没有把未证实收益的复杂度留在实现中。探索数据保留在远端 `results/`、`results-v2/`。

最后一轮使用完全移除 zip 后的代码，固定 40 次相同工具操作，10 组、每组 5 种模式：
无监控、旧 wheel 同步 32、新同步 32、新后台 32、新后台 64。每种模式各占每个运行顺序位置两次。
旧 wheel 是 10 月 31 日构建产物，源码和 wheel SHA256 随环境数据保存；没有用当前代码冒充旧版。
所有主计时均包含最终 close/flush 和 worker 线程退出；另行记录前台循环，不以它冒充持久化端到端耗时。

### 最终结果（后 24 核，10 组）

| 范围（均值） | 无监控 | 旧同步 32 | 新同步 32 | 新后台 32 | 新后台 64 |
|---|---:|---:|---:|---:|---:|
| 40 次工具循环，含最终持久化 | 50.32 ms | 328.29 ms | 293.29 ms | 362.83 ms | **176.11 ms** |
| Session 执行，不含服务初始化/关闭 | 1,075.15 ms | 1,731.90 ms | 1,635.93 ms | 1,731.67 ms | **1,503.04 ms** |
| 整 worker，含初始化/关闭，不含 VM 启动 | 1,931.77 ms | 3,279.64 ms | 2,939.04 ms | 3,127.03 ms | **2,765.81 ms** |

后台 64 相对旧同步 32：工具循环总耗时下降 46.4%，Session 总耗时下降 13.2%，整 worker 总耗时下降 15.7%。
相对同组无监控值，Session **额外**耗时从 656.74 → 427.89 ms（减少约 34.8%），
整 worker 额外耗时从 1,347.87 → 834.03 ms（减少约 38.1%）。不要把“额外耗时降幅”当成全部耗时降幅。
Session 直接配对差值为 -228.86 ms，近似 t 95% CI 为 -391.44～-66.27 ms。
新模式相对无监控仍有 **+39.8% Session 增幅**，尚未达到低开销生产目标；10 组是非独占机器的小样本。

后台 32 本轮没有改善 Session 均值，且工具循环受一次明显长尾影响；全部样本保留，未删异常值。
后台 64 的收益同时包含后台重叠、批次增大以及 endpoint 退出优化，不是“单纯换线程就降低 35%”。
后台 32/64 前台循环约 61 ms，但把最终持久化算上后分别 363/176 ms，说明主要剩余等待在可靠 flush。
64 模式每次执行总事务数为 5（3 个 SDK 批次 + 2 个 Core 事务），同步 32 是 7；事件量不变。

Session 窗口 CPU：旧版 270.55 → 后台 64 的 242.06 ms；相对无监控仍多 48.73 ms。
daemon 峰值 RSS 旧版约 24.50 MiB、新版约 23.39 MiB；工作负载进程约 14.79 → 15.04 MiB。
它们是独立进程的峰值，不是 VM 总 CPU/内存。SDK 导入/初始化仍约 123 ms，服务初始化仍约 403 ms，
所以后续应测长驻服务的热运行、减少重复初始化，并继续优化同步持久化的事务合并；不能只把成本移出计时。

### 验证与证据

- Host：**241 passed / 20 skipped**；Guest：**71 passed**；另有 3 个真实 Bubblewrap/cgroup/Session socket 执行场景全部通过，后台数据排空。
- 新增测试覆盖：定时导出、背压恢复、在途字节上限、并发生产、失联/不完整 ACK、提交成功但 ACK 丢失不重试、fork 拒用、关闭 fencing、turn/context/checkpoint 屏障。
- 每个监控模式每次执行均确认 162 个事件 / 81 个 span，每种模式 10 次共 1,620 个事件；校验与身份归属全部通过。
- Ruff、Pyright、Core/插件 wheel 构建通过。未跑真实 Qwen、未加载完整 eBPF，也未验证 Btrfs rollback；本轮是同负载的受控性能实验。
- 默认可靠同步模式不变；建议有明确 flush/turn 边界的普通 profiling 显式选择 `delivery="background", batch_size=64`。

原始证据：[汇总](evidence/monitoring-background-20261101/summary.json)、
[10 组五模式](evidence/monitoring-background-20261101/guest-benchmark.json)、
[环境和新旧 wheel SHA256](evidence/monitoring-background-20261101/environment.json)、
[Host 测试](evidence/monitoring-background-20261101/host-pytest.log)、
[Guest 测试](evidence/monitoring-background-20261101/guest-pytest.log)、
[真实进程报告](evidence/monitoring-background-20261101/process-report.json)。
同目录保存复现实验/分析脚本；远端工作区为 `/data/hy/Allox/monitoring-background-20261101-v1`，
最终数据在 `results-v3/`。原 dirty main 保持不动；上述实验完成时尚未提交或推送，后续发布状态以 Git 历史为准。
