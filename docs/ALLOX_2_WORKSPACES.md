# Allox 2.0 Agent/Session workspaces

Allox 2.0 separates the lifetime of one user-scoped Allox VM from the
lifetime and rollback history of the Agents and Sessions running inside it.

```text
OpenSandbox + Kata
└── Allox VM (one user trust domain)
    └── mounted workspace store
        └── agents/<agent>/workspace/sessions/<session>/current
```

The OpenSandbox `sandbox` and legacy top-level `checkpoint` commands continue
to manage the whole VM. The new `workspace` commands are scoped by
`agent_id + session_id`; they never restore or replace the Allox VM.

## Execution model

The default `workspace.execution_mode = "anolisa"` follows ANOLISA's `ws-ckpt`
split: the workspace daemon owns Btrfs directory state while the outer Allox
1.0 Sandbox owns process lifetime. `workspace run` enters the selected Session
directory as its working directory and sets `HOME` and `TMPDIR` to that
workspace. Background processes may survive across Agent turns.

Use `allox workspace run --background ...` for an intentional long-running
Session job. It maps to Allox 1.0 / OpenSandbox background execution and
returns an execution id; it must not be combined with automatic checkpointing.
The execution ID is registered outside the workspace. On `workspace rollback`,
Allox interrupts all registered Session executions before swapping the Btrfs
subvolume.

This is **logical workspace isolation inside one user trust domain**, not a
per-command process sandbox. Rollback resets the Session runtime (terminates
registered background jobs) and restores files; it does not restore process
memory or revive a live socket. Agent code must not be given a path to another
user's Allox VM.

Set `workspace.execution_mode = "ephemeral"` only for the stricter legacy
Bubblewrap launcher. It creates a fresh PID and mount namespace for each tool
call, so background tasks do not survive a turn. In either mode, use `TMPDIR`
for rollbackable per-Session temporary files; an explicit absolute `/tmp` is
outer-Sandbox runtime state and is not workspace-managed in ANOLISA mode.

## Trust boundary

`allox-workspace-daemon` runs on the trusted host and is the only component
allowed to create/delete Btrfs subvolumes. It validates every identifier,
serializes mutations, records audit events outside rollback scope, and rejects
checkpoint/rollback while an execution lease is active.

Execution leases are fail-safe and do not expire automatically: a crashed
launcher leaves the Session blocked from mutation until the lease is released
or the daemon is deliberately restarted. This prevents a long command from
silently outliving a TTL and racing with rollback.

The daemon listens on loopback by default. A bearer token is mandatory when it
is configured to listen beyond loopback.

## Storage layout

```text
<store>/
├── .allox/
│   ├── events/                 # never rolled back
│   └── locks/
└── agents/
    └── <agent_id>/
        └── workspace/sessions/
            └── <session_id>/
                ├── current/    # writable Btrfs subvolume
                │   └── .allox-tmp/
                └── checkpoints/
                    └── <checkpoint_id>/  # read-only snapshot
```

Rollback creates a writable snapshot from the selected checkpoint and swaps
only that Session's stable `current` path. Restored socket/FIFO nodes below
`.allox-tmp` are removed by default because a filesystem snapshot cannot
restore their live kernel state.

## Distilled ANOLISA checkpoint core

Allox does not embed ANOLISA's `ws-ckpt` daemon or CLI. It distills the parts
that match the Agent/Session storage model:

- a storage-backend boundary, currently implemented by native Btrfs;
- read-only COW checkpoints and writable-snapshot rollback;
- a per-Session checkpoint DAG with `head`, parent/child links, messages, pins,
  unique-prefix lookup, and ancestor-based rollback;
- an atomically persisted index outside the rollback scope, reconciled against
  snapshot subvolumes after interrupted metadata writes;
- a durable rollback transaction with `preparing`, `prepared`, `old_moved`, and
  `committed` phases. Daemon startup either finishes or safely aborts an
  interrupted workspace swap before serving requests.

Allox keeps a lightweight execution lease for foreground commands instead of
ANOLISA's inotify quiescence heuristic: checkpoint and rollback can be
rejected while that exact command is active. An explicit ANOLISA-mode
background execution is registered as Session runtime state. Raw
`session.rollback` is rejected until the trusted rollback path interrupts that
runtime. ANOLISA's
directory-to-symlink migration,
loop-backed Btrfs image, systemd integration, Agent plugins, diff/preview, and
retention scheduler are not part of this minimal core.

## Session execution

By default, `allox workspace run` runs in the outer Allox 1.0 Sandbox with the
selected Session as its working directory. This is ANOLISA-compatible managed
workspace behavior: the process may survive across turns when started with
`--background`. Workspace rollback terminates registered background executions
before restoring files; it does not restore their OS state.

`workspace.execution_mode = "ephemeral"` instead enters the selected Session
through Bubblewrap. The new mount namespace starts with an empty root, exposes
system files read-only, and binds only the selected Session as `/workspace`.

Kata's container and host-shared filesystems do not reliably support Unix
socket nodes. In ephemeral mode, Bubblewrap mounts a private tmpfs as `/tmp`.
A trusted wrapper copies ordinary temporary files from `/workspace/.allox-tmp`
before the command and synchronizes them back before the namespace exits.
Socket, FIFO, and device nodes work while the command is active but are
deliberately excluded from synchronization. In the default ANOLISA mode,
`TMPDIR` points to `.allox-tmp`, while absolute `/tmp` and other outer runtime
paths are not checkpoint-managed.

## Example

```bash
allox-workspace-daemon --root /data/allox/user-1

allox workspace agent-create agent-a
allox workspace session-create agent-a session-1
allox workspace checkpoint agent-a session-1 --name clean --message "before retry" --pin
allox workspace run agent-a session-1 -- sh -c 'printf changed > state.txt'
allox workspace rollback agent-a session-1 clean
# ANOLISA-compatible lineage semantics: 1=head, 2=head's parent, ...
allox workspace rollback agent-a session-1 --num-ancestors 2
```

## Optional per-turn checkpoints

Allox includes a framework-neutral Agent lifecycle adapter distilled from
ANOLISA's plugin policy. It is disabled by default:

```toml
[workspace]
auto_checkpoint_turns = true
```

The environment override is
`ALLOX_WORKSPACE_AUTO_CHECKPOINT_TURNS=true|false`. The core can be manually
wired by any runtime, but Allox also ships a pluggable LangChain adapter. The
adapter is a thin lifecycle policy layer: checkpoints, rollback, lease checks,
and isolation remain daemon operations.

```python
from allox.plugins import builtin_registry

plugin = builtin_registry().create(
    "langchain-turn-checkpoint",
    workspace_client,
    agent_id,
    session_id,
    resolved_config=resolved_config,
)
agent = plugin.wrap(agent, runtime_session_id=thread_id)

# This automatically emits session_start, message_received and turn_end.
result = agent.invoke(
    {"messages": [{"role": "user", "content": user_message}]},
    config={"metadata": {"allox_turn_id": turn_id}},
)
```

External runtimes can publish an adapter through the Python entry-point group
`allox.plugins`; the entry-point name is its plugin name. A direct manual
integration remains available:

```python
from allox.turn_lifecycle import TurnCheckpointLifecycle

lifecycle = TurnCheckpointLifecycle.from_resolved_config(
    workspace_client,
    agent_id,
    session_id,
    resolved_config,
)
lifecycle.on_session_start(runtime_session_id=thread_id)

with lifecycle.turn(user_message, turn_id=turn_id):
    result = agent.invoke(user_message)
```

When enabled, session start creates a turn-0 baseline and each completed Agent
turn creates one checkpoint after tool execution has stopped. Checkpoint
metadata records the lifecycle event, turn number, timestamp, success state,
and optional runtime turn/session IDs. Lifecycle checkpoint failures are
non-blocking. A rollback performed through `lifecycle.rollback(...)` suppresses
the immediately following automatic checkpoint so the restored state is not
saved again as a new turn.

This is a runtime hook, not a per-command policy. The existing
`workspace run --checkpoint-on-success` option remains independent and should
normally stay off when per-turn checkpoints are enabled.

The Allox VM must mount the same store at the configured `workspace.vm_root`:

```bash
allox sandbox create \
  --host-volume /data/allox/user-1 /var/lib/allox-store \
  --image allox-vm:2.0
```
