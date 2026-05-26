#!/usr/bin/env bash
# hack/run_benchmark.sh
#
# Single-experiment orchestrator for Phase 3 PHPA vs native HPA benchmark.
#
# USAGE: hack/run_benchmark.sh <pattern> <controller> <repeat_idx>
#   pattern    : step | ramp | spike
#   controller : phpa | native_hpa
#   repeat_idx : 1-N (positive integer)
#
# Exit codes:
#   0: success
#   1: prerequisite or argument failure
#   2: experiment runtime failure (k6 / controller crash)
#   3: data collection failure
#
# Side effects:
#   - Creates experiments/<timestamp>_<pattern>_<controller>_r<idx>/
#   - Starts/stops controller process (writes /tmp/controller-current.log)
#   - Re-deploys PHPA sample or native HPA YAML depending on controller
#   - Resets php-apache Deployment to 1 replica before each run

set -euo pipefail

# Locate repo root regardless of invocation path
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# === Argument validation ===
if [ $# -ne 3 ]; then
  echo "USAGE: $0 <pattern> <controller> <repeat_idx>" >&2
  exit 1
fi
PATTERN="$1"
CONTROLLER="$2"
REPEAT_IDX="$3"

case "$PATTERN" in
  step|ramp|spike) ;;
  *) echo "ERROR: pattern must be step|ramp|spike, got '$PATTERN'" >&2; exit 1 ;;
esac
case "$CONTROLLER" in
  phpa|native_hpa) ;;
  *) echo "ERROR: controller must be phpa|native_hpa, got '$CONTROLLER'" >&2; exit 1 ;;
esac
if ! [[ "$REPEAT_IDX" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: repeat_idx must be positive integer, got '$REPEAT_IDX'" >&2
  exit 1
fi

# === Configuration ===
PHPA_SAMPLE="config/samples/autoscaling_v1alpha1_predictivehpa.yaml"
NATIVE_HPA_YAML="$HOME/hpa-project/baseline-demo/hpa.yaml"
CONTROLLER_LOG="/tmp/controller-current.log"
CONTROLLER_STARTUP_TIMEOUT=60
METRIC_ACCUMULATION_SECONDS=30
# Tail observation after k6 exits. Captures scale-down behavior.
# Moved out of k6 stages because ramping-arrival-rate executor exits early
# when target=0 and all in-flight requests are done — see step.js comment.
# Sized to 360s = 300s (native HPA default --horizontal-pod-autoscaler-
# downscale-stabilization) + 60s buffer for the final reconcile + Pod
# termination. The earlier 240s value missed native HPA's full scale-down
# curve in step native_hpa 1 dry-run (only first scale decision captured).
POST_LOAD_TAIL_SECONDS=360
PROM_URL="http://localhost:9090"

# === Step 1: prerequisites ===
echo "[1/11] prerequisite check"
hack/prerequisites_check.sh

# === Step 2: create experiment directory ===
echo ""
echo "[2/11] create experiment directory"
TIMESTAMP_LOCAL=$(date +%Y%m%d_%H%M%S)
EXP_DIR="experiments/${TIMESTAMP_LOCAL}_${PATTERN}_${CONTROLLER}_r${REPEAT_IDX}"
mkdir -p "$EXP_DIR"
echo "  $EXP_DIR"

# === Step 3: write initial metadata.yaml ===
echo ""
echo "[3/11] write metadata.yaml"
START_TIME_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)
START_TIME_UNIX=$(date +%s)
GIT_COMMIT=$(git rev-parse --short HEAD)
GIT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
GIT_DIRTY=$([ -z "$(git status --porcelain)" ] && echo "false" || echo "true")
K6_VERSION_LINE=$(k6 version | head -1)

cat > "$EXP_DIR/metadata.yaml" <<META
experiment_id: ${TIMESTAMP_LOCAL}_${PATTERN}_${CONTROLLER}_r${REPEAT_IDX}
pattern: $PATTERN
controller: $CONTROLLER
repeat: $REPEAT_IDX
start_time_utc: "$START_TIME_UTC"
start_time_unix: $START_TIME_UNIX
end_time_utc: ""
end_time_unix: 0
git:
  commit: $GIT_COMMIT
  branch: $GIT_BRANCH
  dirty: $GIT_DIRTY
env:
  k6_version: "$K6_VERSION_LINE"
  cluster: kind-hpa-dev
  k8s_version: "1.35.0"
result:
  status: in_progress
  failure_reason: ""
META
echo "  start_time_utc: $START_TIME_UTC"

update_metadata() {
  local key="$1" value="$2"
  sed -i "s|^${key}:.*|${key}: ${value}|" "$EXP_DIR/metadata.yaml"
}

fail_experiment() {
  local reason="$1"
  local exit_code="${2:-2}"
  echo "ERROR: $reason" >&2
  sed -i "s|^  status: in_progress|  status: failed|" "$EXP_DIR/metadata.yaml"
  sed -i "s|^  failure_reason: \"\"|  failure_reason: \"$reason\"|" "$EXP_DIR/metadata.yaml"
  END_TIME_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  END_TIME_UNIX=$(date +%s)
  sed -i "s|^end_time_utc:.*|end_time_utc: \"$END_TIME_UTC\"|" "$EXP_DIR/metadata.yaml"
  sed -i "s|^end_time_unix:.*|end_time_unix: $END_TIME_UNIX|" "$EXP_DIR/metadata.yaml"
  exit "$exit_code"
}

# === Step 4: reset Deployment to 1 replica ===
echo ""
echo "[4/11] reset Deployment to 1 replica"
kubectl scale deploy php-apache --replicas=1 >/dev/null
if ! kubectl rollout status deploy/php-apache --timeout=60s >/dev/null; then
  fail_experiment "Deployment rollout did not stabilize within 60s"
fi
echo "  Deployment ready"

# === Step 5: switch controller ===
echo ""
echo "[5/11] switch controller ($CONTROLLER)"

# Kill any existing controller. We send SIGTERM to *every* pattern that
# could possibly be a controller process from a prior run, then wait for
# the metrics/health port (8081) to be released before starting a new one.
# Three signals are necessary because make run forks multiple processes:
#   - "make run" parent (bash wrapper from the Makefile target)
#   - "go run cmd/main.go" (the go toolchain's compile-and-exec wrapper)
#   - "go-build.../main" (the actual compiled binary, often parented to go run)
echo "  killing any existing controller processes..."
pkill -f "make run" 2>/dev/null || true
pkill -f "go run.*cmd/main.go" 2>/dev/null || true
# Two patterns for the compiled binary because Go's build cache lives in
# either /tmp/go-build* (one-shot go run) or ~/.cache/go-build/* (cached
# build); the binary itself is just named "main". We use a strict path
# pattern (/go-build/...) to avoid killing unrelated "main" processes.
pkill -f "/go-build/.*/main" 2>/dev/null || true
# Wait for port 8081 (controller health probe) to be free; that is the
# definitive signal that no controller is bound. Hardcoded 15s ceiling.
for i in $(seq 1 15); do
  if ! ss -ltn 2>/dev/null | grep -q ":8081 "; then
    break
  fi
  if [ $i -eq 15 ]; then
    # Last resort: SIGKILL anything still on :8081
    pkill -9 -f "/go-build/.*/main" 2>/dev/null || true
    pkill -9 -f "go run.*cmd/main.go" 2>/dev/null || true
    pkill -9 -f "make run" 2>/dev/null || true
    sleep 2
  fi
  sleep 1
done

if [ "$CONTROLLER" = "phpa" ]; then
  # Ensure no native HPA conflicts
  kubectl delete hpa php-apache --ignore-not-found=true >/dev/null
  # Ensure PHPA sample exists
  kubectl apply -f "$PHPA_SAMPLE" >/dev/null

  # Truncate controller log so grep below cannot match stale "Starting workers"
  : > "$CONTROLLER_LOG"

  # Start controller in background. We disown so the orchestrator can exit
  # without taking the controller with it (it is intentionally a long-lived
  # process that the orchestrator owns the lifecycle of).
  echo "  starting controller..."
  nohup make run > "$CONTROLLER_LOG" 2>&1 &
  disown
  # Wait for the controller-runtime "Starting workers" log line. The 60s
  # ceiling accommodates the Makefile preamble (controller-gen + fmt + vet
  # + go build), which can take 15-25s on a cold cache.
  STARTED=false
  for i in $(seq 1 $CONTROLLER_STARTUP_TIMEOUT); do
    if grep -q "Starting workers" "$CONTROLLER_LOG" 2>/dev/null; then
      STARTED=true
      break
    fi
    # Also fail fast if make run died (exit-status detectable in log)
    if grep -qE "make: \*\*\*|address already in use|exit status 1" "$CONTROLLER_LOG" 2>/dev/null; then
      fail_experiment "controller failed to start (check $CONTROLLER_LOG; common causes: port 8081 still bound, build error)"
    fi
    sleep 1
  done
  if [ "$STARTED" = "false" ]; then
    fail_experiment "controller did not reach 'Starting workers' within ${CONTROLLER_STARTUP_TIMEOUT}s (check $CONTROLLER_LOG)"
  fi
  echo "  controller started"

elif [ "$CONTROLLER" = "native_hpa" ]; then
  # Ensure PHPA controller is not running (already killed above)
  # PHPA sample stays but is inert without the controller
  kubectl apply -f "$NATIVE_HPA_YAML" >/dev/null
  echo "  native HPA applied"
fi

# === Step 6: metric accumulation pause ===
echo ""
echo "[6/11] metric accumulation pause (${METRIC_ACCUMULATION_SECONDS}s)"
sleep "$METRIC_ACCUMULATION_SECONDS"

# === Step 7: run k6 load ===
echo ""
echo "[7/11] run k6 pattern: $PATTERN"
K6_SCRIPT="hack/k6/${PATTERN}.js"
K6_JSON="$EXP_DIR/k6.json"
if ! k6 run --out json="$K6_JSON" --log-output=file="$EXP_DIR/k6-warnings.log" "$K6_SCRIPT"; then
  fail_experiment "k6 run failed (see $EXP_DIR/k6.json)"
fi

# Tail observation: k6's ramping-arrival-rate executor exits early when
# target=0 and all in-flight requests have completed, so we cannot rely
# on a trailing k6 stage to observe scale-down. The orchestrator pauses
# here to ensure both PHPA and native HPA scale-down sequences are
# captured in the Prometheus and controller log data collected next.
echo "  k6 done; tail observation pause (${POST_LOAD_TAIL_SECONDS}s) for scale-down"
sleep "$POST_LOAD_TAIL_SECONDS"

# === Step 8: record end time ===
END_TIME_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)
END_TIME_UNIX=$(date +%s)
update_metadata "end_time_utc" "\"$END_TIME_UTC\""
update_metadata "end_time_unix" "$END_TIME_UNIX"
echo ""
echo "[8/11] k6 finished (end_time_utc: $END_TIME_UTC)"

# === Step 9: collect data ===
echo ""
echo "[9/11] collect data"

# Prometheus query_range
PROM_OUT="$EXP_DIR/prom.json"
echo "{" > "$PROM_OUT"

prom_query_range() {
  local key="$1" query="$2" comma="$3"
  local encoded
  encoded=$(printf '%s' "$query" | jq -sRr @uri)
  local url="$PROM_URL/api/v1/query_range?query=${encoded}&start=${START_TIME_UNIX}&end=${END_TIME_UNIX}&step=15s"
  local result
  if ! result=$(curl -sf --max-time 10 "$url"); then
    fail_experiment "Prometheus query_range failed for $key" 3
  fi
  echo "  \"$key\": $result${comma}" >> "$PROM_OUT"
}

prom_query_range "replicas" \
  'kube_deployment_status_replicas{namespace="default",deployment="php-apache"}' \
  ","

prom_query_range "cpu_pct" \
  '(avg(rate(container_cpu_usage_seconds_total{namespace="default",pod=~"php-apache-.*",container!=""}[1m]))/avg(kube_pod_container_resource_requests{namespace="default",pod=~"php-apache-.*",resource="cpu"}))*100' \
  ","

prom_query_range "rps_pkt_rate" \
  'sum(rate(container_network_receive_packets_total{namespace="default",pod=~"php-apache-.*"}[1m]))' \
  ""

echo "}" >> "$PROM_OUT"

# K8s events
kubectl get events --sort-by='.lastTimestamp' -o yaml > "$EXP_DIR/events.yaml"

# Controller log (only meaningful for phpa)
if [ "$CONTROLLER" = "phpa" ] && [ -f "$CONTROLLER_LOG" ]; then
  cp "$CONTROLLER_LOG" "$EXP_DIR/controller.log"
fi

echo "  collected: k6.json, prom.json, events.yaml$([ "$CONTROLLER" = "phpa" ] && echo ", controller.log")"

# === Step 10: smoke check ===
echo ""
echo "[10/11] smoke check"
[ -s "$K6_JSON" ] || fail_experiment "k6.json is empty" 3
jq -e . "$K6_JSON" >/dev/null 2>&1 || jq -e -s . "$K6_JSON" >/dev/null 2>&1 || \
  fail_experiment "k6.json is not valid JSON" 3
[ -s "$PROM_OUT" ] || fail_experiment "prom.json is empty" 3
jq -e . "$PROM_OUT" >/dev/null 2>&1 || fail_experiment "prom.json is not valid JSON" 3

K6_REQS=$(jq -s '[.[] | select(.metric=="http_reqs" and .type=="Point") | .data.value] | add // 0' "$K6_JSON")
PROM_REPLICAS_SERIES=$(jq '.replicas.data.result | length' "$PROM_OUT")
echo "  k6 total requests: $K6_REQS"
echo "  prom replicas series count: $PROM_REPLICAS_SERIES"

if [ "$PROM_REPLICAS_SERIES" -eq 0 ]; then
  fail_experiment "prom replicas series is empty (Prometheus may have lost connection)" 3
fi

# === Step 11: mark success ===
echo ""
echo "[11/11] mark success"
sed -i "s|^  status: in_progress|  status: success|" "$EXP_DIR/metadata.yaml"

echo ""
echo "=== experiment $EXP_DIR completed in $((END_TIME_UNIX - START_TIME_UNIX))s ==="
