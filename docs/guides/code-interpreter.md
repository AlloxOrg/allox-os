# Code Interpreter 镜像评估（阶段 3.3）

> **结论**：`opensandbox/code-interpreter` 适合作为**轻量多语言代码沙箱**的第二镜像，与 AIO 并行；**不提供** `/v1` AIO API，应走 **execd**（`allox run` / `allox file`）而非 `allox aio *`。

---

## 对比

| 维度 | AIO (`agent-infra/sandbox`) | Code Interpreter (`opensandbox/code-interpreter`) |
|------|------------------------------|---------------------------------------------------|
| 定位 | 全能 Agent 沙箱（浏览器 + Shell + MCP + VSCode） | 多语言代码执行 + Jupyter |
| Agent API | `/v1/*`（agent-sandbox SDK） | 无 AIO 面 |
| 运维 API | execd（opensandbox SDK） | execd |
| 默认 entrypoint | `/opt/gem/run.sh` | `/opt/opensandbox/code-interpreter.sh` |
| 健康检查 | `GET /v1/shell/sessions` | 不适用；建议 `skip_health_check` |
| 镜像体积 | 较大 | 相对较小 |
| 语言 | Python/Node 等（容器内预装） | Python/Java/Node/Go 多版本可切换 |

## 推荐使用方式

### Profile：`code`

使用示例配置 `examples/profiles/code.toml.example`：

```bash
cp examples/profiles/code.toml.example ~/.allox/code.toml
allox --profile code sandbox create -o json --timeout 30m
allox --profile code run -- python3 --version
allox --profile code run -- source /opt/opensandbox/code-interpreter-env.sh python 3.12 && python3 --version
allox --profile code file cat /etc/os-release
allox --profile code sandbox kill
```

配置要点：

```toml
[defaults]
image = "opensandbox/code-interpreter:latest"
entrypoint = ["/opt/opensandbox/code-interpreter.sh"]
skip_health_check = true   # 无 AIO /v1
# aio_port / aio_health_path 对 code 镜像无效
```

### 为何不新增 `allox code` 命令组？

- Code Interpreter 无独立 REST 产品面，能力与现有 **`allox run` / `allox file`** 完全重叠。
- 通过 **`--profile code`** + 不同 `defaults.*` 即可切换镜像，CLI 代码零重复。
- 若未来 Code Interpreter 暴露专用 HTTP API，再考虑 `allox code` 子命令。

## 构建与 CI

参考 OpenSandbox 官方脚本：

```bash
cd ../OpenSandbox/sandboxes/code-interpreter
TAG=latest ./build.sh   # 需 docker buildx + 推送权限
```

本地快速构建（单架构）：

```bash
cd ../OpenSandbox/sandboxes/code-interpreter
docker build -t opensandbox/code-interpreter:latest .
```

Allox 仓库内可提供 CI job（阶段 4 可选）：在 `images/aio-runtime/build.sh` 模式上为 code-interpreter 增加 `.github/workflows/` 或文档化调用上游 `build.sh`。

## 验收标准（若启用 code profile）

- [ ] `allox --profile code sandbox create` 成功（无 AIO health 超时）
- [ ] `allox --profile code run -- python3 -c "print(1)"` 输出 `1`
- [ ] `allox aio exec` **应失败或不可用**（预期行为，勿混用 API）

## 相关路径

| 资源 | 路径 |
|------|------|
| 上游 Dockerfile | `OpenSandbox/sandboxes/code-interpreter/Dockerfile` |
| 构建脚本 | `OpenSandbox/sandboxes/code-interpreter/build.sh` |
| 示例 profile | `examples/profiles/code.toml.example` |
