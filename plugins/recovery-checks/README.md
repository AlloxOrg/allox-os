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
WAL + synchronous=FULL；这里的持久性指 workspace 回退/daemon 重启，不是
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
- 每个 checkpoint 需读取文件树，每次回退需读取快照与恢复树，开销与数据量相关；
  **不是已证明低开销的常开监控功能**。

fence 在调用 Core 前先落盘；异常或重启后保留。首次 checkpoint 在基线落盘前失败
且没有任何旧基线时，Session 保持隔离，需管理员修复/重建；v1 不自动信任当前状态。
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
