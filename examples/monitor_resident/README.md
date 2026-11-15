# Resident Observer experiment

Optional serial worker, with fresh per-run evidence and fail-closed sealing.
This fixture extends `../monitor_e2e`, not a new workload. See
`../../docs/development/monitor-resident-20261115.md` for results and limits.

All builds, native checks, pytest and workloads run on 117 through the approved
jump host, under `/data/hy/Allox`; each experiment/build re-reads online CPU IDs
and binds to the numerically final 24. No local benchmarks are needed. Do not
copy connection credentials or provider keys into these examples.

The owned `/data/hy/Allox/monitor-resident-20261115/source` is the Allox tree;
the old `/monitor-e2e-20261114` directory supplies the pinned Guest kernel,
rootfs, runtime, BPF objects, Rust caches and source baseline. An independent
Observer copy and new output binaries are used. Global host services/kernel
are not changed. See the prior fixture README for these prerequisites.

1. Copy/overlay this tree into the owned `source` and run `prepare_python_117.py`
   (its archive input is `python.tar.gz`, containing the two example/plugin
   directories). Run `prepare_117.py` to copy the native base, verify/apply
   `plugins/system-effects/observer/resident-v1.patch`, and compile it. The
   original native binaries are not overwritten.
2. Run `validate_117.py` for lint, byte-checked native patch application and
   Python regressions. `run_117.py --smoke` exercises the kernel path, but its
   interleaved resident/one-shot cases are **not performance evidence**.
3. From `/data/hy/Allox/monitor-e2e-20261114`, run the absolute path to
   `source/examples/monitor_resident/run_117.py` in the new root for the formal
   40 cases: 10 alternating off/one-shot pairs
   **before loading resident probes**, then 10 alternating resident/off pairs.
   Every case runs the same 12 actions and original integrity/attribution
   checks. Zero LLM requests, no external model latency in these measurements.
4. Run `summarize_117.py <attempt>` and `postverify_117.py <attempt>`; run the
   previous fixture's `offline_117.py <attempt>` for post-Guest correlation.
   `postverify_117.py` mounts sealed artifacts read-only and independently
   reruns the native verifier for all 20 monitored cases.
5. `run_117.py --safety` tests three Agent/Session identities on the same
   worker and consecutive runs/epochs in one Session, compares old committed
   hashes after reuse, then deliberately sends
   a previous run's FINISH nonce. Expected result: failed/fenced run, exited
   poisoned worker, no successful receipt, empty fault cgroup, next command
   refused, previous sealed artifacts unchanged.

Do not run diagnostics concurrently with timed workloads. Cold first startup
is included among the ten resident runs. Final shutdown is separately recorded
and amortized over all ten; it is not silently discarded. A resident-phase
"off" sample disables SDK/collection, but the idle hooks remain installed.
The earlier baseline off samples have no resident hooks. Keeping resident
hooks loaded during one-shot measurement can avoid its final ftrace detach
grace period and would bias the reference result; staging prevents that.

The worker is process-global and serialized by the existing host lock. It is
not a parallel session scheduler. This mode does not change partial event
coverage, add turn-level causality, or implement recovery v3 reuse. See the
plugin README for explicit shutdown, poisoning and restart contracts.
