# Session ID 文件共享（第一版）

同一 Allox 中，双方 Session 都开启 share 后，可通过 `agent_id/session_id` 和相对
路径访问。没有邀请密钥、公私钥或白名单；**开启意味着允许所有其他已开启 share 的
Session 访问自己的共享范围**。ID 用于寻址，不是密码。默认关闭、默认只读，直到
主动关闭；此版没有一次性、限时授权，也没有跨 VM 转发。

## 启动与使用

在满足 [进程追踪前置条件](process-tracking.md) 的 Guest 内，启动服务：

```sh
# 仅保存在可信 Guest 服务环境，不交给 Agent，也不需要在 Session 之间交换。
export ALLOX_WORKSPACE_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
allox-guest-bootstrap
allox-workspace-daemon \
  --root /var/lib/allox/workspaces \
  --process-tracking ebpf \
  --process-audit-root /var/lib/allox-process-audit \
  --share-tools
```

`--share-tools` 给随后通过 `process.start` 启动的每个 Bubblewrap 挂入
`/run/allox/share.py` 和该 Session 专属的 `/run/allox/share.sock`。
Session 可以在自己的进程中执行以下命令；无需前端或修改 Agent 框架。
开启工具入口并不自动开启共享。

启用 `--share-tools` 时管理 HTTP API 必须配置上述 token，即使仅监听 loopback；
否则 Agent 可能绕过专属 share socket 直接调用管理 API。这个 token 是现有控制面的
管理凭据，与 Session 之间的共享授权无关。生产部署由服务管理器保管，Agent 的
Bubblewrap 环境不继承它。

Session B（假设 ID 是 `agent-b/session-1`）：

```sh
mkdir -p /workspace/output
printf 'hello from B\n' > /workspace/output/report.txt
python3 /run/allox/share.py enable --scope output
```

Session A：

```sh
python3 /run/allox/share.py enable
python3 /run/allox/share.py list agent-b/session-1
python3 /run/allox/share.py read agent-b/session-1 report.txt > /workspace/report-from-b.txt
python3 /run/allox/share.py status
```

`enable` 不指定 scope 时共享自己的整个 `current/`，指定 scope 时目录必须已存在。
访问相对路径从目标的 scope 开始：上例 `report.txt` 对应 B 的 `output/report.txt`。
同名 Session 可能属于不同 Agent，所以地址必须包含两个 ID。

B 允许写入时：

```sh
python3 /run/allox/share.py enable --scope output --permission write
```

B 的活跃 tracked run 结束后，A 可以执行：

```sh
printf 'updated by A\n' | python3 /run/allox/share.py write agent-b/session-1 report.txt
```

`write` 包含读权限；每次写入完整替换文件或创建文件，父目录必须存在。此版写入文件
模式为 `0600`，不保留旧文件元数据。不支持删除、重命名、执行、目录创建或文件挂载。
`read` 向 stdout 输出原始字节，`write` 从 stdin 接收原始字节；支持二进制内容。
每文件最多 256 KiB，每次列目录最多 1000 项，超限报错，不静默截断。

任一方可以关闭自己：

```sh
python3 /run/allox/share.py disable
```

关闭与在途文件操作串行化；返回成功后，新请求不能再通过。对方此前已经读取或复制
的文件不会被收回。系统安装也提供 `allox-share` 入口；Bubblewrap 内始终可用上面的
stdlib Python 脚本方式，不依赖 Agent 的 Python 环境安装 Allox。

## 身份、路径与回退

- Share 实例在可信 daemon 内按 Session 划分，不为每个 Session 启动独立 daemon。
  Unix socket 使用 `SO_PEERCRED` 获取进程 PID，并核验它属于端点对应的 Session
  cgroup；工具不能通过填写来源 ID 冒充其他 Session。原始管理 RPC 仍属于可信控制面，
  管理端 token 不应交给 Agent。此版 Agent 工具依赖现有 tracked/cgroup 路径。
- 仅代理当前 Session workspace 的普通文件。使用目录 FD 和 `O_NOFOLLOW` 逐段打开，
  拒绝绝对路径、`..`、符号链接、硬链接文件、特殊文件和跨文件系统路径。
- share 策略在 `.allox/shares/`，访问来源、目标、路径与结果在目标 Session 的
  `.allox/events/` 下。它们位于 `current/` 外，不随 workspace rollback 恢复。
  记录写入准备和完成事件，不记录文件内容；这不是支持跨 Session 撤销的完整文件日志。
- 每次操作重新解析目标 `current/`。目标回退期间拒绝访问；正在访问时回退请求报忙，
  客户端重试即可。读操作允许目标有活跃进程，但不保证多文件或并发写入时的一致快照。
- 写入要求目标没有活跃执行或已登记后台任务，并持有其 mutation lease 与存储锁。
  临时文件完成写入后原子替换目标，防止工具写入与回退竞争。未登记的 legacy 执行
  不在该并发保证内；共享 workspace 应使用受管理的执行路径。
- A 写入 B 后，B 回退会撤销这次文件修改；A 回退不会撤销对 B 的写入。A 已经复制
  到自己 workspace 的文件由 A 的 checkpoint 管理。关闭 share 后回退不会重新开启它。

## 管理 RPC

可信控制方可通过现有 `/v1/rpc` 调用 `share.enable/disable/status/list/read/write`。
参数 `agent_id`、`session_id` 指定来源 Session；文件请求另需 `target_agent_id`、
`target_session_id`、`path`。`enable` 接受 `scope` 和 `permission`；`write` 接受
`data_base64`；`read` 返回 `data_base64` 与字节数。Agent 使用专属 socket 时没有来源
ID 参数，来源完全由系统确定。
