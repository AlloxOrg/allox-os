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
| `allox-system-effects` | `allox.workspace_features` | `system-effects` | `--system-effects-observer PATH` |

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

Plugin API 1 also has opt-in execution-lifecycle ABIs 1 and 2. A lifecycle plugin receives an exact
Core-owned boundary only after the final Bubblewrap-side launch gate is stopped in a leaf-sealed,
frozen Session cgroup. It must return a per-run handle that remains healthy through Core's final
empty/frozen cgroup attestation. ABI 2 extends that same boundary with the recovery transaction;
it does not currently keep a live task in the cgroup after the Agent tree exits.
Core finalizes handles before deleting the cgroup or releasing the execution lease; startup,
liveness, finalization, or durable-commit failures fence the Session.
This hook is used by `allox-system-effects` to run the privileged Observer outside the Agent
cgroup without allowing the Agent to select its binary, BPF objects, state scopes, or evidence
directory.

Lifecycle ABI 1 is the compatible `A -> B` path. Lifecycle ABI 2 adds an `agent_started()` commit
barrier while the root task is still stopped and accepts an optional immutable recovery plan in
the execution context. After the Agent tree exits, recovery runs require that same cgroup to become
empty, freeze and revalidate its exact ID/path, then retain the execution lease through B,
rollback, C, and offline verification. This makes `PRE_RESTORE` a Core-attested terminal boundary
rather than an unbound post-hoc request.

Session cgroup creation and root-process attachment remain Core behavior. They define process
ownership, endpoint caller identity and whole-tree termination even without eBPF. The process-tree
plugin observes fork/exec/exit and supplies a complete audit trace; consequently a non-eBPF run can
have `lifecycle_complete=true` while `trace_complete=false`.

`allox-system-effects` already loads the Observer's process-lifecycle program, so it is mutually
exclusive with `--process-tracking ebpf`. It serializes active observations across daemon processes
with one host-wide `flock`, held from before S0 through offline verification, because enabled
BPF/Linux-state confirmers take global S0/S1 snapshots. Details and the exact trust contract are in
[`plugins/system-effects/README.md`](../../plugins/system-effects/README.md).

The system-effects plugin preserves protocol-v2 `A -> B` when `process.start` has no recovery
object. An explicit recovery plan selects protocol v3: A and B surround the Agent, a fixed
hash-pinned helper performs deferred-cleanup Allox Session Workspace rollback, and C is captured
before the old subvolume is released. The independent recovery verifier binds all
three snapshots, deltas, checkpoint/recovery identity, recovery record, verdict, and manifest.
`verified=true` establishes integrity and replay, not consistency; the recovery verdict remains a
separate `CONSISTENT_WITHIN_SCOPE`, `INCONSISTENT_WITHIN_SCOPE`, or `INCONCLUSIVE` result.

Filesystem `/workspace` and `/agent/shared` roots are translated to host-visible `current/` and
`shared/` paths for v3, so C resolves the new `current` after the Btrfs swap instead of a retained
mount namespace that still points to B. Allox rollback only restores the Session Workspace; global
BPF/sysctl/mount/network/module state and Agent shared data are intentionally not restored. Any
residual in an enabled scope remains visible in the A-to-C verdict.

Both modes currently support `DIRECT` managed execution only. Attribution is limited to direct
callers under the bound cgroup, and the host lock coordinates only cooperating Observer-enabled
daemons. Docker/Kata need separate lifecycle and identity adapters. Use one management daemon per
workspace root and preserve any incomplete or inconclusive evidence status without promotion.
On 2026-09-19 the DIRECT protocol-v3 path completed a real privileged-QEMU run through automatic
baseline creation, A/B/C capture, deferred Btrfs rollback, the 37-artifact closed set, and
independent offline verification. The deliberately strict full-domain request ended
`INCONCLUSIVE` and fenced the Session: verification was `VERIFIED`, but equal BPF endpoints
lacked the separate Host-authenticated exclusive-writer contract, while raw filesystem/mount and
initial-process identities exposed recovery-mechanism churn. This validates the fail-closed
integration, not an all-domain `CONSISTENT_WITHIN_SCOPE` claim. A second run with
`require_consistent=false` retained the same `INCONCLUSIVE` verdict but completed with
`release_allowed=true` and `session_fenced=false`, validating the explicit advisory policy too.

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
