// hack/k6/lib/common.js
// Shared configuration for Phase 3 benchmark load patterns.
// All numeric values that may need calibration are centralized here so
// that Phase 3.2 calibration only needs to touch this file.

import http from 'k6/http';

// php-apache Service is reached via:
//   kubectl port-forward svc/php-apache 8080:80
// See docs/PHASE3_BENCHMARK_DESIGN.md for the rationale of choosing
// port-forward over an in-cluster k6 Job.
export const BASE_URL = 'http://localhost:8080';

// Per-request timeout. php-apache responses are ~1-5ms when not
// overloaded; the 10s ceiling exists only to prevent indefinite hangs
// from skewing latency percentiles.
export const REQUEST_TIMEOUT = '10s';

// Target RPS for all three patterns. CALIBRATED in Phase 3.2 against
// the actual application behavior of registry.k8s.io/hpa-example.
//
// Calibration findings (experiments/calibration/calibration_20260526_*.md):
//   - RPS=5 (1 RPS/Pod across 5 Pods): CPU=14.5%, latency p95=38ms (clean)
//   - RPS=15 (3 RPS/Pod): saturated, dropped=101, p95 hit 10s timeout
//   - The hpa-example image is intentionally CPU-bound (1M sqrt iterations
//     per request) + Apache prefork MPM limits concurrent workers,
//     yielding ~2-3 RPS/Pod realistic capacity (not the theoretical
//     ~10 RPS/Pod estimated pre-calibration).
//
// TARGET_RPS=25 is chosen as a workload that:
//   1. Triggers scaling from 1 replica (25 RPS on 1 Pod is clearly overload)
//   2. Stabilizes within maxReplicas=10 capacity (25 RPS across 10 Pods =
//      2.5 RPS/Pod, in the linear non-saturated regime)
//   3. Leaves SLA-grade latency headroom post-scale-up for clean PHPA vs
//      native HPA comparison (vs ~34 RPS which would push the system to
//      the saturation knee)
export const TARGET_RPS = 25;

// Quiet period at the start of each test. Required for:
//   1. metrics-server scrape interval (default 15s) refreshing baseline
//   2. Prometheus to accumulate baseline datapoints before load begins
export const PRE_LOAD_QUIET_SECONDS = 30;

// NOTE: Tail observation period (formerly POST_LOAD_TAIL_SECONDS) is no
// longer expressed inside k6 stages — see hack/run_benchmark.sh's
// POST_LOAD_TAIL_SECONDS constant. The k6 ramping-arrival-rate executor
// exits a target=0 stage early once all in-flight iterations finish (a
// CPU-saving optimization in k6), which truncates any tail stage that
// follows a saturated hold. We moved the observation period to the bash
// orchestrator to ensure scale-down data is always captured.

// Standard HTTP request function shared by all load patterns.
// Attaches the load-phase tag so k6 output can be sliced by phase
// post-hoc (e.g. p95 latency during ramp-up vs hold).
export function get(phase) {
  return http.get(BASE_URL + '/', {
    timeout: REQUEST_TIMEOUT,
    tags: { phase: phase },
  });
}
