# 阶段 2 测试记录

> 对应历史 Allox 1.0 `ROADMAP.md` 阶段 2（产品化 CLI）；保留作回归资料。  
> 本文汇总自动化测试与手工验证结果，并保留**实际终端输出**便于对照回归。

**文档版本**：与 `allox-cli` v0.1.0 同步  
**最近更新**：2026-06-09（新增阶段 2.6 MCP CLI 测试）

---

## 1. 测试范围

| 类别 | 说明 | 依赖 |
|------|------|------|
| 单元 / 冒烟 | `pytest`，不连真实沙箱 | 仅 Python + Click |
| 集成（`@integration`） | 阶段 1 生命周期 + 阶段 2 扩展项 | Docker、AIO 镜像、`opensandbox-server` @ `localhost:8080` |
| ROADMAP 2.x 手工验收 | session / 省略 id / execd / renew 等 | 同上 |

**阶段 2 完成标准**（ROADMAP）：session 省略 `sandbox_id` + `-o json` 脚本化 + 多 profile 文档齐全 + **2.6 MCP 验收通过**。

---

## 2. 自动化测试

### 2.1 执行命令

```bash
cd allox-cli
uv run pytest -q                              # 默认排除 integration
uv run pytest -q -m "not integration" -v      # 单元 + 冒烟，带用例名
uv run pytest -m integration -v -rs           # 集成，并打印 skip 原因
```

### 2.2 单元 / 冒烟：完整 pytest 输出（2026-06-08）

```text
============================= test session starts ==============================
platform darwin -- Python 3.12.13, pytest-9.0.3, pluggy-1.6.0
rootdir: /Users/kn0wn/Desktop/Agent/allox-cli
configfile: pyproject.toml
collected 38 items / 11 deselected / 27 selected

tests/test_aio_commands.py .....                                         [ 20%]
tests/test_aio_exec_parse.py ......                                      [ 44%]
tests/test_cli_help.py ..                                                [ 52%]
tests/test_mcp_utils.py .......                                          [ 80%]
tests/test_sandbox_create_session.py ..                                  [ 88%]
tests/test_session.py .....                                              [100%]
tests/test_utils.py ..                                                   [100%]

======================= 27 passed, 11 deselected in 0.08s =======================
```

说明：`test_integration_e2e.py` 与 `test_integration_mcp.py` 在默认 `pytest -q` 下被 **deselected**（未执行），不是失败。

### 2.3 集成测试：完整 pytest 输出（本机无 Server）

```text
collected 38 items / 27 deselected / 11 selected

tests/test_integration_e2e.py::test_e2e_sandbox_lifecycle SKIPPED      [ 11%]
tests/test_integration_mcp.py::test_mcp_servers_lists_expected SKIPPED   [ 22%]
... (其余 MCP 集成用例同样 SKIPPED)

=========================== short test summary info ============================
SKIPPED [11] OpenSandbox server not reachable at localhost:8080
====================== 11 skipped, 27 deselected in 0.08s =======================
```

### 2.4 用例与断言（对照表）

| 文件 | 用例 | 断言要点 |
|------|------|----------|
| `test_cli_help.py` | `test_cli_help` | `exit_code == 0`，`"sandbox"`、`"aio"` ∈ stdout |
| `test_cli_help.py` | `test_sandbox_help` | `"create"` ∈ stdout |
| `test_aio_commands.py` | `test_aio_jupyter_help` | `"run"` ∈ `aio jupyter --help` |
| `test_aio_commands.py` | `test_aio_browser_help` | `"info"` ∈ `aio browser --help` |
| `test_aio_commands.py` | `test_aio_mcp_help` | `servers` / `tools` / `call` ∈ `aio mcp --help` |
| `test_aio_commands.py` | `test_aio_mcp_call_help` | `--args` / `--arg` ∈ `aio mcp call --help` |
| `test_aio_commands.py` | `test_sandbox_create_has_env_option` | `"--env"` 或 `"-e"` ∈ `sandbox create --help` |
| `test_mcp_utils.py` | `test_parse_mcp_*` / `test_build_mcp_*` | MCP 参数解析与 JSON 合并 |
| `test_integration_mcp.py` | `test_mcp_servers_includes_browser` | 至少含 `browser` server |
| `test_integration_mcp.py` | `test_mcp_browser_tools_non_empty` | browser 工具名带 `browser_` 前缀 |
| `test_integration_mcp.py` | `test_mcp_call_browser_tool` | `browser_screenshot` 或 `browser_navigate` |
| `test_integration_mcp.py` | `test_mcp_*_if_configured` | shell/file/markitdown 存在则测，否则 skip |
| `test_integration_mcp.py` | `test_mcp_omit_sandbox_id_uses_session` | 省略 id 使用 session |
| `test_utils.py` | `test_parse_duration_minutes` | `parse_duration("30m")` → 1800s |
| `test_utils.py` | `test_parse_nullable_duration_none` | `"none"` / `"NONE"` → `None` |
| `test_utils.py` | `test_format_api_error_*` | MCP 404/500 可读提示 |
| `test_session.py` | `test_sessions_file_roundtrip` | `sessions.json` 含 `sandbox_id` / `aio_url` / `created_at` |
| `test_session.py` | `test_clear_current_session` | clear 后文件删除 |
| `test_session.py` | `test_load_sessions_missing_file` | 缺失文件 → 空 |
| `test_session.py` | `test_load_sessions_invalid_json` | 损坏 JSON → 空 |
| `test_session.py` | `test_set_current_session_replaces` | 覆盖写入 current |
| `test_sandbox_create_session.py` | `test_sandbox_create_writes_session` | mock create 后 session 落盘 |
| `test_sandbox_create_session.py` | `test_sandbox_create_writes_session_without_aio_url` | endpoint 失败时 `aio_url=""` |
| `test_integration_e2e.py` | `test_e2e_sandbox_lifecycle` | 见 §5（需真实沙箱；阶段 1 用例，阶段 2 仍适用） |

---

## 3. CLI 实际输出摘录

### 3.1 `allox --help`（尾部，阶段 2 新增项）

```text
  --config PATH
  --profile [dev|staging|prod]    Use ~/.allox/<profile>.toml (overridden by
                                  --config).
  -v, --verbose                   Verbose HTTP / health-check logging.
  --no-color
  --version
  -h, --help

Commands:
  aio      Agent tools inside an AIO sandbox (shell, file, browser).
  config   Manage ~/.allox/config.toml.
  file     File operations via OpenSandbox execd (ops / non-AIO path).
  run      Run a command via execd.
  sandbox  Manage sandboxes on OpenSandbox.
  session  Manage local sandbox session (~/.allox/sessions.json).
```

### 3.2 `session --help`

```text
Commands:
  clear    Clear the current session.
  current  Show the current session.
  use      Set current session to an existing sandbox.
```

### 3.3 `sandbox create --help`（阶段 2 变更）

```text
  --ready-timeout TEXT            Max wait for readiness (e.g. 60s). Overrides config.
  -o, --output [table|json|yaml]  Output format (table, json, yaml).
```

### 3.4 `aio exec --help`（可选 SANDBOX_ID + workdir）

```text
Usage: allox aio exec [OPTIONS] [SANDBOX_ID] COMMAND...

Options:
  -w, --workdir TEXT       Working directory (absolute path in sandbox).
  --timeout INTEGER        Command timeout in seconds.
  -o, --output [raw|json]  Output format (raw, json).
```

### 3.5 `run` / `file` / `sandbox renew`

```text
# allox run
Usage: allox run [OPTIONS] [SANDBOX_ID] COMMAND...
  -w, --workdir TEXT
  -o, --output [raw|json]

# allox file
Commands: cat, write

# allox sandbox renew
Usage: allox sandbox renew [OPTIONS] [SANDBOX_ID]
  -t, --timeout TEXT  [required]
  -o, --output [table|json|yaml]
```

### 3.6 `~/.allox/sessions.json`（create 成功后）

```json
{
  "current": {
    "sandbox_id": "ff2b5693-5c8c-480d-bbe5-6beb1c2672ef",
    "aio_url": "http://127.0.0.1:41163",
    "created_at": "2026-06-08T10:15:30+00:00"
  }
}
```

### 3.7 `allox session current -o json`（期望）

```json
{
  "sandbox_id": "ff2b5693-5c8c-480d-bbe5-6beb1c2672ef",
  "aio_url": "http://127.0.0.1:41163",
  "created_at": "2026-06-08T10:15:30+00:00"
}
```

### 3.8 `allox sandbox create -o json`（阶段 2 新增字段）

```json
{
  "id": "ff2b5693-5c8c-480d-bbe5-6beb1c2672ef",
  "image": "ghcr.io/agent-infra/sandbox:latest",
  "aio_url": "http://127.0.0.1:41163",
  "aio_ready_seconds": 8.42,
  "entrypoint": "/opt/gem/run.sh"
}
```

`aio_ready_seconds` 仅在未 `--skip-health-check` 且健康检查通过时出现。

### 3.9 `allox -v sandbox create`（verbose 健康检查，stderr 片段）

```text
[verbose] AIO health check: GET http://127.0.0.1:41163/v1/shell/sessions (timeout 30.0s)
[verbose]   → 502
[verbose]   → 200
[verbose] AIO ready in 8.42s
```

### 3.10 `allox sandbox list`（Rich 表格，table 模式）

非 JSON 时输出 Rich 表格，列含 `ID`、`STATE`（带状态色），而非纯 TSV。

### 3.11 AIO 连接失败（友好提示，exit 1）

```text
Error: Failed to connect to AIO sandbox 'bad-id': ...
Hints:
  • Check endpoint: allox sandbox endpoint bad-id
  • Ensure OpenSandbox server is reachable and firewall allows the port
  • Verify AIO health: GET /v1/shell/sessions returns 200
```

### 3.12 本机无 Server 时 `sandbox create`（与阶段 1 相同）

```text
Sandbox error [INTERNAL_UNKNOWN_ERROR]: None
```

---

## 4. 手工验收：期望输出（平台就绪时）

### 4.1 会话与省略 sandbox_id（2.1）

```bash
allox config set connection.domain localhost:8080
allox sandbox create -o json --timeout 10m | tee /tmp/allox-create.json
cat ~/.allox/sessions.json

allox session current -o json
allox aio exec ls -la                    # 省略 id
allox aio exec -w /home/gem pwd
allox aio screenshot -f /tmp/p2-test.png # 省略 id
allox sandbox endpoint -o json         # 省略 id
```

**期望**：

- `sessions.json` 与 `create -o json` 的 `id` / `aio_url` 一致
- 省略 `sandbox_id` 的 `aio` / `sandbox` 子命令 exit 0

cat ~/.allox/sessions.json
{
  "current": {
    "sandbox_id": "0b15a961-1428-4c51-8c13-c850ff959928",
    "aio_url": "http://127.0.0.1:51305",
    "created_at": "2026-06-08T16:27:45+00:00"
  }
}
allox session current -o json
{
  "sandbox_id": "0b15a961-1428-4c51-8c13-c850ff959928",
  "aio_url": "http://127.0.0.1:51305",
  "created_at": "2026-06-08T16:27:45+00:00"
}
allox aio exec ls -la 
total 52
drwxr-x--- 10 gem  gem  4096 Jun  9 09:33 .
drwxr-xr-x  1 root root 4096 Jun  9 09:33 ..
-rw-r--r--  1 gem  gem   220 Jan  7  2022 .bash_logout
-rw-r--r--  1 gem  gem    27 Jun  9 09:33 .bashrc
drwxr-xr-x  5 gem  gem  4096 Jun  9 09:33 .cache
drwxrwxr-x  6 gem  gem  4096 Jun  9 09:33 .config
drwxr-xr-x  2 gem  gem  4096 Jun  9 09:33 .ipython
drwxr-xr-x  4 gem  gem  4096 Jun  9 09:33 .jupyter
drwxrwxr-x  4 gem  gem  4096 Jun  9 09:33 .local
drwxr-xr-x  3 gem  gem  4096 Jun  9 09:33 .npm
drwxrwxr-x  3 gem  gem  4096 Jun  9 09:33 .npm-global
drwx------  3 gem  gem  4096 Jun  9 09:33 .pki
-rw-r--r--  1 gem  gem   807 Jan  7  2022 .profile
-rw-rw-r--  1 gem  gem     0 Jun  9 09:33 .Xauthority%  
allox aio exec -w /home/gem pwd
/home/gem%       
allox aio screenshot -f /tmp/p2-test.png
╭────────────────────────────────── Screenshot Saved ──────────────────────────────────╮
│ sandbox_id: 835c267b-8a48-4898-976c-44b2369f503f                                     │
│ path: /private/tmp/p2-test.png                                                       │
╰──────────────────────────────────────────────────────────────────────────────────────╯      
allox sandbox endpoint -o json 
{
  "sandbox_id": "835c267b-8a48-4898-976c-44b2369f503f",
  "aio_url": "http://127.0.0.1:58984"
}

### 4.2 session use / clear（2.1）

```bash
allox session clear
allox session current                  # 期望 ClickException
allox session use "$(jq -r .id /tmp/allox-create.json)" -o json
allox session current -o json
```
allox session current
╭────────────────────────────────── Current Session ───────────────────────────────────╮
│ sandbox_id: 835c267b-8a48-4898-976c-44b2369f503f                                     │
│ aio_url: http://127.0.0.1:58984                                                      │
│ created_at: 2026-06-09T01:33:33+00:00                                                │
╰──────────────────────────────────────────────────────────────────────────────────────╯
allox session use "$(jq -r .id /tmp/allox-create.json)" -o json
{
  "sandbox_id": "835c267b-8a48-4898-976c-44b2369f503f",
  "aio_url": "http://127.0.0.1:58984",
  "created_at": "2026-06-09T01:36:41+00:00"
}
allox session current -o json
{
  "sandbox_id": "835c267b-8a48-4898-976c-44b2369f503f",
  "aio_url": "http://127.0.0.1:58984",
  "created_at": "2026-06-09T01:36:41+00:00"
}

### 4.3 输出格式 yaml / list 表格（2.2）

```bash
allox sandbox list
allox sandbox list -o json
allox sandbox list -o yaml
allox sandbox get -o yaml
```

### 4.4 renew（2.4）

```bash
allox sandbox renew --timeout 15m -o json
```

期望 JSON 片段：

```json
{
  "sandbox_id": "ff2b5693-5c8c-480d-bbe5-6beb1c2672ef",
  "expires_at": "2026-06-08T10:30:00+00:00"
}
```

### 4.5 配置项与 profile（2.3 / 2.4）

```bash
allox config set defaults.ready_timeout 60s
allox config set defaults.aio_health_path /v1/shell/sessions
allox config show

cp ~/.allox/config.toml ~/.allox/dev.toml
allox --profile dev sandbox list -o json
```

### 4.6 metadata 默认值（2.3）

创建后通过 `osb sandbox get <id> -o json` 或 Server API 确认 metadata 含：

```json
{
  "tool": "allox",
  "version": "0.1.0"
}
```

用户传入的 `-m key=value` 与之合并，不覆盖 `tool` / `version` 除非显式传入同名键。

### 4.7 清理

```bash
allox sandbox kill -o json             # 省略 id；应清除 current session
allox session current                  # 期望报错「No current session」
```

---

## 5. 功能覆盖对照（ROADMAP 阶段 2）

| ROADMAP 项 | 文档中的实际依据 |
|------------|------------------|
| 2.1 `sessions.json` | §3.6、`test_session.py` |
| 2.1 create 写 session | §3.6、`test_sandbox_create_session.py` |
| 2.1 `session current/use/clear` | §3.2、§4.1–4.2 |
| 2.1 aio 省略 id | §3.4、§4.1 |
| 2.2 `-o table\|json\|raw\|yaml` | §3.3、§4.3、README 对照表 |
| 2.2 `list` Rich 表格 | §3.10、§4.3 |
| 2.2 `aio_ready_seconds` | §3.8、§3.9 |
| 2.2 `aio exec --workdir` | §3.4、§4.1 |
| 2.2 `--verbose` | §3.9 |
| 2.3 默认 metadata | §4.7 |
| 2.3 `--profile` / `--config` | §3.1、§4.6 |
| 2.4 `aio_health_path` / `ready_timeout` | §4.6 |
| 2.4 AIO 失败提示 | §3.11 |
| 2.4 `sandbox renew` | §3.5、§4.4 |
| 2.5 `run` / `file` | §3.5、§4.5 |
| 2.5 README 分工说明 | [README.md](../../../README.md) |
| 2.6 MCP 盘点 | [MCP 指南](../../guides/mcp.md) |
| 2.6 `aio mcp servers/tools/call` | §4.8、`test_aio_commands.py`、`test_mcp_utils.py` |
| 2.6 MCP 集成验收 | `test_integration_mcp.py`、§4.8 |
| 2.6 README MCP 分工 | [README.md](../../../README.md) |

---

## 4.8 MCP CLI（阶段 2.6）

**前提**：已 `sandbox create` 且 session 有效（或显式传入 `sandbox_id`）。

> **命名与镜像差异**：REST 下 browser 工具名为 `browser_navigate`、`browser_screenshot` 等（带 `browser_` 前缀），不是文档简写 `navigate`。`shell` / `file` 等 server 在部分镜像中未写入 `mcp-servers.json`，会返回 404 — 此时用 `aio exec` / `aio read`。详见 [MCP 指南](../../guides/mcp.md)。

### 步骤 1：盘点（必做）

```bash
allox aio mcp servers -o json | jq .
allox aio mcp tools browser -o json | jq '.tools[].name'
```

- **至少**应含 `browser` server；`file` / `shell` / `markitdown` 视镜像而定。
- 记下 `tools[].name` 中的**完整名称**，后续 `mcp call` 必须使用。

### 步骤 2：browser 调用（推荐先测）

```bash
# 导航（工具名以 tools 列表为准）
allox aio mcp call browser browser_navigate \
  --args '{"url":"https://example.com"}' -o json

# 或截图（通常更稳定）
allox aio mcp call browser browser_screenshot -o json
```

(allox-cli) kn0wn@kn0wndeMacBook-Air allox-cli % allox aio mcp tools browser -o json | jq '.tools[].name'
"browser_navigate"
"browser_go_back"
"browser_go_forward"
"browser_form_input_fill"
"browser_get_markdown"
"browser_get_text"
"browser_read_links"
"browser_new_tab"
"browser_tab_list"
"browser_switch_tab"
"browser_close_tab"
"browser_evaluate"
"browser_vision_screen_capture"
"browser_vision_screen_click"
"browser_get_download_list"
"browser_screenshot"
"browser_click"
"browser_select"
"browser_hover"
"browser_get_clickable_elements"
"browser_scroll"
"browser_close"
"browser_press_key"
(allox-cli) kn0wn@kn0wndeMacBook-Air allox-cli % allox aio mcp call browser browser_navigate \
  --args '{"url":"https://example.com"}' -o json
{
  "sandbox_id": "383fabfc-8115-4301-92ef-19b3b4d95187",
  "server": "browser",
  "tool": "browser_navigate",
  "request": {
    "url": "https://example.com"
  },
  "response": {
    "success": true,
    "message": "Successfully executed tool 'browser_navigate' on MCP server 'browser'",
    "data": {
      "meta": null,
      "content": [
        {
          "type": "text",
          "text": "Navigated to https://example.com\nclickable elements(Might be outdated, if an error occurs with the index element, use `browser_get_clickable_elements` to refresh it): \n[]Example Domain\n[]This domain is for use in documentation examples without needing permission. Avoid use in operations.\n[0]<a>Learn more</a>",
          "annotations": null,
          "meta": null
        }
      ],
      "structured_content": null,
      "is_error": false
    },
    "hint": null
  }
}

### 步骤 3：shell / file — 仅当 servers 列表中存在

```bash
# 若 servers 含 shell：
allox aio mcp call shell exec --arg command="echo mcp-hello" -o json

# 若 servers 含 file：
allox aio mcp call file list --args '{"path":"/home/gem"}' -o json
```

**404 `not found in configuration` 时的等效命令**：

```bash
allox aio exec -- echo mcp-hello
allox aio exec -- ls -la /home/gem
```

### 步骤 4：markitdown（可选，server 存在时）

```bash
allox aio exec -- bash -c 'echo "# hi" > /tmp/sample.md'
allox aio mcp call markitdown convert --args '{"path":"/tmp/sample.md"}' -o json
```

### 省略 sandbox_id

```bash
allox aio mcp tools browser -o json    # 使用 current session
```

### 自动化

```bash
uv run pytest -m integration tests/test_integration_mcp.py -v -rs
```

---

## 6. 解除阻塞后复现（完整脚本）

```bash
# 终端 1
docker pull ghcr.io/agent-infra/sandbox:latest
opensandbox-server

# 终端 2
cd allox-cli
source .venv/bin/activate    # 或 uv sync --no-editable
allox config set connection.domain localhost:8080

# ── 创建 + session ──
allox -v sandbox create -o json --timeout 10m | tee /tmp/allox-p2-create.json
cat ~/.allox/sessions.json
allox session current -o json

# ── 省略 sandbox_id ──
allox aio exec ls -la
allox aio exec -w /home/gem pwd
allox aio jupyter run -c 'print(2+2)' -o json
allox aio browser info -o json
allox aio screenshot -f /tmp/allox-p2.png

# ── MCP（阶段 2.6；先盘点再调用）──
allox aio mcp servers -o json | jq .
allox aio mcp tools browser -o json | jq '.tools[].name'
allox aio mcp call browser browser_navigate \
  --args '{"url":"https://example.com"}' -o json
# shell/file 仅当 servers 列表有时再测；否则用 exec：
allox aio exec -- echo mcp-hello
allox aio exec -- ls -la /home/gem

# ── 输出格式 ──
allox sandbox list
allox sandbox list -o yaml
allox sandbox renew --timeout 15m -o json

# ── execd ──
allox run -- echo "execd ok"
allox file cat /etc/hostname
echo "p2" | allox file write /tmp/alox-p2-hello.txt
allox file cat /tmp/alox-p2-hello.txt

# ── session 切换 ──
allox session clear
allox session use "$(jq -r .id /tmp/allox-p2-create.json)"
allox session current

# ── 清理 ──
allox sandbox kill -o json
allox session current    # 应失败

# ── 集成（阶段 1 + 2.6 MCP）──
uv run pytest -m integration tests/test_integration_e2e.py tests/test_integration_mcp.py -v -rs
```

**阶段 2 手工勾选清单**（实跑通过后打勾）：

- [ ] `create` 写入 `~/.allox/sessions.json`
- [ ] `session current` / `use` / `clear` 行为正确
- [ ] `aio *` 省略 `sandbox_id` 可用
- [ ] `sandbox list` Rich 表格 + `-o yaml`
- [ ] `create -o json` 含 `aio_ready_seconds`
- [ ] `-v` 打印健康检查日志
- [ ] `aio exec -w` 生效
- [ ] `sandbox renew --timeout` 返回 `expires_at`
- [ ] `allox run` / `file cat|write` 可用
- [ ] `--profile dev` 读取 `~/.allox/dev.toml`
- [ ] `kill` 后 session 自动清除
- [ ] `aio mcp servers` 至少含 `browser`（其余 server 视镜像）
- [ ] `aio mcp tools browser` 工具名为 `browser_*` 前缀
- [ ] `aio mcp call browser browser_navigate` 或 `browser_screenshot` 成功
- [ ] 无 shell/file MCP 时 `aio exec` 等效命令可用
- [ ] `aio mcp` 省略 `sandbox_id` 可用

---

## 7. 测试基础设施说明

- **`tests/conftest.py`**：`CliRunner` 自动加 `--config <tmp>/config.toml`。
- **`test_session.py`**：使用 `tmp_path` + `monkeypatch` 隔离 `DEFAULT_SESSIONS_PATH`。
- **`test_sandbox_create_session.py`**：mock `SandboxSync.create`，不依赖 Docker。
- **PyYAML**：`-o yaml` 依赖 `pyyaml`（见 `pyproject.toml` dependencies）。
- **`integration` 标记**：见 `pyproject.toml` `[tool.pytest.ini_options] markers`。

---

## 8. 回归检查清单

1. `uv run pytest -q -m "not integration"` → 输出应含 **`27 passed`**（见 §2.2）。  
2. 平台就绪：`uv run pytest -m integration -v` → 至多 **`11 passed`**（1 条 e2e + 10 条 MCP；`shell`/`file`/`markitdown` 未配置时为 skip）。  
3. `allox --help` 含 `session`、`run`、`file`、`--profile`、`-v`（见 §3.1）。  
4. `sandbox create --help` 含 `-o yaml`、`--ready-timeout`（见 §3.3）。  
5. Server 端口与 `connection.domain` 均为 **8080**（OpenSandbox），勿与 8090 混用。

---

## 9. 相关文件

| 路径 | 用途 |
|------|------|
| `src/allox/session.py` | `~/.allox/sessions.json` 读写 |
| `src/allox/commands/session_cmd.py` | `session` 子命令 |
| `src/allox/commands/run_cmd.py` | `run`（execd） |
| `src/allox/commands/file_cmd.py` | `file cat|write`（execd） |
| `src/allox/output.py` | table / json / yaml / raw |
| `src/allox/aio_health.py` | 可配置健康检查 + verbose |
| `src/allox/mcp_utils.py` | MCP 参数解析与输出格式化 |
| `src/allox/commands/aio.py` | `aio mcp servers|tools|call` |
| `docs/guides/mcp.md` | MCP server 盘点与 CLI 示例 |
| `tests/test_mcp_utils.py` | MCP 参数单元测试 |
| `tests/test_integration_mcp.py` | MCP 集成测试（`@integration`） |
| `tests/test_session.py` | session 单元测试 |
| `tests/test_sandbox_create_session.py` | create 写 session mock 测试 |
| `tests/test_integration_e2e.py` | 端到端（阶段 1，阶段 2 回归） |
| [阶段 1 测试记录](./phase-1.md) | 阶段 1 测试记录 |
| [README.md](../../../README.md) | 当前命令用法与 2.0 架构 |
| 历史 `ROADMAP.md` | 1.0 阶段任务；不再属于当前主文档 |
