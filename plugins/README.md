# Optional Allox OS plugins

Each child directory is an independently buildable Python distribution. Installing the
core `allox-os` wheel does not install or contain these implementations. Installation and
activation are separate: entry points are discovered only when the corresponding daemon
option is enabled.

| Distribution | Entry point | Activation |
|---|---|---|
| `allox-process-tree` | `allox.process_trackers:ebpf` | `--process-tracking ebpf` |
| `allox-session-share` | `allox.workspace_features:session-share` | `--share-tools` |
| `allox-session-network` | `allox.workspace_features:session-network` | `--session-network isolated/proxy` |
| `allox-system-effects` | `allox.workspace_features:system-effects` | `--system-effects-observer PATH` plus pinned BPF objects |

The directories share a repository during development, but produce separate wheels. They can
be split into separate repositories without changing the Core plugin ABI.

`allox-system-effects` defaults to compatible protocol-v2 A-to-B observation. Its wheel also
installs `allox-system-effects-recover`; configuring that pinned helper together with
`deployment-observe-recovery-verify` enables opt-in protocol-v3 A/B/rollback/C requests. The
current deployment adapter is `DIRECT` only—Docker and Kata are not implied by installing the
plugin. See [the system-effects guide](system-effects/README.md) and its
[Chinese version](system-effects/README.zh-CN.md).
