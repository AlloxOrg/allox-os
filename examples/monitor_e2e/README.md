# 117 real Agent observability validation

This is an opt-in, fixture-specific integration harness, not a production runtime
installer. Run only in `/data/hy/Allox/monitor-e2e-20261114` on server 117. Each build
and experiment re-reads online CPUs and selects the final 24 logical IDs. The
launcher pins its owned Kata shim/QEMU threads before releasing the Guest gate.
It never changes the shared host kernel or global Kata configuration.

## Components

- `prepare.py`: copy cached sources, build Observer and four Allox wheels.
- `kernel612.py`: verify the pinned upstream kernel archive and build a Guest
  with BTF, BPF LSM, lifecycle tracing, namespaces, cgroups and Btrfs.
- `agent_compat.py fetch|build`: copy the cached Kata 3.10 sources/caches and
  build a local ttrpc compatibility fix. The cached native-library build outputs
  are required; this is not a clean-room Kata build recipe.
- `package_guest.py`: package the new agent into **copies** of the cached image
  and rootfs. Validate the extracted binary hash before inserting the partition.
- `run_117.py`: accept the provider `{base_url,key,model}` configuration over
  SSH stdin (including `auth_header`), keep the real API key in host memory, start an authenticated relay
  and a disposable Kata Guest. Preserve failed attempts, then remove only that
  owned test container. Never print the input configuration.
  `--local-cpu-llm` selects the already-running CPU fixture and an explicitly
  compact system prompt for its 4096-token context. It makes no claim about
  unmodified Codex system-prompt compatibility. `--benchmark-only` runs 10
  alternating off/batch deterministic pairs without any model requests.
- `guest.py` / `action_task.py`: deterministic eBPF probe followed by real
  Codex/Qwen off/strict/batched runs, identity, evidence-integrity and ambiguous
  turn tests. The task creates files, child processes, TCP requests and Unix
  sockets. Its SDK spans are explicit instrumentation, not automatic discovery
  of arbitrary Agent internals.
- `offline_117.py <attempt-directory>`: rejoin sealed streams after Guest exit.
- `summarize_117.py <integration-attempt> <benchmark-attempt>`: retain all 20
  samples and calculate both medians and paired differences separately.

The old Linux 6.1 fixture lacks `file_post_open`, even after enabling BTF/LSM.
The expanded object recorder requires this hook; do not bypass it and report
syscall-only evidence as full object coverage. Linux 6.12 also rejects
`SO_REUSEPORT` on non-INET sockets; `ttrpc-vsock.patch` removes that unnecessary
option from ttrpc 0.8.1's Unix/vsock-only listener. This preserves the existing
9p storage path instead of changing the shared host for virtio-fs.
The fixture kernel also increases the vsock RX buffer to accommodate the legacy
host's 4 KiB payload plus header; `vsock-legacy-rx.patch` does not alter BPF hooks.
The pinned lifecycle object expects the two-argument exit tracepoint. The owned
6.12 fixture therefore includes `sched-exit-group-dead.patch`, backporting the
[upstream exit tracepoint ABI](https://github.com/torvalds/linux/blob/v6.16/include/trace/events/sched.h)
and passing `do_exit`'s actual `group_dead` result, not an inferred live-thread
count. This test does **not** establish compatibility with a stock 6.12 kernel.

`observer-stack.patch` and `observer-scratch.patch` preserve the recorder's event
format/counters but move temporary socket-state storage off the nested BPF
stack. The scratch map is per CPU, used only by non-sleeping hooks; its lifetime
does not cross any helper that re-enters the network stack. The original object
failed real loading with a 560-byte combined stack; inlining alone also failed.

## Interpretation

The check requires real successful model requests, real Agent tool execution,
persisted and ended SDK spans, Core run/cgroup binding, both sealed BPF streams,
an offline verifier receipt, and rejection of altered evidence. The tests check
attempted/published/persisted counts, zero reported drops/failures, completed
barriers/drains and durable WAL. They preserve lifecycle `PARTIAL` and run-level
`INCOMPLETE`: the recorder lacks authoritative initial-task and cgroup-entry
enrollment. Passing this **limited-contract** test is not full observability or
proof that every possible system effect was observed.

Kernel events join exactly to Agent/Session/process run/trace. A single Core
parent turn is a recorded-context attribution, not kernel proof of a specific
inner SDK action. Multiple inner turn IDs must remain ambiguous. The policy does
not trace `read` syscalls. Independent identity fixtures are not a concurrent
Session-isolation test. No rollback, power-loss, detached-process or arbitrary
asynchronous tool attribution claim is made here.

Agent wall times include variable model latency, observer setup/finalization and
execution. One off/on Agent run is a smoke comparison, **not** a statistical
measurement of monitoring overhead. `wall_ns_including_close` in the task covers
the scripted action loop and its final SDK drain, but not the model request.
The separate deterministic pairs measure the **whole** enabled monitoring
pipeline (SDK, collector, BPF, finalization), not eBPF alone. Timing excludes
VM/Store/DB initialization and post-hoc integrity checks. Keep original failed
lab-model and oversized-context attempts alongside any successful fallback.

## Lifecycle diagnostics (2026-11-15)

These scripts are opt-in diagnostics in the same owned 117 fixture; no product
runtime or kernel behavior is patched. `lifecycle_117.py` preserves the launcher,
CPU affinity and safety checks, changing only the Guest diagnostic entry point.

- `lifecycle_117.py`: 10 alternating off/batch pairs; in-memory wall/thread-CPU
  timers around the Core, Observer adapter, collector and Python fsync calls.
- `lifecycle_tools_117.py`: prepare strace and its libraries in a disposable
  build container, into the separate `diagnostic-bin` directory; does not
  install packages on the shared host or change the four Observer artifacts.
- `lifecycle_117.py --strace`: 3 diagnostic pairs with native Observer/verifier
  syscall elapsed times. This adds substantial overhead; not a performance baseline.
- `lifecycle_117.py --kernel-stacks`: 3 pairs sampling only the owned Observer's
  `/proc/<pid>/stack` during FINISH, nominally every 5ms, without strace.
- `--one-pair` bounds a launcher smoke to one pair.
- `lifecycle_summary.py`, `lifecycle_native_summary.py`,
  `lifecycle_stack_summary.py`: read completed attempt directories; preserve
  raw phase records and label nested timers/overlapping syscall times.

The normal-timer 10-pair run is the primary timing result. Native syscall times
and kernel-stack frequencies are diagnostic evidence, not additive phase
budgets or precise CPU percentages. All existing correctness/receipt/hash checks
remain enabled. Full raw evidence remains in each attempt directory on 117.
