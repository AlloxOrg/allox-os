# Allox OS architecture

## Architecture sentence

Allox OS is the user/trust-domain Agent runtime and isolation boundary. Kata is
its current concrete runtime implementation. This repository produces the guest
kernel, root filesystem and trusted services; another VM backend may replace
Kata without changing Allox OS's Agent/Session semantics. Every Agent owns a
first-level workspace and every Session owns a second-level workspace below its
Agent.

## Hierarchy

```text
Host
└── Allox OS (Kata runtime)
    ├── Allox guest kernel + root filesystem
    ├── alloxd / init
    ├── cgroup, namespace and audit services
    └── Btrfs workspace store
            └── Agent Workspace (level 1)
                ├── shared/
                └── Session Workspace (level 2)
                    ├── current/
                    └── checkpoints/
```

## Runtime boundaries

### Host and runtime backend

The host launches and manages Allox OS through a runtime backend. Kata is the
current implementation; it is not an in-guest Allox OS component or a fixed
part of the Allox OS abstraction. OpenSandbox and Allox CLI are not part of
this boundary.

### Allox OS

Allox OS owns the guest kernel, VM processes, guest `/tmp`, devices,
network and root filesystem. It forms the strong isolation boundary for one
user or trust domain.

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
| Guest kernel, memory, devices and root filesystem | Allox OS | runtime-backend lifecycle or VM snapshot restore |
| Agent shared files and Session collection | Agent Workspace (level 1) | Agent lifecycle |
| Session files, `$HOME`, `$TMPDIR` | Session Workspace `current/` | Session checkpoint and rollback |
| Registered Session background execution | Session runtime registry | Session runtime reset |
| Checkpoint DAG and audit log | workspace daemon metadata | daemon transaction lifecycle |

## Restore operations

### Session Workspace rollback

`allox workspace rollback` replaces one Session Workspace's `current/`
subvolume from a selected checkpoint. Other Session Workspaces and Allox OS
continue with their current state.

### Allox OS restore

The host-side runtime backend restores the complete execution environment,
including VM-scoped kernel and userspace state covered by the selected VM
snapshot mechanism.

## Security assumptions

- The current Kata implementation launches the Allox OS guest kernel and root
  filesystem produced by this repository.
- The Btrfs data disk is available inside the guest.
- The workspace daemon owns its token, metadata directory, and Btrfs
  administration interface.
- Host volumes use explicit, documented mount boundaries.
- Agent commands use the Session-provided `$HOME` and `$TMPDIR`.

## Target component ownership

| Component | Runtime responsibility |
|---|---|
| Guest kernel | cgroup/namespace, observability and security mechanisms |
| Rootfs and init | boot, service supervision and privileged in-guest setup |
| `alloxd` | Agent/Session lifecycle, process identity and workspace operations |
| Workspace service | Btrfs checkpoint/rollback and audit metadata |
| Host runtime launcher | Allox OS lifecycle only |

The guest services never depend on OpenSandbox or Allox CLI. The current Python
prototype is transitional and must be replaced or refactored to these component
boundaries.
