# Phase 3 Benchmark Report

> Generated 2026-05-26 23:23 UTC from 18 experiment(s) across 3 pattern(s) × 2 controller(s).

> **Historical evidence notice — updated 2026-09-09.** This is the archived
> May 2026 baseline, not a report on the current controller. The original
> tables and extraction values are retained. Run metadata lists multiple
> commits and includes dirty working trees, so the batch is not identified
> by one immutable controller source revision.
>
> The original workflow used a Service port-forward for the load target.
> This does not establish that newly created replicas served requests;
> per-Pod traffic for these historical runs was not verified. PHPA used a
> 60s downscale window while native HPA used its 300s default, and their
> metric and reconciliation paths also differed. The comparison cannot
> attribute shorter recorded tails to prediction alone.
>
> The tables use historical load-time references and full-experiment
> occupancy windows ranging from 493s to 672s, rather than a common measured
> load-and-tail window. Native runs ending above one replica have incomplete
> tails. Retained `Waste window` values do not necessarily describe complete
> scale-down, and Pod-seconds measure sampled replica occupancy rather than
> CPU consumption or billing. No archived dataset has been recomputed.

For subsequent evidence, see the [Service routing validation](../docs/benchmarks/service-routing-validation.md),
[matched-window controlled pilot](../docs/benchmarks/capacity-and-controlled-pilot-20260906.md),
and [same-controller decision-mode comparison](../docs/benchmarks/decision-mode-ablation-20260907.md).
These separate batches do not reconstruct the baseline's traffic or prove a
general prediction advantage.

## 1. Executive Summary

This historical batch recorded later first scale-up, higher peak replicas and shorter observation-window tails for PHPA. The figures describe this batch under the limitations above. They do not prove that prediction improved efficiency or that service quality was preserved.

| Pattern | Metric | PHPA | native HPA | Δ (PHPA − native) |
|---|---|---|---|---|
| ramp | First scale-up delay | 105 ± 0 s | 80 ± 9 s | +25.0 (+31%) |
| ramp | Peak replicas | 9.7 ± 0.6 | 5.0 ± 0.0 | +4.7 (+93%) |
| ramp | Waste window after k6 stop | 224 ± 0 s | 429 ± 2 s | -205.0 (-48%) |
| ramp | Failed rate | 76.36 ± 3.59% | 77.87 ± 0.96% | -1.5 (-2%) |
| spike | First scale-up delay | 95 ± 17 s | 75 ± 0 s | +20.0 (+27%) |
| spike | Peak replicas | 9.0 ± 1.7 | 5.0 ± 0.0 | +4.0 (+80%) |
| spike | Waste window after k6 stop | 154 ± 17 s | 389 ± 0 s | -235.0 (-60%) |
| spike | Failed rate | 61.09 ± 1.36% | 61.13 ± 0.38% | -0.0 (-0%) |
| step | First scale-up delay | 85 ± 9 s | 75 ± 0 s | +10.0 (+13%) |
| step | Peak replicas | 10.0 ± 0.0 | 5.0 ± 0.0 | +5.0 (+100%) |
| step | Waste window after k6 stop | 194 ± 15 s | 331 ± 69 s | -137.3 (-41%) |
| step | Failed rate | 91.34 ± 1.40% | 91.52 ± 5.25% | -0.2 (-0%) |

## 2. Experiment Setup

- **Total experiments analyzed**: 18
- **Patterns tested**: ramp, spike, step
- **Controllers compared**: phpa, native_hpa (1:1 design)
- **Max repeat index seen**: r3
- **Git commits used**: 508ca66, 6b286c7, 8358b8f, 928c44b, 9f508f8
- **Experiment duration range**: 493s — 672s
- **Cluster**: kind-hpa-dev (single node, Ubuntu 24.04 VM)
- **Target workload**: php-apache (CPU-bound, requests=200m / limits=500m)
- **Load tool**: k6 v1.3.0, target RPS=25 (historical setting; delivered load was reduced by dropped iterations)

The original workflow reset the Deployment, switched controllers, accumulated metrics, ran the selected load pattern, observed a tail and collected artifacts. The archived metadata spans several source revisions and experiment durations; it does not establish one identical execution window across all runs. Relative timing and `after k6 stop` values retain the old extractor's references. Successful artifact collection is separate from successful service or a validated capacity comparison.

## 3. Per-Pattern Comparison

### 3.1 `ramp` pattern

*PHPA runs: 3 | native HPA runs: 3*

| Metric | PHPA | native HPA |
|---|---|---|
| Total requests | 3972 ± 45 | 3933 ± 12 |
| Failed rate | 76.36 ± 3.59% | 77.87 ± 0.96% |
| Dropped iterations | 527 ± 45 | 566 ± 12 |
| p95 latency (all) | 10001 ± 0 ms | 10002 ± 1 ms |
| p95 latency (success only) | 8561 ± 207 ms | 8866 ± 365 ms |
| First scale-up delay | 105 ± 0 s | 80 ± 9 s |
| Convergence time (to peak) | 165 ± 30 s | 100 ± 9 s |
| Peak / steady-state replicas | 9.7 ± 0.6 | 5.0 ± 0.0 |
| First scale-down (after k6 stop) | -146 ± 139 s | 419 ± 0 s |
| Full scale-down (after k6 stop) | 224 ± 0 s | n/a |
| Pod-seconds (experiment total) | 2260 ± 46 | 2840 ± 28 |
| Avg replicas | 3.42 ± 0.07 | 4.30 ± 0.04 |
| Waste window (replicas > 1 after k6 stop) | 224 ± 0 s | 429 ± 2 s |

**PHPA-specific decision counters:**

| Metric | Value |
|---|---|
| Total reconciles | 40 ± 1 |
| Scaled=true count | 14 ± 1 |
| Stabilized=true count | 10 ± 0 |
| Skip reason: `(none)` | 13.7 ± 0.6 |
| Skip reason: `DesiredEqualsCurrent` | 24.7 ± 0.6 |
| Skip reason: `WithinToleranceBand` | 2.0 ± 0.0 |

**Interpretation:**

The original extraction reports first scale-up at 105s for PHPA versus 80s for native HPA, and mean peak replicas of 9.7 versus 5.0. Recorded tail occupancy is 224s versus 429s, but the native group has no reported full scale-down time. Failed-rate means are 76.4% and 77.9%, and all-request p95 is near 10s. These observations do not identify smoothing or a single-Pod startup bottleneck as the cause, nor establish preserved service quality.

### 3.2 `spike` pattern

*PHPA runs: 3 | native HPA runs: 3*

| Metric | PHPA | native HPA |
|---|---|---|
| Total requests | 2011 ± 20 | 2004 ± 13 |
| Failed rate | 61.09 ± 1.36% | 61.13 ± 0.38% |
| Dropped iterations | 238 ± 20 | 245 ± 13 |
| p95 latency (all) | 10001 ± 0 ms | 10001 ± 1 ms |
| p95 latency (success only) | 9358 ± 215 ms | 9185 ± 162 ms |
| First scale-up delay | 95 ± 17 s | 75 ± 0 s |
| Convergence time (to peak) | 125 ± 17 s | 90 ± 0 s |
| Peak / steady-state replicas | 9.0 ± 1.7 | 5.0 ± 0.0 |
| First scale-down (after k6 stop) | -26 ± 17 s | 379 ± 9 s |
| Full scale-down (after k6 stop) | 164 ± 0 s | 389 ± 0 s |
| Pod-seconds (experiment total) | 1860 ± 270 | 2695 ± 9 |
| Avg replicas | 2.95 ± 0.43 | 4.28 ± 0.02 |
| Waste window (replicas > 1 after k6 stop) | 154 ± 17 s | 389 ± 0 s |

**PHPA-specific decision counters:**

| Metric | Value |
|---|---|
| Total reconciles | 37 ± 2 |
| Scaled=true count | 15 ± 1 |
| Stabilized=true count | 7 ± 1 |
| Skip reason: `(none)` | 14.7 ± 1.2 |
| Skip reason: `DesiredEqualsCurrent` | 22.0 ± 2.0 |
| Skip reason: `WithinToleranceBand` | 2.0 (n=1) |

**Interpretation:**

The original extraction reports first scale-up at 95s for PHPA versus 75s for native HPA, mean peak replicas of 9.0 versus 5.0, and recorded tail occupancy of 154s versus 389s. Both failed-rate means are about 61%, with dropped iterations and all-request p95 near 10s. Different stabilization windows and unverified traffic distribution prevent attributing the tail difference to prediction or treating similar failure rates as equivalent service.

### 3.3 `step` pattern

*PHPA runs: 3 | native HPA runs: 3*

| Metric | PHPA | native HPA |
|---|---|---|
| Total requests | 3707 ± 37 | 3693 ± 89 |
| Failed rate | 91.34 ± 1.40% | 91.52 ± 5.25% |
| Dropped iterations | 793 ± 37 | 807 ± 89 |
| p95 latency (all) | 10009 ± 13 ms | 10003 ± 2 ms |
| p95 latency (success only) | 8346 ± 551 ms | 9069 ± 238 ms |
| First scale-up delay | 85 ± 9 s | 75 ± 0 s |
| Convergence time (to peak) | 115 ± 9 s | 90 ± 0 s |
| Peak / steady-state replicas | 10.0 ± 0.0 | 5.0 ± 0.0 |
| First scale-down (after k6 stop) | -16 ± 15 s | n/a |
| Full scale-down (after k6 stop) | 194 ± 15 s | n/a |
| Pod-seconds (experiment total) | 2165 ± 113 | 2395 ± 346 |
| Avg replicas | 3.85 ± 0.28 | 4.27 ± 0.10 |
| Waste window (replicas > 1 after k6 stop) | 194 ± 15 s | 331 ± 69 s |

**PHPA-specific decision counters:**

| Metric | Value |
|---|---|
| Total reconciles | 37 ± 4 |
| Scaled=true count | 14 ± 3 |
| Stabilized=true count | 9 ± 1 |
| Skip reason: `(none)` | 14.3 ± 3.2 |
| Skip reason: `DesiredEqualsCurrent` | 20.3 ± 3.5 |
| Skip reason: `WithinToleranceBand` | 2.0 ± 0.0 |

**Interpretation:**

The original extraction reports first scale-up at 85s for PHPA versus 75s for native HPA, and mean peak replicas of 10.0 versus 5.0. PHPA's recorded tail occupancy is 194s; native HPA's 331s is an observation-window value from runs without a recorded downscale. Failed-rate means exceed 91% and all-request p95 is near 10s. The smaller PHPA occupancy does not establish useful service efficiency.

## 4. Cross-Pattern Findings

The three-pattern means describe this batch. Consistent directions across three small groups do not establish structural behavior or justify extrapolation to other workloads.

- **Prediction and stabilization were confounded.** PHPA's 60s window and native HPA's 300s default can affect tail occupancy independently of the input signal. The different metric paths and control loops remain additional variables.
- **The original tail reductions are limited by timing and censoring.** Their ordering across spike, ramp and step does not establish that prediction benefits more transient loads. Some native runs ended before scale-down completed, and the historical references are not a matched measured load window.
- **Lower total Pod-seconds are descriptive occupancy values.** The tables show less PHPA occupancy in each pattern over unequal experiment durations, alongside higher peaks and poor service outcomes. They do not establish lower CPU consumption, billing or service-adjusted resource cost.
- **Similar failed-rate means do not establish equivalence or noninferiority.** Each group has three repeats, substantial failures, dropped iterations and all-request p95 around 10s. No statistical service-preservation claim is supported.
- **The cause of historical failures remains unresolved.** Load-shape differences, startup behavior and routing are plausible contributors, but this batch did not isolate them. A common replica formula alone cannot produce a clean causal comparison of the prediction algorithm.

## 5. Known Limitations

### Sampling & instrumentation

- **Replica timeline precision is 15s** (prom step size). Per-second scale events are visible only via events.yaml, which is unreliable for PHPA (no SuccessfulRescale emitted) and contaminated across experiments by 1h K8s event TTL — see commit e862d1b for rationale.
- **Sample size n=3 per (pattern, controller)** supports only the descriptive summary presented here. No hypothesis, equivalence or noninferiority tests were performed; no statistical-significance claim is made.
- **Historical time references and incomplete tails** limit comparisons. Negative values in the original `First scale-down (after k6 stop)` rows denote a first recorded downscale before the extractor's stop reference; they are not a measured negative post-load response time. `n/a` and extraction warnings must not be read as completed scale-down.
- **Single-node kind cluster** does not reflect production scheduling latency, node-to-node network jitter, or PV provisioning delays.

### Warnings emitted during extraction

- `no scale-down events detected; experiment ended at steady_state=5 replicas`
  - Affected: 20260527_002241_step_native_hpa_r1, 20260527_011955_step_native_hpa_r2, 20260527_030722_step_native_hpa_r3, 20260527_035111_ramp_native_hpa_r2

### Experiments excluded from aggregation

- skipped _INCOMPLETE_20260526_231116_step_phpa_r1: marked incomplete

### PHPA implementation scope (v1alpha1)

- In-memory stabilization-window history (single replica controller only; restart loses history).
- CPU metric only; no memory or custom metrics.
- Deployment scaleTargetRef only; no StatefulSet / ReplicaSet support.
- minReplicas=0 (scale-to-zero) accepted by CRD validation but coerced to 1 at runtime.
