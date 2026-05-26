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

// Target RPS for all three patterns. PROVISIONAL — Phase 3.2 calibration
// will determine the actual value that drives PHPA to maxReplicas=10
// with target=50% CPU utilization.
//
// Calibration approach:
//   1. Probe with constant low RPS (10/30/100 @ 60s each, no scaling)
//   2. Derive linear RPS-to-CPU coefficient from steady-state CPU
//   3. Back-calculate from target: 10 pods * 200m * 50% = 1000m
export const TARGET_RPS = 150;

// Quiet period at the start of each test. Required for:
//   1. metrics-server scrape interval (default 15s) refreshing baseline
//   2. Prometheus to accumulate baseline datapoints before load begins
export const PRE_LOAD_QUIET_SECONDS = 30;

// Tail observation period after load drops to zero. Sized to capture:
//   - Native HPA's full scale-down (~5-6min observed in Phase 0)
//   - PHPA's scale-down (~150s observed in Phase 2 commit 51bf289)
// 240s gives the native HPA case ~30s margin past its typical 5min mark.
export const POST_LOAD_TAIL_SECONDS = 240;

// Standard HTTP request function shared by all load patterns.
// Attaches the load-phase tag so k6 output can be sliced by phase
// post-hoc (e.g. p95 latency during ramp-up vs hold).
export function get(phase) {
  return http.get(BASE_URL + '/', {
    timeout: REQUEST_TIMEOUT,
    tags: { phase: phase },
  });
}
