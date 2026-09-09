# Allox eBPF process tracker

This provider is a small, independently replaceable native component. It uses
the same process-domain propagation pattern as ActPlane, but assigns the run at
the Session cgroup boundary: a trusted controller maps the stopped gate's
cgroup ID to an opaque run cookie, the first sched event establishes the Guest
kernel PID, `sched_process_fork` copies that identity to children, and
`sched_process_exec` / `sched_process_exit` emit lifecycle events. This remains
correct when the collector and Agent run in different PID namespaces.

It deliberately does not kill processes. Allox uses the Session cgroup as the
authoritative membership and termination mechanism.

Build requirements are `clang`, `bpftool`, `libbpf`, `libelf`, `zlib`, `make`,
and a C compiler. The program does not use CO-RE and therefore does not require
kernel BTF. It must be rebuilt and kernel-tested with the shipped Allox Guest
kernel because tracepoint field layouts are not a cross-kernel stable ABI.

```sh
make
sudo ./allox-process-tracker
```

The stdin/stdout NDJSON control protocol is versioned by ABI. The Python
adapter validates the ABI before accepting executions.
