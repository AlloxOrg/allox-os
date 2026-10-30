# Agent 观测与 profiling：首版结果

日期：2026-10-30。基于 `origin/main@be25aa9`，开发目录 `allox-os-monitoring`，
本地分支 `feat/agent-monitoring-profiling`；原 `allox-os` 中的联合回退改动未修改。

## 本轮交付

独立、默认关闭的 `allox-agent-monitor` 插件：

| 需求 | 首版覆盖 |
|---|---|
| 调用轨迹 | SDK span、父子/因果关系、并行组、循环、参数/结果摘要、错误/取消状态 |
| 模型计时 | 实际客户端请求时长；导入 vLLM queue/TTFT/generation，不猜测缺失值 |
| 工具计时 | 调用整体时长、真正执行器内部时长、显式审批/排队/传输/MCP phase |
| 系统效果 | Core process run 关联、Session cgroup CPU/内存峰值/block I/O 采样接口；文件/网络事件待接入 |
| 监控自身开销 | 发送失败、拒绝、采集时长、序列化字节、存储、开关监控配对 benchmark |
| 跨运行 profiling | 单 trace 及同一 Agent 的多 Session 语义路径聚合；不自动推断任务语义 |

Agent 在 Bubblewrap 内使用只追加的 Session socket，不需要管理 token。
身份与 process run 由受信侧校验和绑定；框架上报与 Core 观测标明不同来源。
具体接口和接入示例见 [插件说明](../../plugins/agent-monitor/README.md)。

## 验证结果

- 服务器：117；独立目录 `/data/hy/Allox/agent-monitoring-20261030-v1`。
- 每项任务前重读在线 CPU，使用 `taskset` 绑定最终 24 个逻辑 CPU：`24–47`。
- Python 3.10：**112 passed，18 skipped**，见 [pytest 原始结果](agent-monitoring-results-20261030/pytest.txt)。
- Ruff 通过；Core 与插件独立 wheel 构建通过。
- Pyright 1.1.414：本地 Node 纯静态检查，**0 errors / 0 warnings**。117 的 Node
  获取遇到证书/网络失败，未把远端类型检查记为通过。
- Core wheel 不包含监控实现，默认关闭时不加载插件；证据见
  [wheel 独立性检查](agent-monitoring-results-20261030/wheel-independence.json)。

测试包含真实 HTTP/Unix socket 通信、真实 SIGSTOP/exec/wait 子进程链路，
但 cgroup/身份和文件快照使用测试替身；不等价于真实 Kata/Btrfs/eBPF 验证。
跳过项需要额外的 Guest 内核、内核工具或显式开启的特权测试环境。

## 开销：目前明显过高

5 组配对、每组 40 个 CPU+等待合成工具调用。同步 SQLite WAL/FULL 模式下，
本次总耗时增幅中位数约 **5169.7%（52.7 倍）**；全部已提交事件得到确认。
这说明逐事件同步落盘不适合直接用于低开销采集，不能将此版本称作性能完成版。

原始数据：[benchmark.json](agent-monitoring-results-20261030/benchmark.json)。
该基线测 SDK + 本地 SQLite 的稳态执行，不含初始化/报表查询，也不是 Session socket、
真实 LLM 或 eBPF 负载；数据写入 117 的临时文件系统，不能外推为 Guest Btrfs 性能。
事件丢失率和计时误差上界仍为 unknown，不会因“收到的事件完整”而宣称全链路零丢失。

## 下一步

1. 优先做有界批处理/异步 exporter，明确 flush、异常退出和崩溃丢失边界后复测。
2. 接实际 Agent 框架、实验室模型服务和 MCP 客户端，验证真实时序与 provider 指标。
3. 将 process-tree/system-effects 的文件、网络和内核事件接入同一 trace；在 Kata Guest
   验证真实 cgroup 身份、资源归属与 collector 故障行为。

本轮未修改 rollback、一致性检测或权限策略，也未提交或推送代码。
