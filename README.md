<div align="center">

<img src="./assets/allox-logo.png" alt="Allox logo" width="128" />

# Allox CLI

> Allox 2.0 development: per-Agent/per-Session workspace isolation and
> rollback are documented in [docs/ALLOX_2_WORKSPACES.md](docs/ALLOX_2_WORKSPACES.md).

**面向 AI Agent 的可恢复执行工作区**

从环境创建、任务执行到断点恢复，用一套 CLI 完成完整 Agent 工作流。

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)

[Features](#features) · [Quick Start](#quick-start) · [Demo](#demo-end-to-end-agent-workflows) · [Tests](#tests-validation-and-quality) · [Documentation](#documentation)

</div>

Allox 将隔离环境、Agent 工具、文件传输、当前会话和 Checkpoint 恢复统一到一个命令入口。它既适合开发者在终端交互使用，也提供稳定的结构化输出供 Agent、脚本和 CI 调用。

## Features

- **可恢复工作区**：支持手动、定时和操作成功后的 Checkpoint，可恢复 `latest` 或指定版本。
- **Stateful Session**：创建或恢复后自动选中当前环境；后续命令可以省略 ID，销毁后自动清理。
- **Agent-ready 工具集**：统一使用 Shell、File、Browser、Jupyter、Screenshot 和 MCP 能力。
- **Runtime Readiness**：等待 Agent 服务真正可用，而不只是等待容器启动，并报告就绪耗时。
- **可靠文件传输**：支持二进制流式与递归传输；下载使用临时 staging，并拒绝符号链接和路径逃逸。
- **MCP 原生入口**：发现 MCP Server、列出工具并直接调用。
- **多环境配置**：通过 Profile 切换 dev、staging、prod、自定义镜像或轻量执行环境。
- **自动化友好**：按场景提供 `table`、`json`、`yaml` 和 `raw` 输出。

## Quick Start

### Install

当前版本从源码安装：

```bash
git clone https://github.com/AlloxOrg/allox-cli.git
cd allox-cli

# 当前项目从本地路径加载 OpenSandbox Python SDK
git clone --depth 1 https://github.com/opensandbox-group/OpenSandbox.git OpenSandbox

uv sync --no-editable
```

激活虚拟环境：

```bash
# Linux / macOS
source .venv/bin/activate

# Windows PowerShell
.venv\Scripts\Activate.ps1
```

验证安装：

```bash
allox --version
allox --help
```

不激活虚拟环境时，可使用 `uv run --no-editable allox ...`。开发中修改 `src/allox` 后，执行：

```bash
uv sync --reinstall-package allox-cli
```

> 不建议直接执行 `uv run allox`：它可能将项目切换为 editable 安装，覆盖当前安装方式。

### Configure

Allox 当前使用 [OpenSandbox](https://github.com/opensandbox-group/OpenSandbox) 管理环境生命周期，默认使用 [AIO Sandbox](https://github.com/agent-infra/sandbox) 提供 Agent Runtime。两者都需要在本机准备，但不需要手动运行它们的源码：OpenSandbox Server 通过 Python 包安装并作为本地服务启动，AIO Sandbox 通过 Docker 拉取并由 Server 按需创建。开始前请确认 Docker 已运行。

#### Start the Server

```bash
# 安装到当前 uv 虚拟环境
uv pip install opensandbox-server

# 生成本地 Server 配置并启动
opensandbox-server init-config ~/.sandbox.toml --example docker
opensandbox-server
```

在另一个终端验证：

```bash
curl http://127.0.0.1:8080/health
# {"status":"healthy"}
```

生产环境应在 `~/.sandbox.toml` 中配置 API Key。仅限本地开发且明确接受无鉴权风险时，可设置 `OPENSANDBOX_INSECURE_SERVER=YES`。

#### Pull the Default Image

```bash
# 下载默认 Agent Runtime 到本机 Docker
docker pull ghcr.io/agent-infra/sandbox:latest
```

生产环境应固定具体 tag，避免使用 `latest`。

#### Initialize Allox

```bash
allox config init
allox config set connection.domain localhost:8080
allox config set connection.protocol http
allox config show
```

如果服务端开启了鉴权：

```bash
allox config set connection.api_key YOUR_API_KEY
```

Allox 会依次读取命令行选项、环境变量、本地配置文件和内置默认配置，位置靠前的设置会覆盖后面的设置。需要在不同环境间切换时，可通过 `--profile dev|staging|prod|custom|code` 选择对应的 `~/.allox/<profile>.toml` 配置文件。

## Demo: End-to-End Agent Workflows

以下示例默认已经完成安装与配置。先确认 Docker、服务端和 Allox 配置可用：

```bash
docker info
curl http://127.0.0.1:8080/health
allox config show
```

### 1. Complete Agent Workflow

创建环境后，Allox 会自动将其记录为当前 session。下面的操作均不需要重复传入 `sandbox_id`：

```bash
# 创建环境并等待 Runtime 就绪
allox sandbox create --timeout 30m -o json
allox session current -o json

# Shell
allox aio exec -- python3 -c "print('hello from allox')"

# Jupyter；结果应包含 status: ok 和输出 4
allox aio jupyter run -c "print(2 + 2)" -o json

# Browser；输出 CDP/VNC 地址，并保存截图到本地
allox aio browser info -o json
allox aio screenshot -f allox-demo.png -o json

# 单文件上传、读取和下载
allox file upload ./README.md /tmp/allox-README.md -o json
allox aio read /tmp/allox-README.md
allox file download /tmp/allox-README.md ./README.from-allox.md -o json

# 递归目录传输
mkdir -p .allox-demo/nested
printf "hello\n" > .allox-demo/nested/result.txt
allox file upload --recursive ./.allox-demo /tmp/allox-demo -o json
allox file download --recursive /tmp/allox-demo ./allox-demo-output -o json

# 销毁当前环境；current session 会被自动清除
allox sandbox kill -o json

# 预期提示不存在当前 session
allox session current

# 本地生成：allox-demo.png、README.from-allox.md、.allox-demo/、allox-demo-output/
# 确认结果后可自行删除
```

### 2. Checkpoint and Restore

下面的 Bash 示例使用 [`jq`](https://jqlang.github.io/jq/) 从 JSON 输出中读取 ID，完整演示“保存 V1 → 修改为 V2 → 恢复 V1 → 验证 → 清理”：

```bash
# 创建源环境并记录 ID
SOURCE_ID=$(allox sandbox create --timeout 30m -o json | jq -r '.id')

# 写入 V1 并保存 Checkpoint
allox aio exec -- sh -c "echo v1 > /home/gem/version.txt"
CHECKPOINT_ID=$(allox checkpoint create --name v1 -o json | jq -r '.id')

# 将当前工作区修改为 V2
allox aio exec -- sh -c "echo v2 > /home/gem/version.txt"
allox checkpoint list -o json

# 从 V1 Checkpoint 创建新环境，并自动切换 current session
RESTORED_ID=$(allox checkpoint restore "$CHECKPOINT_ID" --timeout 30m -o json | jq -r '.id')

# 预期输出 v1
allox aio exec -- cat /home/gem/version.txt

# 恢复会创建新环境，不会覆盖或销毁源环境
# 因此需要删除 Checkpoint，并显式清理恢复环境和源环境
allox checkpoint delete "$CHECKPOINT_ID" -o json
allox sandbox kill "$RESTORED_ID" -o json
allox sandbox kill "$SOURCE_ID" -o json
```

### 3. Automatic Checkpoint

在 `~/.allox/config.toml` 中启用后，Allox 只在指定操作成功时创建 Checkpoint：

```toml
# ~/.allox/config.toml
[checkpoint]
enabled = true
on_success = true
operations = ["run", "file.write", "file.upload", "aio.exec", "aio.jupyter"]
interval = "5m"
strict = false
```

也可以为当前 session 运行前台定时保存：

```bash
allox checkpoint watch --interval 5m
```

`strict = false` 表示自动保存失败只产生警告，不改变原操作的成功结果。`watch` 会持续运行，按 `Ctrl+C` 停止。

### 4. MCP Discovery and Call

MCP Server 和工具随运行镜像而变化，调用前应先发现实际能力：

```bash
# 创建环境（如当前已有 session，可省略）
allox sandbox create --timeout 30m -o json

# 发现 Server 和完整工具名
allox aio mcp servers -o json
allox aio mcp tools browser -o json

# 调用浏览器工具；具体工具名以 tools 输出为准
allox aio mcp call browser browser_navigate \
  --args '{"url":"https://example.com"}'

allox sandbox kill -o json
```

部分镜像没有启用全部 MCP Server，出现 404 不代表环境生命周期异常。Shell 和文件操作可分别回退到 `allox aio exec`、`allox aio read` 或 `allox file *`。

详见 [MCP Server 使用说明](./docs/MCP_SERVERS.md)。

## Command Reference

| 命令 | 用途 |
|---|---|
| `allox sandbox create/list/get/endpoint/renew/pause/resume/kill` | 环境生命周期 |
| `allox session current/use/clear` | 当前工作环境 |
| `allox aio exec/read/screenshot` | Shell、文件读取与截图 |
| `allox aio jupyter run` | Jupyter 代码执行 |
| `allox aio browser info` | 获取 CDP/VNC 信息 |
| `allox aio mcp servers/tools/call` | MCP 发现与调用 |
| `allox checkpoint create/list/restore/delete/watch` | 工作区保存与恢复 |
| `allox run` | 通用命令执行 |
| `allox file cat/write/upload/download` | 文件操作与传输 |
| `allox config init/show/set/path` | 本地配置 |

使用 `allox <command> --help` 查看完整参数。

## Tests: Validation and Quality

### Unit Tests

无需 Docker 或正在运行的服务端：

```bash
uv run pytest -m "not integration" -q
uv run ruff check src tests
```

### End-to-End

准备完整运行环境后执行：

```bash
uv run pytest -m integration tests/test_integration_e2e.py -v
```

覆盖流程：

```text
create → exec → screenshot → Jupyter → browser info
       → file upload/download → recursive transfer → kill
```

其他集成测试：

```bash
# MCP
uv run pytest -m integration tests/test_integration_mcp.py -v

# 自定义 Runtime image；先执行 docker/build.sh
uv run pytest -m integration tests/test_integration_custom_image.py -v
```

## Documentation

[MCP Server 使用说明](./docs/MCP_SERVERS.md)介绍 MCP Server 发现、工具列表与调用流程。

[自定义镜像指南](./docs/CUSTOM_IMAGE.md)说明如何构建和验证自定义 Runtime image；[Code Interpreter 指南](./docs/CODE_INTERPRETER.md)介绍轻量 Code Interpreter 镜像的配置与使用方式。

测试与验收记录按阶段整理：[Phase 1](./docs/PHASE1_TESTING.md)覆盖基础生命周期与工具能力，[Phase 2](./docs/PHASE2_TESTING.md)覆盖 Session、Profile、MCP 与脚本化能力，[Phase 3](./docs/PHASE3_TESTING.md)覆盖自定义镜像。

## Built With

- [Python 3.10+](https://www.python.org/) — 主要开发语言。
- [Click](https://click.palletsprojects.com/) — CLI 命令、参数解析与上下文管理。
- [Rich](https://github.com/Textualize/rich) — 终端表格、状态面板和交互输出。
- [HTTPX](https://www.python-httpx.org/) — Runtime 健康检查与 HTTP 通信。
- [OpenSandbox](https://github.com/opensandbox-group/OpenSandbox) — 环境生命周期、endpoint、execd、snapshot 与隔离能力。
- [AIO Sandbox](https://github.com/agent-infra/sandbox) — Shell、File、Browser、Jupyter、MCP 与 VSCode Agent Runtime。
- [Docker](https://www.docker.com/) — 本地容器运行环境。
- [uv](https://github.com/astral-sh/uv) 与 [Hatchling](https://hatch.pypa.io/latest/) — 依赖管理、虚拟环境和 Python 包构建。
- [pytest](https://docs.pytest.org/) 与 [Ruff](https://docs.astral.sh/ruff/) — 自动化测试、Lint 和代码质量检查。

## License

Apache-2.0。完整许可证文本见 [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0)。
