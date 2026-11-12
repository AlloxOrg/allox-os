# Post-hoc trace correlation

独立发行包 `allox-trace-correlation`，默认不安装、不启用。只查询已有记录，
不启动 eBPF 探针，不修改 collector / SDK，不挂载任何管理入口给 Agent。
无需安装 agent-monitor 或 system-effects 插件的代码，输入仍须由它们事先采集。

## 接入

```bash
pip install ./plugins/trace-correlation
allox-trace-correlate --trace-root /var/log/allox/agent-monitor \
  --audit-root /var/log/allox/processes --agent-id a --session-id s \
  --run-id RUN_ID --limit 50
```

离线命令不需要运行 workspace daemon。trace 数据库使用 SQLite `mode=ro` /
`query_only` 读取，包括已提交的 WAL；不打开 TraceStore 写接口。
它可以使用 SQLite 的 WAL/SHM 锁文件机制，不承诺完全不触碰 SQLite 辅助文件。
不更改源 trace、事件流、receipt、manifest 或 workspace。

也可在受认证的管理端启用：

```bash
# ALLOX_WORKSPACE_TOKEN 由受信部署环境设置，不交给 Agent。
allox-workspace-daemon --root /var/lib/allox/workspaces \
  --process-audit-root /var/log/allox/processes \
  --correlation-trace-root /var/log/allox/agent-monitor
```

仅开启关联插件不会启动进程服务，不需要 cgroup/eBPF 内核能力。
管理 daemon 原有的 Btrfs store 前提仍适用。两个输入目录都必须在 workspace 回退范围外。

RPC：

- `correlation.trace`：`agent_id, session_id, trace_id` → Core process span 的 run 引用。
- `correlation.run`：`agent_id, session_id, run_id, trace_id?` → 关联记录及一页事件。
  `stream` 可选 `system-effects.jsonl` / `process-lifecycle.jsonl`，
  `offset` 为事件序号，`limit` 默认 50、最多 200。用 `next_offset` 翻页。
- `correlation.status`：插件开关状态。

## 关联与可信度

通过 Core-owned `process_run_id` 精确关联，不靠 PID 或时间邻近猜测。
检查 process state 与 Observer receipt 的 Agent/Session/run/epoch，
如 Core 已记录 monitor trace/span ID，也必须相符。查询不信任 receipt 里的
绝对路径，只按配置 root 和经过验证的 run ID 访问固定相对路径。

支持 Observer v2 / v3 的 artifact manifest；按 receipt 中记录的 SHA-256 核对
manifest，再按 manifest 核对所选 JSONL 的 SHA-256 和长度。不跟随证据目录/文件软链接。
JSON、事件条数、行大小和单页输出均有界；每次翻页重新核验已提交流。
同一 Correlator/daemon 对不超过 8 MiB 的流可缓存已验证 JSONL 的行索引：
每次查询仍重新读取全部内容并核对 SHA256/长度，只有摘要相同才复用索引、
仅解析所选页。最多缓存 4 个索引、合计 200,000 个行边界；更大的流保持原有
有界逐行验证路径。缓存不绕过 no-follow、文件变化检查、Agent/Session/run 绑定或
不完整证据状态。首次查询与每次新启动的离线 CLI 仍需完整解析，未宣称一样快。
源文件缺失、尚未结束、未提交入 manifest 分别返回
`missing_evidence`、`run_not_finalized`、`stream_not_committed`。
原 Core 进程记录已被清理时返回 `missing_core_record`，不会仅凭 trace 强配事件。
身份或摘要不符直接拒绝。

结果区分：

- `integrity=matched_recorded_sha256`：与已记录摘要一致，**不是数字签名**。
- `recorded_verification`：保留 Observer 先前的 verifier 结果；本插件不重新运行 verifier。
- `observer_evidence_status` / `recovery_verdict`：原样保留，不把“不完整”或“未确定”变为成功。
- `turn.status=declared_parent_turn`：Core process span 有 turn 声明，且关联的
  同 run span 没有冲突的 turn 声明。它是应用上下文，不是内核证明的语义归属。
  多 turn 返回 `ambiguous`；没有可用的父 turn 时不从时间窗口猜测。

不推断由 Unix socket / D-Bus / 外部 daemon 代办的间接效果；不宣称精确到某一个
tool span。没采集过的 eBPF 事件不能事后补出。原事件可能包含路径、参数等敏感内容，
关联输出仅供受信管理端使用，默认未作为 Agent 工具暴露。
