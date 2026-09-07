# Load-onset latency diagnostic

## Question and authorized scope

The September 7 decision-mode experiment found mean first Scale increases near
49 seconds in all three modes. Each mode had one onset near 29 seconds and two
near 59 seconds. Early Current observations sometimes remained below the
expansion threshold. These observations do not isolate measurement, scheduling,
decision, or workload startup delays.

The user authorized the following order: instrument the information and scaling
timeline, run a small Current-mode diagnostic in a dedicated Kind cluster, and
only then choose a single-variable follow-up if the evidence warrants it. Deliver
the evidence, a latency breakdown, and a Chinese explanation of the work and
method tradeoffs. The user confirmed the three public test seams below and the
implementation review baseline
`6034dfdbac9927c03d84d03d1859fb3522d24ac5`.

## Timing contract

Every reconciliation must expose a correlation key and wall-clock boundaries
for its start, metrics query, decision, successful Scale write if any, and end.
Retain early exits and query failures. Log the observation, raw and bounded
forecast, effective signal, replica recommendation, policy outcome, and skip
reason before a later status-write failure can erase decision evidence.

Prometheus range-query evaluation timestamps are not raw scrape timestamps.
Label the returned evaluation grid, configured rate window, query duration, and
raw source sample timestamps separately. Obtain source-series evidence through
the diagnostic collector; do not add diagnostic queries to the production
decision path. A post-run historical query cannot prove when a sample first
became visible to the controller. Periodic visibility observations must retain
their request/response intervals and uncertainty.

The workload timeline distinguishes the requested Scale target, observed
Deployment replica count, Pod creation and Ready condition, Service endpoint
eligibility, and tagged request evidence. Ready does not prove traffic was
served. Access-log collection time is not request-start time; preserve each
available timestamp's meaning and precision. Unknown or missing observations
remain unknown rather than becoming zero latency.

Instrumentation must preserve the existing metrics query, forecast, default
mode, shared readiness requirement, 30-second requeue configuration, 10-percent
tolerance, replica limits, and stabilization behavior. Wall-clock diagnostic
timestamps must not change the fake clock used by policy tests.

## Pilot design

Use the existing dedicated `hpa-dev` Kind cluster only after checking its current
identity, resources, monitoring configuration, pinned workload/generator images,
and previous calibration applicability. Save the complete original fixture
specifications before mutation. Use a separate source and output directory;
preserve earlier evidence and unrelated checkouts.

Run Current at 25 RPS, min/max 1/10, target CPU 50%, stabilization 60 seconds,
alpha 30%, history 5 minutes, horizon 30 seconds. Keep the original 30-second
quiet period, 181-second step schedule, and 360-second post-load window. The
controller binary and diagnostic observer configuration are shared by all runs.

The planned pilot has two repeat blocks and three onset offsets relative to an
observed idle controller cycle: 0, 10, 20 seconds, then 20, 10, 0 seconds. Save
the intended onset and actual observed phase; startup overhead and extra
resource-triggered reconciliations can prevent exact phase control. A missed
phase is an observed protocol deviation, never silently reassigned or rerun.
The ready generator waits at a bounded 180-second gate. After its readiness
receipt, select a completed idle controller cycle and schedule process launch at
`ceil(anchor finish + 30 seconds + requested offset)`. Keep the existing quiet
stage; intended workload onset is another 30 seconds later. Record the actual k6
scenario start through `k6/execution` at each request attempt, and derive the
actual workload schedule from that clock. Freeze a 2-second circular phase-error
tolerance; a gap longer than 32 seconds is flagged separately rather than reduced
modulo the cadence. The shell launch clock has one-second precision.

Raw CPU counters and CPU-request gauges are queried as literal 90-second range
vectors. Independent CPU-expression and Kubernetes observations target a
2-second interval without catch-up bursts. Every call retains its request and
response boundaries, so a cycle is not presented as an atomic snapshot.

Check recovery between runs. Preserve failed attempts, missed phases, collector
errors, HTTP failures, timeouts, and dropped iterations. If the environment no
longer matches the calibration evidence, record a separate fixed-replica
revalidation before proceeding. No automatic parameter selection or selective
reruns based on service outcomes.

## Analysis and follow-up decision

Produce per-run timelines and distinguish observed waiting intervals from
causal claims: demand to source visibility, visibility to controller query,
query to policy decision and Scale write, and Scale write to workload readiness
and tagged traffic. A one-minute rate window describes averaging semantics,
not a guaranteed one-minute transport delay. Additional collector load and
shared-host contention remain experimental limitations.

Report all runs, actual phase offsets, first genuine Scale target increase,
first Ready/traffic evidence for a new Pod, HTTP 200 fraction, all-request p95,
dropped iterations, and replica occupancy over the fixed window. Do not infer
service improvement from successful-request latency or lower occupancy alone.

Choose at most one follow-up variable after writing the diagnosis: controller
requeue interval if coordination dominates, metrics sampling/calculation if the
information path dominates, or workload startup if readiness dominates. If the
evidence cannot distinguish these, state what remains unresolved and retain the
existing behavior. The diagnostic succeeds by locating or narrowing the source
of delay; a performance improvement is not a predetermined acceptance test.

## Verification and delivery

The confirmed test seams are the Prometheus query/results and diagnostic logs,
the public controller behavior and diagnostic output, and the analysis CLI
processing raw evidence. Use focused red/green regressions,
then the required `make lint-fix`, `make test`, and offline harness/analysis
checks. Review standards and this specification independently before freezing
the experimental source. Commit on the current branch as required by the
user-invoked implement skill.

Archive raw evidence and checksums outside Git. Commit the compact results,
figures, and Chinese walkthrough. Verify restoration of original resource specs,
removal of owned load/controller processes, and preservation of unrelated
resources. Do not remove pre-existing SSH access.

## Reference semantics

- [Prometheus query evaluation and staleness](https://prometheus.io/docs/prometheus/latest/querying/basics/#staleness):
  query evaluation timestamps are selected independently of stored samples.
- [k6 execution timing](https://grafana.com/docs/k6/latest/javascript-api/k6-execution/):
  `scenario.startTime` identifies scenario start in milliseconds; distinguish
  that from process launch and the first attempted request.
- [k6 lifecycle](https://grafana.com/docs/k6/latest/using-k6/test-lifecycle/):
  setup runs after initialization and before scenario execution. Any setup gate
  needs a bounded deadline and an explicit actual-start receipt.
