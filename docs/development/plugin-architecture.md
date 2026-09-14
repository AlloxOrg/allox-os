# Optional plugin architecture

Allox OS separates installation from activation. The Core wheel contains the workspace store,
checkpoint/rollback, Session execution/cgroup boundary, Bubblewrap plan builder and plugin ABI.
Feature implementations are separate Python distributions discovered with
`importlib.metadata.entry_points` only when their daemon option is enabled.

This follows the useful boundary in DeepSeek Harness profiles and bundles: an installed package
is not automatically part of the active composition, and activated components own reversible
effects that are closed in reverse order. Allox uses a small Python ABI instead of embedding the
Cordis runtime because these plugins modify trusted Guest OS execution plans.

## Distributions

| Distribution | Discovery group | Provider | Activation |
|---|---|---|---|
| Core `allox-os` | — | workspace, execution, cgroup, plugin ABI | always |
| `allox-process-tree` | `allox.process_trackers` | `ebpf` | `--process-tracking ebpf` |
| `allox-session-share` | `allox.workspace_features` | `session-share` | `--share-tools` |
| `allox-session-network` | `allox.workspace_features` | `session-network` | `--session-network isolated/proxy` |

The repository is a development monorepo, but every directory under `plugins/` has its own
`pyproject.toml` and produces its own wheel. Building/installing the Core wheel neither embeds nor
downloads plugin implementations. The packages can move to independent repositories without
changing their entry points or Core ABI.

## Core ABI

`FeatureManager` validates ABI 1 and composes each enabled feature's `ExecutionContribution`:

- an outer command prefix such as `nsenter`;
- trusted environment variables;
- declarative Bubblewrap mounts;
- non-secret run metadata;
- namespaced RPC routes and a reverse-order `close()` lifecycle.

Duplicate RPC prefixes, mount targets, environment values or metadata fail closed. If a configured
provider is not installed, daemon construction fails explicitly instead of silently weakening the
requested boundary. Plugins installed but not enabled are not imported.

Session cgroup creation and root-process attachment remain Core behavior. They define process
ownership, endpoint caller identity and whole-tree termination even without eBPF. The process-tree
plugin observes fork/exec/exit and supplies a complete audit trace; consequently a non-eBPF run can
have `lifecycle_complete=true` while `trace_complete=false`.

## Verification

Final validation on server 117 used the last 24 online logical CPUs (`24-47`) and independently
built four wheels. The Core wheel was inspected and contained none of the three plugin module
names. Installing all wheels without enabling features registered their entry points but imported
no plugin modules.

Kata 6.1.62 + Bubblewrap tests:

- combined process-tree/share/network suite: `53 passed`;
- Core + share wheel only, with network/process-tree packages absent: `SHARE_ONLY_PASS`;
- Core + network wheel only, with share/process-tree packages absent: `NETWORK_ONLY_PASS`;
- real Qwen-Codex execution: direct egress blocked, HTTP proxy available, generic connector reached
  `ssh.github.com:443`, captured an `SSH-2.0-*` banner, and the eBPF plugin produced a complete trace.

Final remote evidence directories:

- `/data/hy/Allox/plugin-refactor-20260914-v3/allox-plugin-kata-20260914T042404Z`
- `/data/hy/Allox/plugin-refactor-20260914-v3/allox-plugin-share-only-20260914T042615Z`
- `/data/hy/Allox/plugin-refactor-20260914-v3/allox-plugin-network-only-20260914T042648Z`
- `/data/hy/Allox/plugin-refactor-20260914-v3/allox-qwen-network-20260914T042733Z`
