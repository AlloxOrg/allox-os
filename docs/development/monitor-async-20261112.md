# 监控异步落盘：117 实测（2026-11-12）

后续新增可选的 [多 action / 时间窗口策略](monitor-actions-20261112.md)，显式放宽普通监控的逐 action
持久化要求。本文保留原始逐 action 屏障下的对照结果，不与新策略混淆。

## 结论

新增可选 collector 侧异步队列和同 channel group commit，默认不开启。
连续工具调用的总耗时中位数下降约 30%–35%，主要来自 FULL 提交次数从 5 次降为 2 次。
不是所有场景都加速：真实 Unix socket、每 action 都有持久化边界时，中位数反而增加约 20%。
原 background 模式前台已经不等待提交，新模式的前台返回时间没有改善。

## 对照方法

- 通过跳板连接 117；目录 `/data/hy/Allox/monitor-async-20261112`。
- 每个工作进程启动前重新读取 online CPU，数值排序后用 taskset 绑定末 24 核；本轮为 24–47。
- SQLite 数据与临时目录位于 `/data` 的 XFS `/dev/sdb1`，WAL + synchronous=FULL；没有用 tmpfs 替代磁盘提交。
- 原负载：40 次 `sum(i*i for i in range(1000)) + sleep(0.001)`，batch_size=32。
- 每个场景 10 组；每组 off/sync/background/queued 顺序轮换，串行运行，共 160 个工作进程。
- 连续场景为 160 个 SDK 事件；每 action 一个 turn 的场景为 240 个事件，三种模式都明确执行同样的 turn durability barrier。
- direct 是进程内 collector adapter；socket 是真实 AF_UNIX 请求和 SO_PEERCRED 路径，cgroup owns/lease 使用测试替身，不是实际 Kata/cgroup 成本。
- socket 场景的 Core process start 在计时前写入；下表提交次数扣除这 1 次严格提交。
- 时间从工具循环开始到 `Tracer.close()` 和 queued writer 排空结束；包含最终 flush。
  不包含 DB/endpoint 初始化、读回 oracle、DB 关闭及 endpoint teardown。
- 每次重新打开数据库，核对全部 SDK span 数量、完成状态，以及 SDK attempted=acknowledged、health complete。

## 结果

下表均为 10 组总耗时中位数，单位 ms；百分比为两模式中位数之比，不是置信区间。

| 场景 | 关闭监控 | 同步批量 | 原 background | 新 queued | queued 相对 background | FULL 提交次数 background → queued |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| direct，连续调用 | 51.18 | 249.63 | 212.12 | 138.53 | 降低 34.7% | 5 → 2 |
| socket，连续调用 | 51.12 | 288.30 | 219.59 | 153.97 | 降低 29.9% | 5 → 2 |
| direct，每 action turn | 51.09 | 1292.03 | 1529.54 | 1418.95 | 降低 7.2%，波动较大 | 40 → 40 |
| socket，每 action turn | 50.51 | 1320.15 | 1361.51 | 1638.03 | **增加 20.3%** | 40 → 40 |

连续调用的前台返回/最终 flush 中位数（两项中位数不必恰好等于总耗时中位数）：

| 场景 | 原 background 前台 / flush | 新 queued 前台 / flush |
| --- | ---: | ---: |
| direct | 70.75 / 141.78 | 77.90 / 60.76 |
| socket | 71.66 / 148.22 | 85.89 / 68.09 |

新模式仍逐批进行一次入队 RPC，增加编码、回执检查和 collector 排队开销；不是零成本发送。
若 turn 内只有一个 action，flush 会立即结束合并窗口：仍需 40 次 FULL 提交，socket 又多一个 flush 请求，收益无法保证。
不能以减少 turn 屏障、降低 FULL 同步级别来声称安全性相同的加速。

117 为共享机器，磁盘波动明显，未剔除任何样本。例如连续 socket 组包含原 background 1544.8ms、
新 queued 912.6ms 的离群样本，不能把中位数改善当作每次调用或尾延迟保证。
socket 连续调用的逐组改善率中位数为 33.8%；socket turn 场景逐组变化中位数为变慢 29.4%。
本轮没有真实 LLM、eBPF 捕获或新 Kata 实验，不能外推为完整智能体任务加速比例。

## 持久化与隔离验证

全套：**354 passed, 21 skipped**；Ruff 通过，Pyright 0 errors / 0 warnings；Core 与监控 wheel 构建通过。
新队列的测试覆盖：

- 入队立即返回 durable=false；阻塞磁盘提交时 flush 不能提前成功。
- 多批次同 channel 原子提交；输入深复制；无跨 Session/source/run 合并。
- 一个 channel 非法事件使该组整体回滚，不影响其他 Session 提交；错误状态保持 incomplete。
- 队列限额/背压、并发入队、关闭排空、封闭 channel、fork 后禁止复用。
- 拒绝外部 channel/旧实例/未来序号/伪造 durable ACK；软模式 checkpoint 仍 fail-closed。
- turn、context、checkpoint、正常退出边界；真实 Session socket 队列协议、查询/导出前排空、Core execution 结束排空。
- 子进程 `os._exit(0)` 强制绕过关闭：未提交尾部确实消失，新 collector 拒绝旧回执。
- 120 个开启监控的性能样本全部通过重开数据库后的 SDK span oracle 与 durable ACK 计数核对。

崩溃测试不等于断电测试。没有承诺未 flush 尾部零丢失，也没有更改 system-effects 或恢复安全日志的同步写入。
collector 的边界只能排空它已收到的数据，不能替其他 Agent 进程发送尚在本地的 SDK 缓冲。

## 启用与边界

daemon：`--monitor-root <path> --monitor-async-write`。
Agent：`Tracer(SessionQueueTransport(), delivery="queued", batch_size=32, strict=True)`。
正常 context 退出会 flush，run/turn 退出自动执行 fail-closed barrier。
详细回执协议、容量和崩溃边界见 [插件说明](../../plugins/agent-monitor/README.md)。

默认队列 4096 事件/8 MiB，单 channel 1024 事件，最多 1024 channel；元数据不自动淘汰，
达到上限明确拒绝，需要生命周期清理/容量运维后才能用于无限长运行。保留为实验性开关，不全局替换默认模式。

实验完成时，源码与上轮修复同在 `fix/recovery-safety-performance` 工作区，尚未提交或推送；最终实现随本记录一同发布。

## 复现与原始记录

- [完整逐组 JSON](evidence/monitor-async-20261112/benchmark.json)
- [执行日志及中位数](evidence/monitor-async-20261112/benchmark.log)
- [最终校验及构建日志](evidence/monitor-async-20261112/validation-final.log)
- [117 验证入口](../../examples/monitor_async/validate.py)
- [117 四场景对照入口](../../examples/monitor_async/benchmark.py)
- [单工作进程负载](../../examples/monitoring_benchmark.py)
