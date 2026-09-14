# Session 网络隔离

Allox OS 可选地为每个 `agent_id/session_id` 建立一个长期存活的 Linux network
namespace。该 namespace 由可信的 workspace daemon 持有，不属于 Session Workspace，
因此 workspace checkpoint/rollback 不会复制或恢复内核网络状态。

该功能默认关闭。关闭时，受追踪执行仍沿用原来的 Guest 网络；启用后可选择三种模式：

| 模式 | 行为 |
|---|---|
| `inherit` | 继承 Allox Guest 网络，不建立 namespace |
| `isolated` | 只有 Session 自己的 loopback，无直接出站路径 |
| `proxy` | 与 `isolated` 相同，但在 `127.0.0.1:3128` 提供可信 HTTP/HTTPS 代理 |

`proxy` 模式没有 veth 和默认路由。namespace 内的 HTTP 代理和 Bubblewrap
内的通用连接工具均通过 Session 专属 AF_UNIX socket，把连接请求交给 Guest
根网络 namespace 中的 broker。每个 Session 使用独立 socket，broker 可连接
公网、私网或 loopback TCP 目标，并把连接结果写入 workspace 之外的
`egress.jsonl`。当前版本尚未加入目标 allowlist。

这一版同时提供 HTTP absolute-form/HTTPS `CONNECT` 和协议无关的字节转发：

```bash
python3 /run/allox/network.py connect <host> <port>
```

工具将 stdin/stdout 与目标 TCP 连接双向绑定，可作为 SSH `ProxyCommand`：

```bash
ssh -o 'ProxyCommand=python3 /run/allox/network.py connect ssh.example.com 22' user@example
```

因此 HTTP(S)、SSH、Git over HTTPS 和 Git over SSH 均可使用同一 broker。SOCKS 环境变量
兼容、逻辑对象地址、allowlist、凭据注入和 ActPlane 策略仍属于后续接口。

## 启动

network namespace 的创建和 loopback 配置要求可信 Guest daemon 具备
`CAP_SYS_ADMIN` 与 `CAP_NET_ADMIN`。Agent 进入 Bubblewrap 前仍会丢弃全部 capability。

```bash
pip install allox-session-network
allox-guest-bootstrap
allox-workspace-daemon \
  --root /var/lib/allox/workspaces \
  --process-audit-root /var/lib/allox/audit/processes \
  --session-network isolated \
  --network-root /run/allox-network \
  --network-socket-root /dev/shm/allox-network
```

网络插件不要求安装 eBPF 进程树插件。已安装但未传入 `--session-network`（默认
`disabled`）时，插件不会被导入，也不会创建 namespace、Broker 或 socket。

`--network-root` 保存策略、Session 身份、日志与审计；必须位于 workspace store 外。
`--network-socket-root` 只保存运行期 Unix socket，应使用短路径和支持 socket 的 Guest
tmpfs。当前 Kata `virtio-9p` 配置下推荐 `/dev/shm/allox-network`。

daemon 启用网络模块后，可在 Session 没有活跃执行时通过管理 RPC 切换模式：

```json
{"action":"network.configure","params":{"agent_id":"agent-a","session_id":"session-1","mode":"proxy"}}
```

查询状态：

```json
{"action":"network.status","params":{"agent_id":"agent-a","session_id":"session-1"}}
```

同一 Session 的多次 `process.start` 会进入同一个 network namespace；不同 Session
可以绑定相同 loopback 端口，且不能通过该地址看到彼此的服务。切换模式或 daemon
退出时会关闭已有 broker 连接并销毁 namespace。Session rollback 若终止进程树，
相关 TCP 连接随进程关闭，但 Session 的网络策略与 namespace 本身保持不变。

## 已验证边界

117 上的真实 Kata + Bubblewrap + eBPF 测试覆盖：

- 同一 Session 两次执行的 network namespace inode 不变；
- 两个 Session 同时绑定 `127.0.0.1:45678`；
- `isolated` 和 `proxy` 中直接连接外部 API 均失败；
- 通过固定代理访问外部 HTTP 服务成功并产生 broker 审计；
- 通用连接工具可透传任意二进制 TCP 数据，并完成真实 SSH 协议握手；
- qwen-codex 的模型调用通过代理完成，智能体真实执行命令并写回 Session Workspace；
- 全部受测 Agent 进程及子进程继续由 eBPF/cgroup 正确归属。
