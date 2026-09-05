// hack/k6/lib/common.js
// Shared configuration for Phase 3 benchmark load patterns.
// All numeric values that may need calibration are centralized here so
// that Phase 3.2 calibration only needs to touch this file.

import http from 'k6/http';

// The runner executes inside the dedicated Kind cluster and sends each request
// through the Service ClusterIP. Its fixed environment disables connection
// reuse so existing keep-alive connections do not pin traffic to earlier Pods.
export const BASE_URL = 'http://php-apache.default.svc:80';

// Per-request timeout. php-apache responses are ~1-5ms when not
// overloaded; the 10s ceiling exists only to prevent indefinite hangs
// from skewing latency percentiles.
export const REQUEST_TIMEOUT = '10s';

// Historical offered load, retained only to preserve the workload definition.
// Earlier capacity estimates used a port-forward that selected one Pod; the
// intended 10-Pod capacity and latency headroom have not been validated.
// Recalibrate through the Service before treating new matrix data as evidence.
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
