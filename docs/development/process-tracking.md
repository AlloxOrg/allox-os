# Agent / Session 进程追踪

进程追踪是可插拔能力，默认关闭。`alloxd` 只依赖
`ProcessTrackingBackend` 协议；内置 `ebpf` adapter 通过版本化 NDJSON
协议连接独立的 `allox-process-tracker` 二进制。因此更新 eBPF 程序不要求修改
workspace/checkpoint 代码，第三方 provider 也可以通过
`allox.process_trackers` Python entry-point 安装。

## 组件边界

```text
alloxd
  ├─ ProcessTrackingService    归属、run 状态、审计持久化
  ├─ ProcessTrackingBackend    可替换 provider 接口
  │    └─ EbpfBackend          collector 控制协议 adapter
  ├─ SessionCgroup             权威成员集合、cgroup.kill
  └─ exec gate                 seed 完成前禁止目标代码运行

allox-process-tracker
  └─ eBPF                      fork 继承，exec/exit 事件
```

这个分工是有意的：eBPF 回答“进程属于哪个 run、发生了什么”，Session cgroup
回答“哪些进程必须一起冻结或停止”。不能用可能丢事件的审计流代替资源所有权边界。

## eBPF 归属模型

实现参考 ActPlane 的
[`ts_proc` / `te_fork`](https://github.com/eunomia-bpf/ActPlane/blob/master/bpf/taint_engine.bpf.h)
和 [sched hooks](https://github.com/eunomia-bpf/ActPlane/blob/master/bpf/process.bpf.c) 模式：

1. `alloxd` 启动一个只负责等待的 exec gate，gate 立即 `SIGSTOP`。
2. 控制面把 gate PID 移入对应 Agent/Session cgroup。
3. provider 从 Session cgroup 的 inode 得到内核 cgroup ID，把控制面生成的随机
   run cookie 写入 `session_domains[cgroup_id]`。这一步不依赖容器 PID 与 Guest
   初始 PID 的数值是否相同。
4. 收到 provider ACK 后，控制面 `SIGCONT`，gate 才会 exec 目标 Agent。
5. sched tracepoint 用 `bpf_get_current_cgroup_id()` 确认 Session 归属，并在
   `process_domains[Guest TID]` 中维护出生标识和父子关系；fork/exec/exit 都记录。
6. 根进程退出后仍等待 Session cgroup 变空，避免遗漏 double-fork、`setsid()`
   或清空环境变量的后代。

归属不依赖 Agent 自报的环境变量、当前 PPID 或 `/proc` 事后扫描。内核 ring buffer
事件只带 opaque cookie，`agent_id/session_id/run_id` 由可信控制面补齐。审计目录必须
位于 workspace store 外，避免被 Session snapshot 一起回退。

线程也有独立记录，事件中的 `pid` 是 Guest 初始 PID namespace 下的任务 TID。
`process_id` 使用 boot ID、run ID、初始 TID 和出生标识，线程 exec 后身份保持连续。
`parent_process_id` 保留创建关系，父进程退出后仍可查询。
collector 统计丢事件/映射失败，发生损失时使后端失败；结束时先确认整个 cgroup
（含嵌套 cgroup）为空，再核对该 run 的内核产出数与已读事件数，最后完成审计。

当前 eBPF 程序位于 `native/process-tracker/`，只使用 sched tracepoint，不依赖
CO-RE 或 Guest BTF。tracepoint 的字段布局不是跨任意内核版本的稳定 ABI，因此发布
Guest Kernel 时必须重新构建并运行真实内核测试。collector 的控制协议 ABI 当前为 1；
adapter 会在启动时核对 ABI，版本不匹配时拒绝启动任务并保持 fail-closed。

## 启用与关闭

关闭时不加载任何 BPF 程序：

```sh
allox-workspace-daemon \
  --root /var/lib/allox/workspaces \
  --process-tracking disabled
```

Allox Guest 的 init/service manager 先准备内核控制面，再启动 daemon：

```sh
allox-guest-bootstrap \
  --cgroup-root /sys/fs/cgroup/allox \
  --tracefs-root /sys/kernel/tracing

allox-workspace-daemon \
  --root /var/lib/allox/workspaces \
  --process-tracking ebpf \
  --process-tracker-command /usr/libexec/allox/allox-process-tracker \
  --process-audit-root /var/lib/allox-process-audit \
  --process-cgroup-root /sys/fs/cgroup/allox \
  --kill-session-processes-on-rollback
```

`allox-guest-bootstrap` 属于 Allox OS Guest，不通过 OpenSandbox 或 Allox CLI
注入。它只应由可信启动环境调用：必要时将 cgroup v2 remount 为可写、建立 Allox
专属子树、挂载 tracefs，并检查三个 sched tracepoint。任一步失败都会阻止追踪服务启动。

collector 需要加载 BPF 并挂载 perf tracepoint 的权限（通常为 Guest 内可信 root，或
经过验证的 `CAP_BPF`、`CAP_PERFMON`/`CAP_SYS_ADMIN` 与 sysctl/LSM 策略组合），
`alloxd` 需要管理指定 cgroup v2 子树。
部署要求 Linux 5.14+、`CONFIG_BPF_SYSCALL`、`CONFIG_BPF_EVENTS`、sched tracepoints
和 cgroup v2 的 `cgroup.kill`。daemon/collector 可以位于 Kata container PID
namespace；Session 归属使用 cgroup ID，审计事件中的 PID 仍是 Guest 初始 namespace
的 TID。
不能只按 kernel 名称推断可用性；必须在实际 Guest 镜像中验证程序加载和 tracepoint
挂载。权限不足时 provider 会拒绝 ready，daemon 不会接受新的 tracked run。

开关在 daemon 启动时生效。升级时先停止活跃 run，正常关闭 daemon，再替换 collector
或 provider 并重启；本版没有运行中的无缝热切换。
这些权限只给 Guest 内的可信服务。tracked launcher 在建立 mount/PID namespace 后
使用 `--cap-drop ALL` 启动 Agent，并且不向 Agent 挂载 cgroupfs、tracefs、审计目录或
其他 Session workspace。不能把 daemon token 或 OpenSandbox 管理接口交给 Agent。

`--kill-session-processes-on-rollback` 是独立的可选开关，默认关闭，并且只能在进程
追踪启用时使用。开启后，`session.rollback` 会先持有 Session 启动栅栏，通过
`cgroup.kill` 终止该 Session 的根进程、普通子进程以及 double-fork/setsid 后代；确认
cgroup 为空、执行租约释放后才恢复 workspace。单次 RPC 可用布尔参数
`kill_processes` 覆盖 daemon 默认值。杀进程、确认退出或获取 mutation lease 任一步
失败时，回退不会开始。成功响应包含 `terminated_process_runs`。

## 服务接口

Allox OS 通过 `process.status/start/list/get/events/stop` RPC 提供进程树能力，通过
`session.rollback` 的 `kill_processes` 参数控制单次回退行为。
RPC 请求/响应属于 OS 服务协议；用户命令、展示格式和远程传输适配由独立的
`allox-cli` 仓库实现。活跃 run 持有 Session 执行租约，
checkpoint/rollback 不能和写入并发。停止、超时与 daemon 关闭使用 Session
`cgroup.kill`，而不是按照审计事件逐 PID 发送信号。

daemon 突然死亡时，不承诺自动杀死所有后代。重启读取同一审计目录后，未完成的
Session 会保持 fenced，防止对仍在写入的 workspace 回退。管理员需确认并清理遗留
cgroup，再恢复服务；不要通过更换审计目录或关闭追踪来绕过此状态。

`process.start` 是当前受追踪的直接启动接口。迁移期遗留的 exec 路径尚未自动进入
这个 cgroup/eBPF 路径；正式 Guest 接入时应让 Agent 本身由 Allox OS 的 Session
执行接口启动，而不是给共享命令执行器错误地绑定单一 Session 身份。

## 尚未覆盖

- 文件和网络行为：本轮只有进程生命周期；后续可增加 BPF-LSM/tracepoint provider，
  不改变控制层接口。
- 独立 network namespace/细粒度出站策略；当前 Bubblewrap 保留 Guest 网络。
- 审计保留策略和管理员解除异常 fence 的接口。
- Guest VM 内存回滚；workspace 回滚仍是 Btrfs 语义。

117 当前宿主机为旧内核，不能直接加载该 BPF 程序；真实加载必须发生在 Allox Guest
内。测试同时覆盖隔离的 Debian 6.12 KVM Guest 与 Allox 专用 Kata 6.1.62 Guest
（启用 tracing/BPF events）。117 上曾用 OpenSandbox 作为外部测试启动器验证完整
Kata 链路，但 OpenSandbox 不属于 Allox OS，也不是进程追踪依赖。Agent 侧看不到
这些内核控制面。构建、协议和 Python 回归不能替代 Guest 内核测试。
