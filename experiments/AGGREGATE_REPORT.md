# Phase 3 Benchmark Report

> Generated 2026-05-26 23:23 UTC from 18 experiment(s) across 3 pattern(s) × 2 controller(s).

## 1. Executive Summary

PHPA's design intent: trade slower first-scale-up response for faster scale-down and lower resource waste. The numbers below quantify both sides of that trade.

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
- **Load tool**: k6 v1.3.0, target RPS=25 (calibrated; see Phase 3.2)

Each experiment follows the same 11-step orchestrator (`hack/run_benchmark.sh`): reset Deployment to 1 replica → switch controller → 30s metric accumulation → k6 load (211s) → 360s tail observation → collect prom + events + controller log → smoke check → mark success.

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

PHPA's first scale-up is ~25s slower than native HPA (105s vs 80s). This reflects the EWMA + 1m Prometheus rate path's smoothing tax. PHPA over-provisions: peak replicas 9.7 vs native 5.0. EWMA's forward extrapolation overshoots when the rate-of-change is high. PHPA finishes scale-down faster: waste window 224s vs native HPA's 429s, the core selling point. Business impact: PHPA's failed-rate is 76.4% vs native HPA's 77.9% (lower is better). Difference is small because the bottleneck is the single-Pod start-up window, not the controller.

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

PHPA's first scale-up is ~20s slower than native HPA (95s vs 75s). This reflects the EWMA + 1m Prometheus rate path's smoothing tax. PHPA over-provisions: peak replicas 9.0 vs native 5.0. EWMA's forward extrapolation overshoots when the rate-of-change is high. PHPA finishes scale-down faster: waste window 154s vs native HPA's 389s, the core selling point.

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

PHPA's first scale-up is ~10s slower than native HPA (85s vs 75s). This reflects the EWMA + 1m Prometheus rate path's smoothing tax. PHPA over-provisions: peak replicas 10.0 vs native 5.0. EWMA's forward extrapolation overshoots when the rate-of-change is high. PHPA finishes scale-down faster: waste window 194s vs native HPA's 331s, the core selling point.

## 4. Cross-Pattern Findings

When the matrix includes multiple patterns, this section calls out what holds independent of load shape — e.g., whether PHPA's scale-down advantage replicates under ramp and spike, or only under step. With 3 patterns now covered (ramp, spike, step), the following trends emerge:

- *(populated by inspecting per-pattern tables above)*

## 5. Known Limitations

### Sampling & instrumentation

- **Replica timeline precision is 15s** (prom step size). Per-second scale events are visible only via events.yaml, which is unreliable for PHPA (no SuccessfulRescale emitted) and contaminated across experiments by 1h K8s event TTL — see commit e862d1b for rationale.
- **Sample size n=3 per (pattern, controller)** is below the threshold for formal statistical inference. Reported mean ± stdev is engineering summary only — no t-tests, no p-values.
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
