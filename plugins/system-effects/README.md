# allox-system-effects

[English](README.md) | [简体中文](README.zh-CN.md)

Optional Allox workspace feature that binds one managed Agent execution to the
Linux/eBPF `deployment-observe` protocol. The Observer remains a separate
privileged process; it is not imported into the workspace daemon and is never
mounted into the Agent sandbox.

## Current scope: compatible v2 and opt-in v3

Without a `process.start.params.recovery` object, the integration preserves the
protocol-v2 behavior:

```text
A: S0 after trusted sandbox setup, before the Agent command is released
   -> operation, object, and process history while the Agent runs
B: S1 after Core proves the same Session cgroup empty and frozen
```

It reports scoped `A -> B` system effects and does not perform a rollback.
Supplying an explicit recovery object selects protocol v3:

```text
A -> Agent execution -> B -> Allox Session Workspace rollback -> C
```

The Observer then produces and verifies the `A -> B`, `B -> C`, and `A -> C`
deltas. Protocol v3 requires the daemon-owned recovery helper and independent
recovery verifier to be configured; callers cannot choose either executable or
pass arbitrary helper arguments.

The current adapter requires the Observer deployment kind to be `DIRECT`: the
Agent command runs in the final Allox Bubblewrap environment while sharing the
target Linux kernel with the Observer. This is not a Docker adapter and does
not claim visibility into Docker container identity. It is also not a Kata
adapter: a host Observer cannot see syscalls made inside a Kata guest. Those
deployment modes require separate lifecycle and identity adapters.

Attribution is direct-caller attribution. A recorded operation is established
as having been issued by a task under the exact registered cgroup root. If the
Agent asks an out-of-cgroup daemon to perform an operation, this integration
does not infer a causal relationship and must not attribute the daemon's
effect to the Agent.

The trusted Allox wrapper also runs inside the bound cgroup. Its `/tmp`
persistence steps can therefore appear in operation history, and `.allox-tmp`
can appear in a broad workspace filesystem delta. Such records are accurately
attributed to the managed execution domain, but are not proof that Agent code
authored that individual operation. Use narrower filesystem roots when the
experiment intends to exclude Allox runtime state.

## Lifecycle and trust boundary

Core creates a dedicated, leaf-sealed Session cgroup and constructs the final
Bubblewrap command from one daemon-lifetime launcher thread, so
`--die-with-parent` is not bound to a transient RPC handler. After trusted
`/tmp` baseline setup, the inner shell stops itself with `SIGSTOP` before it
executes the Agent command. This barrier is marker-free: it does not create a
token or launch-marker file in the Session workspace. Core requires exactly
one stopped inner task, pins it with a pidfd, freezes the cgroup, and binds the
run to its exact cgroup ID/path, root PID, mount-namespace inode,
network-namespace inode, boot ID, run ID, and epoch.

For v2 the plugin then:

1. writes a new `0600` Observer config outside the evidence directory;
2. starts the configured absolute `deployment-observe --config PATH` argv with
   `shell=False`, closed inherited file descriptors, and a fixed environment;
3. accepts `READY` only when protocol version, run/epoch, root cgroup ID, BPF
   object digests, deployment manifest, boot ID, root PID, and mount/network
   namespace identities all match Core's trusted execution context;
4. continuously drains stderr and reports an unexpected exit through Core's
   fail-closed callback;
5. accepts `FINISH` only with Core's exact empty/frozen cgroup attestation;
6. requires a bound `FINISHED` response and exit status zero, then securely
   reads and hashes the report and artifact manifest without following links;
7. runs the independent `deployment-observe-verify` binary against an external
   expectation and requires `verified=true` before returning a receipt.

Core rechecks the pidfd-pinned task's cgroup and namespace identities after
`READY`, then resumes that exact task through the pidfd and thaws the cgroup.
Agent-supplied argv and environment are not executed before this point.

For v3, Core waits for the complete Bubblewrap/Agent tree to exit, proves that
the same Session cgroup is empty, freezes it, and revalidates its exact identity
before `PRE_RESTORE`. The Observer captures B, runs the pinned
`allox-system-effects-recover` helper, captures C, and completes the v3 artifact
transaction. Core retains the execution lease and defers deletion of the old
workspace subvolume until C and the recovery evidence are committed. The independent
`deployment-observe-recovery-verify` result binds the checkpoint, recovery ID,
three snapshots, three deltas, recovery record, and terminal manifest.
The workload timeout is cancelled when Bubblewrap exits; Observer S1 and
offline verification use the separate finalization timeout and cannot
retroactively relabel an already-exited workload as timed out.

An `INCOMPLETE`, `PARTIAL`, or `UNPROVEN_NO_EVENTS` Observer result can be a
valid, offline-verified result and remains visible as such. Here,
`verified=true` proves the integrity and replay checks for the evidence that was
actually collected; it does not mean that coverage was complete. For v3,
`verified=true` also does **not** mean the rollback was consistent: consumers
must separately inspect `CONSISTENT_WITHIN_SCOPE`,
`INCONSISTENT_WITHIN_SCOPE`, or `INCONCLUSIVE`. The plugin
never upgrades a weaker status to `COMPLETE`, and a negative event result must
not be presented as proof of no system effect when its evidence status or a
domain verdict is incomplete. There is no Agent-facing RPC prefix and no
`process.start` parameter can replace the trusted binary, BPF objects, hashes,
profile, or state scopes.

The plugin serializes observations across daemon instances with an advisory
kernel `flock` on `/run/allox-system-effects.lock`. It acquires the lock before
starting the Observer/S0 transaction and holds it through S1, terminal artifact
validation, the independent verifier, and receipt creation. Global BPF, sysctl,
module, mount, and network snapshots from overlapping runs would otherwise
contaminate each other. Lock acquisition is bounded by `ready_timeout`; an
overlapping run fails closed after that deadline.

The lock path defaults to `/run/allox-system-effects.lock` and can be changed by
the plugin-level `host_lock_path` setting. Its parent must be a canonical,
root/daemon-owned directory that is not group/world writable. An existing lock
must be a private, singly-linked regular file; symlinks and special files are
rejected and runtime opens use `O_NOFOLLOW`. Every daemon observing the same
kernel must use the same underlying lock inode (containers must share that lock
path); using distinct paths intentionally voids cross-daemon serialization.

This `flock` is a cooperative serialization mechanism, not an exclusive-writer
proof for the target Linux. A process that does not acquire the same lock can
still change host-global BPF, sysctl, module, mount, or network state between
S0 and S1. Use an exclusive/quiescent target Linux for experiments that need
clean endpoint attribution, or retain such deltas as potentially contaminated.
Operationally, run one Allox management daemon per workspace root: the host
lock does not coordinate workspace leases, rollback, or ownership between
independent daemons.

## Install

Build the Observer artifacts from `tools/bpf/bpf-state-observer` in the Linux
tree and install them into a root-owned, non-writable directory. Install this
directory as a separate Python distribution; installing `allox-os` alone does
not install the plugin.

The Guest kernel must include the options in
[`kernel/allox-system-effects.config`](kernel/allox-system-effects.config), expose
`/sys/kernel/btf/vmlinux`, mount writable cgroup v2, and have `bpf` in the
effective LSM order (`cat /sys/kernel/security/lsm`). Bubblewrap is required by
the Allox managed-execution path.

```sh
pip install ./plugins/system-effects

sha256sum \
  /usr/libexec/bpf-state-observer/system_effects.bpf.o \
  /usr/libexec/bpf-state-observer/process_lifecycle.bpf.o
```

The distribution registers:

```text
allox.workspace_features:system-effects
allox-system-effects-recover
```

Build and install `deployment-observe-recovery-verify` alongside
`deployment-observe` and `deployment-observe-verify` when protocol v3 will be
used. The console script is the fixed recovery helper. It calls the Allox
WorkspaceStore deferred-cleanup rollback API; do not replace it with a generic
shell runner or a helper whose arguments can be selected by the Agent.

## Activate

All five Observer/object arguments are one trusted daemon configuration and
must be supplied together. The verifier defaults to a sibling named
`deployment-observe-verify`. The standalone system-effects Observer already
records process lifecycle, so the separate Allox process-tree provider must be
disabled.

```sh
ALLOX_WORKSPACE_TOKEN='<management-token>' \
allox-workspace-daemon \
  --root /var/lib/allox/workspaces \
  --process-audit-root /var/log/allox/processes \
  --process-tracking disabled \
  --system-effects-observer /usr/libexec/bpf-state-observer/deployment-observe \
  --system-effects-verifier /usr/libexec/bpf-state-observer/deployment-observe-verify \
  --system-effects-recorder-object /usr/libexec/bpf-state-observer/system_effects.bpf.o \
  --system-effects-recorder-sha256 '<64-lowercase-hex>' \
  --system-effects-lifecycle-object /usr/libexec/bpf-state-observer/process_lifecycle.bpf.o \
  --system-effects-lifecycle-sha256 '<64-lowercase-hex>' \
  --system-effects-host-lock /run/allox-system-effects.lock \
  --system-effects-profile balanced \
  --system-effects-object-resolution KERNEL_OBJECT_EXPANDED_PARTIAL
```

To enable opt-in v3 requests, add the recovery verifier and the exact hash of
the installed helper:

```sh
RECOVER=/opt/allox-venv/bin/allox-system-effects-recover
RECOVER_SHA=$(sha256sum "$RECOVER" | awk '{print $1}')

allox-workspace-daemon \
  ... \
  --system-effects-recovery-verifier /usr/libexec/bpf-state-observer/deployment-observe-recovery-verify \
  --system-effects-recovery-helper "$RECOVER" \
  --system-effects-recovery-helper-sha256 "$RECOVER_SHA" \
  --system-effects-recovery-timeout-ms 120000 \
  --system-effects-recovery-max-output-bytes 65536
```

The helper and both verifiers must be canonical, singly linked, root/daemon
owned regular executables that are not group/world writable. The SHA shown
above is computed for that installation; it is not a portable constant. Omit
all recovery options if only the backward-compatible v2 path is required.

Optional state confirmers consume trusted JSON files in the exact standalone
Observer schema:

```text
--system-effects-bpf-scope /etc/allox/effects/bpf-scope.json
--system-effects-filesystem-scope /etc/allox/effects/filesystem-scope.json
--system-effects-linux-scope /etc/allox/effects/linux-scope.json
```

The files are read once during plugin activation through bounded, stable,
no-follow reads. Unknown plugin configuration keys are rejected. Default
runtime tuning is `poll_interval_ms=20`, `drain_timeout_ms=5000`,
`ready_timeout=30`, and `finalize_timeout=60` from the Core CLI. Polling is the
only steady userspace wakeup; filesystem/BPF/Linux confirmers run at S0/S1
boundaries rather than on each syscall.

When mount or network confirmation is enabled, the plugin replaces the scope
template's namespace PID/inode with the exact frozen Bubblewrap process identity
for each run. The resulting per-run scope is committed into the Observer config;
the returned deployment manifest must report the same namespace inodes.

For v3 filesystem confirmation, `/workspace` and `/agent/shared` roots in the
scope are translated to the host-visible Session `current/` and Agent
`shared/` paths. A/B/C are captured from the Observer mount namespace, so C
resolves the new `current` subvolume after rollback rather than a retained
Bubblewrap mount that still references B. Other filesystem roots are not
rewritten.

## Opt-in A/B/rollback/C request

Recovery is selected per run. Omitting the object keeps protocol v2 and does
not roll back anything:

```json
{
  "action": "process.start",
  "params": {
    "agent_id": "agent-a",
    "session_id": "session-1",
    "argv": ["/bin/sh", "-c", "printf changed > effect.txt"],
    "timeout": 60,
    "recovery": {
      "scrub_runtime": true,
      "require_consistent": true
    }
  }
}
```

`checkpoint_id` and `recovery_id` are optional. When `checkpoint_id` is
omitted, Allox creates and pins `baseline-<run_id>` before releasing the Agent.
An explicit checkpoint must be an existing complete ID; prefix resolution is
not accepted. An omitted recovery ID becomes `recovery-<run_id>`.
`scrub_runtime` defaults to true. `require_consistent` also defaults to true and
fences the Session when verified evidence yields anything other than
`CONSISTENT_WITHIN_SCOPE`; setting it false changes fencing policy, not the
recorded verdict.

Allox rollback restores only that Session Workspace. It does not restore
`/agent/shared`, BPF state, sysctls, mounts, host network configuration, or
kernel modules. A residual in any enabled scope is therefore correctly
reported as `INCONSISTENT_WITHIN_SCOPE`; incomplete capture, changed identity,
or an unproven quiescence condition is `INCONCLUSIVE`.

Two strict current boundaries matter when selecting scopes:

- Protocol v3 does not yet consume the Host-signed `exclusive-vm-v1` contract.
  Therefore equal BPF endpoints cannot authorize `UNCHANGED_WITHIN_SCOPE`;
  enabling the BPF confirmer without a positive BPF delta makes the combined
  recovery verdict `INCONCLUSIVE` by design.
- The generic filesystem confirmer compares raw device/inode/timestamp
  metadata. A Btrfs snapshot swap can restore path contents and metadata while
  still changing the subvolume-backed `st_dev` and root ctime. Those raw
  differences remain visible; the plugin does not silently reinterpret them as
  Btrfs-semantic equality.

## Evidence

For an Allox audit directory `<audit>/<run_id>`, the plugin retains:

```text
<audit>/<run_id>/system-effects/                         closed Observer artifacts
<audit>/<run_id>/system-effects-config.json              exact external config
<audit>/<run_id>/system-effects-verification-expectation.json
<audit>/<run_id>/system-effects-verification.json        offline verifier result
<audit>/<run_id>/system-effects-receipt.json             Allox commit receipt
```

`system-effects/` must not exist before startup and is owned exclusively by
the Observer. The config, expectation, verifier output, and receipt stay
outside that closed artifact set. Any digest mismatch, malformed or unsolicited
protocol message, timeout, symlink/non-regular artifact, verifier failure, or
Observer crash prevents verified finalization and leaves Core responsible for
keeping the Session fenced.

The closed Observer directory includes the protocol-v2 observation contract,
deployment manifest, READY/finalization identity barriers, binary WALs and
canonical JSONL projections for system effects and process lifecycle, the
deployment report, and the digest-committing artifact manifest. Depending on
the configured scopes and object mode, it also includes socket-state evidence
and these endpoint tuples:

```text
bpf-state-s0.json         bpf-state-s1.json         bpf-state-verdict.json
filesystem-state-s0.json  filesystem-state-s1.json  filesystem-state-comparison.json
linux-state-s0.json       linux-state-s1.json       linux-state-comparison.json
```

`system-effects-receipt.json` binds the Allox run/session/cgroup identity,
quiescence attestation, exact report and artifact-manifest digests, Observer
evidence status, and independent verification result. It is an A-to-B evidence
receipt for v2.

For protocol v3 the closed directory additionally contains:

```text
snapshot-a.json           snapshot-b.json           snapshot-c.json
agent-delta.json          recovery-delta.json        residual-delta.json
recovery.json             recovery-verdict.json      recovery-artifact-manifest.json
```

The receipt and verification output bind `checkpoint_id`, `recovery_id`, the
recovery-verifier result, and the terminal manifest. Verification establishes
artifact integrity and replay; the separate recovery verdict establishes
whether A and C matched within the declared scope.

The repository test suite covers the DIRECT adapter, protocol state machine,
deferred-cleanup rollback, artifact checks, and error paths. A real privileged
QEMU run on 2026-09-19 additionally completed automatic baseline creation,
A/B/C capture, successful deferred Btrfs rollback, all 37 committed artifacts,
and independent recovery verification (`VERIFIED`). Its full-domain verdict
was deliberately preserved as `INCONCLUSIVE`, and `require_consistent=true`
fenced the Session, because the BPF negative claim was unauthorised and the raw
Btrfs/mount/process identities were not A/C-equal. This is an end-to-end
validation of the fail-closed integration, not a claim of all-domain recovery
consistency. A second run with `require_consistent=false` kept the same
`INCONCLUSIVE` evidence verdict while completing with `release_allowed=true`
and `session_fenced=false`; that option changes release policy only, exactly
as documented. Docker and Kata still require deployment-specific lifecycle and
identity adapters; DIRECT results must not be generalized to either mode.
