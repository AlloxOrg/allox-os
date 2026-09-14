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

The directories share a repository during development, but produce separate wheels. They can
be split into separate repositories without changing the Core plugin ABI.
