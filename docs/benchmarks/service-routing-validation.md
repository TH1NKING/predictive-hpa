# Service routing validation before the next benchmark

Status: implementation and protocol prepared on **2026-09-05**, followed by
an authorized live diagnostic with 14 valid probes on the dedicated Kind
cluster. The [results report](service-routing-validation-results-20260905.md)
records request distribution and a repeated one-to-five-replica improvement
at 25 RPS under a diagnostic service criterion. This was a fixed-replica
calibration; a formal autoscaler comparison remains separate. The v2 release,
its tag, archived datasets and checksums remain historical records.

## Why calibration comes first

The [v2 publication](stabilization-window-ablation-v2.md) records a harness whose
load target was a Service port-forward. A port-forward session selects one Pod;
it does not send the load through Service distribution across replicas. This
follows the [Kubernetes port-forward reference](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_port-forward/).
It does not establish which Pods received requests in every historical run or
prove the cause of v2's high failure rates.

The new load path is:

```text
k6 Pod → http://php-apache.default.svc:80 → Service endpoints → php-apache Pods
```

Prometheus may still use a host port-forward for observation; that connection
is separate from the application load path. The first acceptance gate is
measured request distribution and capacity at fixed replica counts, before
comparing autoscalers or changing the prediction algorithm.

Both calibration and benchmark runs now use `noConnectionReuse: true` so each
request opens a fresh connection. This policy differs from the archived v2
harness and can itself affect throughput and latency; record it as an additional
change when comparing campaigns. Service DNS alone does not prove balanced
traffic with a small pool of persistent connections.

## Offline validation

The following commands inspect the implementation or print plans. They do not
start a cluster or generate traffic. Run from the repository root in Bash:

```bash
python3 -m unittest discover -s hack/tests -p 'test_*.py'
python3 -m unittest discover -s hack/analyze -p 'test_*.py'
bash hack/run_calibration.sh --dry-run
bash hack/run_matrix.sh --dry-run
git diff --check
```

The existing 27-run matrix remains a pilot plan with three repeats per group.
A printed plan or passing script test does not validate traffic distribution,
capacity, or readiness for a formal v3 campaign.

## Before any live run

Use an explicitly selected, dedicated Kind cluster with the target Deployment,
Service, and monitoring installed. `BENCHMARK_CONTEXT` must name that cluster's
context and match the active context. Do not run against a shared development
or production cluster.

Record the following **before load starts**, with UTC timestamps, in a
machine-readable campaign manifest and an environment snapshot:

| Item | Required record |
|---|---|
| Source | Full commit SHA, exact tag, clean tracked and untracked status, script/configuration checksums |
| Images | Immutable digests for k6, target application, PHPA controller, monitoring and cluster node images; retain runtime image IDs as corroboration |
| Environment | OS/kernel, CPU and memory allocation, node topology, Docker/Kind/Kubernetes/kubectl/k6 versions |
| Routing | Explicit context, namespace, Service definition, selector, session affinity, ready endpoint and Pod identities, connection-reuse settings |
| Resources | Per-container CPU/memory requests and limits, node allocatable capacity, k6 resource allocation and placement |
| Workload | RPS levels, duration, quiet/tail periods, VU settings, timeout, repeat order, chosen latency/success thresholds |
| Observation | Scrape intervals, PromQL and query step, log source, clock synchronization, raw-data destination and retention |

A version tag such as `grafana/k6:1.3.0` is convenient for a diagnostic preview;
formal runs require `grafana/k6:1.3.0@sha256:<verified-digest>` and pinned target
and controller images. Resolve and verify real digests before running; the
placeholder is not a usable image. Keep output outside the source tree when
checking source cleanliness. A runner's local metadata does not replace the
complete campaign manifest above.

The shared runner requests `500m` CPU and `512Mi` memory, has no CPU limit, and
limits memory to `1Gi` and output storage to `2Gi`. Its default startup/run
timeouts are `120s`/`900s`, configurable through
`K6_RUNNER_STARTUP_TIMEOUT_SECONDS`/`K6_RUNNER_TIMEOUT_SECONDS`. Resource
allocation and a successful process exit do not prove generator headroom.

## Fixed 1/5/10 replica calibration

Calibration requires fixed replicas. Remove every HPA/PHPA object targeting
`default/php-apache` in the dedicated cluster before starting; the calibration
preflight rejects these objects even if their controllers are paused. Verify
the target has enough node capacity for ten ready replicas, and that request
logs identify individual Pods. Do not modify historical experiment directories.

First inspect a plan with explicit settings:

```bash
PROBE_REPLICAS='1 5 10' \
PROBE_RPS_LIST='1 3 5' \
PROBE_DURATION_SECONDS=90 \
CALIBRATION_ROOT=benchmark-runs/service-routing-calibration \
  bash hack/run_calibration.sh --dry-run
```

The initial `1, 3, 5 RPS` sweep is a low-load diagnostic starting point, not a
claim about service capacity. Extend it only after checking successful request
rates, latency, per-Pod distribution and generator headroom. Run the same RPS
levels at each replica count so comparisons remain meaningful. Preserve every
probe's settings; do not silently change a workload mid-comparison.
Replica and RPS lists must each occupy one line; accepted values are `1..10`
replicas, `1..1000` RPS, and `60..600` seconds per probe.

After the preceding live-run prerequisites and execution authorization are met,
the same plan can be executed in the dedicated cluster:

```bash
# Example context name; use the actual dedicated cluster context.
BENCHMARK_CONTEXT=kind-service-routing \
K6_IMAGE='grafana/k6:1.3.0@sha256:<verified-digest>' \
PROBE_REPLICAS='1 5 10' \
PROBE_RPS_LIST='1 3 5' \
PROBE_DURATION_SECONDS=90 \
CALIBRATION_ROOT=/path/outside-repo/service-routing-calibration \
  bash hack/run_calibration.sh
```

The calibration runner temporarily sets the target's replica count during probes
and attempts to restore its original replica count on exit, refusing to overwrite
intervening Deployment or autoscaler changes. Check `restore.log` and any
restoration error, especially after interruption.

Each invocation creates a unique `<CALIBRATION_ROOT>/<UTC>_<unique>/` directory:

| Location | Recorded evidence |
|---|---|
| Run root | `manifest.json`, `git-status.txt`, `deployment-before.json`, `service-before.json`, `nodes-before.json`, `kubernetes-version.json` captured before probes |
| `replicas-N_rps-R_<unique>/` | `probe.json`, `pods-before.json`, `pods-after.json`, `endpoints-before.json`, `endpoints-after.json`, `cpu-by-pod.json`, `load-generator-cpu.json`, `load-generator-memory.json`, `<Pod>.log` |
| Each probe's k6 files | `k6.json`, `k6-summary.json`, `k6-stdout.log`, `k6-warnings.log`, `k6-version.txt`, `k6-start-time-unix`, `k6-end-time-unix`, `k6-exit-code`, `k6-runner.json`, runner manifests and Pod status |
| Run summary | `summary.json`, `report.md`, generated by `hack/analyze/calibration.py` |

The pre-run snapshot records source and cluster details but does not enforce a
clean tagged checkout or capture every environment item above. Supplement it
before formal execution. Failed output collection retains the runner Pod and
ConfigMap for recovery; inspect `k6-collection-errors.log` promptly, before the
Pod's active deadline ends its execution.

## Acceptance evidence

At each fixed replica count, compare evidence from the same recorded load
interval, excluding warmup and readiness transitions:

1. **Ready endpoints:** exactly the intended application replicas are ready
   and selected by the Service. No Pod restarts or concurrent autoscaling
   invalidate the fixed-replica interval. Matching before/after snapshots alone
   cannot rule out a transient readiness change between those snapshots.
2. **Requests per Pod:** count application access-log requests with the unique
   `phpa-routing/<token>` User-Agent, which identifies this probe and excludes
   unrelated traffic. Account for missing or truncated logs.
   Every ready Pod must receive load in sufficiently long, multi-connection
   5/10-replica probes. Report totals and shares; equal shares are not assumed.
   Missing request evidence means routing is unverified, even if CPU rises.
3. **CPU per Pod:** preserve per-Pod CPU time series and relate usage to each
   Pod's request count and resource limit. Aggregate CPU alone cannot show that
   requests reached every replica. The summary reports mean CPU in cores,
   excluding samples in the first 30 seconds of k6 execution. The 1-minute CPU
   rate window still includes some earlier idle time; this is an observation
   mean, not a steady-state capacity estimate. Inspect the recorded k6 CPU/memory
   series and scheduling limits to rule out a saturated generator; empty
   generator series mean its headroom has not been measured.
4. **Achieved throughput:** separate offered RPS, completed requests, successful
   responses, failures/timeouts, dropped iterations and success-only latency
   from all-request latency. Additional replicas must increase sustainable
   successful throughput at a stated latency/success target when demand is
   sufficient. Low offered load alone cannot establish capacity gains, and a
   linear 5×/10× gain is not assumed.

Do not treat an automatically generated summary or lack of script errors as a
pass for all four conditions. Inspect raw evidence, compare more than one
probe, and record any imbalance or generator/node bottleneck before accepting
the routing path. If the initial sweep never approaches capacity, extend the
RPS range in a separately recorded diagnostic sweep.

Analysis exits nonzero if any probe lacks required routing evidence; the
`problems` lists explain the missing observations. Even a successful routing
summary always sets `capacity_validated: false`: capacity needs separate review
across replica counts and load levels. Reported throughput uses the recorded k6
execution interval, including the time allowed for in-flight requests to finish.

## Gate for a formal v3 comparison

Use calibration to define and freeze three load regimes with explicit
application latency and success thresholds:

| Regime | Selection criterion |
|---|---|
| Normal | Below measured capacity, meeting the declared latency/success target |
| Near capacity | Around the measured throughput/latency knee, with sufficient generator headroom |
| Overload | Beyond sustainable target capacity; report saturation, timeouts and dropped iterations explicitly |

For each regime, specify offered load relative to both initial and maximum
replica capacity. A workload may overload one initial Pod yet fit after
expansion; report this distinction. The September 5 diagnostic observed this
at 25 RPS: one Pod failed the criterion while five passed. That result does
not make 25 RPS a universally validated capacity setting; calibrate it for the
declared environment and the normal, near-capacity and overload regimes.

Before a formal v3 run, match Native-60 and PHPA-60 on minimum/maximum replicas,
CPU target, resource requests/limits, application image, stabilization window,
load and environment. Record remaining differences in metric source, sampling,
reconciliation interval and scale policies. Keep the Native-300 comparison
separate when estimating the window effect.

Plan at least `n=5` per pattern/controller/regime for descriptive reporting, or
justify the repeat count with an advance power/precision analysis. Five repeats
alone do not establish statistical significance. Freeze the primary metrics,
repeat order, failure/exclusion rules, confidence-interval method and censoring
policy before collecting the formal data. Rotate or randomize controller order
within matched blocks, preserve failed runs, and use a separate campaign root.

The single-run interface remains
`bash hack/run_benchmark.sh step native_hpa_60 1`; it now launches k6 in the
cluster. The default campaign/root are `service-routing-v1` and
`experiments/service-routing-v1`. This interface and the three-repeat matrix
are pilot tooling, not a complete formal v3 protocol.

Report request success, first scale-up timing, peak replicas, total
Pod-seconds, post-load resource tail and censored outcomes separately. Matching
the window compares complete controllers; it does not isolate the causal effect
of prediction. Investigating current-only, prediction-only or hybrid decisions
within one controller is a subsequent experiment after routing and capacity
are established.

Archive large raw data in a declared external location with checksums. Keep
GitHub Release assets limited to compact report, manifest, source/environment
snapshots and extracted analysis records. Do not overwrite v2 artifacts or
reuse historical datasets for new run output.
