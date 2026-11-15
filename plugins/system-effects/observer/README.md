# Resident Observer native patch (experimental, opt-in)

`resident-v1.patch` modifies the separate GPL-2.0-only Rust Observer, not the
Allox Guest kernel or BPF programs. Original SPDX headers are retained; this
patch is also GPL-2.0-only. It is paired with the Python `resident_observer`
adapter. An unmodified Observer does **not** support this option.

The cached 117 Observer source is the tested base; the six input and output
file hashes in `resident-v1.json` are authoritative. This is not a claim that
an arbitrary upstream checkout or another Observer release is compatible.
The base also needs the BPF/kernel fixes described in
`docs/development/monitor-e2e-20261114.md`; this patch does not replace them.

On a separate writable Observer source copy:

```sh
python3 /path/to/allox-os/examples/monitor_resident/apply_observer_patch.py /path/to/observer
cargo build --release --locked --offline --bin deployment-observe --bin deployment-observe-verify
```

The helper verifies every input hash, applies `patch --fuzz=0 -p2`, then
verifies every result hash. Already-matching results are accepted without
reapplying. A different source version is refused. A matching file set is not
a substitute for pinning the rest of the build environment.

The prototype retains the main links, programs and maps, with a **new**
ring-buffer consumer, callbacks, config, WALs, JSONL and sealed evidence per run.
The two asynchronous TCP producer links still detach/re-attach at each run's
seal boundary. Resource pooling requires empty run/object state maps, cleared
root identity slots, no pending callbacks, completed drains and durable
artifact sealing. Any unproven condition terminates the worker.

Native mode: `deployment-observe --resident-stdio`. It accepts serial START /
FINISH / SHUTDOWN commands through trusted private pipes. The existing v2
READY/FINISHED protocol is retained; a separate nonce/hash/epoch-bound IDLE
ACK additionally authorizes the next run. Only balanced expanded observation
v2 is allowed. Recovery v3 and optional state scopes stay one-shot.

4096 successful run identities are retained to reject replay; the next START
is refused and requires explicit worker shutdown/recreation. There is no
automatic restart, concurrent multi-run worker, or privileged Agent endpoint.
