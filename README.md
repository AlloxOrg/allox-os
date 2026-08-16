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
└── Allox OS（Kata runtime）          # 用户/信任域级强隔离边界
    ├── Guest Kernel + Rootfs         # 本仓库构建的运行时
    ├── alloxd / init                 # 可信控制服务
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
| Session Workspace（二级） | Session 生命周期 | `current`、checkpoint DAG、执行租约和注册的后台任务 |
| Turn | 单次 Agent 交互 | 可选的 turn-end 自动 checkpoint |

详细设计见 [架构总览](docs/architecture/overview.md) 和 [Workspace 模型](docs/architecture/workspaces.md)。

## 状态边界

### Session Workspace rollback 会做什么

- 只回退指定 `agent_id + session_id` 的 Btrfs workspace。
- 回退前终止该 Session 已登记的后台执行。
- 保留其他 Agent、其他 Session 和 Allox OS 运行时本身。
- checkpoint 索引、审计事件与事务日志保存在 rollback 范围外。

### Allox OS 级状态

- 当前 Kata backend 管理 CPU、RAM、Guest Kernel、设备和根文件系统。
- VM 级 `/tmp`、系统服务和 VM 内进程由 VM 生命周期管理。
- VM 级快照/恢复由 Allox OS 的宿主机 backend 负责；它与 Session Workspace rollback
  是两种独立操作。

Allox 默认把 Session 的 `HOME` 指向 `current/`，把 `TMPDIR` 指向 `current/.allox-tmp/`。Agent 使用 `$TMPDIR` 创建的普通临时文件可进入 Session checkpoint；显式写 `/tmp` 属于 VM 级状态。

Session 通过 `ALLOX_AGENT_WORKSPACE` 定位一级 workspace，通过
`ALLOX_AGENT_SHARED` 访问其中的 `shared/`；`HOME` 和工作目录指向当前二级
Session Workspace。

## Session 执行边界

`alloxd` 为每个 `agent_id/session_id` 建立独立 cgroup，并在启动时为 Session
建立 PID、mount、user 与 network namespace。Session 的所有子进程继承该 cgroup
归属；受信控制面可据此追溯进程来源，并在 rollback 前以 cgroup 为单位终止它们。

Session Workspace 绑定到进程的工作目录和 `HOME`；私有临时目录绑定为该
Session 的 `/tmp`。这使普通临时文件、Unix socket 与 Workspace 具有相同的
所有权边界；回退不依赖全局 `/tmp` 的软链接状态。

## 快速开始

### 当前迁移状态

当前 Python 原型保留了迁移前的控制面适配代码；它不能定义 Allox OS 的目标运行时。
重构完成后，仓库将直接产出由当前 Kata runtime 启动的 Guest Kernel、Rootfs 和
运行时服务；其他 runtime backend 不应改变 Allox OS 的 Agent/Session 语义。

目标 VM 内，`alloxd` 使用 Btrfs 数据盘（例如
`/var/lib/allox/workspaces`）管理 Agent/Session：

```bash
alloxd agent create agent-a
alloxd session create agent-a session-1
alloxd checkpoint create agent-a session-1 --name clean
alloxd checkpoint rollback agent-a session-1 clean
```

所有 Agent 命令均由 `alloxd` 放入对应 Agent/Session cgroup 和 namespace；回退前
先终止该 Session 的进程树，再恢复其 Btrfs 子卷。

## 目录结构

```text
allox-os/
├── kernel/                  # Allox Guest Kernel 的配置与补丁（目标）
├── rootfs/                  # Allox OS Rootfs、init 和系统服务（目标）
├── services/                # alloxd、观测与 workspace 服务（目标）
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
启用后，`alloxd` 在 Session 建立时创建基线 checkpoint，并在每个成功结束的
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
