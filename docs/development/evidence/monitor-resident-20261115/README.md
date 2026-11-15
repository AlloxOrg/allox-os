# 117 resident Observer evidence

- `report.json`: all 40 fixed-workload cases; each monitored case preserves
  the original limited-coverage, attribution, tamper-rejection and SDK checks.
- `resident-summary.json`: ten paired samples per phase, full-run/task timings,
  cold first run, stable worker/kernel identities, final shutdown and amortized
  mean. See the report for the distinction between hook-free baseline off and
  resident-idle off.
- `runtime.json`: owned Kata run, CPU affinity, successful exit/removal,
  zero gateway requests. Model-related fields are inherited launcher metadata;
  this benchmark did not call a model.
- `postverification.json`: native offline verifier rerun on all 20 monitored
  artifact sets after Guest/worker exit; evidence mounted read-only.
- `offline-report.json`: 40 successful post-Guest stream correlation queries.
- `safety-report.json` / `safety-runtime.json`: three Agent/Session identities,
  two consecutive epochs in one Session, sealed-file hash comparisons and an
  intentionally rejected stale FINISH nonce. Worker exit=1 and poisoning in
  this report are the **expected injected failure**, not a benchmark failure.
- `validation-final.log`: Ruff, existing configured Pyright scope, exact native
  patch/idempotence checks, 395 passed / 21 skipped. Pyright's existing project
  include list does not cover system-effects; no stronger static claim is made.
- `build.log` / `build-identities.json`: native build output and SHA256 identities
  for the actual resident binaries and pre-existing BPF objects.

Raw WALs/JSONL remain in the two owned attempt directories on 117 identified in
the development report. This directory contains reports, not connection or
provider credentials. No LLM-dependent latency or syscall-count equality is
claimed. PARTIAL / INCOMPLETE coverage remains intentional.
