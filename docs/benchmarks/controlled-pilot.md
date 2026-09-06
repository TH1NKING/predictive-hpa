# Calibrated Service-path controller pilot

This protocol scopes the follow-up to the September 5 routing diagnostic:
measure capacity bounds, configure the harness, then compare Native-60 and
PHPA-60 in six step-load runs. It is a descriptive pilot, not the formal v3
matrix described in [Service validation](service-routing-validation.md).
The controller algorithm and its API remain unchanged.

## Capacity selection before comparing controllers

Use the dedicated `kind-hpa-dev` cluster and an independent source checkout
and output directory. Record the commit, source checksums, immutable k6 and
application image digests, resource allocation, monitoring configuration and
environment before load. Snapshot the existing application and autoscaler
specifications, temporarily remove the benchmark autoscalers for fixed-replica
control, and restore the specifications after each phase. Keep the older VM
checkout and historical evidence intact.

The initial September 6 plan contains nine 90-second probes:

| Fixed replicas | Offered RPS |
|---|---|
| 1 | 10, 15, 20 |
| 5 | 40, 60, 80 |
| 10 | 80, 120, 160 |

The diagnostic criterion remains HTTP 200 responses / all completed requests
at least 99%, all-request p95 at most 500 ms, and zero dropped iterations.
Preserve timeouts in the denominator and report success-only latency separately.
Check per-Pod identified requests, stable Pod/endpoint identities, CPU samples,
and generator CPU/memory observations. An overloaded application is an observed
failure; missing routing evidence or a limited generator is a measurement
limitation, not a measured application capacity bound.

This is an adaptive diagnostic. Record every refinement before starting it.
Repeat useful pass/fail boundaries in reverse order with 180-second probes.
If all tested rates fail, add a lower-rate probe in the current environment.
If all rates pass through 160 RPS, report the tested lower bound rather than
inventing a maximum. Inspect recovery and resource headroom before extending a
phase; a fixed cooldown alone cannot prove recovery. No exact capacity or
statistical significance follows from these short probes.

Freeze one pilot RPS after calibration and before either controller run: choose
a load that exceeds the observed one-Pod criterion but is supported by multiple
Pods. Store the chosen rate and rationale in the campaign manifest. This tests
the transition from initial overload to adequate capacity after scale-up.

## Configurable harness

The public interface remains `bash hack/run_benchmark.sh PATTERN CONTROLLER N`.
Both single-run and matrix interfaces validate configuration before cluster
operations. The matrix additionally accepts the selectors below.

| Variable | Default | Accepted values |
|---|---|---|
| `RPS` | `25` | Integer 1–1000; this is an input range, not validated host capacity |
| `BENCHMARK_PATTERNS` | `step ramp spike` | Nonempty, unique, space-separated subset |
| `BENCHMARK_CONTROLLERS` | `native_hpa_300 native_hpa_60 phpa` | Nonempty, unique, space-separated subset |
| `BENCHMARK_REPEATS` | `3` | Integer 1–1000 |
| `EXPERIMENTS_ROOT` | `experiments/service-routing-v1` | Separate campaign output directory |
| `CAMPAIGN` | `service-routing-v1` | Campaign identity |
| `BENCHMARK_CONTEXT` | None | Explicit dedicated Kind context matching the active context |
| `BENCHMARK_PYTHON` | Analysis virtual environment, otherwise `PYTHON` / `python3` | Python interpreter with PyYAML |

For example, inspect a six-run plan without contacting a cluster (25 is an
example value; use the campaign's frozen calibrated rate for execution):

```bash
BENCHMARK_PATTERNS=step \
BENCHMARK_CONTROLLERS='native_hpa_60 phpa' \
BENCHMARK_REPEATS=3 RPS=25 \
EXPERIMENTS_ROOT=/path/outside-source/pilot/runs \
CAMPAIGN=capacity-pilot-20260906 \
BENCHMARK_CONTEXT=kind-hpa-dev \
  bash hack/run_matrix.sh --dry-run
```

Live execution requires the selected cluster and pinned images described above.
Omit `--dry-run` only after inspecting the effective plan. The runner sends
requests from a k6 Pod through the application Service, with connection reuse
disabled. Requests carry `User-Agent: phpa-benchmark/<experiment-id>` so optional
per-Pod access-log collection can identify individual runs, including Pods later
removed during scale-down.

Virtual users are derived from offered load and the 10-second request timeout:
`preAllocatedVUs=max(20, 10*RPS)`, `maxVUs=max(40, 12*RPS)`. The extra concurrency
reduces the chance that a slowing target prevents k6 from offering the intended
load. It consumes generator memory and is not proof of generator headroom;
inspect actual usage and dropped iterations. k6 documents that arrival-rate
executors need sufficient VUs and that runtime allocation itself has a cost:
[arrival-rate VU allocation](https://grafana.com/docs/k6/latest/using-k6/scenarios/concepts/arrival-rate-vu-allocation/).

Each run records `protocol_version: controlled-pilot-v1`, RPS, VU allocation,
source and configuration SHA256 values. The source fingerprint covers relevant
controller, workload, configuration, runner and analysis sources. The effective
configuration also identifies the cluster context, k6 image, traffic path and
fixed timing. It excludes repeat count and controller order, so expanding the
same campaign can reuse compatible completed runs. Historical or changed
configurations are not treated as completed new runs. Live application resource
and image settings still require the recorded environment checks; an offline
fingerprint cannot validate cluster state.

Completion also requires a valid extraction whose experiment, controller and
window fields match the metadata. An extraction failure stops the matrix and
keeps that repeat pending; its raw evidence is retained. A later attempt writes
a new timestamped directory, and discovery checks the newest attempt first.

## Matched six-run design and measurement windows

Match Native-60 and PHPA-60 on min/max replicas (1/10), CPU target (50%),
application image and resource requests/limits, 60-second scale-down window,
load and environment. Use three matched repeat blocks, with controller order:
Native/PHPA, PHPA/Native, Native/PHPA. Retain all failed runs and state any rerun
reason. Three repeats support a descriptive pilot, not significance claims.

The step schedule remains 30 seconds quiet, 1 second ramp-up, 179 seconds hold,
then 1 second ramp-down. The common observation boundary is **planned offered
load end +360 seconds**. The orchestrator allows an additional scrape before
collecting data, but this does not extend the cost window.

`extract.json` retains historical `scaling` and `resource` field definitions for
compatibility. New campaigns use the separate `measurement` object:

| Measurement | Definition |
|---|---|
| First scale-up | First observed replica increase after load onset, excluding the initial quiet period |
| Peak replicas | Maximum observed replicas from load onset to the fixed tail boundary |
| Total comparable Pod-seconds | Integral of observed Deployment status replicas over that same window |
| Post-load Pod-seconds | Replica integral over the fixed 360-second tail |
| Excess post-load Pod-seconds | Integral of replicas above the minimum over that tail |
| Resource tail | Time above minimum replicas after offered load stops; report incomplete scale-down as censored |

Runner timestamp files anchor the schedule. Preparation, controller compilation,
request drain and artifact-copy duration do not enlarge the compared window.
Replica observations use a 15-second grid and step-held values; these are sampled
replica-cost estimates, with corresponding timing uncertainty. Missing timestamp
files, inconsistent observation boundaries or replica gaps over 15 seconds make
the controlled measurement invalid. Extraction returns nonzero for such a run.

The controlled aggregate uses the new fields, rejects mixed protocol/load/source/
configuration identities and checks agreement between metadata and extracted
records. Report HTTP 200 fraction, all-request and success-only p95, dropped
iterations, first scale-up, peak replicas and resource cost together. Lower
latency alone does not establish a resource benefit.

Native and PHPA still differ in metric source, sampling, reconciliation and
scale policies. Record these differences. Matching the stabilization window
compares complete controllers; it does not isolate the causal effect of the
prediction algorithm. Preserve the v2 datasets, release and conclusions as
historical records rather than rewriting them with new observations.

## Verification and evidence

Run the existing offline runner and analysis suites, shell syntax checks and
the configured dry-run before load. Check controller unit tests before the pilot
when building the frozen source in Linux. Archive raw evidence outside Git with
checksums, including failed attempts, environment snapshots, restoration receipts
and exact source. Commit the protocol, harness changes and a compact results
report. Revoke the task's temporary SSH key after evidence retrieval.
