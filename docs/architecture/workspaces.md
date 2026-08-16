# Agent/Session workspace model

## Scope

A workspace is the rollback unit inside one Allox Kata VM. Its identity is the
pair `agent_id + session_id`; a VM sandbox ID is not a workspace identity.

## Storage layout

```text
/var/lib/allox/workspaces/
├── .allox/
│   ├── events/                 # append-only audit, outside rollback
│   ├── indexes/                # checkpoint DAG, outside rollback
│   ├── locks/                  # mutation serialization
│   └── transactions/           # crash-recovery records
└── agents/
    └── <agent_id>/
        └── workspace/sessions/
            └── <session_id>/
                ├── current/    # writable Btrfs subvolume
                │   └── .allox-tmp/
                └── checkpoints/
                    └── <checkpoint_id>/  # read-only snapshot
```

All identifiers are validated before being converted to paths. The trusted
daemon is the only component allowed to create, swap, or delete subvolumes.

## Checkpoint

Creating a checkpoint makes a read-only COW snapshot of `current/` and then
atomically persists its DAG entry. Metadata includes parent/child links,
message, pin status, timestamps, and optional lifecycle information.

Checkpoint IDs support exact and unique-prefix lookup. Ancestor rollback uses
the current DAG head; it does not infer history from directory timestamps.

## Rollback

Rollback is a durable transaction:

1. acquire the Session mutation lock;
2. reject while a foreground execution lease is active;
3. interrupt registered Session background executions;
4. create a writable snapshot from the selected checkpoint;
5. move the old `current` aside and install the restored subvolume;
6. reconcile the checkpoint index and commit the transaction;
7. remove the old subvolume after the stable path is valid.

Startup recovers incomplete swaps before serving requests. Transaction state
is outside the Session subvolume, so rollback cannot erase its own recovery
record.

## Process and socket semantics

Workspace snapshots are filesystem snapshots, not kernel snapshots.

- `managed` mode runs commands in the persistent Kata VM with the Session as
  `cwd`, `HOME`, and `TMPDIR`.
- Registered background executions are Session runtime state and are stopped
  before rollback.
- A process started outside the managed execution path is VM state and is not
  guaranteed to stop.
- Unix socket/FIFO/device nodes cannot restore their live kernel endpoints from
  a filesystem snapshot. Restored special nodes below `.allox-tmp` are removed.
- Absolute `/tmp` is VM-level state. `$TMPDIR` points to the Session-owned
  `.allox-tmp` directory.

`ephemeral` mode optionally adds Bubblewrap inside the Kata VM. It creates a
fresh PID/mount namespace and private `/tmp` for each command. Ordinary temp
files are synchronized through `.allox-tmp`; socket, FIFO, and device nodes are
excluded.

## Turn lifecycle

When `workspace.auto_checkpoint_turns = true`, the lifecycle adapter creates:

- a Session baseline at runtime-session start;
- one checkpoint after each completed Agent turn;
- metadata containing turn number, success, timestamp, and optional external
  runtime IDs.

The adapter is policy only. It does not hold Btrfs privileges and it does not
change rollback scope. A rollback suppresses the immediately following
automatic checkpoint so the restored state is not immediately re-saved.

## API examples

```bash
allox workspace agent-create agent-a
allox workspace session-create agent-a session-1
allox workspace checkpoint agent-a session-1 --name clean
allox workspace run agent-a session-1 -- sh -c 'echo v2 > state.txt'
allox workspace rollback agent-a session-1 clean
allox workspace rollback agent-a session-1 --num-ancestors 2
```

The daemon defaults to loopback. If exposed through an OpenSandbox endpoint, a
bearer token is mandatory and the endpoint must remain scoped to the owning
VM/control plane.
