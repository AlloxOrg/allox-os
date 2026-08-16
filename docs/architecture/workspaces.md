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
