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

## Session execution

`allox workspace run` acquires an execution lease and enters the selected
Session through Bubblewrap. The new mount namespace starts with an empty root,
exposes system files read-only, and binds only the selected Session as
`/workspace`.

Kata's container and host-shared filesystems do not reliably support Unix
socket nodes. Bubblewrap therefore mounts a private tmpfs as `/tmp`. A trusted
wrapper in the same mount namespace copies ordinary temporary files from
`/workspace/.allox-tmp` before the command and synchronizes them back before
the namespace exits. Socket, FIFO, and device nodes work while the command is
active but are deliberately excluded from synchronization. This preserves
rollback for regular `/tmp` files without pretending that live socket state is
restorable.

## Example

```bash
allox-workspace-daemon --root /data/allox/user-1

allox workspace agent-create agent-a
allox workspace session-create agent-a session-1
allox workspace checkpoint agent-a session-1 --name clean
allox workspace run agent-a session-1 -- sh -c 'printf changed > state.txt'
allox workspace rollback agent-a session-1 clean
```

The Allox VM must mount the same store at the configured `workspace.vm_root`:

```bash
allox sandbox create \
  --host-volume /data/allox/user-1 /var/lib/allox-store \
  --image allox-vm:2.0
```
