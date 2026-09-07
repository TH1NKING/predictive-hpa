#!/usr/bin/env bash
# hack/run_benchmark.sh
#
# Single-experiment orchestrator for the stabilization-window ablation benchmark.
#
# USAGE: hack/run_benchmark.sh <pattern> <controller> <repeat_idx>
#   pattern    : step | ramp | spike
#   controller : native_hpa_300 | native_hpa_60 | phpa | phpa_current | phpa_hybrid
#   repeat_idx : 1-N (positive integer)
#
# Exit codes:
#   0: success
#   1: prerequisite or argument failure
#   2: experiment runtime failure (k6 / controller crash)
#   3: data collection failure
#
# Side effects:
#   - Creates $EXPERIMENTS_ROOT/<timestamp>_<pattern>_<controller>_r<idx>/
#   - Starts/stops only its own controller process (per-run controller.log)
#   - Re-deploys PHPA sample or native HPA YAML depending on controller
#   - Resets php-apache Deployment to 1 replica before each run

set -euo pipefail

# Locate repo root regardless of invocation path
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"
source "$SCRIPT_DIR/lib/k6_runner.sh"
source "$SCRIPT_DIR/lib/benchmark_config.sh"

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
  native_hpa_300)
    NATIVE_HPA_YAML="config/benchmark/native-hpa.yaml"
    SCALE_DOWN_STABILIZATION_SECONDS=300
    PREDICTION_VARIANT="none"
    ;;
  native_hpa_60)
    NATIVE_HPA_YAML="config/benchmark/native-hpa-60.yaml"
    SCALE_DOWN_STABILIZATION_SECONDS=60
    PREDICTION_VARIANT="none"
    ;;
  phpa|phpa_current|phpa_hybrid)
    NATIVE_HPA_YAML=""
    SCALE_DOWN_STABILIZATION_SECONDS=60
    PREDICTION_VARIANT="ewma_damped_cap"
    ;;
  *)
    echo "ERROR: controller must be native_hpa_300|native_hpa_60|phpa|phpa_current|phpa_hybrid, got '$CONTROLLER'" >&2
    exit 1
    ;;
esac
if ! [[ "$REPEAT_IDX" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: repeat_idx must be positive integer, got '$REPEAT_IDX'" >&2
  exit 1
fi

# === Configuration ===
benchmark_config_init
benchmark_config_fingerprint
DECISION_MODE=$(benchmark_decision_mode "$CONTROLLER")
PHPA_SAMPLE="config/samples/autoscaling_v1alpha1_predictivehpa.yaml"
EXPERIMENTS_ROOT="${EXPERIMENTS_ROOT:-experiments/service-routing-v1}"
case "$EXPERIMENTS_ROOT" in
  /*) ;;
  *) EXPERIMENTS_ROOT="$REPO_ROOT/$EXPERIMENTS_ROOT" ;;
esac
CAMPAIGN="${CAMPAIGN:-service-routing-v1}"
CONTROLLER_PID=""
CONTROLLER_KUBECONFIG=""
CONTROLLER_BINARY=""
CONTROLLER_STARTUP_TIMEOUT=60
METRIC_ACCUMULATION_SECONDS=30
# Tail observation after k6 exits. Captures scale-down behavior.
# Moved out of k6 stages because ramping-arrival-rate executor exits early
# when target=0 and all in-flight requests are done — see step.js comment.
# Sized to 360s = the longest configured window (Native-300) + 60s buffer
# for the final reconcile + Pod
# termination. The earlier 240s value missed native HPA's full scale-down
# curve in a step native_hpa_300 dry-run (only first scale decision captured).
POST_LOAD_TAIL_SECONDS=360
PROM_URL="http://localhost:9090"

# === Step 1: prerequisites ===
echo "[1/11] prerequisite check"
bash hack/prerequisites_check.sh

# === Step 2: create experiment directory ===
echo ""
echo "[2/11] create experiment directory"
TIMESTAMP_LOCAL=$(date +%Y%m%d_%H%M%S)
EXP_DIR="${EXPERIMENTS_ROOT}/${TIMESTAMP_LOCAL}_${PATTERN}_${CONTROLLER}_r${REPEAT_IDX}"
mkdir -p "$EXPERIMENTS_ROOT"
mkdir "$EXP_DIR"
echo "  $EXP_DIR"
CONTROLLER_LOG="$EXP_DIR/controller.log"

# === Step 3: write initial metadata.yaml ===
echo ""
echo "[3/11] write metadata.yaml"
START_TIME_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)
START_TIME_UNIX=$(date +%s)
GIT_COMMIT=$(git rev-parse HEAD)
GIT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
GIT_DIRTY=$([ -z "$(git status --porcelain)" ] && echo "false" || echo "true")
K8S_VERSION=$(k6_runner_kubectl version -o json | jq -r ' .serverVersion.gitVersion')

cat > "$EXP_DIR/metadata.yaml" <<META
experiment_id: ${TIMESTAMP_LOCAL}_${PATTERN}_${CONTROLLER}_r${REPEAT_IDX}
campaign: "$CAMPAIGN"
traffic_path: "$K6_TRAFFIC_PATH"
load_generator: "$K6_EXECUTION_MODE"
endpoint: "$K6_BASE_URL"
k6_image: "$K6_IMAGE"
connection_reuse: false
protocol_version: "$BENCHMARK_PROTOCOL_VERSION"
rps: $RPS
benchmark_source_sha256: "$BENCHMARK_SOURCE_SHA256"
benchmark_config_sha256: "$BENCHMARK_CONFIG_SHA256"
pre_allocated_vus: $BENCHMARK_PRE_ALLOCATED_VUS
max_vus: $BENCHMARK_MAX_VUS
post_load_tail_seconds: $POST_LOAD_TAIL_SECONDS
load_start_time_unix: 0
offered_load_end_time_unix: 0
observation_end_time_unix: 0
pattern: $PATTERN
controller: $CONTROLLER
repeat: $REPEAT_IDX
scale_down_stabilization_seconds: $SCALE_DOWN_STABILIZATION_SECONDS
prediction_variant: "$PREDICTION_VARIANT"
decision_mode: "$DECISION_MODE"
start_time_utc: "$START_TIME_UTC"
start_time_unix: $START_TIME_UNIX
end_time_utc: ""
end_time_unix: 0
git:
  commit: $GIT_COMMIT
  branch: $GIT_BRANCH
  dirty: $GIT_DIRTY
env:
  k6_version: "see k6-version.txt"
  cluster: "$BENCHMARK_CONTEXT"
  k8s_version: "$K8S_VERSION"
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

# Own only the controller process started below. Unexpected shell errors and
# interrupts mark the run failed while retaining all partial local evidence.
cleanup_benchmark() {
  local status=$?
  trap - EXIT INT TERM
  if [ -n "$CONTROLLER_PID" ]; then
    kill -TERM "$CONTROLLER_PID" 2>/dev/null || true
    for ((stop_wait=0; stop_wait<15; stop_wait++)); do
      kill -0 "$CONTROLLER_PID" 2>/dev/null || break
      sleep 1
    done
    kill -KILL "$CONTROLLER_PID" 2>/dev/null || true
    wait "$CONTROLLER_PID" 2>/dev/null || true
  fi
  if [ -n "$CONTROLLER_KUBECONFIG" ]; then
    rm -f -- "$CONTROLLER_KUBECONFIG"
  fi
  if [ -n "$CONTROLLER_BINARY" ]; then
    rm -f -- "$CONTROLLER_BINARY"
  fi
  if grep -q '^  status: in_progress' "$EXP_DIR/metadata.yaml"; then
    sed -i 's|^  status: in_progress|  status: failed|' "$EXP_DIR/metadata.yaml"
    sed -i 's|^  failure_reason: ""|  failure_reason: "orchestrator interrupted or unexpected command failure"|' "$EXP_DIR/metadata.yaml"
    update_metadata end_time_utc "\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
    update_metadata end_time_unix "$(date +%s)"
    [ "$status" -ne 0 ] || status=2
  fi
  exit "$status"
}
trap cleanup_benchmark EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Record the precise target configuration and runtime image IDs before changes.
k6_runner_kubectl get deploy php-apache -o json > "$EXP_DIR/deployment-before.json"
k6_runner_kubectl get pods -l run=php-apache -o json > "$EXP_DIR/workload-pods-before.json"
k6_runner_kubectl get service php-apache -o json > "$EXP_DIR/service-before.json"
k6_runner_kubectl get endpointslices -l kubernetes.io/service-name=php-apache -o json > "$EXP_DIR/endpoints-before.json"
k6_runner_kubectl get hpa -o json > "$EXP_DIR/hpa-before.json"
k6_runner_kubectl get predictivehpas -o json > "$EXP_DIR/phpa-before.json"

# Refuse extra policies targeting this Deployment rather than deleting objects
# outside the two benchmark fixtures that this orchestrator manages.
if ! jq -e 'all(.items[]; .spec.scaleTargetRef.name != "php-apache" or .metadata.name == "php-apache")' \
    "$EXP_DIR/hpa-before.json" >/dev/null; then
  fail_experiment "another HPA targets php-apache; remove that conflict before benchmarking" 1
fi
if ! jq -e 'all(.items[]; .spec.scaleTargetRef.name != "php-apache" or .metadata.name == "predictivehpa-sample")' \
    "$EXP_DIR/phpa-before.json" >/dev/null; then
  fail_experiment "another PHPA targets php-apache; remove that conflict before benchmarking" 1
fi

# Do not kill unrelated make/go processes or the owner of a shared port. An
# existing controller must be stopped deliberately before any benchmark writes.
command -v ss >/dev/null || fail_experiment "ss is required to check controller listener ownership" 1
if ss -ltn | awk '$4 ~ /:8081$/ {found=1} END {exit !found}'; then
  fail_experiment "port 8081 is already in use; stop the existing controller before benchmarking" 1
fi

# Disable both policies before resetting replicas, so a prior run cannot race
# the reset. Every Kubernetes command is bound to the validated Kind context.
echo ""
echo "[4/11] disable previous autoscaling policies and reset Deployment"
k6_runner_kubectl delete hpa php-apache --ignore-not-found=true >/dev/null
k6_runner_kubectl delete -f "$PHPA_SAMPLE" --ignore-not-found=true >/dev/null
k6_runner_kubectl scale deploy php-apache --replicas=1 >/dev/null
if ! k6_runner_kubectl rollout status deploy/php-apache --timeout=60s --request-timeout=65s >/dev/null; then
  fail_experiment "Deployment rollout did not stabilize within 60s"
fi
# rollout status can return while old Pods are still terminating. Start every
# controller from exactly one ready Pod, including the native-HPA baseline.
RESET_READY=false
for ((reset_wait=0; reset_wait<90; reset_wait++)); do
  if k6_runner_kubectl get pods -l run=php-apache -o json |
      jq -e '.items | length == 1 and all(.[];
        .metadata.deletionTimestamp == null and
        any(.status.conditions[]?; .type == "Ready" and .status == "True"))' >/dev/null; then
    RESET_READY=true
    break
  fi
  sleep 2
done
[ "$RESET_READY" = true ] || fail_experiment "Deployment did not settle at one ready Pod"

echo ""
echo "[5/11] switch controller ($CONTROLLER)"
if [[ "$CONTROLLER" = phpa* ]]; then
  # Resolve the same fixture for each treatment, changing only its decision
  # mode before any controller starts. Keep the exact submitted CR as evidence.
  k6_runner_kubectl create --dry-run=client -f "$PHPA_SAMPLE" -o json |
    jq --arg mode "$DECISION_MODE" '.spec.decisionMode = $mode' > "$EXP_DIR/phpa-applied.json"
  k6_runner_kubectl apply -f "$EXP_DIR/phpa-applied.json" >/dev/null
  k6_runner_kubectl get predictivehpa predictivehpa-sample -o json > "$EXP_DIR/phpa-after-apply.json"
  jq -e --arg mode "$DECISION_MODE" '.spec.decisionMode == $mode' "$EXP_DIR/phpa-after-apply.json" >/dev/null ||
    fail_experiment "PHPA decisionMode was not persisted; install the generated CRD before benchmarking" 1
  # The private kubeconfig is kept in /tmp, never in exported run artifacts.
  CONTROLLER_KUBECONFIG=$(mktemp /tmp/phpa-benchmark-kubeconfig.XXXXXX)
  chmod 600 "$CONTROLLER_KUBECONFIG"
  k6_runner_kubectl config view --minify --flatten --raw > "$CONTROLLER_KUBECONFIG"
  CONTROLLER_BINARY=$(mktemp /tmp/phpa-benchmark-controller.XXXXXX)
  if ! go build -o "$CONTROLLER_BINARY" ./cmd; then
    fail_experiment "controller build failed"
  fi
  KUBECONFIG="$CONTROLLER_KUBECONFIG" "$CONTROLLER_BINARY" > "$CONTROLLER_LOG" 2>&1 &
  CONTROLLER_PID=$!
  STARTED=false
  for ((i=0; i<CONTROLLER_STARTUP_TIMEOUT; i++)); do
    if grep -q "Starting workers" "$CONTROLLER_LOG"; then STARTED=true; break; fi
    kill -0 "$CONTROLLER_PID" 2>/dev/null || fail_experiment "controller failed to start (see controller.log)"
    sleep 1
  done
  [ "$STARTED" = true ] || fail_experiment "controller startup timed out (see controller.log)"
else
  k6_runner_kubectl apply -f "$NATIVE_HPA_YAML" >/dev/null
fi

# === Step 6: metric accumulation pause ===
echo ""
echo "[6/11] metric accumulation pause (${METRIC_ACCUMULATION_SECONDS}s)"
sleep "$METRIC_ACCUMULATION_SECONDS"

# === Step 7: run k6 load ===
echo ""
echo "[7/11] run k6 pattern: $PATTERN"
K6_JSON="$EXP_DIR/k6.json"
if ! k6_runner_run "${PATTERN}.js" "$EXP_DIR" "RPS=$RPS" "PROBE_TOKEN=$(basename "$EXP_DIR")"; then
  fail_experiment "in-cluster k6 run failed (see k6-runner.json and retained artifacts)"
fi

# Tail observation: k6's ramping-arrival-rate executor exits early when
# target=0 and all in-flight requests have completed, so we cannot rely
# on a trailing k6 stage to observe scale-down. The orchestrator pauses
# here to ensure both PHPA and native HPA scale-down sequences are
# captured in the Prometheus and controller log data collected next.
# Use the same offered-load and observation boundaries for both controllers.
# Request drain, artifact copying and controller preparation must not extend
# one group's cost window. The runner records the actual process timestamps.
K6_START_TIME_UNIX=$(cat "$EXP_DIR/k6-start-time-unix")
case "$PATTERN" in
  step) OFFERED_DURATION_SECONDS=211 ;;
  ramp) OFFERED_DURATION_SECONDS=270 ;;
  spike) OFFERED_DURATION_SECONDS=241 ;;
esac
LOAD_START_TIME_UNIX=$((K6_START_TIME_UNIX + 30))
OFFERED_LOAD_END_TIME_UNIX=$((K6_START_TIME_UNIX + OFFERED_DURATION_SECONDS))
OBSERVATION_END_TIME_UNIX=$((OFFERED_LOAD_END_TIME_UNIX + POST_LOAD_TAIL_SECONDS))
update_metadata load_start_time_unix "$LOAD_START_TIME_UNIX"
update_metadata offered_load_end_time_unix "$OFFERED_LOAD_END_TIME_UNIX"
update_metadata observation_end_time_unix "$OBSERVATION_END_TIME_UNIX"
# Collect one more scrape after the fixed boundary so analysis can interpolate
# at that boundary without extrapolating a stale replica value.
TAIL_WAIT_SECONDS=$((OBSERVATION_END_TIME_UNIX + 15 - $(date +%s)))
if (( TAIL_WAIT_SECONDS > 0 )); then
  echo "  Observing fixed post-load tail; ${TAIL_WAIT_SECONDS}s remaining including final scrape"
  sleep "$TAIL_WAIT_SECONDS"
fi

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
  local url="$PROM_URL/api/v1/query_range?query=${encoded}&start=$((K6_START_TIME_UNIX - 15))&end=${END_TIME_UNIX}&step=15s"
  local result
  if ! result=$(curl -sf --max-time 10 "$url"); then
    fail_experiment "Prometheus query_range failed for $key" 3
  fi
  jq -e '.status == "success"' <<<"$result" >/dev/null || \
    fail_experiment "Prometheus returned an error for $key" 3
  echo "  \"$key\": $result${comma}" >> "$PROM_OUT"
}

prom_query_range "replicas" \
  'kube_deployment_status_replicas{namespace="default",deployment="php-apache"}' \
  ","

prom_query_range "cpu_pct" \
  '(avg(rate(container_cpu_usage_seconds_total{namespace="default",pod=~"php-apache-.*",container!=""}[1m]))/avg(kube_pod_container_resource_requests{namespace="default",pod=~"php-apache-.*",resource="cpu"}))*100' \
  ","

prom_query_range "cpu_by_pod" \
  'sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="default",pod=~"php-apache-.*",container!="",container!="POD"}[1m]))' \
  ","

prom_query_range "load_generator_cpu" \
  'sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="default",pod=~"phpa-k6-.*",container="k6"}[1m]))' \
  ","

prom_query_range "load_generator_memory" \
  'sum by (pod) (container_memory_working_set_bytes{namespace="default",pod=~"phpa-k6-.*",container="k6"})' \
  ","

prom_query_range "rps_pkt_rate" \
  'sum(rate(container_network_receive_packets_total{namespace="default",pod=~"php-apache-.*"}[1m]))' \
  ""

echo "}" >> "$PROM_OUT"

# K8s events
k6_runner_kubectl get events --sort-by='.lastTimestamp' -o yaml > "$EXP_DIR/events.yaml"

k6_runner_kubectl get pods -l run=php-apache -o json > "$EXP_DIR/workload-pods-after.json"
k6_runner_kubectl get endpointslices -l kubernetes.io/service-name=php-apache -o json > "$EXP_DIR/endpoints-after.json"

echo "  collected: k6.json, prom.json, events.yaml$([[ "$CONTROLLER" = phpa* ]] && echo ", controller.log")"

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

if [[ "$CONTROLLER" = phpa* ]]; then
  kill -0 "$CONTROLLER_PID" 2>/dev/null || fail_experiment "controller exited during the experiment"
fi
[ "$K6_REQS" -gt 0 ] || fail_experiment "k6 did not record any requests" 3

# === Step 11: mark success ===
echo ""
echo "[11/11] mark success"
sed -i "s|^  status: in_progress|  status: success|" "$EXP_DIR/metadata.yaml"

echo ""
echo "=== experiment $EXP_DIR completed in $((END_TIME_UNIX - START_TIME_UNIX))s ==="
