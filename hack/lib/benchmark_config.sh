#!/usr/bin/env bash
# Shared, offline configuration and experiment identity for the controlled pilot.
# Source after k6_runner.sh, then call benchmark_config_init and
# benchmark_config_fingerprint before contacting the cluster.

BENCHMARK_CONFIG_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

benchmark_config_init() {
  LATENCY_DIAGNOSTIC="${LATENCY_DIAGNOSTIC-false}"
  case "$LATENCY_DIAGNOSTIC" in
    true|false) ;;
    *) echo "ERROR: LATENCY_DIAGNOSTIC must be true or false" >&2; return 1 ;;
  esac
  RPS="${RPS-25}"
  if [ "$LATENCY_DIAGNOSTIC" = true ]; then
    BENCHMARK_PATTERNS="${BENCHMARK_PATTERNS-step}"
    BENCHMARK_CONTROLLERS="${BENCHMARK_CONTROLLERS-phpa_current}"
    if [ "$RPS" != 25 ] || [ "$BENCHMARK_PATTERNS" != step ] || \
        [ "$BENCHMARK_CONTROLLERS" != phpa_current ]; then
      echo "ERROR: latency diagnostics require RPS=25, step, and phpa_current" >&2
      return 1
    fi
    case "${LATENCY_OFFSET_SECONDS-}" in
      0|10|20) ;;
      *) echo "ERROR: LATENCY_OFFSET_SECONDS must be 0, 10, or 20" >&2; return 1 ;;
    esac
    LATENCY_GATE_TIMEOUT_SECONDS="${LATENCY_GATE_TIMEOUT_SECONDS-180}"
    if [ "$LATENCY_GATE_TIMEOUT_SECONDS" != 180 ]; then
      echo "ERROR: latency gate timeout is fixed at 180 seconds" >&2
      return 1
    fi
    export LATENCY_OFFSET_SECONDS LATENCY_GATE_TIMEOUT_SECONDS
  else
    BENCHMARK_PATTERNS="${BENCHMARK_PATTERNS-step ramp spike}"
    BENCHMARK_CONTROLLERS="${BENCHMARK_CONTROLLERS-native_hpa_300 native_hpa_60 phpa}"
  fi
  BENCHMARK_REPEATS="${BENCHMARK_REPEATS-3}"
  BENCHMARK_PROTOCOL_VERSION="controlled-pilot-v1"

  if ! [[ "$RPS" =~ ^[1-9][0-9]{0,3}$ ]] || (( RPS > 1000 )); then
    echo "ERROR: RPS must be an integer from 1 to 1000" >&2
    return 1
  fi
  if ! [[ "$BENCHMARK_REPEATS" =~ ^[1-9][0-9]{0,3}$ ]] || (( BENCHMARK_REPEATS > 1000 )); then
    echo "ERROR: BENCHMARK_REPEATS must be an integer from 1 to 1000" >&2
    return 1
  fi
  local value member seen
  for value in "$BENCHMARK_PATTERNS" "$BENCHMARK_CONTROLLERS"; do
    if [[ "$value" = *$'\n'* || "$value" = *$'\r'* ]]; then
      echo "ERROR: benchmark selector lists must be on one line" >&2
      return 1
    fi
  done
  read -r -a BENCHMARK_PATTERN_VALUES <<< "$BENCHMARK_PATTERNS"
  read -r -a BENCHMARK_CONTROLLER_VALUES <<< "$BENCHMARK_CONTROLLERS"
  if (( ${#BENCHMARK_PATTERN_VALUES[@]} == 0 || ${#BENCHMARK_CONTROLLER_VALUES[@]} == 0 )); then
    echo "ERROR: benchmark selector lists must not be empty" >&2
    return 1
  fi
  seen=" "
  for member in "${BENCHMARK_PATTERN_VALUES[@]}"; do
    case "$member" in
      step|ramp|spike) ;;
      *) echo "ERROR: invalid benchmark pattern '$member'" >&2; return 1 ;;
    esac
    if [[ "$seen" = *" $member "* ]]; then
      echo "ERROR: duplicate benchmark pattern '$member'" >&2
      return 1
    fi
    seen+="$member "
  done
  seen=" "
  for member in "${BENCHMARK_CONTROLLER_VALUES[@]}"; do
    case "$member" in
      native_hpa_300|native_hpa_60|phpa|phpa_current|phpa_hybrid) ;;
      *) echo "ERROR: invalid benchmark controller '$member'" >&2; return 1 ;;
    esac
    if [[ "$seen" = *" $member "* ]]; then
      echo "ERROR: duplicate benchmark controller '$member'" >&2
      return 1
    fi
    seen+="$member "
  done

  # Size concurrency for the 10-second HTTP timeout, with allocation headroom.
  # These formulas also apply in k6/lib/common.js and the calibration workload.
  BENCHMARK_PRE_ALLOCATED_VUS=$(( RPS * 10 > 20 ? RPS * 10 : 20 ))
  BENCHMARK_MAX_VUS=$(( RPS * 12 > 40 ? RPS * 12 : 40 ))
  export RPS BENCHMARK_PATTERNS BENCHMARK_CONTROLLERS BENCHMARK_REPEATS LATENCY_DIAGNOSTIC
}

# The selector is a treatment identity, excluded from the shared configuration
# hash so same-controller decision policies can be compared within one campaign.
benchmark_decision_mode() {
  case "$1" in
    native_hpa_300|native_hpa_60) printf '%s\n' none ;;
    phpa) printf '%s\n' Predictive ;;
    phpa_current) printf '%s\n' Current ;;
    phpa_hybrid) printf '%s\n' Hybrid ;;
    *) echo "ERROR: unknown controller '$1'" >&2; return 1 ;;
  esac
}

benchmark_config_fingerprint() {
  command -v sha256sum >/dev/null || {
    echo "ERROR: sha256sum is required to identify benchmark sources" >&2
    return 1
  }
  # Hash the effective working files, including local changes. Reports, tests,
  # timestamps and selector order/count are not inputs to an individual run.
  # Relative paths make this identity independent of the source checkout path.
  BENCHMARK_SOURCE_SHA256=$(
    set -o pipefail
    cd "$BENCHMARK_CONFIG_REPO_ROOT" || exit 1
    {
      printf '%s\n' go.mod go.sum hack/run_benchmark.sh hack/run_matrix.sh \
        hack/prerequisites_check.sh hack/lib/k6_runner.sh hack/lib/benchmark_config.sh \
        hack/analyze/extract.py hack/analyze/aggregate.py
      if [ "$LATENCY_DIAGNOSTIC" = true ]; then
        printf '%s\n' hack/run_latency_diagnostic.py hack/analyze/latency.py
      fi
      find api cmd internal -type f -name '*.go' ! -name '*_test.go'
      find config/benchmark config/samples -type f -name '*.yaml'
      find hack/k6 -type f -name '*.js'
    } | LC_ALL=C sort | while IFS= read -r benchmark_source; do
      sha256sum -- "$benchmark_source" || exit 1
    done | sha256sum | cut -d ' ' -f 1
  ) || return 1
  BENCHMARK_CONFIG_SHA256=$(
    {
      printf '%s\n' \
      "protocol=$BENCHMARK_PROTOCOL_VERSION" "source=$BENCHMARK_SOURCE_SHA256" \
      "rps=$RPS" "pre_allocated_vus=$BENCHMARK_PRE_ALLOCATED_VUS" "max_vus=$BENCHMARK_MAX_VUS" \
      'quiet_seconds=30' 'metric_accumulation_seconds=30' 'post_load_tail_seconds=360' \
      'pattern_durations_seconds=step:211,ramp:270,spike:241' 'request_timeout_seconds=10' \
      "benchmark_context=${BENCHMARK_CONTEXT:-unset-offline-plan}" \
      "endpoint=${K6_BASE_URL:?source k6_runner.sh first}" "k6_image=$K6_IMAGE" \
      "traffic_path=$K6_TRAFFIC_PATH" "load_generator=$K6_EXECUTION_MODE" 'connection_reuse=false'
      if [ "$LATENCY_DIAGNOSTIC" = true ]; then
        printf '%s\n' 'latency_diagnostic=latency-diagnostic-v1' \
          "latency_offset_seconds=$LATENCY_OFFSET_SECONDS" \
          "latency_gate_timeout_seconds=$LATENCY_GATE_TIMEOUT_SECONDS" \
          'latency_observer_interval_seconds=2' 'latency_phase_tolerance_seconds=2' \
          'latency_gate_clock_precision_seconds=1' 'latency_launch_rounding=ceil'
      fi
    } | sha256sum | cut -d ' ' -f 1
  ) || return 1
}
