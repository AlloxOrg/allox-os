# VM 内 Runtime MCP Server 盘点

> 对应历史 Allox 1.0 `ROADMAP.md` 阶段 2.6.1；该文件不再属于 2.0 主文档。  
> 官方镜像：`ghcr.io/agent-infra/sandbox`（REST 前缀 `/v1/mcp/*`）。

## 重要：以运行中沙箱为准

文档与 README 中的 server / 工具名可能与**当前镜像版本**不一致。调用前务必先盘点：

```bash
allox aio mcp servers -o json
allox aio mcp tools <server> -o json | jq '.tools[].name'
```

常见差异：

| 文档简写 | 实际 REST 工具名（browser 示例） | 说明 |
|----------|----------------------------------|------|
| `navigate` | `browser_navigate` | browser 工具通常带 `browser_` 前缀 |
| `screenshot` | `browser_screenshot` | 同上 |
| `shell` / `file` server | 可能**未配置** | 404 `not found in configuration` 时用 `aio exec` / `aio read` |

## 访问方式

| 方式 | 端点 | 说明 |
|------|------|------|
| REST（**allox CLI 使用**） | `GET /v1/mcp/servers`、`GET /v1/mcp/{server}/tools`、`POST /v1/mcp/{server}/tools/{tool}` | `agent-sandbox` SDK `client.mcp.*` |
| MCP Hub 协议 | `GET/POST http://<aio>:8080/mcp` | 扁平工具名如 `browser_navigate`；与 REST 命名可能不同 |

## 镜像可能内置的 MCP Server

以下来自 AIOsandbox 上游仓库的 `README.md`；**是否全部启用取决于镜像 tag / `mcp-servers.json`**。

| server | 文档记载工具（简写） | 能力 |
|--------|---------------------|------|
| `browser` | `browser_navigate`, `browser_screenshot`, `browser_click`, … | 浏览器自动化（较新版本普遍可用） |
| `file` | `read`, `write`, `list`, … | 文件系统（部分镜像未启用） |
| `shell` | `exec`, `create_session`, `kill` | Shell（部分镜像未启用） |
| `markitdown` | `convert`, `extract_text`, … | 文档转 Markdown（部分镜像未启用） |

## Allox CLI 用法

```bash
# 1. 盘点（可省略 sandbox_id，使用当前 session）
allox aio mcp servers -o json
allox aio mcp tools browser -o json | jq '.tools[].name'

# 2. browser：使用 tools 列表中的完整名称
allox aio mcp call browser browser_navigate \
  --args '{"url":"https://example.com"}' -o json

allox aio mcp call browser browser_screenshot -o json

# 3. shell / file：仅当 servers 列表中存在时再调用
allox aio mcp call shell exec --arg command="echo hello" -o json   # 若 404 见下文
allox aio mcp call file list --args '{"path":"/home/gem"}' -o json

# 4. 无 shell/file MCP 时的等效命令
allox aio exec -- echo hello
allox aio exec -- ls -la /home/gem
allox aio read /home/gem/某个文件
```

## 与 `aio exec` / `aio read` 的分工

| 场景 | 推荐命令 |
|------|----------|
| 人类 / 脚本直接跑 shell、读文件 | `allox aio exec`、`allox aio read`（**始终可用**） |
| 仅 browser MCP 已启用 | `allox aio mcp call browser <tool>` |
| Agent 统一 MCP 工具面、跨 server 编排 | `allox aio mcp call`（server 存在时） |
| 浏览器截图保存到本机 | `allox aio screenshot`（专用命令更简单） |
| 查工具参数 schema | `allox aio mcp tools <server>` |

## 实测校验脚本

```bash
allox aio mcp servers -o json | jq .

for s in browser file shell markitdown; do
  echo "=== $s ==="
  allox aio mcp tools "$s" -o json 2>/dev/null | jq '.tools[].name' || echo "(server not configured)"
done
```

将结果与本文对照；有差异时更新本文并同步相关集成测试。

## 参考

- AIOsandbox 上游仓库：`website/docs/zh/examples/browser.md`
- AIOsandbox 上游仓库：`website/docs/zh/guide/basic/mcp.md`
- AIOsandbox 上游仓库：`website/docs/public/v1/openapi.json` 中的 `/v1/mcp/*`
