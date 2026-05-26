# Phase 3 Benchmark Report

> Generated 2026-05-26 18:26 UTC from 4 experiment(s) across 1 pattern(s) × 2 controller(s).

## 1. Executive Summary

PHPA's design intent: trade slower first-scale-up response for faster scale-down and lower resource waste. The numbers below quantify both sides of that trade.

| Pattern | Metric | PHPA | native HPA | Δ (PHPA − native) |
|---|---|---|---|---|
| step | First scale-up delay | 82 ± 11 s | 75 ± 0 s | +7.5 (+10%) |
| step | Peak replicas | 10.0 ± 0.0 | 5.0 ± 0.0 | +5.0 (+100%) |
| step | Waste window after k6 stop | 186 ± 11 s | 312 ± 84 s | -125.0 (-40%) |
| step | Failed rate | 90.97 ± 1.76% | 94.53 ± 0.74% | -3.6 (-4%) |

## 2. Experiment Setup

- **Total experiments analyzed**: 4
- **Patterns tested**: step
- **Controllers compared**: phpa, native_hpa (1:1 design)
- **Max repeat index seen**: r2
- **Git commits used**: 508ca66, 6b286c7, 8358b8f, 928c44b
- **Experiment duration range**: 493s — 613s
- **Cluster**: kind-hpa-dev (single node, Ubuntu 24.04 VM)
- **Target workload**: php-apache (CPU-bound, requests=200m / limits=500m)
- **Load tool**: k6 v1.3.0, target RPS=25 (calibrated; see Phase 3.2)

Each experiment follows the same 11-step orchestrator (`hack/run_benchmark.sh`): reset Deployment to 1 replica → switch controller → 30s metric accumulation → k6 load (211s) → 360s tail observation → collect prom + events + controller log → smoke check → mark success.

## 3. Per-Pattern Comparison

### 3.1 `step` pattern

*PHPA runs: 2 | native HPA runs: 2*

| Metric | PHPA | native HPA |
|---|---|---|
| Total requests | 3720 ± 42 | 3642 ± 1 |
| Failed rate | 90.97 ± 1.76% | 94.53 ± 0.74% |
| Dropped iterations | 780 ± 42 | 858 ± 0 |
| p95 latency (all) | 10013 ± 16 ms | 10003 ± 2 ms |
| p95 latency (success only) | 8050 ± 288 ms | 8986 ± 270 ms |
| First scale-up delay | 82 ± 11 s | 75 ± 0 s |
| Convergence time (to peak) | 112 ± 11 s | 90 ± 0 s |
| Peak / steady-state replicas | 10.0 ± 0.0 | 5.0 ± 0.0 |
| First scale-down (after k6 stop) | -8 ± 11 s | n/a |
| Full scale-down (after k6 stop) | 186 ± 11 s | n/a |
| Pod-seconds (experiment total) | 2168 ± 159 | 2295 ± 424 |
| Avg replicas | 3.98 ± 0.25 | 4.25 ± 0.12 |
| Waste window (replicas > 1 after k6 stop) | 186 ± 11 s | 312 ± 84 s |

**PHPA-specific decision counters:**

| Metric | Value |
|---|---|
| Total reconciles | 35 ± 4 |
| Scaled=true count | 12 ± 1 |
| Stabilized=true count | 8 ± 0 |
| Skip reason: `(none)` | 12.5 ± 0.7 |
| Skip reason: `DesiredEqualsCurrent` | 20.5 ± 4.9 |
| Skip reason: `WithinToleranceBand` | 2.0 ± 0.0 |

**Interpretation:**

PHPA's first scale-up is ~8s slower than native HPA (82s vs 75s). This reflects the EWMA + 1m Prometheus rate path's smoothing tax. PHPA over-provisions: peak replicas 10.0 vs native 5.0. EWMA's forward extrapolation overshoots when the rate-of-change is high. PHPA finishes scale-down faster: waste window 186s vs native HPA's 312s, the core selling point. Business impact: PHPA's failed-rate is 91.0% vs native HPA's 94.5% (lower is better). Difference is small because the bottleneck is the single-Pod start-up window, not the controller.

## 4. Cross-Pattern Findings

*Only 1 pattern(s) available; cross-pattern findings deferred until the full matrix is complete.*

## 5. Known Limitations

### Sampling & instrumentation

- **Replica timeline precision is 15s** (prom step size). Per-second scale events are visible only via events.yaml, which is unreliable for PHPA (no SuccessfulRescale emitted) and contaminated across experiments by 1h K8s event TTL — see commit e862d1b for rationale.
- **Sample size n=3 per (pattern, controller)** is below the threshold for formal statistical inference. Reported mean ± stdev is engineering summary only — no t-tests, no p-values.
- **Single-node kind cluster** does not reflect production scheduling latency, node-to-node network jitter, or PV provisioning delays.

### Warnings emitted during extraction

- `no scale-down events detected; experiment ended at steady_state=5 replicas`
  - Affected: 20260527_002241_step_native_hpa_r1, 20260527_011955_step_native_hpa_r2

### Experiments excluded from aggregation

- skipped _INCOMPLETE_20260526_231116_step_phpa_r1: marked incomplete

### PHPA implementation scope (v1alpha1)

- In-memory stabilization-window history (single replica controller only; restart loses history).
- CPU metric only; no memory or custom metrics.
- Deployment scaleTargetRef only; no StatefulSet / ReplicaSet support.
- minReplicas=0 (scale-to-zero) accepted by CRD validation but coerced to 1 at runtime.
