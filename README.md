<div align="center">

<img src="./assets/allox-logo.png" alt="Allox logo" width="128" />

# Allox OS 2.0

**一个 Kata VM，内部按 Agent / Session 隔离和回退 workspace**

[架构](#架构) · [状态边界](#状态边界) · [快速开始](#快速开始) · [目录结构](#目录结构) · [开发](#开发)

</div>

Allox 2.0 不再把“为每次 Agent 任务创建一个独立容器”作为核心模型。它先为一个用户或信任域创建一个长期存在的 **Kata VM**，再在 VM 内通过 workspace 服务管理多个 Agent 和 Session。

Kata VM 提供 Guest Kernel 级强隔离；workspace 层提供低成本的 Session 文件隔离、checkpoint、rollback 和执行租约。两者解决的是不同问题，不能互相替代。

## 架构

```text
Host
├── Allox CLI / OpenSandbox control plane
└── Kata VM                         # 用户/信任域级强隔离边界
    ├── allox-workspace-daemon      # VM 内可信控制服务
    ├── Agent Runtime / AIO / MCP
    └── Btrfs workspace store
        └── agents/
            └── <agent_id>/
                └── workspace/sessions/
                    └── <session_id>/
                        ├── current/       # 当前可写 Session
                        └── checkpoints/   # 只读 COW snapshots
```

核心层次：

| 层次 | 生命周期 | 负责内容 |
|---|---|---|
| Kata VM | 用户或信任域级 | Guest Kernel、VM 内进程、网络、系统 `/tmp`、设备与根文件系统 |
| Agent | VM 内逻辑身份 | Agent 名称空间及其 Session 集合 |
| Session | Agent 任务/线程级 | `current` workspace、checkpoint DAG、执行租约和注册的后台任务 |
| Turn | 单次 Agent 交互 | 可选的 turn-end 自动 checkpoint |

详细设计见 [架构总览](docs/architecture/overview.md) 和 [Workspace 模型](docs/architecture/workspaces.md)。

## 状态边界

### Workspace rollback 会做什么

- 只回退指定 `agent_id + session_id` 的 Btrfs workspace。
- 回退前终止该 Session 已登记的后台执行。
- 保留其他 Agent、其他 Session 和 Kata VM 本身。
- checkpoint 索引、审计事件与事务日志保存在 rollback 范围外。

### Workspace rollback 不会做什么

- 不恢复整台 Kata VM 的 CPU、RAM、Guest Kernel 或设备状态。
- 不回退 VM 级 `/tmp`、系统服务或未登记的 VM 内进程。
- 不等同于 OpenSandbox 的整 VM snapshot/replace。

Allox 默认把 Session 的 `HOME` 指向 `current/`，把 `TMPDIR` 指向 `current/.allox-tmp/`。Agent 使用 `$TMPDIR` 创建的普通临时文件可进入 Session checkpoint；显式写 `/tmp` 属于 VM 级状态。

## 执行模式

```toml
[workspace]
vm_root = "/var/lib/allox/workspaces"
execution_mode = "managed"
auto_checkpoint_turns = false
```

- `managed`：默认模式。命令在同一 Kata VM 内执行，使用对应 Session 作为 `cwd/HOME/TMPDIR`；后台命令必须登记，rollback 前由 daemon 终止。
- `ephemeral`：每次命令额外进入 Bubblewrap PID/mount namespace；适合不需要跨命令进程状态的场景。

这里的 Bubblewrap 是 VM 内第二层轻量隔离，不替代外层 Kata VM。

## 快速开始

### 1. 安装开发环境

```bash
git clone https://github.com/AlloxOrg/allox-os.git
cd allox-os

# 当前开发版本从本地路径加载 OpenSandbox Python SDK
git clone --depth 1 https://github.com/opensandbox-group/OpenSandbox.git OpenSandbox
uv sync --no-editable
```

验证：

```bash
uv run --no-editable allox --version
uv run --no-editable allox --help
```

### 2. 配置 OpenSandbox + Kata

宿主机必须先安装并注册 Kata runtime。参考配置位于 [deploy/opensandbox-kata.toml.example](deploy/opensandbox-kata.toml.example)：

```bash
cp deploy/opensandbox-kata.toml.example ~/.sandbox.toml
opensandbox-server
```

该配置中的 `[secure_runtime]` 必须指向 Kata；使用普通 runc 只能获得容器隔离，不符合 Allox 2.0 的默认强隔离模型。

### 3. 准备 VM 内 Runtime 镜像

```bash
cd images/aio-runtime
./build.sh
cd ../..
```

镜像提供 AIO、Shell、File、Browser、Jupyter 和 MCP 等 VM 内服务。它是 Guest userspace，不是隔离边界；隔离边界由 Kata 提供。

### 4. 初始化 CLI

```bash
allox config init
allox config set connection.domain localhost:8080
allox config set connection.protocol http
allox config set defaults.image allox/aio-runtime:v2
```

### 5. 创建外层 VM

`vm` 是 Allox 2.0 的主命令名；`sandbox` 作为兼容别名保留：

```bash
allox vm create --timeout 30m -o json
allox vm list -o json
```

### 6. 在 VM 内创建 Agent / Session

`allox-workspace-daemon` 应运行在 Kata VM 内，并使用 VM 内 Btrfs 数据盘，例如 `/var/lib/allox/workspaces`。开发环境可将其端口通过受控 endpoint 暴露给 CLI：

```bash
allox workspace init
allox workspace agent-create agent-a
allox workspace session-create agent-a session-1

allox workspace checkpoint agent-a session-1 \
  --name clean --message "before task"

allox workspace run agent-a session-1 -- \
  sh -c 'printf changed > state.txt'

allox workspace rollback agent-a session-1 clean
```

后台任务必须通过受管入口启动：

```bash
allox workspace run agent-a session-1 --background -- python worker.py
```

daemon 会在 rollback 前中断该 Session 登记的后台任务；存在活动前台租约时，checkpoint/rollback 会被拒绝。

## CLI 边界

| 命令 | 所属层次 | 用途 |
|---|---|---|
| `allox vm ...` | Kata VM | 创建、查询、续期、暂停、恢复、销毁外层 VM |
| `allox sandbox ...` | Kata VM | `vm` 的兼容别名 |
| `allox workspace ...` | Agent/Session | 创建 workspace、执行、checkpoint、rollback |
| `allox aio ...` | VM 内 Runtime | Shell、Jupyter、Browser、Screenshot、MCP |
| `allox file ...` | VM 内 Runtime | 文件读写与上传下载 |
| `allox checkpoint ...` | 整 VM/旧接口 | OpenSandbox image snapshot；不要与 Session rollback 混用 |

## 目录结构

```text
allox-os/
├── src/                     # 主源码目录
│   └── allox/               # Python 包（import allox）
│       ├── cli/             # VM 外 CLI、命令和输出
│       ├── vm/              # 外层 OpenSandbox + Kata 生命周期
│       ├── workspace/       # VM 内 Agent/Session/checkpoint/rollback
│       ├── runtime/         # VM 内 AIO/MCP/健康检查
│       ├── integrations/    # LangChain 等 Agent turn 适配
│       └── config.py        # 跨层配置解析
├── images/aio-runtime/      # Kata VM 内 Runtime OCI 镜像
├── deploy/                  # OpenSandbox + Kata 部署配置
├── docs/
│   ├── architecture/        # 当前 2.0 架构与状态语义
│   ├── guides/              # Runtime、MCP、镜像使用指南
│   └── development/         # 当前开发设计选择
├── examples/                # 配置示例
└── tests/                   # 单元与集成测试
```

这套目录直接对应运行边界：`vm/` 不包含 Session rollback 实现，`workspace/` 不管理 Kata 生命周期，`runtime/` 不拥有 checkpoint 元数据。

## Agent turn checkpoint

可选启用：

```toml
[workspace]
auto_checkpoint_turns = true
```

框架适配器位于 `allox.integrations` entry-point group。当前内置 LangChain 适配器；它只发布 Session/Turn 生命周期事件，真正的 checkpoint 与 rollback 仍由 workspace daemon 执行。

## 开发

```bash
# 单元测试
uv run pytest -m "not integration" -q

# 静态检查
uv run ruff check src tests

# 完整集成测试需要 OpenSandbox、Kata 和 Runtime image
uv run pytest -m integration -v
```

## 文档

- [架构总览](docs/architecture/overview.md)
- [Agent/Session Workspace 模型](docs/architecture/workspaces.md)
- [Runtime 镜像](docs/guides/runtime-image.md)
- [MCP](docs/guides/mcp.md)
- [部署边界](deploy/README.md)

## License

Apache-2.0。
