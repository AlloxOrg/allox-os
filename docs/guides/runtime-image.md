# 自定义 VM 内 Runtime 镜像

> 阶段 3 交付物：在官方 `ghcr.io/agent-infra/sandbox` 基础上衍生私有镜像，**换 tag 即可升级环境，CLI 无需改动**。

## 目录结构

```text
images/aio-runtime/
├── Dockerfile                      # FROM 官方 pin tag + 额外依赖
├── build.sh                        # 本地构建 / 推送私有仓库
├── allox_health_server.py          # 示例自定义服务（9090）
├── supervisord.allox_health.conf   # → /opt/gem/supervisord/
└── nginx.allox_health.conf         # → /opt/gem/nginx/
```

## 快速构建

```bash
cd allox-os/images/aio-runtime
chmod +x build.sh

# 拉取官方基础镜像并构建本地 tag
./build.sh

# 指定基础 tag（生产禁止裸 latest）
BASE_TAG=1.0.0.150 ./build.sh

# 推送到私有仓库
REGISTRY=your-registry.io/allox TAG=v2 ./build.sh --push
```

默认产出镜像：`allox/aio-runtime:v2`（基于 `ghcr.io/agent-infra/sandbox:1.0.0.150`）。

## 配置 allox 使用自定义镜像

```bash
allox config set defaults.image allox/aio-runtime:v2
allox config show
```

或在 profile 中覆盖（见 `examples/profiles/custom.toml.example`）：

```bash
cp examples/profiles/custom.toml.example ~/.allox/custom.toml
allox --profile custom sandbox create -o json
```

> `--profile custom` 需在 `main.py` 的 profile 列表中包含 `custom`，或使用 `--config ~/.allox/custom.toml`。

## 镜像定制说明

### 基础镜像

Dockerfile 使用 **pin tag** 而非 `latest`：

```dockerfile
ARG BASE_TAG=1.0.0.150
FROM ghcr.io/agent-infra/sandbox:${BASE_TAG}
```

升级时只需改 `BASE_TAG` 并重新构建。

### 添加依赖

| 类型 | 示例 | 安装路径 |
|------|------|----------|
| apt | `jq`, `tree`, `curl` | `/usr/bin/*` |
| pip | `httpx` | `/usr/local/bin/*` |
| npm | `npm i -g <pkg>` | 全局 node_modules |

### 自定义服务（supervisord + nginx）

AIO 镜像按约定目录自动挂载：

- 进程：`/opt/gem/supervisord/*.conf`
- 路由：`/opt/gem/nginx/*.conf`

本仓库示例在 **8080** 上新增路由 `GET /allox-health`，代理到内部 9090 端口的 health server。

### 删减 AIO 组件

若不需要 Code Server 等，可在 Dockerfile 中删除对应 conf（构建后 AIO 子命令文档需同步调整）：

```dockerfile
RUN rm -f /opt/gem/supervisord/supervisord.code_server.conf \
          /opt/gem/nginx/code_server.conf
```

删减后 `allox aio *` 中与 VSCode 相关的 URL 将不可用；shell / browser / MCP 等仍走 `/v1`。

## 验证清单

构建完成后运行仓库中的 Runtime image 集成测试进行验收。

核心步骤：

```bash
# 1. 创建（health_check 应通过）
allox sandbox create -o json

# 2. 验证额外 apt 依赖
allox aio exec -- jq --version

# 3. 验证自定义路由
allox aio exec -- curl -sf http://127.0.0.1:8080/allox-health

# 4. 阶段 1 端到端仍通过
allox aio exec -- ls -la
allox aio screenshot -f /tmp/test.png
allox sandbox kill
```

## 端口说明

| 端口 | 服务 | 访问方式 |
|------|------|----------|
| 8080 | AIO 主入口（nginx） | `allox sandbox endpoint` → `aio_url` |
| 9090 | 自定义 health（内部） | 经 nginx `/allox-health` 代理 |
| 8888 | Jupyter | AIO portal |
| 8200 | Code Server | `/code-server/`（若未删减） |

自定义端口需在 nginx conf 中注册，并通过 8080 统一对外；OpenSandbox 创建沙箱时仍只需暴露 `defaults.aio_port`（8080）。

## 参考

- AIOsandbox 上游仓库中的 `website/docs/en/blog/announcing-0.mdx`（Extension and Ecosystem → Custom Images）
- 历史 Allox 1.0 `ROADMAP.md` 的阶段 3（该文件不再属于 2.0 主文档）
- [Code Interpreter 评估](./code-interpreter.md)（非 AIO 轻量 Runtime 评估）
