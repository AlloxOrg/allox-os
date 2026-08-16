# Tests

本目录包含 Allox OS 的单元测试和集成测试。

```bash
# 单元测试
uv run pytest -m "not integration" -q

# 静态检查
uv run ruff check src tests

# OpenSandbox、Kata 和 Runtime image 集成测试
uv run pytest -m integration -v
```

`integration` marker 标识依赖完整运行环境的测试，其余测试可在本地开发环境执行。

