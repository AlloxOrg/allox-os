# Allox 2.0 architecture

## Design sentence

Allox 2.0 uses one Kata VM as the user/trust-domain isolation boundary and
manages Agent and Session state inside that VM through rollbackable workspaces.

## Components

```text
Host control plane
├── allox CLI
└── OpenSandbox Server
    └── Kata runtime
        └── Allox VM
            ├── execd / AIO runtime
            ├── allox-workspace-daemon
            └── Btrfs workspace data disk
```

### Host control plane

The host side creates, discovers, renews, pauses, resumes, and destroys the
outer VM. It does not implement Agent/Session rollback.

### Kata VM

The VM owns a guest kernel and forms the strong boundary from the host and
other Allox VMs. VM processes, the guest `/tmp`, devices, and network state are
VM-scoped.

### Workspace daemon

The daemon is a trusted in-VM service. Agent processes do not receive Btrfs
administration rights. The daemon validates identifiers, serializes mutation,
holds checkpoint metadata outside rollback scope, and controls Session
execution leases.

### Agent and Session

An Agent is a namespace. A Session is the unit of writable state and rollback.
Different Sessions may share the same Kata VM but never the same `current`
subvolume.

## State ownership

| State | Owner | Workspace rollback |
|---|---|---|
| Guest kernel, memory, devices | Kata VM | unchanged |
| VM root filesystem and `/tmp` | Kata VM | unchanged |
| Agent identity and Session list | workspace daemon | unchanged except explicit management |
| Session `current/` files | Session | restored |
| Session `.allox-tmp/` ordinary files | Session | restored |
| Registered Session background execution | Session runtime registry | terminated before rollback |
| Unregistered VM process | Kata VM | not guaranteed to change |
| Checkpoint DAG and audit log | daemon metadata | preserved |

## Two different restore operations

### Session rollback

`allox workspace rollback` swaps one Session's Btrfs `current` subvolume. It is
fast and leaves the VM and other Sessions running.

### VM replacement or snapshot restore

OpenSandbox/Kata lifecycle operations replace or restore the whole execution
environment. They are slower and have a different failure domain. Allox 2.0
does not silently translate Session rollback into VM replacement.

## Security assumptions

- OpenSandbox must select the Kata runtime, not plain runc, for the default
  production profile.
- The Btrfs data disk must be available inside the guest. A VirtioFS/9p host
  share is not a substitute for a Btrfs subvolume store.
- Agent commands cannot access the workspace daemon's token, metadata
  directory, or Btrfs administration interface.
- Host volumes are explicit exceptions to the VM snapshot/rollback boundary
  and must be narrow and documented.
- Agent code should use the Session-provided `$HOME` and `$TMPDIR`; absolute
  guest paths are VM-scoped.

## Source layout mapping

| Source directory | Runtime responsibility |
|---|---|
| `allox.cli` | operator/client-side command surface |
| `allox.vm` | outer OpenSandbox/Kata lifecycle |
| `allox.workspace` | in-VM Agent/Session storage and rollback |
| `allox.runtime` | in-VM AIO/MCP service access |
| `allox.integrations` | Agent-framework lifecycle adapters |

The direction of dependency should remain `cli -> vm/workspace/runtime` and
`integrations -> workspace`. The workspace core must not import CLI commands or
OpenSandbox VM lifecycle code.
