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
│   ├── memory/                         # optional private logical snapshots
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

`alloxd` creates the level-1 workspace and its `shared/` area, then creates a
level-2 workspace inside that Agent Workspace for each Session. The Session
namespace binds `current/` as its working-directory and `HOME` scope. A private
temporary directory below `current/` is bound as `/tmp`; sibling Session
Workspaces are not mounted in that namespace.

## Checkpoint

Creating a checkpoint makes a read-only COW snapshot of the selected Session
Workspace's `current/` subvolume and atomically persists its DAG entry. Metadata
includes parent/child links, message, pin status, timestamps, and lifecycle
information.

Checkpoint IDs support exact and unique-prefix lookup. Ancestor rollback uses
the current DAG head.

On Linux x86-64 and arm64, the Btrfs backend uses directory-descriptor ioctls
for snapshot creation and subvolume deletion instead of starting a
`btrfs` subprocess for each operation. Other architectures retain the CLI
path; embedding callers can also select it with
`BtrfsBackend(native_ioctls=False)`. A failed mutating ioctl is not retried
through the CLI. The workspace transaction, index, fsync and cleanup ordering
are unchanged.

## Rollback

Session Workspace rollback is a durable transaction:

1. acquire the Session mutation lock;
2. fence the Session execution registry;
3. terminate the Session cgroup, including all descendant processes;
4. create a writable snapshot from the selected checkpoint;
5. move the previous `current/` aside and install the restored subvolume;
6. reconcile the checkpoint index and commit the transaction;
7. release the previous subvolume.

Startup recovery completes or aborts an interrupted swap before the daemon
serves workspace operations.

## Process and socket semantics

- Every Session owns a cgroup and PID/mount/user/network namespace. Children
  inherit the Session cgroup, which provides stable Agent/Session attribution.
- The trusted `alloxd` is the only component allowed to create, move or destroy
  those cgroups and namespaces.
- A private Session temporary directory is mounted as `/tmp`. No Session uses
  the Allox OS global `/tmp` for agent-controlled runtime state.
- Rollback first terminates the Session cgroup, so a discarded branch cannot
  retain a live listener, socket or child process after its workspace state is
  restored.

## Turn lifecycle

When enabled for an Agent or Session, the lifecycle adapter creates a Session
baseline at runtime-session start and a checkpoint after each completed Agent
turn. Each checkpoint records the turn number, result, timestamp and external
runtime identifiers.

## Session logical memory

Run the workspace daemon with `--session-memory` and an authenticated
management endpoint (for example, set `ALLOX_WORKSPACE_TOKEN`). Only a trusted
Agent adapter may use this endpoint; never pass its token into a Session
sandbox. The feature is off by default.

Memory v1 captures Session-private messages, summary, task state, and tool
results as JSON (up to 512 KiB per snapshot). It does not capture the Agent's
heap, provider-hosted conversation state, shared memory, or external effects.
The adapter must build each model request from the active snapshot rather than
reuse a cached conversation or a provider thread that still contains discarded
messages. Keep system instructions, permissions and credentials out of memory.

`SessionMemory.for_turn()` reads the current state under an execution lease.
Hold it across model input construction and invocation; release it before
launching a separately leased command or writing the resulting state. Pass the
read revision to `SessionMemory.checkpoint()` to publish updated memory and the
filesystem checkpoint together. A turn adapter can instead pass
`memory_update=(state, revision)` to `TurnCheckpointLifecycle.on_session_start`
and `on_turn_end` (with automatic checkpoints enabled). Initialize a memory
baseline before the first turn if it must be rewindable. Regular checkpoints
also bind the active memory version if one exists.

- **Joint rollback**: `session.rollback` restores both workspace and bound
  memory once a Session uses managed memory. An older unbound checkpoint needs
  an explicit files-only request.
- **Files only**: pass `restore_memory: false` to `session.rollback`.
- **Memory only**: call `memory.restore` with an exact checkpoint ID and the
  current `expected_revision`. It does not terminate or rewind OS processes.

The index publishes the file head and memory head together after the workspace
swap. If recovery is interrupted, execution and memory access remain fenced
until the rollback journal is resolved. A new revision rejects pre-rollback
writes even if the restored content hash matches an older version. Discarded
checkpoint history and private memory blobs are retained; audit events record
memory identifiers, not the snapshots themselves. Blob garbage collection is
not part of v1. Memory-enabled indices use schema 2 and cannot be opened with
pre-memory builds.

## API examples

```bash
# Level 1: Agent Workspace
alloxd agent create agent-a

# Level 2: Session Workspace below agent-a
alloxd session create agent-a session-1
alloxd checkpoint create agent-a session-1 --name clean
alloxd session exec agent-a session-1 -- sh -c 'echo v2 > state.txt'
alloxd checkpoint rollback agent-a session-1 clean
```

The daemon binds to an in-guest control socket by default. Any host-facing
control channel is an Allox OS interface (for example vsock) and must be
authenticated; OpenSandbox is not part of this channel.
