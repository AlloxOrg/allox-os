# 事后关联插件验证 · 2026-11-11

代码基于 `e028a7a`；可选独立 wheel `allox-trace-correlation`。
实现说明见 [插件 README](../../plugins/trace-correlation/README.md)。

所有运行、构建在 117 的 `/data/hy/Allox/trace-correlation-20261111`，
每个任务重读在线 CPU 并绑定末 24 个逻辑核（本次 24–47）。

- 专项 28 通过：真实 SQLite/WAL、离线 CLI、管理服务分发、只读数据校验、
  Agent/Session/epoch 隔离、turn 歧义、v2/v3 manifest、分页、摘要篡改、
  软链接/FIFO 拒绝、缺少历史数据、不启用则无接口。
- 核心与 system-effects 回归：292 通过、21 个环境相关跳过。
- Ruff、Pyright 静态检查通过；Core 与插件 wheel 构建通过。
- 独立 Python 环境只安装 Core 与关联 wheel，不安装两套采集器：
  entry point 发现及加载通过；可读原 Qwen 实验的真实 trace。
- 旧 Qwen 归档缺少对应 Core audit，结果为 `missing_core_record`，
  没有返回伪造的内核关联。完整 eBPF 事件关联使用显式 Observer 格式 fixture
  验证，本轮没有新增探针，也没有新采集真实 eBPF 事件。

[原始测试和构建日志](evidence/trace-correlation-20261111/validation.log)；
[独立安装与历史数据试读](evidence/trace-correlation-20261111/package-smoke.json)。
