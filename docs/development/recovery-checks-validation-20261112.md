# 恢复检查初版验证 · 2026-11-12

基于 main `1710893`，工作分支 `feat/recovery-checks`；默认关闭的独立发行包
`allox-recovery-checks`。设计、来源和未覆盖范围见
[插件说明](../../plugins/recovery-checks/README.md)。

## 运行位置

117，经用户配置的跳板连接，工作目录 `/data/hy/Allox/recovery-checks-20261112`。
每个测试/构建任务重新解析在线 CPU 并绑定最后 24 个逻辑 ID，本次为 24–47。
没有修改宿主机默认 Kata 配置，也未构建或运行本地沙箱。

## 回归与打包

- 专项 27 项：联合回退、重复回退、跨 Agent/Session 隔离、未知语义覆盖拒绝、
  pending/committed/aborted effect、稳定 operation ID、文件/记忆故障注入、
  中断后 fence 跨 daemon 重启、检查期间禁止新租约且无锁顺序死锁、拒绝部分
  scope/目标前缀、快照篡改、错误 checkpoint head、硬链接/软链接/权限、特殊节点及扫描限额。
- 有限历史：shared-prefix 的 `2 → 1 → 0` 淘汰链、相同 trace 的 outcome 身份保留，
  并将小模型结果与独立的“穷举所有子族”oracle 比较（36 个输入配置）。
- 全量 Core + system-effects 测试 319 通过、21 个环境相关跳过。
- Ruff E4/E7/E9/F/I 检查；Core 与插件 wheel 构建；独立环境只安装两只 wheel，
  验证插件 entry point 与有限历史接口，不依赖采集器。
- **Pyright 未运行成功**：117 宿主机没有 Node.js，Python wrapper 下载 Node 时
  遭遇证书校验失败。没有禁用 TLS 校验；最终脚本对无 Node 环境明确记录跳过。

## Kata + 真实 Btrfs

使用已有独立测试内核 `6.1.62-allox-joint-btrfs`，新建 network-none Kata Guest。
Guest 中是实际 Btrfs backend、cgroup v2、Bubblewrap 及长驻 Unix socket listener，
不是目录复制 backend 或假的 process service。

1. 同一 Agent 下 target/control 两个 Session，各绑定独立文件和序列化记忆 checkpoint。
2. control 启动长驻 listener，target 连续三次修改文件/记忆并启动旧分支 listener。
3. target 回退后自动断言为 scoped consistent；额外通过 pidfd 验证旧 listener 退出，
   cgroup 为空，旧 `/tmp` listener 不可达；control 同一 PID 持续响应。
4. 负例：故意在文件恢复成功后写入错误记忆，必须判为不一致、禁止新执行租约，
   control 不受影响；重新执行正确联合回退后解除 fence。
5. 外部 provider 用 workspace 外的提交文件模拟，prepare→commit→settle 后回退
   必须在杀进程之前被拒绝，target/control 原 listener 都仍然存活。

这是**确定性系统工作负载**，没有在本轮调用 Qwen/运行真实 LLM Agent；
provider 是本地 fixture，不代表已经集成支付/云 API 的生产适配器。
底层为 Guest tmpfs 上的 loop Btrfs，不声称验证物理盘掉电、网络服务状态恢复
或任意 Agent heap/FD 恢复。v1 仍采用 runtime reset，而非进程 checkpoint resume。

## 复现与证据

[117 测试/构建入口](../../examples/recovery_checks/validate_117.py)、
[Kata 启动器](../../examples/recovery_checks/run_guest_117.py)、
[Guest 验证用例](../../examples/recovery_checks/guest.py)。
启动器引用 117 上已有的 Kata 配置、Btrfs 测试内核、工具及旧联合实验 helper；
并非适用于任意新服务器的一键部署包。

最终运行日志和 Guest 报告保存在本目录 `evidence/recovery-checks-20261112/`。
原始实验目录同时保留失败的环境/启动尝试；不得将失败尝试算作通过结果。

- [最终测试、构建与独立安装日志](evidence/recovery-checks-20261112/validation-final.log)
- [最终 Guest 控制台](evidence/recovery-checks-20261112/guest-final.log)
- [最终 Guest 结构化报告](evidence/recovery-checks-20261112/guest-3t4i2stm/report.json)
