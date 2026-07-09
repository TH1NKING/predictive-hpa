# Why this experiment is incomplete

This directory contains data from the first end-to-end attempt of
`hack/run_benchmark.sh step phpa 1` (basis commit f3ed499).

## What happened

k6 exited at t=220s instead of the configured t=450s. The trailing
`target: 0` stage (239s tail observation) was truncated to ~9 seconds.

Root cause: the k6 ramping-arrival-rate executor exits a `target: 0`
stage as soon as all in-flight iterations have completed (a built-in
CPU-saving optimization). Because the hold stage was saturated, k6's
last in-flight request finished exactly 10s after hold ended (the
http_req_timeout ceiling). The tail stage saw all VUs idle + target=0
and exited early — at the cost of all scale-down observability.

## What this experiment did capture (still useful)

- Scale-up trajectory: 1 -> 4 -> 10 replicas in ~30s (vs Phase 0 native
  HPA baseline 1 -> 4 -> 5 -> 6 -> 8 over ~90s, 4 steps).
- Confirms PHPA scales up significantly faster than native HPA on the
  same load pattern.

## What is missing

- Any scale-down data. Native HPA's ~5-6 min scale-down vs PHPA's
  ~60-90s window-stabilized scale-down — the project's headline
  comparison — could not be observed.

## Fix

See git commit following f3ed499: tail observation moved out of k6
stages into `hack/run_benchmark.sh` as a bash `sleep`. k6 now exits
right after the load ends; the orchestrator pauses for 240s before
collecting Prometheus data.
