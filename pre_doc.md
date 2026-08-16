# Allox 项目能力总结

## Allox：统一执行控制

Allox 将 Agent 日常操作统一收束到一个 CLI 入口中，以 `allox` 作为产品化控制面，向下连接 `opensandbox-server` 管理沙箱，向内连接 AIO 镜像提供的 `/v1` 能力。它把 shell 执行、文件读写、浏览器截图、Jupyter 代码执行、MCP 工具调用等能力统一在 `allox aio *` 命令下，同时保留 `allox run`、`allox file` 这类基于 OpenSandbox execd 的运维执行路径。通过统一配置优先级、统一输出格式 `table/json/yaml/raw`、统一 session 解析和 profile 管理，Allox 让人和 Agent 都可以用稳定、可脚本化的方式控制沙箱执行，而不需要直接区分底层 OpenSandbox、agent-sandbox 或具体 endpoint 细节。

## Allox：沙箱生命周期管理

Allox 通过 `allox sandbox` 命令组封装 OpenSandbox 的生命周期能力，覆盖创建、列表、查看、endpoint 查询、续期和销毁等核心流程。创建沙箱时默认使用 AIO 镜像 `codewisdom/aio_sandbox:latest`、入口 `/opt/gem/run.sh`，支持传入环境变量、metadata、TTL 超时或 `--timeout none` 手动清理模式，并在创建后等待 AIO 健康检查 `/v1/shell/sessions` 就绪。成功创建后，Allox 会自动记录当前 session 到 `~/.allox/sessions.json`，后续命令可以省略 sandbox id；销毁当前沙箱时也会自动清理 session，从而形成“创建、使用、续期、销毁”的闭环管理体验。

## Allox：任务执行安全隔离

Allox 的任务执行隔离建立在 OpenSandbox 的容器化沙箱之上：每个 Agent 任务运行在独立 sandbox 中，命令执行、文件访问、浏览器自动化、Jupyter 代码执行都被限制在对应容器环境内，避免直接污染宿主机或其他任务。平台层还支持通过网络策略限制沙箱出站访问，并可在 OpenSandbox server 级别配置安全容器运行时，例如 gVisor、Kata Containers 或 Firecracker 后端，以获得比默认 runc 更强的系统调用或虚拟化隔离。对 Allox 用户来说，这些安全能力以基础设施配置的方式透明生效，CLI 侧仍保持同样的 `allox aio exec`、`allox run` 等执行接口。

## Allox：智能体执行可观测

Allox 在 CLI 层提供可读和可机器解析的执行反馈，例如沙箱状态、endpoint、AIO 就绪耗时、命令输出、exit code、Jupyter 执行结果、MCP 调用结果以及浏览器 CDP/VNC 信息，并支持 `--verbose` 输出连接与健康检查细节，方便定位 endpoint、防火墙或 AIO readiness 问题。底层 OpenSandbox 进一步规划和实现了 execd、egress、ingress 的 OpenTelemetry metrics 与结构化日志能力，可观测 HTTP 请求、执行耗时、文件操作、系统资源、网络策略命中、出站访问等信号。整体上，Allox 负责把智能体执行过程变成可查看、可脚本消费、可排障的操作流，OpenSandbox 负责提供更底层的指标和日志采集基础。

## Allox：沙箱启动与读档

Allox 的沙箱启动与读档能力可以围绕“降低冷启动成本、提高环境复用率、减少重复初始化”来设计：短期可通过镜像预拉取、固定镜像 digest、避免 `latest` 带来的重复拉取、节点侧缓存和 warm sandbox pool 来减少从零创建容器的时间；中期可以维护一组已完成 AIO 初始化、浏览器/Jupyter/MCP 服务已就绪的空闲沙箱对象，用户请求到来时直接从池中分配，并在释放后按策略清理工作目录、重置环境变量和网络策略，再回收到池中；长期则可以引入 rootfs snapshot 或 microVM snapshot，将“已安装依赖、已启动服务、已加载工作区”的沙箱状态保存为可恢复对象，读档时不再完整执行镜像拉取、容器初始化和服务启动，而是根据 snapshot metadata 恢复到某个稳定检查点。为了让读档可管理，Allox 可以在 session 之外维护 `SandboxObject` 元数据，例如 `sandbox_id`、`image_digest`、`snapshot_id`、`workspace_hash`、`dependency_profile`、`aio_ready_at`、`last_used_at`、`state`、`ttl` 和 `restore_policy`，从而支持按任务类型选择“冷启动、热池复用、快照恢复”三种路径，在成本和启动速度之间做可控权衡。