# Deployment boundary

Allox 2.0 requires OpenSandbox to launch the outer workload with the named
Kata runtime. The OCI image supplies the guest userspace; Kata supplies the
VM, guest kernel, and host isolation boundary.

`opensandbox-kata.toml.example` is a starting point, not a universal production
configuration. In particular, restrict `allowed_host_paths`, pin image tags,
enable API authentication, and configure networking for the deployment.

Inside each Allox VM, run `allox-workspace-daemon` against a Btrfs-backed data
disk mounted at `/var/lib/allox/workspaces`. Do not point the daemon at a
VirtioFS/9p host share: Session checkpoint and rollback require Btrfs subvolume
operations inside the guest.
