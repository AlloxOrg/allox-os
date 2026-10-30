# main 合并与监控实测（2026-10-30）

## 结论

`observer-system-effects-plugin` 已合并、非强制推送至 GitHub main：
[`44977ad`](https://github.com/AlloxOrg/allox-os/commit/44977adf21d2734cad412d4185d14aa7447717aa)。
保留了 main 的参数校验和类型检查，修正可选 recovery 参数兼容；pidfd 测试按能力跳过，并在真实 Guest 中补测通过。

监控插件已在此 main 基础上适配、测试，位于本地 `feat/agent-monitoring-profiling`，
本轮没有把它一并推入 main。原 `allox-os` 目录的未提交联合回退改动未覆盖。

所有构建、运行在 117 `/data/hy/Allox/agent-monitoring-main-20261030-v2`；按本轮用户要求不绑核。

## 结果与证据

以下链接均指向 [本轮原始结果目录](agent-monitoring-integration-results-20261030/)。

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| 纯合并版 main，核心 + system-effects | 162 通过、20 环境相关跳过 | `main-merge-results/pytest.log` |
| 监控适配版，核心 + 插件 + bridge | 203 通过、20 环境相关跳过 | `validation-results/pytest-final.log` |
| Kata 内监控/bridge + pidfd 生命周期 | 42 全通过；不是整个测试集 | `guest-unit-results/pytest.log` |
| 真实 Guest 三个 Agent/session 组合 | 全部通过，含两项跨 session profiling 检查 | `contract-results/contract-report.json` |
| 真实 Codex + 实验室 Qwen，两轮任务 | 9 项验收全部通过 | `real-agent-results/allox-monitor-lab-20261030T102053Z/report.json` |
| Ruff、Pyright、Core/plugin wheel | 通过；Pyright 为本地纯静态检查 | Ruff/build 原始日志，Pyright 0 errors/0 warnings |

## 真实智能体

使用已有 `qwen-codex-exec` 外壳和实验室 `qwen3.8-27b-int4`，Agent 进程、shell 及其子进程均在
真实 Kata Guest 的 Bubblewrap/session cgroup 内。第一轮执行任务得到 55；第二轮恢复同一
Codex thread，使用仅在对话中提供的标记，生成 56。两轮 exit code 均为 0，共实际执行 3 条 shell 命令。

采集到 **14 个 span**：1 run、2 turn、5 次实际模型请求、2 个 SDK tool、2 个内部执行 phase、
2 个 Core process。全部收到的 span 已结束。Session SDK 事件经真实 Unix socket 上报，
process run 由 collector 绑定；Core CPU 计数分别为 1,340,615 与 904,711 微秒。

模型服务只提供 Chat Completions，`/responses` 为 404。因此测试侧使用兼容桥：保留真实工具定义，
处理 Qwen 的 system message 顺序、明确且合法的 XML 工具调用，以及 argv 数组到命令字符串的等价转换。
仅暴露 shell 所需工具，不把普通文字命令当工具执行；有 8 项 bridge 测试。没有修改模型返回值来伪造结果。
兼容桥的 Responses/SSE 配置参考 [official OpenAI documentation](https://learn.chatgpt.com/docs/config-file/config-reference)。

LLM Key 只通过 SSH stdin 交给 117 临时代理，在内存中使用；不写入 Guest、workspace 或 Git。
本轮产物已做实际 Key 字符串扫描，未发现泄漏。没有改动原模型服务，也没有启动 CPU 模型副本。

前五轮失败尝试保留在同目录：依次暴露旧 shim/工具名称兼容、system message 顺序不兼容、
模型输出文字调用/错误 argv 格式等问题。它们不计作成功；最终验收以 `...T102053Z` 为准。

## 真实身份与跨 session 检查

`agent-a/s1`、`agent-a/s2`、`agent-b/s1` 在同一 Guest 并发执行。验证：

- 各自的同名文件和监控记录没有串线；SDK 事件归属正确的 agent/session/process run。
- 无管理 token；显式伪造 agent/session 的上报被拒绝。
- 子进程实际执行，CPU 计数非零；错误 span、工具内部耗时均记录，最终无 running span。
- 同一 trace ID 在不同 session 下不混淆；同一 Agent 的两条 session 可按语义路径聚合为两次 probe。

这里使用 DirectoryBackend 测试 workspace 布局，**没有测试 Btrfs snapshot/rollback**，也不是安全边界完备性证明。

## 尚未完成，不能称为全覆盖

1. **不是无埋点的全过程监控。** 模型经测试代理计时，任务工具经 SDK 埋点；Codex 的全部任意工具内部
   执行、MCP 各阶段、任意文件/网络动作尚未自动关联。3 条 shell 命令并不等于采集了 3 个 SDK tool span。
2. **Trace 仍有断点。** 两个 Session SDK Tracer 独立生成 trace，经 `process_run_id` 与 Core 关联，
   不会自动成为同一棵父子树。本次一个逻辑任务在 profile 中显示 3 个 trace/run；这不是执行了三个任务。
3. **Self time 不完整。** Core 和框架 clock ID 不同，两个 turn 的 self time 为 unknown，
   因而整体 `profile.complete=false`。所有收到的 span 已结束，不应把这个标记误读成已证实丢事件，
   也不能宣称完整时序已经解决。
4. **资源只实测到 CPU。** Guest 未提供 memory peak、block I/O 计数，记录明确列出 unavailable；
   模型服务未提供 queue/TTFT/generation 分项，保持 unknown，不能用客户端总耗时推算。
5. **真实 eBPF 未接入本轮 trace。** 当前 Guest 6.1.62 缺少所需 BTF 等设施，未加载 system-effects BPF，
   不能用本轮结果声称文件、网络或内核系统副作用已完整采到。

## Profiling 自身开销

5 组配对，每组 40 次 CPU+等待合成工具调用。总耗时增幅中位数 **5411.1%**，即约 **55.1 倍**。
见 `validation-results/benchmark.json`。这是同步 SQLite WAL/FULL 逐事件落盘负载，
不是实际 LLM 任务的开销比例，也不是 eBPF 开销；当前不适合作为低开销生产采集器。

该实验中提交事件均确认，SDK failed_events 为 0，但无法证明从未提交的事件是否丢失；端到端事件丢失率
及计时误差上界仍为 unknown。优先改进方向是有界批处理/异步 exporter、明确 flush/崩溃边界，随后打通
trace 上下文与 clock domain，再接入内核文件/网络事件和 provider 分项指标。

测试临时容器及代理已退出，实验结果与构建缓存保留。
