# Observer／探针常驻复用实验（2026-11-15）

## 结果

已实现可选的 `resident_observer: true`；默认仍走单次模式。
在 117 的独立 Kata Guest、真实 Btrfs 上，使用上一轮相同的 12-action 固定负载，
分别完成旧生命周期与常驻生命周期各 **10 组开关对照，共 40 次运行**。
全部通过原有负载、SDK、身份、封存、校验和事后关联检查；不调用 LLM。

| 完整 run（中位数） | 每次启动／退出 Observer | 常驻 Observer／主探针 |
| --- | ---: | ---: |
| 关闭监控 | 3375.17 ms | 3388.51 ms |
| 开启监控 | 7456.61 ms | 5186.08 ms |
| 逐组配对增加延迟的中位数 | +3956.71 ms | +1812.15 ms |
| 逐组配对增加延迟的平均数 | +4021.71 ms | +1853.45 ms |

**配对额外延迟中位数降低 54.2%；开启监控的完整 run 中位数降低 30.4%。**
中位数之差不等于配对差值的中位数。

常驻首次冷启动 **包含在这 10 次内**，耗时 5425.83 ms；其余 9 次中位数
5101.98 ms。最后统一释放探针耗时 **1850.17 ms**，exit=0、无 poisoned 状态。
把最后关闭成本摊入这 10 次后：

- 常驻开启完整 run 平均 **5432.44 ms**，对比单次模式 7415.27 ms，降低 26.7%。
- 配对平均额外成本从 **4021.71 ms 降至 2038.47 ms，降低 49.3%**。

任务循环＋SDK close 中位数：单次模式 off/on 为 1982.68/2228.11 ms；常驻模式
为 1895.65/2191.60 ms。主要改善发生在完整生命周期，未删减任务内观测。
这些是本轮实测，不是每个内部阶段的独立计时；剩余约 2 秒不可全部归因到某个函数。
持久化封存、独立 verifier、每次排空及两条异步 TCP link 的封口仍然保留。

## 保留的边界

- Worker、主 links/programs/maps 常驻；每个 run 新建身份绑定、挑战 nonce、
  ring consumer、回调状态、WAL、JSONL、证据目录。Agent 无 Worker 控制接口。
- Core 仍证明对应 cgroup 空且冻结，完成排空、注销、状态 map／root slot 清理，
  并持久化封存。两条异步 TCP producer link 仍逐次卸载封口、下次重新挂载。
- 原生 FINISHED 后增加绑定 run/epoch/nonce/报告及 manifest 摘要的 IDLE ACK。
  Python 收到有效 ACK 才向原有 v2 runner 放行 FINISHED；独立 verifier、fsync
  和现有 host lock 不变。ACK 不冒充 Worker OS 进程退出。
- 错误或不能证明清空即毒化并终止 Worker，不自动重启／降级。Core 保留失败封禁。
  4096-run 防重放上限、配置更改需要显式关闭重建；不支持 fork 后复用。
- 本轮仅 balanced / expanded partial / observation-only v2；无可选全局状态 scope。
  recovery v3 保留原单次路径，不能把这组结果推广到所有模式。

## 安全与证据检查

正式 10 个常驻 run 使用同一 Worker PID；首个 `reused=false`，后 9 个为 true。
主探针的 program/map/link IDs 完全一致；不是仅保留空进程、内部仍每次重建探针。

独立真实内核故障实验依次运行 agent-a/s1、agent-b/s1、agent-a/s2，复用同一 Worker
及内核对象，分别使用独立 Store 的身份绑定；**不是共享 Store 并发隔离攻击实验**。
后续 run 及故障注入后，之前 manifest 列出的文件 SHA256 仍不变。
另外在同一 Store／同一 Session 连续执行两次，实测 epoch 递增、run ID 不同，
同一 Worker／主探针继续复用，两份封存文件在故障后仍保持不变。
随后对下一 run 故意发送前一个成功 run 的 FINISH nonce，实测：

- 原生协议拒绝，Worker 非零退出并被标记 poisoned；
- 该 run 失败、Session 被封禁、无成功 receipt、故障 cgroup 已清空；
- 下一 Session 命令在执行前被拒绝，测试 marker 未创建。

正式 Guest 及 Worker 退出后，以只读挂载再次运行原生 verifier：**20/20 VERIFIED**；
再分页关联两类原始流：**40/40 stream 查询通过**，身份、摘要和 turn 结论保持一致。
全套回归 **395 passed、21 skipped**，Ruff 通过；仓库既有类型检查范围为
0 errors / 0 warnings（该配置未包含 system-effects 插件，不冒充其完整静态检查）。
补丁输入／输出字节校验及
精确结果的幂等检查通过，见 evidence 中的最终 validation log。
协议测试覆盖错误 nonce/hash/epoch/sequence、对象变化、缺失 ACK、提前 EOF、
非法输出、重复 FINISH、并发 begin、fork 及独立 verifier 失败后的禁止复用。

## 测量方法与限制

沿用上一轮 Guest、对象文件及 12-action 负载，二进制同一构建分别选择单次／常驻。
完整 run 含启动绑定、SDK 排空、观察结束与验证；不含 VM 启动、Store/DB 初始化、
事后关联查询。没有把完整监控成本冒充纯 eBPF hook 开销。

先跑 10 组单次 on/off，确保这时无常驻探针；再加载常驻 Worker 跑 10 组 resident/off。
每组顺序交替。否则常驻 link 会使单次模式不再承担最后一条 ftrace link 的卸载等待，
导致参考成本被低估。常驻阶段的 off 是 SDK／采集关闭但 hooks 空闲存在；
基线阶段 off 不含这些 hooks，两个 off 均值恰好接近，不代表任何负载下 idle 成本为零。
阶段间的缓存、共享宿主负载等仍未完全控制；10 次不是尾延迟保证。

每次构建／实验重读 online CPU，数值排序取最后 24 个，本轮为 24–47。
独立 Kata 的 shim／QEMU 线程在释放 Guest 负载前绑定；Guest 为 4 vCPU／4 GiB。
未替换共享宿主内核、重启服务或更改全局 Kata 配置。

观测语义仍为 PARTIAL／INCOMPLETE：balanced 不覆盖所有 read、mmap、io_uring，
生命周期无完整权威 S0，跨 daemon 委托因果和精确 tool-turn 归属仍不能推断。
所有测试均要求保留这些保守标签。这是有限观测契约和故障拒绝测试，不是任意行为
完整捕获、断电恢复、跨租户对抗或所有内核对象零残留的证明。

## 复现与产物

- 实现及复现入口：[examples/monitor_resident](../../examples/monitor_resident/README.md)。
- 原生 GPL Observer 补丁、基线及结果文件 SHA256：
  [observer/resident-v1.json](../../plugins/system-effects/observer/resident-v1.json)。
- 构建／新二进制目录：117 `/data/hy/Allox/monitor-resident-20261115`。
- 正式原始数据：117 `/data/hy/Allox/monitor-e2e-20261114/attempt-be59046548`。
- 故障及同 Session 连续 epoch 实验：117
  `/data/hy/Allox/monitor-e2e-20261114/attempt-8038072d55`。
  初轮故障实验 `attempt-5fbbc47c6a` 也保留在服务器。
- 紧凑报告：[evidence/monitor-resident-20261115](evidence/monitor-resident-20261115/)。
  完整 WAL／JSONL 留存 117，未复制密钥、连接信息或全部原始流入仓库。

实验完成时未自动提交或推送；后续按用户要求发布。
