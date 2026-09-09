# Tests

本目录包含 Allox OS 的单元测试和集成测试。

```bash
# 单元测试
uv run pytest -m "not integration" -q

# 静态检查
uv run ruff check src tests

# OpenSandbox、Kata 和 Runtime image 集成测试
uv run pytest -m integration -v

# 真实 eBPF/cgroup v2 测试（只能在隔离的特权测试 VM 中运行）
ALLOX_EBPF_TEST=1 uv run pytest tests/test_process_tracking_kernel.py -v
```

真实内核测试包含 `kill_processes=true` 的 Session rollback：先以 `cgroup.kill`
终止包含 `setsid()` 后代的进程树，再验证 workspace 已恢复且审计记录保留。

`integration` marker 标识依赖完整运行环境的测试，其余测试可在本地开发环境执行。
