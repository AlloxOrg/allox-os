# 多 action / 时间窗口落盘（2026-11-12，117）

## 结论

已增加可选的普通监控持久化策略：累计多个 action 或达到时间窗口，由后台提交。
采用 8 action / 50ms 后，40 action 合成负载的总耗时中位数由原 background 的 1316ms
降为 221ms（约降低 83.2%，含最终 flush）。FULL 提交次数从每组 40 次降为 3–4 次。

**这是放宽普通监控的逐 action 持久化要求，不是相同崩溃保证下的透明加速。**
默认关闭；checkpoint、恢复安全日志、显式持久化屏障不改变。未确认落盘尾部可能在崩溃时丢失。

## 使用

daemon 继续使用 `--monitor-root <path> --monitor-async-write`。SDK 显式启用：

```python
from allox_agent_monitor import Tracer
from allox_agent_monitor.transport import SessionQueueTransport

with Tracer(SessionQueueTransport(), delivery="queued", batch_size=32,
            action_batch_size=8, action_flush_interval=0.05,
            strict=True) as tracer:
    for item in work_items:
        with tracer.span("action", "turn"):
            tracer.call_tool("work", process, item)
```

- turn 结束计作一个完成 action；子 tool/phase 不重复计数。其他框架可改用一次 `action_finished()`。
- 8 个尚未确认的完成 action，或最早未确认事件等待到 50ms，先到即请求后台提交。
- 不需要下一次 action 才检查定时器；持续新事件不重置最早事件的计时；长 action 的中间事件也可提交。
- worker 不在等待磁盘期间占用生产者/health 的锁。容量包含本地排队和在途数据，满时背压并提前请求提交。
- wire 批次上限、collector 的 20ms 计时器和显式边界也可能提前提交；不是每批严格等于 8 个 action。
- 显式 flush/barrier、`tracer.checkpoint(...)`、parent context 导出、run span 结束和正常 close
  仍等待匹配的 FULL durable ACK。collector 的 mutation/export/execution-end 屏障仍排空已收到的记录。
- 外部管理操作不能排空另一个进程尚在 SDK 本地的缓冲。若每个 action 内都调用 checkpoint/context/barrier，仍会频繁落盘。
- 50ms 是尝试开始提交的等待窗口，不是磁盘完成时限，也不是最大丢失窗口。
- Core process 事件、system-effects 和恢复安全日志没有改成这种延迟策略。

实现位于 [ActionBatchExporter](../../plugins/agent-monitor/src/allox_agent_monitor/action_batch.py)；
完整配置边界见 [插件说明](../../plugins/agent-monitor/README.md)。

## 117 对照

通过跳板连接，目录 `/data/hy/Allox/monitor-actions-20261112`。
各工作进程启动前重新读取在线逻辑 CPU，数值排序后 taskset 绑定最后 24 个（本轮 24–47）。
SQLite 数据在 `/data` 的 XFS `/dev/sdb1`，保持 WAL/FULL；只有 Unix socket 放在短 `/tmp` 路径。

每组四模式轮换顺序，串行运行 10 组，共 40 个测量工作进程：

- off：相同业务负载，无监控。
- background：原后台发送，每 action turn barrier。
- queued：上轮 collector 异步模式，仍每 action turn barrier。
- actions：新策略，8 action / 50ms，不再每 turn 等待落盘，最终 close 必须等待。

每个开启监控的工作进程执行 40 次 `sum(i*i for i in range(1000)) + sleep(0.001)`，
每 action 具有 turn/tool/phase 共 6 个事件，总计 240 事件、120 个 SDK span；wire batch_size=32。
使用真实 AF_UNIX 请求、SO_PEERCRED 和 Session 端点协议，但 cgroup owns/lease 使用测试替身，
没有真实 Kata、LLM 或 eBPF 捕获。不能外推为完整智能体任务的加速比例。

计时从业务循环开始，到 Tracer.close 和 queued writer 排空结束；包括最后的 flush。
不计 DB/endpoint 初始化、读回核对、DB close 和 endpoint teardown。
socket 的 Core process start 在计时前写入，下表提交次数扣除这 1 次严格提交。

| 模式 | 总耗时中位数 ms | 前台循环中位数 ms | 最终 flush 中位数 ms | FULL 提交次数中位数 | CPU 中位数 ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| off | 50.84 | 50.84 | 0.00 | 0 | 7.83 |
| background，逐 action 屏障 | 1316.12 | 1316.05 | 0.08 | 40 | 173.10 |
| queued，逐 action 屏障 | 1512.65 | 1512.50 | 0.13 | 40 | 231.41 |
| actions，8 / 50ms | 221.02 | 83.61 | 139.18 | 3.5 | 122.80 |

中位数的前台与最终 flush 相加不必等于总耗时中位数。
新策略相对 background 总耗时中位数降低 83.2%，相对 queued 逐 action 模式降低 85.4%。
相比关闭监控仍有约 170ms 额外时间，不是零开销；共享磁盘波动未控制，全部样本保留，不能把中位数当成尾延迟保证。

新策略十组的提交次数依次为 `4,4,3,4,4,3,4,3,3,3`。
30 个开启监控的测量样本全部重新打开数据库，核对 SDK span 数量与完成状态、SDK attempted=acknowledged。
新策略每组均确认 240 个事件、40 个完成且持久化的 action，health complete。

## 验证

最终全套 **374 passed, 21 skipped**；Ruff 通过，Pyright 0 errors / 0 warnings；Core 与监控 wheel 构建通过。
新增 20 个策略测试用例，覆盖：

- 七个 action 不触发、八个触发；停止调用后计时器排空；没有完成 action 时也定时提交。
- 磁盘提交被阻塞时，新的 action 和 health 不被后台 I/O 锁住。
- context、checkpoint、run、flush、close 均覆盖延迟策略并等待持久化。
- 输入冻结、非法 JSON、错误 ACK、背压失败、fork 后禁止复用、关闭 worker、无重复 action 计数。
- 真实 Session socket 协议下保留 Core 绑定的 session-sdk/process_run_id，不由 SDK 指定身份。
- 失败时 checkpoint 不运行，即使 SDK 是 best-effort 也不能变成成功屏障。

旧的 collector SIGKILL/尾部丢失、旧回执拒绝、跨 Session 事务隔离等测试继续通过。
另跑一次公开 benchmark CLI 的 off/on 小规模 smoke，验证 queued + action 参数可复现。
本轮未新增断电测试，没有未 flush 尾部零丢失保证。

## 原始记录

- [10 组完整 JSON](evidence/monitor-actions-20261112/benchmark.json)
- [运行日志](evidence/monitor-actions-20261112/benchmark.log)
- [最终校验与构建](evidence/monitor-actions-20261112/validation-final.log)
- [公开 benchmark CLI smoke](evidence/monitor-actions-20261112/smoke.json)
- [117 对照入口](../../examples/monitor_async/actions_benchmark.py)
- [117 验证入口](../../examples/monitor_async/validate.py)，传 `/data/hy/Allox/monitor-actions-20261112` 作为首个参数。

实验完成时，实现与上轮修复同在 `fix/recovery-safety-performance` 工作区，尚未提交或推送；最终实现随本记录一同发布。
