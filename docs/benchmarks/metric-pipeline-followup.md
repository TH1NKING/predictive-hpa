# Metric pipeline and paired CPU-window observation

The user authorized this implementation and pilot on September 8, 2026, and
confirmed the public observer CLI/output files and analysis CLI as test seams.
The review baseline is `24a13f468b81a770d1a3ed99d1d1fa3c9e1396c0`.
Implement with focused red/green checks, independent Standards and Spec reviews,
and commit on the current branch. Deliver a Chinese explanation of the work,
evidence, alternatives and limitations.

## Question and scope

The previous ten-run retrospective found three observations of recent CPU
increments preceding the one-minute threshold. However, 142 of 200 pre-expansion
raw snapshots had fewer than two samples inside 30 seconds. These are sample
counts, not measured short-window query failure rates. This follow-up observes
the actual source endpoint, available scrape evidence and real Prometheus
30/60-second expressions before considering a change to control actions.

Keep production CPU rate at 60 seconds and Current pilot reconciliation at
30 seconds. No CRD, predictor or policy change is required. Instrument the
existing observer through an explicit opt-in and include that option and the
source node in benchmark identity. Preserve prior evidence and checkouts.

## Observation contract

- Choose one evaluation timestamp per cycle for raw CPU, CPU requests and both
  window expressions. Retain cycle identity and each request's wall-clock
  boundaries, duration, response, empty result and error. Equal evaluation time
  does not make independent queries an atomic TSDB snapshot.
- Keep `prom_cpu_evaluated` as the original 60-second expression. Add
  `prom_cpu_evaluated_30s`, evaluated by the running Prometheus engine. Do not
  replace `rate` with counter differences or historical post-run visibility.
- Read the selected node's `/metrics/cadvisor` through the Kubernetes node proxy.
  Retain relevant CPU counter and `container_last_seen` exposition lines,
  including explicit timestamps if supplied. Endpoint request time, exported
  sample time and a last-seen value have different meanings. Missing timestamps
  remain unknown. Match source and stored series conservatively; repeated
  values or multiple candidates cannot establish a unique update or ingestion.
- Poll the cAdvisor target's public last-scrape, duration, health and error
  evidence. Retain failed requests. Record engine version and effective
  monitoring configuration separately without credentials. Verify the running
  version's timestamp semantics before labeling derived scrape intervals;
  polling the latest scrape can miss earlier attempts and is not a complete log.
  Also retain actual 90-second range vectors of that target's `up`, scrape
  duration and sample counts to expose retained failures between target polls.
- Record cycle duration, request counts and missed two-second cadence. Do not
  issue catch-up bursts. Measure added observation load; its service impact
  remains unidentified without an observer-off control.
- Provide a read-only single-cycle CLI for preflight and boundary tests. Refuse
  output overwrite. A failed collection is retained and returns nonzero.

## Analysis contract

Read all required inputs before accepting no expansion as a valid result. Use
actual k6 `scenario.startTime + 30s` as onset. Principal analysis includes
evaluations at or after onset with responses ending strictly before the first
successful increased Scale target's write start. Preserve the no-expansion
case with a null cutoff instead of inventing pre-expansion coverage.

Report every assigned run and every cycle, paired window availability, first
strictly-above-55% observations and their request intervals. Distinguish empty,
failed, missing, nonfinite, multiple-series and mismatched-evaluation results.
Count per-series raw samples in `(evaluation - window, evaluation]`. Counters,
source exposition and scrape snapshots retain identities and quality flags.
Report unequal query snapshots and uncertain source-to-store matching honestly.
Output must be outside input runs and must not overwrite existing evidence.

Use hand-worked public CLI fixtures, including errors and missing observations,
then run the complete offline harness/analysis suites. Independently recompute
the pilot's key numbers without importing the new analyzer. Preserve input
checksums and include traceable compact result data.

## Frozen small pilot

Use the existing dedicated Kind `hpa-dev` only after read-only identity checks
and fresh fixture backups. The initial check found a recent VM/node restart;
perform a separately archived 25 RPS fixed-replica revalidation (1 and 5 replicas,
90 seconds each) before the observation campaign. Keep calibration results
separate from controller observations. Restore and verify original specs.

The observation campaign has three assigned Current step runs, all with a
10-second requested onset offset and 30-second reconciliation. Keep the pinned
workload/generator images, CPU request, min/max 1/10, target 50%, stabilization
60 seconds, alpha 30%, history 5 minutes and horizon 30 seconds. Reuse the
30-second quiet stage, 181-second step and complete 360-second post-load tail.
One frozen source, controller binary and observation configuration serves all
three runs. Record actual phase rather than silently reassigning it.

Retain infrastructure failures, query gaps, HTTP failures, dropped iterations
and phase deviations. Stop later slots on collection or restoration failure;
never selectively rerun an unfavorable result. Reuse existing owned-process,
UID/spec conflict and fixture-restoration guards. Preserve all unrelated
resources, older source directories and existing SSH access.

## Decision and teaching

The deliverable is a diagnosis, not a predetermined performance improvement.
Report whether the short window reliably provides usable values, whether its
threshold observations are consistently earlier, and what source/scrape evidence
can explain. With only three runs, describe outcomes without population claims.
If availability or attribution remains unresolved, retain the default and name
the evidence still needed. A later single-variable closed-loop comparison is
justified only after this observation step; do not infer HTTP success, p95 or
Pod-seconds improvements from a passive window comparison.

The Chinese walkthrough must explain the implementation, timestamp meanings,
why real paired queries and source evidence help, how the tests constrain bugs,
and the drawbacks of immediate tuning, counter-difference substitutes and
historical queries. Include actual results, reproducible commands, review
outcomes and restoration verification.
