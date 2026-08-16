# Deployment boundary

Allox OS is the Guest OS of a Kata VM. This directory describes the host-side
deployment boundary: the Kata runtime launches the Guest Kernel and Rootfs
produced by this repository. OpenSandbox, execd and AIO are not Allox OS
dependencies and are not part of the target deployment.

The host launcher must provide an explicit Kata configuration, VM resources,
network policy and the Allox OS Guest Kernel/Rootfs artifacts. Inside the VM,
`alloxd` uses a Btrfs-backed data disk at `/var/lib/allox/workspaces`; do not
place that store on a VirtioFS/9p host share because Session rollback needs
Btrfs subvolume operations inside the guest.

`opensandbox-kata.toml.example` is a migration-era artifact. It does not
describe the Allox OS target architecture and must not be used as the basis for
a new deployment.
