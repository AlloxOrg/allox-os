# Tests

本目录包含 Allox OS 的单元测试和集成测试。

```bash
# 单元测试与可在当前平台运行的集成测试
uv run pytest -q

# 静态检查
uv run ruff check src tests

# 真实 eBPF/cgroup v2 测试（只能在隔离的特权测试 VM 中运行）
ALLOX_EBPF_TEST=1 uv run pytest tests/test_process_tracking_kernel.py -v

# Linux 文件共享权限、路径与回退互斥测试
uv run pytest tests/test_sharing.py -v

# 实际 Bubblewrap 中的双 Session share 工具（同样要求隔离测试 VM）
ALLOX_EBPF_TEST=1 uv run pytest tests/test_share_kernel.py -v

# 实际 Session network namespace 与 broker 测试
ALLOX_NETWORK_TEST=1 uv run pytest tests/test_network_kernel.py -v
```

真实内核测试包含 `kill_processes=true` 的 Session rollback：先以 `cgroup.kill`
终止包含 `setsid()` 后代的进程树，再验证 workspace 已恢复且审计记录保留。

Share 测试覆盖双向 opt-in、只读/可写、撤销、来源 cgroup 核验、路径与链接逃逸拒绝、
写入与执行/回退互斥，以及目标回退后重新读取 `current/`。默认使用目录快照测试后端；
实际 Btrfs 测试须使用相应的 Guest 数据盘，不把目录复制测试等同于 Btrfs 快照测试。

需要 Linux capability、cgroup v2、Bubblewrap、eBPF 或 network namespace 的测试会在
条件不满足时自动跳过；真实验证应在隔离的 Allox OS/Kata 测试 VM 中运行。
