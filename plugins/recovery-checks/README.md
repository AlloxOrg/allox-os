# Recovery checks · opt-in v1

将恢复校验与恢复执行分开：Core 执行原有 Btrfs/记忆回退，独立插件负责
回退前准入、回退后断言及失败后的持久化执行禁令。不新增 eBPF 探针，
不挂载任何管理 socket 到 Agent，不改变默认未启用时的回退行为。

## 启用

安装 Core 与独立发行包 `allox-recovery-checks`，daemon 加：

```sh
allox-workspace-daemon --root /srv/allox-workspaces \
  --listen 127.0.0.1:8092 --session-memory \
  --recovery-checks-root /var/lib/allox-recovery \
  --process-audit-root /var/lib/allox-audit \
  --kill-session-processes-on-rollback
```

必须通过环境变量配置 `ALLOX_WORKSPACE_TOKEN`。证据目录必须位于 workspace
存储外、不得暴露给 Agent，并与唯一的 workspace store 绑定。SQLite 使用
WAL + synchronous=FULL；同一 store 与 evidence root 均只允许一个活动恢复管理实例。
第二实例会在 journal 恢复/插件启用前被拒绝，而不是依赖各自的内存禁令缓存。
这里的持久性指 workspace 回退/daemon 重启，不是
整个 VM snapshot 回退后的外部防回滚锚点。

## 提炼了什么

1. ZDTM：建立期望状态 → 修改/恢复 → 独立断言，不以恢复 API 返回成功为判据。
   文件断言覆盖相对路径、类型、权限、UID/GID、内容 SHA256、软链接目标及
   scope 内的硬链接拓扑；不以 inode/PID 数值相等冒充语义等价。
   记忆断言比较 checkpoint 绑定版本与规范化内容摘要，允许 revision 单调递增。
2. Agent history safety：不可逆副作用的记录不随 workspace 回退，已提交的
   operation 不因旧逻辑状态复活而重新视为新操作；pending/结果不确定的请求
   阻止恢复。第一版不提供 replay proxy、补偿执行或自动 fork。
3. 有限历史诊断器：对显式枚举的 outcome/完整词及 prefix-closed policy，
   按“每个前缀仍兼容的所有 outcome 都必须存在可完成后继”计算最大不动点。
   保留 outcome 身份、淘汰轮次和反例前缀，不采用不充分的“每分支各有一条
   安全路径”。这是独立诊断 RPC，**不是现场执行授权证书**。

## 管理 RPC

以下接口只供可信管理端/适配器使用，均走现有认证 `/v1/rpc`。

- `recovery.coverage`：Agent/Session 创建后、第一个 effect/checkpoint 前，
  传入 `adapter_id`、`complete=true`，固定完整 effect ledger 的适配器契约。
  **这是受信任适配器的覆盖声明，不是插件自动证明了没有漏事件。**
  未声明时可保存 checkpoint，但 semantic rollback 结果为 INCONCLUSIVE 并拒绝执行。
- `recovery.effect.prepare`：`operation_id` + 64 位十六进制 `fingerprint`，
  在外部请求发出前持久化 pending；不保存原始参数或凭证，不发放授权。
- `recovery.effect.settle`：相同 ID，`outcome=committed|aborted`。
  aborted 必须是确认未提交，不能把超时/响应丢失当 aborted；不确定时保持 pending。
- 重复 prepare 返回 `dispatch_new=false`，要求按原 ID 向 provider 查询/协调；
  相同 ID 换指纹被拒绝。ID 在 Agent/Session 内唯一，适配器对外必须使用带完整
  身份的稳定幂等 ID。这里不是网络层的 exactly-once 执行代理。
- 原有 `checkpoint.create` 自动绑定独立断言基线，无需另一套 checkpoint API。
- 原有 `session.rollback` 要求完整 checkpoint ID，联合恢复文件与记忆；
  配置进程服务时必须显式传 `kill_processes=true`。
  先确认副作用/快照基线，再允许 Core 杀进程和恢复，最后验证、解除禁令。
  在 checkpoint 后新增 committed effect 会被保守拒绝，不会尝试假装撤销外部服务。
- `recovery.status`：查询 fence、最新报告与适配器覆盖声明。
- `recovery.verify`：仅重新校验已中断回退的原目标；只有一致才解除 fence。
  也可重试一个已有基线的 guarded rollback。不存在“直接清空禁令即验证通过”的接口。
- `recovery.admit`：`outcomes` 为 outcome → 完整词列表 → occurrence ID 列表，
  `allowed` 为策略最大词列表。最多 16 outcomes、每 outcome 16 词、词长 8、
  256 个策略词。输入须完整且已经规范化；不解析任意 Agent 代码或推导真实因果图。

`memory.restore` 的单独回退、checkpoint 前缀/祖先解析、files-only 回退在本插件
v1 下拒绝，避免绕过联合断言。未启用插件时原功能保持不变。
v1 不与 system-effects 的内部 A/B/C 自动回退同时启用，避免产生未经过本插件
准入的第二条回退路径；可与 agent-monitor/trace-correlation 共存。

## 一致性的准确含义

报告只有 `CONSISTENT_WITHIN_SCOPE` / `INCONSISTENT_WITHIN_SCOPE` / `INCONCLUSIVE`。
没有进程服务时 runtime_reset 为 NOT_CHECKED，报告 scope 不含该项。

- runtime 采用 **reset-not-resume**：旧 Session cgroup 必须为空，不是恢复
  checkpoint 时刻的堆、FD、监听服务或进程执行点。
- `scrub_runtime=true` 仅将 `.allox-tmp` 的 socket/FIFO 节点从期望文件树去除，
  普通 tmp 文件仍须恢复；检查清理后的文件树，而不是宣称这些 OS 对象已恢复。
  仍需保留的 socket/FIFO 节点将导致 INCONCLUSIVE，要求服务重建/额外检查器。
- 不涵盖 xattr/ACL、时间戳、workspace 根目录属性、挂载及不受管理的写入者，
  也不验证外部数据库/网络服务内容。语义安全只针对完整适配器契约下的已记录 effects。
- 文件断言 no-follow、按目录 FD 访问；软链接只比较目标字符串，不读取它指向的文件。
  每次扫描上限 10,000 条目、256 MiB、64 层；超过上限或不支持的设备节点不算通过。
  通过每实例共享、最多 4 线程的池并行计算完整 SHA256；限制在途文件描述符，
  异常时等待已提交任务关闭自己的 FD。没有以 mtime 或 inode 缓存替代内容校验。
  新 checkpoint 基线只存一份文件树，旧格式仍可读；scrub 期望树按既有策略派生。
- 每个 checkpoint 需读取文件树，每次回退需读取快照与恢复树，开销与数据量相关；
  **不是已证明低开销的常开监控功能**。

临时 reservation 从预检开始阻止新执行。Core 完成只读校验后，必须在首次发信号、
修改快照或写恢复日志之前通知插件持久化 fence；因此依赖 Core mutation boundary ABI 1。
参数错误、重复 checkpoint、无效 reset token、已有 execution lease 等未开始改动的
拒绝只释放临时 reservation，不误封禁。已经进入改动边界后的异常/重启仍保留 fence。
reset token 在整个回退期间被 pin，不能并发 complete 或重入；残留但不在 run 缓存内的
进程也必须按精确 Session cgroup 清理并确认排空。不同 Session 的长检查使用独立锁，
SQLite FULL 提交仍在短共享锁内串行。前缀删除按 Core 返回的完整 ID 清理基线。
首次 checkpoint 已开始修改、却在基线落盘前失败且没有旧基线时，Session 仍隔离，
需管理员修复/重建；v1 不自动信任当前状态。
直接调用 WorkspaceStore、禁用插件或修改证据库属于可信管理员绕过，不受本接口保护。

## 来源与实现边界

- [ZDTM 测试协议](https://criu.org/ZDTM_test_suite)、
  [env00.c](https://github.com/checkpoint-restore/criu/blob/criu-dev/test/zdtm/static/env00.c)。
- [agent-check-restore-safety](https://github.com/eunomia-bpf/agent-check-restore-safety)，
  阅读版本 `c3fbdae3675b7e83b9b1e261eea37d9d4c066d60`，尤其是
  `artifact/exact_history_realization.py` 的 prefix-robust fixed point 与
  `artifact/test_exact_history_realization.py` 的 shared-prefix 反例。

本插件为独立编写的 Allox 实现，没有复制/打包上游代码。没有移植完整 causal
pomset、fresh/alias 求解、Fork/Merge 安装器、provider gateway 或 Lean 证明，
也不宣称继承其理论保证。归一化完整词检查与保守 effect frontier 是首个受限切片。

## 2026-11-12 修复验证

基于 `4ad25c6`，分支 `fix/recovery-safety-performance`。全部构建、测试与基准在
117 `/data/hy/Allox/recovery-fixes-20261112` 执行，每次重读在线 CPU 并绑定末 24 核
（本次 24–47），性能任务串行运行。338 测试通过、21 项环境相关跳过；Ruff 与
Python 3.10/Linux Pyright standard 检查通过。Pyright 使用已有 Node 容器及显式
typeshed 路径，没有禁用 TLS 或修改宿主机全局运行时。

真实 Kata + Btrfs 上重跑原有故障用例：普通拒绝不再误封禁；无效 reset token
不再杀 listener；不同 Session 的状态查询不等待另一 Session 的扫描；第二恢复
实例被拒绝；前缀删除无残留基线。三轮文件/记忆联合恢复及故意记忆损坏后的
禁令仍正确。额外用真实子进程模拟 run 表丢失：遗留 listener 被精确 cgroup 清理，
另一 Session 进程存活。这不是一次完整的 daemon SIGKILL/重启实验。

原负载各 10 组、轮换开关顺序，文件内容、权限、路径、软/硬链接及记忆的独立
oracle 均通过。启用检查时的中位数，单位 ms：

| 文件数 / 容量 | checkpoint 之前 → 修复后 | rollback 之前 → 修复后 |
| --- | ---: | ---: |
| 128 / 4 MiB | 33.87 → 22.61 | 57.21 → 42.16 |
| 1024 / 4 MiB | 120.54 → 106.10 | 185.41 → 171.40 |
| 128 / 32 MiB | 154.22 → 72.60 | 268.96 → 110.46 |
| 1024 / 32 MiB | 259.60 → 127.91 | 401.43 → 221.67 |

检查仍显著慢于不做检查。底层是 Guest tmpfs 上的 loop Btrfs、暖缓存，不代表
物理盘性能；使用了最多 4 个 hash worker，不能当成单核 CPU 工作量减少。前后
为同一共享主机的不同时段，未控制其他租户负载。没有新增真实 LLM/eBPF 采集。

事后关联 fixture 在同一 Correlator 重复查询 1 万事件、每页 50 条的中位数约
82.4 → 13.1 ms；首次查询仍需完整解析。SDK 只减少重复 JSON 编码，磁盘持久化
瓶颈**没有解决**：相同微负载背景模式本轮约 203 → 225 ms/40 次小工具调用，
同步模式约 241 → 241 ms，不能据此声称 SDK 端到端优化成功；所有事件仍已确认。

[结构化对比与诊断](../../docs/development/evidence/recovery-fixes-20261112/fix-report.json)、
[测试/构建/类型检查](../../docs/development/evidence/recovery-fixes-20261112/validation-complete.log)、
[Guest 原始报告](../../docs/development/evidence/recovery-fixes-20261112/guest-3dx3ie8s/report.json)。
复现入口在 `examples/recovery_fixes/`，依赖 117 上保留的基线 audit/Guest fixture，
不是任意新服务器的一键安装器。故意注入延迟的锁测试只证明不串行等待，不能
把其中 250 ms 当作生产环境每次请求的实际延迟。
