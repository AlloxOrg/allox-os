# allox-system-effects

[English](README.md) | 简体中文

`allox-system-effects` 是 Allox Workspace Daemon 的系统效果观测插件。它在命令运行前后采集状态，并用 eBPF 记录命令进程树产生的 syscall、内核对象和进程生命周期证据。

## 它在 Allox 中怎样工作

```text
allox-workspace-daemon（特权）
├── deployment-observe（Bubblewrap 外，负责观测）
└── Session cgroup
    └── Bubblewrap
        └── Agent 命令
```

Bubblewrap（`bwrap`）是 Linux 进程沙箱工具，不是虚拟机，也不是 Docker。Allox 用它为 Agent 命令创建独立的 PID 和 mount namespace、私有 `/tmp`，并只暴露当前 Session 的 `/workspace` 和 Agent 的 `/agent/shared`；`/usr`、`/etc` 等系统目录只读，同时清空环境变量并丢弃 Linux capabilities。

Bubblewrap 命令本身默认不创建独立 network namespace；如果启用 Allox 的 Session 网络功能，网络 namespace 由该独立层配置。当前也没有 user namespace、seccomp 或资源配额。无论是否隔离网络，Agent 和 Observer 都共享目标 Linux 内核，因此 Observer 能看到 Agent 对这个内核直接造成的效果。Observer 自身运行在 Bubblewrap 外，Agent 无需、也不应在沙箱里启动 Observer。

当前插件有两种兼容模式：

- Allox `DIRECT` 执行路径，即由 `process.start` 直接启动的 Bubblewrap 命令；
- 默认协议 v2 的 A→B 观测：A 是执行前状态，B 是进程树退出后的状态；
- 显式 recovery 协议 v3：采集 A，执行 Agent 后采集 B，由受信 helper 执行 Allox
  Session Workspace rollback，再采集 C 并比较 A→B、B→C、A→C；
- 以 Session cgroup 为边界的直接调用者归因；
- syscall、对象和进程生命周期记录；
- 可选的文件系统、BPF、sysctl、mount、网络配置、内核模块和残留进程 S0/S1 确认；
- 离线证据校验和 fail-closed Session fencing。

没有 `process.start.params.recovery` 时继续使用 v2，不会自动 rollback。指定 recovery
才进入 v3，且 recovery helper、独立 recovery verifier 和它们的信任配置必须已启用。
当前适配仍只支持 Allox `DIRECT` 执行；这不等于 Docker/Kata 已接入。

Allox 的启动包装代码与 Agent 位于同一个 Session cgroup，所以在较宽的文件系统
scope 中可能看到 `.allox-tmp` 等执行层效果；这能证明效果来自受观测执行域，但不能
单独证明它由 Agent 代码亲自写入。

## 1. 前置条件

目标 Linux 需要：

- cgroup v2，并支持 `cgroup.freeze`；
- BTF；
- BPF LSM；
- Bubblewrap；
- Btrfs；Allox workspace root 必须位于 Btrfs 上；
- root 或具备等价特权的 daemon。

先检查：

```bash
test -r /sys/kernel/btf/vmlinux
test -e /sys/fs/cgroup/cgroup.freeze
grep -qw bpf /sys/kernel/security/lsm
command -v bwrap
command -v btrfs
findmnt -T /var/lib/allox/workspaces -no FSTYPE
```

最后一条必须输出 `btrfs`。推荐内核配置见 [kernel/allox-system-effects.config](kernel/allox-system-effects.config)。

## 2. 构建 Observer

在 Linux/Observer 仓库中执行：

```bash
cd /path/to/linux/tools/bpf/bpf-state-observer
make release system-effects-bpf process-lifecycle-bpf
```

需要得到：

```text
.output/cargo-target/release/deployment-observe
.output/cargo-target/release/deployment-observe-verify
.output/cargo-target/release/deployment-observe-recovery-verify
.output/system_effects.bpf.o
.output/process_lifecycle.bpf.o
```

安装为稳定、root 管理的文件：

```bash
sudo install -d -m 755 /usr/libexec/bpf-state-observer
sudo install -m 755 \
  .output/cargo-target/release/deployment-observe \
  .output/cargo-target/release/deployment-observe-verify \
  .output/cargo-target/release/deployment-observe-recovery-verify \
  /usr/libexec/bpf-state-observer/
sudo install -m 644 \
  .output/system_effects.bpf.o \
  .output/process_lifecycle.bpf.o \
  /usr/libexec/bpf-state-observer/
```

这些文件必须是普通文件，不能被 group/world 写入，也不要用可变的软链接替代。

## 3. 安装 Allox 和插件

开发目录安装示例：

```bash
sudo python3 -m venv /opt/allox-venv
sudo /opt/allox-venv/bin/pip install /path/to/allox-os
sudo /opt/allox-venv/bin/pip install /path/to/allox-os/plugins/system-effects
```

也可以先构建 wheel，再把 Allox 和插件 wheel 安装到同一个虚拟环境。
插件安装会提供 root 管理的 console script：

```text
/opt/allox-venv/bin/allox-system-effects-recover
```

它是唯一受支持的 v3 recovery helper：Observer 校验其 SHA-256 后执行它，让它调用
Allox WorkspaceStore 的 deferred-cleanup rollback。不要换成接受 Agent 参数或执行任意
shell 的通用 helper。

准备运行目录：

```bash
sudo install -d -m 700 /var/lib/allox/workspaces
sudo install -d -m 700 /var/log/allox/processes
findmnt -T /var/lib/allox/workspaces -no FSTYPE
```

## 4. 配置状态确认范围

三个 scope 文件都是可选的：

```text
/etc/allox/effects/bpf-scope.json
/etc/allox/effects/filesystem-scope.json
/etc/allox/effects/linux-scope.json
```

不提供 scope 时，Observer 仍会记录操作、对象和进程历史，但不会对相应领域给出 S0/S1 最终状态结论。

可以从 Observer 仓库的示例开始：

```text
/path/to/linux/tools/bpf/bpf-state-observer/config/default-scope.json
/path/to/linux/tools/bpf/bpf-state-observer/config/filesystem-scope.example.json
```

文件系统 scope 中，Bubblewrap 内的 `/workspace` 十六进制路径是：

```text
2f776f726b7370616365
```

`/agent/shared` 的十六进制路径是 `2f6167656e742f736861726564`。v3 中插件会把这两个
Bubblewrap 路径分别映射到宿主视角的 Session `current/` 和 Agent `shared/`，并要求
Observer 从自己的 mount namespace 捕获 A/B/C。这样 rollback 交换 `current` 子卷后，
C 解析的是新的 `current`，而不是仍指向旧 B 子卷的保留 mount namespace。其他 scope
根不会自动改写。

插件会在每次运行时写入实际的 mount/network namespace PID 与 inode，不要在配置里硬编码某次运行的 PID。scope 也必须是 daemon 信任的普通文件，并且不能被 group/world 写入。

## 5. 启动 Workspace Daemon

基础启动示例：

```bash
TOKEN='replace-with-a-random-management-token'
OBSERVER_DIR=/usr/libexec/bpf-state-observer
REC_SHA=$(sha256sum "$OBSERVER_DIR/system_effects.bpf.o" | awk '{print $1}')
LIFE_SHA=$(sha256sum "$OBSERVER_DIR/process_lifecycle.bpf.o" | awk '{print $1}')
RECOVER=/opt/allox-venv/bin/allox-system-effects-recover
RECOVER_SHA=$(sha256sum "$RECOVER" | awk '{print $1}')

sudo env ALLOX_WORKSPACE_TOKEN="$TOKEN" \
  /opt/allox-venv/bin/allox-workspace-daemon \
  --root /var/lib/allox/workspaces \
  --listen 127.0.0.1:8092 \
  --process-tracking disabled \
  --process-audit-root /var/log/allox/processes \
  --process-cgroup-root /sys/fs/cgroup/allox \
  --system-effects-observer "$OBSERVER_DIR/deployment-observe" \
  --system-effects-verifier "$OBSERVER_DIR/deployment-observe-verify" \
  --system-effects-recovery-verifier "$OBSERVER_DIR/deployment-observe-recovery-verify" \
  --system-effects-recovery-helper "$RECOVER" \
  --system-effects-recovery-helper-sha256 "$RECOVER_SHA" \
  --system-effects-recovery-timeout-ms 120000 \
  --system-effects-recovery-max-output-bytes 65536 \
  --system-effects-recorder-object "$OBSERVER_DIR/system_effects.bpf.o" \
  --system-effects-recorder-sha256 "$REC_SHA" \
  --system-effects-lifecycle-object "$OBSERVER_DIR/process_lifecycle.bpf.o" \
  --system-effects-lifecycle-sha256 "$LIFE_SHA" \
  --system-effects-profile full \
  --system-effects-object-resolution KERNEL_OBJECT_EXPANDED_PARTIAL \
  --system-effects-ready-timeout 60 \
  --system-effects-finalize-timeout 120 \
  --system-effects-host-lock /run/allox-system-effects.lock
```

要启用全部现有状态确认器，再加入：

```bash
  --system-effects-bpf-scope /etc/allox/effects/bpf-scope.json \
  --system-effects-filesystem-scope /etc/allox/effects/filesystem-scope.json \
  --system-effects-linux-scope /etc/allox/effects/linux-scope.json
```

`--process-tracking disabled` 只关闭旧的、会重复加载 hook 的 process-tree provider；`process.start` 仍可使用，进程生命周期由本插件记录。

如果只需要兼容的 v2 A→B 模式，可以省略五个
`--system-effects-recovery-*` 参数。启用 v3 时，helper 和 recovery verifier 必须是
canonical、单硬链接、root/daemon 所有且 group/world 不可写的普通可执行文件；helper
SHA-256 必须是 64 位小写十六进制值。上面的 `RECOVER_SHA` 是部署时计算示例，不是可
跨安装复制的固定值。

生产部署建议将 daemon 交给 systemd 管理，并把 token 放入权限受限的 EnvironmentFile，不要写入仓库。

## 6. 创建 Agent、Session 并执行命令

下面用 HTTP RPC 演示。先定义一个辅助函数：

```bash
TOKEN='replace-with-a-random-management-token'

rpc() {
  curl -sS http://127.0.0.1:8092/v1/rpc \
    -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/json' \
    -d "$1"
}
```

创建 Agent 和 Session：

```bash
rpc '{"action":"agent.create","params":{"agent_id":"agent-a"}}'
rpc '{"action":"session.create","params":{"agent_id":"agent-a","session_id":"session-1"}}'
```

启动一次受观测命令：

```bash
START=$(rpc '{
  "action":"process.start",
  "params":{
    "agent_id":"agent-a",
    "session_id":"session-1",
    "argv":["/bin/sh","-c","printf \"hello from agent\\n\" > effect.txt"],
    "env":{"DEMO":"1"},
    "timeout":60
  }
}')

RUN_ID=$(printf '%s\n' "$START" | jq -r '.result.run_id')
printf '%s\n' "$RUN_ID"
```

`process.start` 在 Observer 已经 READY、A 快照完成并释放 Agent 后返回；这不代表命令已经结束。

查询状态：

```bash
rpc "{
  \"action\":\"process.get\",
  \"params\":{
    \"agent_id\":\"agent-a\",
    \"session_id\":\"session-1\",
    \"run_id\":\"$RUN_ID\"
  }
}" | jq .
```

重复查询，直到 `state` 变成 `completed`、`failed` 或 `stopped`。

一次健康的成功运行通常应同时满足：

- `state == "completed"`；
- `root_exit_code == 0`；
- `lifecycle_complete == true`；
- `session_fenced == false`；
- `cgroup_removed == true`；
- system-effects feature 状态为 `FINISHED`；
- offline verification 状态为 `VERIFIED`。

## 7. 显式执行 A→B→rollback→C

v3 recovery 是每次 `process.start` 的显式选择，不会改变未带 recovery 的请求。最短
示例是让 Allox 在释放 Agent 前自动创建并 pin 一个 baseline checkpoint：

```bash
START=$(rpc '{
  "action":"process.start",
  "params":{
    "agent_id":"agent-a",
    "session_id":"session-1",
    "argv":["/bin/sh","-c","printf changed > effect.txt"],
    "timeout":60,
    "recovery":{
      "scrub_runtime":true,
      "require_consistent":true
    }
  }
}')
```

`recovery` 支持四个键：

- `checkpoint_id`：可省略；省略时创建并 pin `baseline-<run_id>`。指定时必须是完整、
  已存在的 checkpoint ID，不接受前缀；
- `recovery_id`：可省略；省略时生成 `recovery-<run_id>`，显式值必须在该恢复合同中唯一；
- `scrub_runtime`：默认 `true`，恢复时清理 `.allox-tmp` 的运行时内容；
- `require_consistent`：默认 `true`。当离线证据有效但 A→C 不是
  `CONSISTENT_WITHIN_SCOPE` 时仍 fence Session；设为 `false` 只改变是否 fence，绝不
  把不一致改写成一致。

也可以先创建一个精确 checkpoint，再指定它：

```bash
rpc '{"action":"checkpoint.create","params":{
  "agent_id":"agent-a","session_id":"session-1","checkpoint_id":"clean-v1"
}}'

rpc '{"action":"process.start","params":{
  "agent_id":"agent-a","session_id":"session-1",
  "argv":["/bin/sh","-c","printf changed > effect.txt"],
  "recovery":{"checkpoint_id":"clean-v1","recovery_id":"experiment-1"}
}}'
```

v3 生命周期为：冻结的 Agent 进程树上采集 A → 放行 Agent → 整个 Bubblewrap/Agent
进程树退出 → Core 证明同一 Session cgroup 已空并冻结后采集 B → Observer 调用固定且
hash-pinned 的 `allox-system-effects-recover` → Allox 交换 Session Workspace `current`
→ 采集 C → 独立 verifier 重放和校验全部证据。执行 lease 会一直保留到 C 和离线验证
结束，旧 `current` 子卷的删除也推迟到 C 证据提交之后；当前实现没有 live keeper。

Allox rollback **只恢复当前 Session Workspace**。它不恢复 `/agent/shared`，也不恢复
BPF、sysctl、mount、本机网络配置、内核模块等目标 Linux 全局状态。因此 Agent 在这些
scope 留下的效果会在 A→C 中如实显示为 `INCONSISTENT_WITHIN_SCOPE`；快照不完整、身份
变化或静止条件不足则应为 `INCONCLUSIVE`。

选择 scope 时还要接受两个严格边界：

- protocol v3 尚未接入 Host 签名的 `exclusive-vm-v1` 合同，因此 BPF 的 A/C 清单相同
  也不能授权 `UNCHANGED_WITHIN_SCOPE`。启用 BPF confirmer 且没有正向 BPF 差分时，
  总 recovery verdict 按设计会是 `INCONCLUSIVE`；
- 通用文件系统 confirmer 会比较原始 device/inode/时间戳。Btrfs snapshot swap 即使
  恢复了路径内容和权限，也可能改变子卷对应的 `st_dev` 与根目录 ctime；插件会保留
  这些原始差异，不会把它们静默改写成“Btrfs 语义一致”。

## 8. 查看 Observer 记录

每次 run 的证据目录为：

```bash
AUDIT="/var/log/allox/processes/$RUN_ID"

sudo jq . "$AUDIT/system-effects-receipt.json"
sudo jq . "$AUDIT/system-effects-verification.json"
# 以下三个文件仅在对应 scope 启用时存在
sudo jq . "$AUDIT/system-effects/filesystem-state-comparison.json"
sudo jq . "$AUDIT/system-effects/bpf-state-verdict.json"
sudo jq . "$AUDIT/system-effects/linux-state-comparison.json"
sudo less "$AUDIT/system-effects/system-effects.jsonl"
sudo less "$AUDIT/system-effects/process-lifecycle.jsonl"
```

各文件含义：

- `system-effects-receipt.json`：Allox 与 Observer 的运行收据、身份和完成状态；
- `system-effects-verification.json`：离线 verifier 的重放与完整性结论；
- `system-effects.jsonl`：syscall 和对象效果事件；
- `process-lifecycle.jsonl`：fork、exec、exit 等生命周期事件；
- `*-state-comparison.json`：v2 A→B 的最终状态差分。

protocol v3 的 `system-effects/` 还包含：

```text
snapshot-a.json
snapshot-b.json
snapshot-c.json
agent-delta.json       # A→B，Agent 产生了什么
recovery-delta.json    # B→C，恢复撤销/新增了什么
residual-delta.json    # A→C，恢复后还残留什么
recovery.json          # helper、checkpoint、时间、退出状态和输出摘要
recovery-verdict.json
recovery-artifact-manifest.json
```

`system-effects-receipt.json` 和 `system-effects-verification.json` 会绑定 recovery verifier
结果及 `checkpoint_id`/`recovery_id`。`verified=true` 或状态 `VERIFIED` 只表示 artifact
hash、协议绑定和离线重放通过，**不表示 rollback 一致**；必须另外读取 verdict。健康的
恢复结论是 `CONSISTENT_WITHIN_SCOPE`，另有 `INCONSISTENT_WITHIN_SCOPE` 和
`INCONCLUSIVE`。

`process.events` 是旧 process-tree provider 的接口，不是本插件事件的读取入口。

注意：`VERIFIED` 表示证据结构、绑定和离线重放通过，不等于“Linux 的所有状态都被完整覆盖”。如果没有独占 actor 合同，或者 scope、静止条件、事件完整性不足，某些领域得到 `INCOMPLETE` 或 `INCONCLUSIVE` 是正确行为。关闭旧 provider 后，顶层兼容字段 `trace_complete=false` 也可能出现，应以 system-effects receipt/verification 为准。

## 9. 常见问题

### Observer 在 READY 前失败

检查 BTF、BPF LSM、BPF object 路径与 SHA-256。BPF LSM 没有启用时不能静默降级。

### Workspace 创建或 checkpoint 失败

确认 `--root` 所在文件系统是 Btrfs，并检查 daemon 权限。

### scope 被拒绝

确认它是普通文件、owner 可信、group/world 不可写，且内容满足对应 schema。

### 第二个 run 因锁冲突失败

`--system-effects-host-lock` 是宿主级协作锁。具有重叠全局确认域的 run 不应并发；通常每个 workspace root 只运行一个 daemon。

### 请求 recovery 时在 Agent 启动前失败

确认 daemon 已同时配置 recovery helper 路径/hash 和 recovery verifier，指定 checkpoint
是完整且存在的 ID，并检查 helper/recovery verifier 的 owner、mode、硬链接数和实际
SHA-256。Agent 不能通过 RPC 选择 helper 或传入任意 helper argv。

### 报告里出现非 Agent 产生的变化

BPF、sysctl、mount、网络和模块等快照可能覆盖宿主全局状态。如果同一时间还有外部 actor 修改这些状态，差分会受到污染。要对“没有变化”给出强结论，需要独占、静止且 scope 明确的实验合同。

## 当前安全边界

这套接入适合在受控目标 Linux 上观察由 Allox 直接启动的合作型 Agent。它能可靠建立“事件由目标 Session cgroup 内进程直接发起”的证据，但不会推断 D-Bus、Unix socket 或其他 daemon 委托造成的因果关系，也不能防御拥有足够宿主特权、可以迁移 cgroup、卸载 Observer 或篡改证据的 workload。

当前实现和仓库内测试覆盖 DIRECT 适配、协议状态机、deferred-cleanup rollback、artifact
校验及错误路径。2026-09-19 又在真实 privileged QEMU 中完成了一次协议 v3 验证：
自动 baseline、A/B/C、Btrfs deferred rollback、37 个闭集 artifact 和独立 recovery
verifier 均完成，离线结果为 `VERIFIED`。由于 BPF 阴性结论没有独占合同，并且原始
Btrfs/mount/初始进程身份在 A/C 间存在机制性变化，完整域 verdict 被如实保留为
`INCONCLUSIVE`，`require_consistent=true` 也正确 fence 了 Session。这证明 fail-closed
接入链路真实可运行，不等于“所有域已经一致恢复”。第二次运行设置
`require_consistent=false`，证据仍保持 `INCONCLUSIVE`，但 run 正确以
`release_allowed=true`、`session_fenced=false` 完成，证明该参数只改变放行策略、
不会篡改 verdict。Docker/Kata 仍需要各自的生命周期与身份 adapter，不能把 DIRECT
的结果外推给它们。
