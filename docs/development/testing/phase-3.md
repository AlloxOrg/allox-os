# 阶段 3 测试记录 — 自定义 Runtime 镜像

> 对应历史 Allox 1.0 `ROADMAP.md` 阶段 3.1–3.2；保留作回归资料。  
> 历史记录前置：Docker、OpenSandbox Server、当前 `images/aio-runtime/build.sh` 对应的 Runtime image。

---

## 3.1 衍生镜像

| 项 | 命令 / 路径 | 预期 | 结果 |
|----|-------------|------|------|
| Dockerfile | `images/aio-runtime/Dockerfile` | `FROM ghcr.io/agent-infra/sandbox:<pin>` | |
| apt/pip 依赖 | Dockerfile RUN | `jq`, `httpx` 等已安装 | |
| supervisord | `supervisord.allox_health.conf` | 复制到 `/opt/gem/supervisord/` | |
| nginx | `nginx.allox_health.conf` | 复制到 `/opt/gem/nginx/` | |
| 构建 | `cd images/aio-runtime && ./build.sh` | 产出 `allox/aio-runtime:v2` | |
| 推送（可选） | `REGISTRY=... ./build.sh --push` | 私有仓库可 pull | |
| 配置镜像 | `allox config set defaults.image allox/aio-runtime:v2` | `config show` 显示新 image | |

---

## 3.2 验证自定义镜像

### 创建与健康检查

```bash
allox config set defaults.image allox/aio-runtime:v2
allox sandbox create -o json --timeout 5m
```

| 检查项 | 命令 | 预期 |
|--------|------|------|
| 创建成功 | 上式 | JSON 含 `id`、`aio_url` |
| AIO 就绪 | create 输出 `aio_ready_seconds` | 有数值且 health_check 未超时 |
| 官方 health | `curl -sf $AIO_URL/v1/shell/sessions` | HTTP 200 |

### 自定义能力

```bash
ID=<sandbox_id>
allox aio exec $ID -- cat /opt/allox/image-version.txt
# 预期: allox-aio-runtime:v2

allox aio exec $ID -- jq --version
# 预期: jq 版本号

allox aio exec $ID -- python3 -c "import httpx; print(httpx.__version__)"
# 预期: 0.28.1

allox aio exec $ID -- curl -sf http://127.0.0.1:8080/allox-health
# 预期: {"status":"ok","service":"allox-custom","version":"v1"}
```

### 自定义端口 / endpoint

```bash
allox sandbox endpoint $ID -o json
# aio_url 仍为 8080 入口；/allox-health 经 nginx 代理
```

### 阶段 1 端到端回归（ROADMAP 1.5）

```bash
allox aio exec $ID -- echo hello
allox aio screenshot $ID -f /tmp/phase3-test.png
allox aio jupyter run $ID -c "print(2+2)" -o json
allox aio browser info $ID -o json
allox sandbox kill $ID -o json
```

| 步骤 | 通过 |
|------|------|
| create `-o json` | [ ] |
| aio exec | [ ] |
| aio screenshot | [ ] |
| sandbox kill | [ ] |

---

## 集成测试（pytest）

```bash
# 需先构建自定义镜像
cd images/aio-runtime && ./build.sh

# 指定镜像跑集成用例
export ALLOX_CUSTOM_IMAGE=allox/aio-runtime:v2
uv run pytest -m integration tests/test_integration_custom_image.py -v
```

---

## 删减组件时的文档同步

若 Dockerfile 中移除 Code Server：

- [ ] README 中 VSCode URL 标注「自定义镜像已禁用」
- [ ] `aio browser` / MCP 等仍可用则保留说明

---

## 里程碑

| 日期 | 备注 |
|------|------|
| | 阶段 3 完成 |
