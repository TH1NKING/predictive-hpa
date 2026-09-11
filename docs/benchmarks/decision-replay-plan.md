# Same-input decision replay and expansion timing

The user authorized this work on 2026-09-11 and confirmed review baseline
`6855020c5049d6abf69238fdc4660981532d20ca`. Test seams are the replay CLI's
input, output and exit status, and the timing analysis CLI's input, reports and
exit status. Existing controller tests protect unchanged production behavior.

## Deliverables

- Reuse Go prediction and scaling policy to compare Current, Predictive and
  Hybrid with the same observations, policy time, observed replicas and requested
  replicas. Each mode retains its own recommendation history. Never feed a
  counterfactual replica recommendation back as an observed replica count.
- Verify the recorded mode's decisions before publishing comparisons. Recompute
  prediction only when the complete returned CPU history is available. Legacy
  logs omit the first accepted CPU value; preserve that gap and label replay
  using the recorded forecast separately from recomputed prediction.
- Read original controller logs from startup, retain rejected observations and
  validate their history-preservation/reset semantics. Cross-check sample counts,
  target/PHPA identity, configuration and successful Scale write chains against
  recorded API observations. Fail closed on contradictory evidence.
- Align load onset, source sample times, accepted observations, policy decisions,
  successful Scale responses and bounded Ready observations. Separate measured
  intervals, sampled state, explanations of a logged decision and unknown causes.
- Analyze all 18 assigned September 10 warm runs; publish compact replay inputs,
  results, file hashes, a Chinese teaching guide and a single-variable experiment
  hypothesis based on the findings. Preserve the original evidence.

## Boundaries

Offline replay does not establish new HTTP success, p95, replica-time, routing or
production performance. Source sample timestamps do not establish exporter cache
refresh or precise scrape execution. API sampling cannot exclude an external
writer that changes a value and restores it between reads. A skipped decision is
not a failed Scale write, and Ready is not proof of serving a request.

Keep the default mode, reconciliation interval, CPU rate window and safety policy.
No new live benchmark campaign is part of this PR. The user reported a Docker
Desktop startup error during work; continue offline without Docker/WSL probes.
Preserve unrelated in-progress CRD/configuration-validation changes separately.
