# Allox OS architecture

## Architecture sentence

Allox OS uses one Kata VM as the user/trust-domain isolation boundary. Inside
the VM, every Agent owns a first-level workspace and every Session owns a
second-level workspace below its Agent.

## Hierarchy

```text
Host control plane
├── allox CLI
└── OpenSandbox Server
    └── Kata runtime
        └── Allox VM
            ├── execd / AIO runtime
            ├── allox-workspace-daemon
            └── Btrfs workspace store
                └── Agent Workspace (level 1)
                    ├── shared/
                    └── Session Workspace (level 2)
                        ├── current/
                        └── checkpoints/
```

## Runtime boundaries

### Host control plane

The host creates, discovers, renews, pauses, resumes, and destroys the Kata VM.

### Kata VM

The Kata VM owns the guest kernel, VM processes, guest `/tmp`, devices, network,
and root filesystem. It forms the strong isolation boundary for one user or
trust domain.

### Workspace daemon

The trusted in-VM daemon creates both workspace levels, validates identifiers,
serializes mutations, controls execution leases, and maintains checkpoint
metadata and audit events.

### Agent Workspace (level 1)

An Agent Workspace is the long-lived ownership and isolation boundary for one
Agent. It owns the Agent's shared files and its collection of child Session
Workspaces.

### Session Workspace (level 2)

A Session Workspace is the execution and filesystem rollback boundary below an
Agent Workspace. Its `current/` path is a writable Btrfs subvolume;
`checkpoints/` stores immutable snapshots of that subvolume.

## State ownership

| State | Owner | Lifecycle operation |
|---|---|---|
| Guest kernel, memory, devices and root filesystem | Kata VM | VM lifecycle or snapshot restore |
| Agent shared files and Session collection | Agent Workspace (level 1) | Agent lifecycle |
| Session files, `$HOME`, `$TMPDIR` | Session Workspace `current/` | Session checkpoint and rollback |
| Registered Session background execution | Session runtime registry | Session runtime reset |
| Checkpoint DAG and audit log | workspace daemon metadata | daemon transaction lifecycle |

## Restore operations

### Session Workspace rollback

`allox workspace rollback` replaces one Session Workspace's `current/`
subvolume from a selected checkpoint. Other Session Workspaces and the Kata VM
continue with their current state.

### Kata VM restore

OpenSandbox/Kata lifecycle operations restore the complete execution
environment, including VM-scoped kernel and userspace state covered by the
selected backend.

## Security assumptions

- OpenSandbox selects the Kata runtime for the production profile.
- The Btrfs data disk is available inside the guest.
- The workspace daemon owns its token, metadata directory, and Btrfs
  administration interface.
- Host volumes use explicit, documented mount boundaries.
- Agent commands use the Session-provided `$HOME` and `$TMPDIR`.

## Source layout mapping

| Source directory | Runtime responsibility |
|---|---|
| `allox.cli` | operator/client-side command surface |
| `allox.vm` | outer OpenSandbox/Kata lifecycle |
| `allox.workspace` | first-level Agent and second-level Session workspace management |
| `allox.runtime` | in-VM AIO/MCP service access |
| `allox.integrations` | Agent-framework lifecycle adapters |

The dependency direction is `cli -> vm/workspace/runtime` and
`integrations -> workspace`.
