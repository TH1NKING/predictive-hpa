# Phase 3 Benchmark Report

> Generated 2026-07-09 01:52 UTC from 18 experiment(s) across 3 pattern(s) × 2 controller(s).

> **Historical evidence notice — updated 2026-09-09.** This report preserves the
> July 2026 batch and its original tables. It describes an earlier controller
> and benchmark harness, not the current implementation. All 18 run metadata
> files record commit `409227d` with `dirty: true`, so that commit alone does
> not identify the exact code executed.
>
> The historical harness at that commit targets `localhost:8080` through
> `kubectl port-forward svc/php-apache`. That path does not establish request
> distribution across replicas; actual per-Pod traffic in these runs was not
> verified. PHPA used a 60s downscale window while native HPA used its 300s
> default. Together with differing metric and reconciliation paths, this
> prevents attributing the observed differences to prediction alone.
>
> These tables retain the old extraction conventions: inferred load boundaries,
> 15s replica samples, and total Pod-seconds over experiments of different
> lengths (612–675s). Some native runs ended before returning to one replica.
> The reported tails are therefore observation-window values, not necessarily
> complete scale-down durations. Pod-seconds measure sampled replica occupancy,
> not CPU consumption or billing. No original data or table has been recomputed.

For subsequent evidence, start with the [Service routing validation](../docs/benchmarks/service-routing-validation.md),
[matched-window controlled pilot](../docs/benchmarks/capacity-and-controlled-pilot-20260906.md),
and [same-controller decision-mode comparison](../docs/benchmarks/decision-mode-ablation-20260907.md).
Those are separate batches; they neither reconstruct historical traffic nor
establish a general prediction advantage.

## 1. Executive Summary

This batch observed later first scale-up, higher peak replicas, and shorter recorded post-load tails for PHPA. These are descriptive observations under the historical configurations and timing conventions above. They do not establish a resource-efficiency benefit or the cause of the differences.

| Pattern | Metric | PHPA | native HPA | Δ (PHPA − native) |
|---|---|---|---|---|
| ramp | First scale-up delay | 105 ± 0 s | 90 ± 15 s | +15.0 (+17%) |
| ramp | Peak replicas | 8.0 ± 1.7 | 5.0 ± 0.0 | +3.0 (+60%) |
| ramp | Waste window after k6 stop | 214 ± 17 s | 426 ± 6 s | -211.7 (-50%) |
| ramp | Failed rate | 76.11 ± 2.23% | 74.09 ± 1.65% | +2.0 (+3%) |
| spike | First scale-up delay | 85 ± 17 s | 80 ± 9 s | +5.0 (+6%) |
| spike | Peak replicas | 6.3 ± 0.6 | 5.0 ± 0.0 | +1.3 (+27%) |
| spike | Waste window after k6 stop | 164 ± 0 s | 393 ± 8 s | -229.3 (-58%) |
| spike | Failed rate | 55.95 ± 1.67% | 59.64 ± 3.27% | -3.7 (-6%) |
| step | First scale-up delay | 95 ± 17 s | 80 ± 9 s | +15.0 (+19%) |
| step | Peak replicas | 10.0 ± 0.0 | 5.3 ± 0.6 | +4.7 (+88%) |
| step | Waste window after k6 stop | 164 ± 0 s | 372 ± 1 s | -207.7 (-56%) |
| step | Failed rate | 91.52 ± 2.18% | 90.98 ± 2.49% | +0.5 (+1%) |

## 2. Experiment Setup

- **Total experiments analyzed**: 18
- **Patterns tested**: ramp, spike, step
- **Controllers compared**: phpa, native_hpa (1:1 design)
- **Max repeat index seen**: r3
- **Git commits used**: 409227d
- **Experiment duration range**: 612s — 675s
- **Cluster**: kind-hpa-dev (single node, Ubuntu 24.04 VM)
- **Target workload**: php-apache (CPU-bound, requests=200m / limits=500m)
- **Load tool**: k6 v1.3.0, target RPS=25 (historical setting; delivered load was reduced by dropped iterations)

The historical orchestrator intended to reset the Deployment to one replica, switch controllers, accumulate metrics, run the selected load pattern, observe the tail, and collect Prometheus samples, events and controller logs. Its metadata start precedes setup; the old extractor inferred load start and stop from that timestamp plus fixed offsets instead of recording the actual k6 process boundaries. Setup time and pattern duration therefore matter when interpreting relative timing. A successful collection status does not establish successful service or a valid capacity comparison.

## 3. Per-Pattern Comparison

### 3.1 `ramp` pattern

*PHPA runs: 3 | native HPA runs: 3*

| Metric | PHPA | native HPA |
|---|---|---|
| Total requests | 3957 ± 34 | 4005 ± 35 |
| Failed rate | 76.11 ± 2.23% | 74.09 ± 1.65% |
| Dropped iterations | 543 ± 33 | 494 ± 35 |
| p95 latency (all) | 10003 ± 1 ms | 10002 ± 1 ms |
| p95 latency (success only) | 8897 ± 171 ms | 8517 ± 185 ms |
| First scale-up delay | 105 ± 0 s | 90 ± 15 s |
| Convergence time (to peak) | 135 ± 0 s | 115 ± 9 s |
| Peak / steady-state replicas | 8.0 ± 1.7 | 5.0 ± 0.0 |
| First scale-down (after k6 stop) | -66 ± 139 s | 419 ± 0 s |
| Full scale-down (after k6 stop) | 214 ± 17 s | 419 s (n=1) |
| Pod-seconds (experiment total) | 2158 ± 217 | 2785 ± 37 |
| Avg replicas | 3.25 ± 0.35 | 4.22 ± 0.06 |
| Waste window (replicas > 1 after k6 stop) | 214 ± 17 s | 426 ± 6 s |

**PHPA-specific decision counters:**

| Metric | Value |
|---|---|
| Total reconciles | 38 ± 1 |
| Scaled=true count | 12 ± 2 |
| Stabilized=true count | 8 ± 2 |
| Skip reason: `(none)` | 11.7 ± 2.1 |
| Skip reason: `DesiredEqualsCurrent` | 26.7 ± 3.2 |

**Interpretation:**

The historical extraction reports first scale-up at 105s for PHPA versus 90s for native HPA, and mean peak replicas of 8.0 versus 5.0. Recorded tail occupancy is 214s versus 426s, but a complete native return to one replica was observed in only one run. Failed-rate means are 76.1% and 74.1%, with all-request p95 near 10s. The batch does not isolate how smoothing, collection delay, routing or control settings contributed to these observations.

### 3.2 `spike` pattern

*PHPA runs: 3 | native HPA runs: 3*

| Metric | PHPA | native HPA |
|---|---|---|
| Total requests | 2050 ± 20 | 2039 ± 23 |
| Failed rate | 55.95 ± 1.67% | 59.64 ± 3.27% |
| Dropped iterations | 199 ± 20 | 210 ± 23 |
| p95 latency (all) | 10002 ± 1 ms | 10002 ± 1 ms |
| p95 latency (success only) | 9080 ± 75 ms | 9138 ± 126 ms |
| First scale-up delay | 85 ± 17 s | 80 ± 9 s |
| Convergence time (to peak) | 175 ± 96 s | 95 ± 9 s |
| Peak / steady-state replicas | 6.3 ± 0.6 | 5.0 ± 0.0 |
| First scale-down (after k6 stop) | -76 ± 137 s | 374 ± 0 s |
| Full scale-down (after k6 stop) | 164 ± 0 s | 389 ± 0 s |
| Pod-seconds (experiment total) | 1472 ± 54 | 2648 ± 27 |
| Avg replicas | 2.34 ± 0.08 | 4.20 ± 0.05 |
| Waste window (replicas > 1 after k6 stop) | 164 ± 0 s | 393 ± 8 s |

**PHPA-specific decision counters:**

| Metric | Value |
|---|---|
| Total reconciles | 37 ± 1 |
| Scaled=true count | 13 ± 2 |
| Stabilized=true count | 9 ± 2 |
| Skip reason: `(none)` | 12.7 ± 2.3 |
| Skip reason: `DesiredEqualsCurrent` | 24.0 ± 2.0 |

**Interpretation:**

The historical extraction reports first scale-up at 85s for PHPA versus 80s for native HPA, mean peak replicas of 6.3 versus 5.0, and recorded tail occupancy of 164s versus 393s. Failed-rate means are 56.0% and 59.6%, with all-request p95 near 10s. Three repeats with unverified request distribution do not establish a service advantage, noninferiority or a single-Pod startup bottleneck.

### 3.3 `step` pattern

*PHPA runs: 3 | native HPA runs: 3*

| Metric | PHPA | native HPA |
|---|---|---|
| Total requests | 3702 ± 55 | 3713 ± 60 |
| Failed rate | 91.52 ± 2.18% | 90.98 ± 2.49% |
| Dropped iterations | 798 ± 55 | 786 ± 59 |
| p95 latency (all) | 10005 ± 3 ms | 10002 ± 1 ms |
| p95 latency (success only) | 8814 ± 810 ms | 8697 ± 821 ms |
| First scale-up delay | 95 ± 17 s | 80 ± 9 s |
| Convergence time (to peak) | 135 ± 0 s | 100 ± 9 s |
| Peak / steady-state replicas | 10.0 ± 0.0 | 5.3 ± 0.6 |
| First scale-down (after k6 stop) | -156 ± 121 s | n/a |
| Full scale-down (after k6 stop) | 164 ± 0 s | n/a |
| Pod-seconds (experiment total) | 2240 ± 191 | 2712 ± 269 |
| Avg replicas | 3.73 ± 0.32 | 4.52 ± 0.45 |
| Waste window (replicas > 1 after k6 stop) | 164 ± 0 s | 372 ± 1 s |

**PHPA-specific decision counters:**

| Metric | Value |
|---|---|
| Total reconciles | 36 ± 2 |
| Scaled=true count | 12 ± 3 |
| Stabilized=true count | 8 ± 2 |
| Skip reason: `(none)` | 12.0 ± 3.5 |
| Skip reason: `DesiredEqualsCurrent` | 24.3 ± 2.5 |

**Interpretation:**

The historical extraction reports first scale-up at 95s for PHPA versus 80s for native HPA, and mean peak replicas of 10.0 versus 5.3. PHPA's recorded tail occupancy is 164s; native HPA's 372s is limited by observation ending before full scale-down in every run. Failed-rate means exceed 90% for both controllers and all-request p95 is near 10s. Neither the reported tail difference nor lower total occupancy establishes useful service efficiency.

## 4. Cross-Pattern Findings

Across these three patterns, the archived group means show consistent directions for first scale-up and peak replicas. This is a description of this batch, not evidence that the behavior is structural or independent of workload and environment.

- **The configurations did not isolate prediction.** PHPA and native HPA differed in downscale windows, metric paths and control behavior. The shorter recorded PHPA tails cannot be credited to EWMA alone.
- **The damping and prediction cap were engineering constraints, not a measured causal explanation.** This batch has no matched uncapped control. Higher PHPA peak replicas remain visible in all three tables; a forecast cap does not establish a bound on fleet-wide overshoot relative to native HPA.
- **Lower recorded total Pod-seconds do not establish efficiency.** The tables report less PHPA occupancy in each pattern, but use unequal full-experiment windows, inferred load boundaries and some incomplete tails. Service outcomes were poor, and sampled replica occupancy is neither consumed CPU nor cloud cost.
- **Similar failed-rate means do not establish equivalent service.** Every group had dropped iterations and an all-request p95 around 10s. No equivalence or noninferiority analysis was performed; the root causes of failures and historical request distribution remain unresolved.
- **Load-shape differences are observations rather than a validated causal model.** Step had higher failure rates than ramp or spike in this batch. The data do not isolate load shape, startup capacity or controller choice as the explanation.

## 5. Known Limitations

### Sampling & instrumentation

- **Replica timeline precision is 15s** (prom step size). Per-second scale events are visible only via events.yaml, which is unreliable for PHPA (no SuccessfulRescale emitted) and contaminated across experiments by 1h K8s event TTL — see commit e862d1b for rationale.
- **Sample size n=3 per (pattern, controller)** supports only the descriptive summary presented here. No hypothesis, equivalence or noninferiority tests were performed; no statistical-significance claim is made.
- **Historical time references and incomplete tails** limit comparisons. Negative values in the original `First scale-down (after k6 stop)` rows mean that the extractor's first recorded downscale preceded its inferred stop reference; they are not a measured negative post-load response time. `n/a` and extraction warnings must not be read as completed scale-down.
- **Single-node kind cluster** does not reflect production scheduling latency, node-to-node network jitter, or PV provisioning delays.

### Warnings emitted during extraction

- `no scale-down events detected; experiment ended at steady_state=5 replicas`
  - Affected: 20260708_210035_step_native_hpa_r1, 20260708_212105_step_native_hpa_r2
- `no scale-down events detected; experiment ended at steady_state=6 replicas`
  - Affected: 20260708_214134_step_native_hpa_r3

### PHPA implementation scope (v1alpha1)

- In-memory stabilization-window history (single replica controller only; restart loses history).
- CPU metric only; no memory or custom metrics.
- Deployment scaleTargetRef only; no StatefulSet / ReplicaSet support.
- minReplicas=0 (scale-to-zero) accepted by CRD validation but coerced to 1 at runtime.
