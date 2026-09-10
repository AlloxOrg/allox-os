<div align="center">

<img src="./assets/allox-logo.png" alt="Allox logo" width="128" />

# Allox OS

**Allox OS（当前实现：Kata runtime）：按 Agent / Session 隔离、观测与回退**

[架构](#架构) · [状态边界](#状态边界) · [快速开始](#快速开始) · [目录结构](#目录结构) · [开发](#开发)

</div>

Allox OS 是一个用户或信任域专属的 Agent runtime。当前具体实现使用 Kata；本
仓库构建 Guest Kernel、Rootfs 以及可信服务，并在该运行时内管理多个 Agent 和
Session。Kata 是当前后端实现，而不是 Allox OS 不可替换的架构定义。

当前 Kata backend 提供 Guest Kernel 级的宿主机隔离；Allox OS 提供运行时内的
进程归属、workspace 隔离、checkpoint、rollback 与观测能力。两层共同组成 Agent
的可信执行环境。

Allox OS 的目标架构不依赖 Allox CLI、OpenSandbox、execd 或 AIO Runtime；它们不
属于 Allox OS 的运行时边界。

## 架构

```text
Host
└── Allox OS                         # 用户/信任域级强隔离边界
    ├── Kata runtime                 # 当前可替换的 VM runtime 实现
    ├── Guest Kernel + Rootfs         # 本仓库构建的运行时
    ├── workspace daemon / init       # 可信控制服务
    ├── cgroup / namespace / audit
    └── Btrfs workspace store
        └── agents/
            └── <agent_id>/
                └── workspace/                       # 一级 Agent Workspace
                    ├── shared/                       # Agent 共享文件
                    └── sessions/
                        └── <session_id>/              # 二级 Session Workspace
                            ├── current/               # 当前可写执行状态
                            └── checkpoints/           # 只读 COW snapshots
```

核心层次：

| 层次 | 生命周期 | 负责内容 |
|---|---|---|
| Allox OS | 用户或信任域级 | Guest Kernel、VM 内进程、网络、系统 `/tmp`、设备与根文件系统 |
| Agent Workspace（一级） | Agent 生命周期 | Agent 共享文件、身份配置及其 Session Workspace 集合 |
| Session Workspace（二级） | Session 生命周期 | `current`、checkpoint DAG、执行租约和受追踪运行记录 |
| Turn | 单次 Agent 交互 | 可选的 turn-end 自动 checkpoint |

详细设计见 [架构总览](docs/architecture/overview.md) 和 [Workspace 模型](docs/architecture/workspaces.md)。

## 状态边界

### Session Workspace rollback 会做什么

- 只回退指定 `agent_id + session_id` 的 Btrfs workspace。
- 可选在回退前通过 Session cgroup 终止其全部已追踪进程及后代；默认关闭。
- 保留其他 Agent、其他 Session 和 Allox OS 运行时本身。
- checkpoint 索引、审计事件与事务日志保存在 rollback 范围外。

### Allox OS 级状态

- 当前 Kata backend 管理 CPU、RAM、Guest Kernel、设备和根文件系统。
- VM 级 `/tmp`、系统服务和 VM 内进程由 VM 生命周期管理。
- VM 级快照/恢复由 Allox OS 的宿主机 backend 负责；它与 Session Workspace rollback
  是两种独立操作。

在 tracked 执行路径中，Session 的 `HOME` 指向 `current/`，Bubblewrap 提供私有
`/tmp`，并在正常退出时把其中的普通文件同步到 `current/.allox-tmp/`。Unix socket、
FIFO 和设备节点不进入 checkpoint。迁移期 legacy/managed 路径只把 `TMPDIR` 指向
`.allox-tmp/`；该路径显式写入的系统 `/tmp` 仍属于 VM 级状态。

Session 通过 `ALLOX_AGENT_WORKSPACE` 定位一级 workspace，通过
`ALLOX_AGENT_SHARED` 访问其中的 `shared/`；`HOME` 和工作目录指向当前二级
Session Workspace。

## Session 执行边界

当前 tracked 执行路径中，`allox-workspace-daemon` 为每个
`agent_id/session_id` 建立独立 cgroup，
并通过 Bubblewrap 为每次执行建立 PID/mount namespace、最小文件视图和私有 `/tmp`；
Agent 启动前会丢弃全部 Linux capabilities。Session 的所有子进程继承该 cgroup
归属；受信控制面可据此追溯进程来源，并在 rollback 前以 cgroup 为单位终止它们。
user namespace、network namespace 与细粒度出站策略仍是后续工作。

Session Workspace 绑定到进程的工作目录和 `HOME`；私有 `/tmp` 只属于本次
Bubblewrap 执行。普通临时文件可同步回 Session Workspace，Unix socket 等内核
运行态则在执行结束或进程树被终止时消失；回退不依赖全局 `/tmp` 的软链接状态。

### 当前已实现：可插拔 eBPF 进程追踪

Allox OS daemon 提供默认关闭的 process-tracker provider 接口。内置 `ebpf` provider
通过独立原生 collector，在内核 sched tracepoint 上传播 Agent/Session run 归属并记录
fork、exec、exit；Session cgroup 是实际成员集合和整树终止边界。collector 与控制层
使用版本化协议，因此可以独立升级或替换。

这是显式启用的新 OS 服务接口，**迁移期遗留的 run/execd 路径不会自动受到追踪**。
Allox CLI 对该接口的用户侧适配属于独立的 `allox-cli` 仓库。启用参数、Bubblewrap
边界、构建方法、API 和限制见
[进程追踪说明](docs/development/process-tracking.md)。

## 快速开始

### 当前迁移状态

当前 Python 原型保留了迁移前的控制面适配代码；它不能定义 Allox OS 的目标运行时。
重构完成后，仓库将直接产出由当前 Kata runtime 启动的 Guest Kernel、Rootfs 和
运行时服务；其他 runtime backend 不应改变 Allox OS 的 Agent/Session 语义。

当前 Guest 内的可执行控制入口是 `allox-workspace-daemon`。Allox OS 通过
`/v1/rpc` 提供 Agent/Session、checkpoint、rollback 和进程追踪接口；面向用户的
命令行封装由独立的 `allox-cli` 仓库提供。

```bash
allox-workspace-daemon \
  --root /var/lib/allox/workspaces \
  --listen 127.0.0.1:8092
```

以下请求直接使用当前 RPC 协议：

```bash
curl -sS http://127.0.0.1:8092/v1/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"agent.create","params":{"agent_id":"agent-a"}}'

curl -sS http://127.0.0.1:8092/v1/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"session.create","params":{"agent_id":"agent-a","session_id":"session-1"}}'

curl -sS http://127.0.0.1:8092/v1/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"checkpoint.create","params":{"agent_id":"agent-a","session_id":"session-1","checkpoint_id":"clean"}}'

curl -sS http://127.0.0.1:8092/v1/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"session.rollback","params":{"agent_id":"agent-a","session_id":"session-1","checkpoint_id":"clean"}}'
```

进程追踪显式启用后，单次 `session.rollback` 可传入 `"kill_processes": true`；
也可用 daemon 参数 `--kill-session-processes-on-rollback` 设置默认策略。开启后，
Allox OS 先终止目标 Session 的受追踪进程树，再恢复其 Btrfs workspace；其他
Agent、Session 和 Kata VM 不受影响。Kata VM 级恢复会由 VM 生命周期自然重置
全部 Guest 进程，不依赖此 Session 钩子。

## 目录结构

```text
allox-os/
├── kernel/                  # Allox Guest Kernel 的配置与补丁（目标）
├── rootfs/                  # Allox OS Rootfs、init 和系统服务（目标）
├── services/                # 可信 daemon、观测与 workspace 服务（目标）
├── deploy/                  # 当前 Kata runtime 的宿主机部署配置
├── docs/
│   ├── architecture/        # 当前架构与状态语义
│   ├── guides/              # Runtime、MCP、镜像使用指南
│   └── development/         # 当前开发设计选择
├── examples/                # 配置示例
└── tests/                   # 单元与集成测试
```

目录重构以此目标边界为准；现有 `src/` 是迁移期实现，不是 Allox OS 的最终组件边界。

## Agent turn checkpoint

Agent framework 可通过 Allox OS 的 guest 接口发布 Session/Turn 生命周期事件。
启用后，Allox OS daemon 在 Session 建立时创建基线 checkpoint，并在每个成功结束的
turn 后创建 checkpoint；该策略必须可按 Agent 或 Session 关闭。

## 开发

```bash
# 当前迁移期 Python 原型
uv run pytest -m "not integration" -q
uv run ruff check src tests
```

## 文档

- [文档索引](docs/README.md)
- [架构总览](docs/architecture/overview.md)
- [Agent/Session Workspace 模型](docs/architecture/workspaces.md)
- [部署边界](deploy/README.md)

## License

Apache-2.0。
