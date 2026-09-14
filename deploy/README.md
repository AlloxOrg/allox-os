# Deployment boundary

Allox OS is an Agent runtime; Kata is its current concrete runtime
implementation. This directory describes the host-side deployment boundary:
the current Kata backend launches the Guest Kernel and Rootfs produced by this
repository. OpenSandbox, execd and AIO are not Allox OS dependencies and are
not part of the target deployment.

The host launcher must provide an explicit Kata configuration, VM resources,
network policy and the Allox OS Guest Kernel/Rootfs artifacts. Inside the
runtime, `alloxd` uses a Btrfs-backed data disk at `/var/lib/allox/workspaces`;
do not place that store on a VirtioFS/9p host share because Session rollback
needs Btrfs subvolume operations inside the guest.

The Allox Guest init sequence, rather than a host sandbox API, owns the runtime
prerequisites. It must run `allox-guest-bootstrap` before the workspace daemon so
cgroup v2 is writable in the trusted layer and the Allox cgroup subtree exists.
When the independently installed process-tree plugin is enabled, its bootstrap
also mounts tracefs; its kernel fragment lives at
`plugins/process-tree/kernel/allox-process-tracking.config`.
