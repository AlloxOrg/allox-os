# Two-level workspace model

## Workspace hierarchy

Allox OS organizes workspace state in two levels:

1. **Agent Workspace (level 1)** — identified by `agent_id`; owns Agent-shared
   files and the collection of Session Workspaces.
2. **Session Workspace (level 2)** — identified by `agent_id + session_id`;
   owns writable execution state, checkpoints, and rollback history for one
   Session.

```text
/var/lib/allox/workspaces/
├── .allox/
│   ├── events/                         # append-only audit
│   ├── indexes/                        # checkpoint DAG
│   ├── locks/                          # mutation serialization
│   └── transactions/                   # crash-recovery records
└── agents/
    └── <agent_id>/
        └── workspace/                  # level 1: Agent Workspace
            ├── shared/                 # Agent-shared files
            └── sessions/
                └── <session_id>/       # level 2: Session Workspace
                    ├── current/         # writable Btrfs subvolume
                    │   └── .allox-tmp/
                    └── checkpoints/
                        └── <checkpoint_id>/  # read-only snapshot
```

`agent-create` creates the level-1 workspace and its `shared/` area.
`session-create` creates a level-2 workspace inside that Agent Workspace.
Session commands use `current/` as `cwd`, `HOME`, and `TMPDIR` scope.
They receive `ALLOX_AGENT_WORKSPACE` as the level-1 workspace path and
`ALLOX_AGENT_SHARED` as its `shared/` area. Bubblewrap mode mounts that shared
area and the selected Session Workspace while keeping sibling Session
Workspaces outside the mount namespace.

## Checkpoint

Creating a checkpoint makes a read-only COW snapshot of the selected Session
Workspace's `current/` subvolume and atomically persists its DAG entry. Metadata
includes parent/child links, message, pin status, timestamps, and lifecycle
information.

Checkpoint IDs support exact and unique-prefix lookup. Ancestor rollback uses
the current DAG head.

## Rollback

Session Workspace rollback is a durable transaction:

1. acquire the Session mutation lock;
2. fence the Session execution registry;
3. interrupt registered Session background executions;
4. create a writable snapshot from the selected checkpoint;
5. move the previous `current/` aside and install the restored subvolume;
6. reconcile the checkpoint index and commit the transaction;
7. release the previous subvolume.

Startup recovery completes or aborts an interrupted swap before the daemon
serves workspace operations.

## Process and socket semantics

- `managed` mode runs commands in the persistent Kata VM with the Session
  Workspace as `cwd`, `HOME`, and `TMPDIR`.
- Registered background executions belong to the Session runtime registry and
  are stopped before rollback.
- Absolute `/tmp` belongs to the Kata VM. `$TMPDIR` points to the Session-owned
  `.allox-tmp/` directory.
- Restored socket and FIFO entries below `.allox-tmp/` are scrubbed during
  rollback.
- `ephemeral` mode adds a Bubblewrap PID/mount namespace and private `/tmp` for
  each command.

## Turn lifecycle

With `workspace.auto_checkpoint_turns = true`, the lifecycle adapter creates a
Session baseline at runtime-session start and a checkpoint after each completed
Agent turn. Each checkpoint records the turn number, result, timestamp, and
external runtime identifiers.

## API examples

```bash
# Level 1: Agent Workspace
allox workspace agent-create agent-a

# Level 2: Session Workspace below agent-a
allox workspace session-create agent-a session-1
allox workspace checkpoint agent-a session-1 --name clean
allox workspace run agent-a session-1 -- sh -c 'echo v2 > state.txt'
allox workspace rollback agent-a session-1 clean
```

The daemon binds to loopback by default. An OpenSandbox endpoint can expose it
to the owning control plane with bearer-token authentication.
