// hack/k6/lib/common.js
// Shared, validated configuration for controlled benchmark load patterns.

import http from 'k6/http';

// The runner executes inside the dedicated Kind cluster and sends each request
// through the Service ClusterIP. Its fixed environment disables connection
// reuse so existing keep-alive connections do not pin traffic to earlier Pods.
export const BASE_URL = 'http://php-apache.default.svc:80';

// Bound request drain when the application is overloaded. Timeouts remain
// failures in the all-request latency and success-rate measurements.
export const REQUEST_TIMEOUT = '10s';

// The orchestrator records and explicitly passes RPS to the in-cluster runner.
// A missing value preserves the historical default; an explicit empty or invalid
// value fails before issuing any requests.
const configuredRPS = __ENV.RPS === undefined ? '25' : __ENV.RPS;
if (!/^[1-9][0-9]{0,3}$/.test(configuredRPS) || Number(configuredRPS) > 1000) {
  throw new Error('RPS must be an integer from 1 to 1000');
}
export const TARGET_RPS = Number(configuredRPS);

// Sustain the offered arrival rate even while requests approach their timeout.
// Report dropped iterations and generator resources to check this assumption.
export const PRE_ALLOCATED_VUS = Math.max(20, TARGET_RPS * 10);
export const MAX_VUS = Math.max(40, TARGET_RPS * 12);

// Identify this experiment in per-Pod access logs, including Pods removed
// during scale-down. Keep manual invocations usable without an orchestrator.
const requestHeaders = {};
if (__ENV.PROBE_TOKEN !== undefined) {
  if (!/^[A-Za-z0-9_-]+$/.test(__ENV.PROBE_TOKEN)) {
    throw new Error('PROBE_TOKEN must contain only letters, digits, underscores or hyphens');
  }
  requestHeaders['User-Agent'] = `phpa-benchmark/${__ENV.PROBE_TOKEN}`;
}

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
    headers: requestHeaders,
    tags: { phase: phase },
  });
}
