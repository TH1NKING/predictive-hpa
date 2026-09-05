#!/usr/bin/env bash
# Diagnose Service routing at fixed replica counts before choosing benchmark RPS.
# --dry-run is entirely offline. Actual runs require an explicit dedicated Kind
# context and NO autoscaler targeting php-apache. Original replicas are restored.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"
PROBE_REPLICAS="${PROBE_REPLICAS:-1 5 10}"
PROBE_RPS_LIST="${PROBE_RPS_LIST:-1 3 5}"
PROBE_DURATION_SECONDS="${PROBE_DURATION_SECONDS:-90}"
CALIBRATION_ROOT="${CALIBRATION_ROOT:-benchmark-runs/service-routing-calibration}"
PROM_URL="${PROM_URL:-http://localhost:9090}"
PYTHON="${PYTHON:-python3}"

die() { echo "ERROR: $*" >&2; exit 1; }
[[ $# -eq 0 || ( $# -eq 1 && "$1" = --dry-run ) ]] || die "Usage: $0 [--dry-run]"
[[ "$PROBE_DURATION_SECONDS" =~ ^[1-9][0-9]{1,2}$ ]] &&
  (( PROBE_DURATION_SECONDS >= 60 && PROBE_DURATION_SECONDS <= 600 )) || die "Probe duration must be 60..600 seconds"
[[ "$PROBE_REPLICAS$PROBE_RPS_LIST" != *$'\n'* && "$PROBE_REPLICAS$PROBE_RPS_LIST" != *$'\r'* ]] || die "Probe lists must be on one line"
read -r -a replica_levels <<< "$PROBE_REPLICAS"
read -r -a rps_levels <<< "$PROBE_RPS_LIST"
(( ${#replica_levels[@]} && ${#rps_levels[@]} )) || die "Probe lists must not be empty"
for replicas in "${replica_levels[@]}"; do
  [[ "$replicas" =~ ^([1-9]|10)$ ]] || die "Probe replicas must be 1..10"
done
for rps in "${rps_levels[@]}"; do
  [[ "$rps" =~ ^[1-9][0-9]{0,3}$ ]] && (( rps <= 1000 )) || die "Probe RPS must be 1..1000"
done
if [[ "${1:-}" = --dry-run ]]; then
  echo "Offline calibration plan: in-cluster Service http://php-apache.default.svc:80"
  echo "Fresh HTTP connections; per-Pod access logs and CPU; output root: $CALIBRATION_ROOT"
  for replicas in "${replica_levels[@]}"; do
    for rps in "${rps_levels[@]}"; do
      printf 'replicas=%s rps=%s duration=%ss\n' "$replicas" "$rps" "$PROBE_DURATION_SECONDS"
    done
  done
  exit 0
fi

source "$SCRIPT_DIR/lib/k6_runner.sh"
k6_runner_validate_config
k6_runner_require_kind_context
for tool in jq curl "$PYTHON"; do command -v "$tool" >/dev/null || die "Missing tool: $tool"; done
kube() { kubectl --context "$BENCHMARK_CONTEXT" --namespace default --request-timeout=20s "$@"; }

# Refuse conflicting writers; do not delete the user's autoscalers or processes.
assert_no_autoscalers() {
  local hpas phpas crd
  hpas=$(kube get hpa -o json) || return 1
  if jq -e '[.items[] | select(.spec.scaleTargetRef.name == "php-apache" and .spec.scaleTargetRef.kind == "Deployment")] | length > 0' <<< "$hpas" >/dev/null; then
    echo "Native HPA still targets php-apache; remove it before fixed-replica calibration" >&2
    return 1
  fi
  crd=$(kube get crd predictivehpas.autoscaling.brian.io --ignore-not-found -o name) || return 1
  if [[ -n "$crd" ]]; then
    phpas=$(kube get predictivehpas -o json) || return 1
    if jq -e '[.items[] | select(.spec.scaleTargetRef.name == "php-apache" and .spec.scaleTargetRef.kind == "Deployment")] | length > 0' <<< "$phpas" >/dev/null; then
      echo "PHPA still targets php-apache; remove it before fixed-replica calibration" >&2
      return 1
    fi
  fi
}
assert_no_autoscalers || die "Calibration requires exclusive control of Deployment replicas"
curl -fsS --max-time 10 "$PROM_URL/-/healthy" >/dev/null || die "Prometheus unavailable"
deployment=$(kube get deployment php-apache -o json)
original_replicas=$(jq -r '.spec.replicas' <<< "$deployment")
deployment_uid=$(jq -r '.metadata.uid' <<< "$deployment")
last_replicas=""
mkdir -p "$CALIBRATION_ROOT"
RUN_DIR=$(mktemp -d "$CALIBRATION_ROOT/$(date -u +%Y%m%dT%H%M%SZ)_XXXXXX")
restore_replicas() {
  local status=$? current
  trap - EXIT
  if [[ -n "$last_replicas" ]]; then
    if current=$(kube get deployment php-apache -o json) &&
       [[ $(jq -r '.metadata.uid' <<< "$current") = "$deployment_uid" ]] &&
       assert_no_autoscalers &&
       kube scale deployment php-apache --resource-version="$(jq -r '.metadata.resourceVersion' <<< "$current")" \
         --current-replicas="$last_replicas" --replicas="$original_replicas" > "$RUN_DIR/restore.log" 2>&1; then
      echo "Restored original replicas: $original_replicas"
    else
      echo "Replica restoration failed or Deployment changed; inspect $RUN_DIR/restore.log" >&2
      status=1
    fi
  fi
  exit "$status"
}
trap restore_replicas EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Contemporaneous, non-secret evidence; never export kubeconfig credentials.
printf '%s\n' "$deployment" > "$RUN_DIR/deployment-before.json"
kube get service php-apache -o json > "$RUN_DIR/service-before.json"
kube get nodes -o json > "$RUN_DIR/nodes-before.json"
kubectl --context "$BENCHMARK_CONTEXT" version -o json > "$RUN_DIR/kubernetes-version.json"
git status --porcelain > "$RUN_DIR/git-status.txt"
jq -n --arg started "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg commit "$(git rev-parse HEAD)" --arg branch "$(git branch --show-current)" \
  --arg context "$BENCHMARK_CONTEXT" --arg image "$K6_IMAGE" \
  --arg endpoint "$K6_BASE_URL" --arg replicas "$PROBE_REPLICAS" --arg rps "$PROBE_RPS_LIST" \
  --argjson duration "$PROBE_DURATION_SECONDS" --argjson original "$original_replicas" \
  '{schema_version:1,started_at:$started,git:{commit:$commit,branch:$branch},context:$context,
    k6_image:$image,endpoint:$endpoint,connection_reuse:false,replica_levels:$replicas,rps_levels:$rps,
    duration_seconds:$duration,original_replicas:$original}' > "$RUN_DIR/manifest.json"

wait_fixed_replicas() {
  local desired="$1" attempt pods actual ready
  for attempt in $(seq 1 90); do
    actual=$(kube get deployment php-apache -o jsonpath='{.spec.replicas}') || return 1
    [[ "$actual" = "$desired" ]] || { echo "Another writer changed replicas" >&2; return 1; }
    pods=$(kube get pods -l run=php-apache -o json) || return 1
    ready=$(jq '[.items[] | select(.metadata.deletionTimestamp == null and any(.status.conditions[]?; .type == "Ready" and .status == "True"))] | length' <<< "$pods")
    if [[ $(jq '.items | length' <<< "$pods") = "$desired" && "$ready" = "$desired" ]]; then return 0; fi
    sleep 2
  done
  return 1
}

for replicas in "${replica_levels[@]}"; do
  assert_no_autoscalers || die "Autoscaler appeared during calibration"
  last_replicas="$replicas"
  kube scale deployment php-apache --replicas="$replicas" >/dev/null
  wait_fixed_replicas "$replicas" || die "Fixed replicas did not become ready"
  sleep 60
  for rps in "${rps_levels[@]}"; do
    assert_no_autoscalers || die "Autoscaler appeared during calibration"
    wait_fixed_replicas "$replicas" || die "Replica set changed before probe"
    probe_dir=$(mktemp -d "$RUN_DIR/replicas-${replicas}_rps-${rps}_XXXXXX")
    token=$(basename "$probe_dir")
    start_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    start_epoch=$(date +%s)
    kube get pods -l run=php-apache -o json > "$probe_dir/pods-before.json"
    kube get endpointslices -l kubernetes.io/service-name=php-apache -o json > "$probe_dir/endpoints-before.json"
    jq -n --arg token "$token" --arg start "$start_utc" --argjson replicas "$replicas" \
      --argjson rps "$rps" --argjson duration "$PROBE_DURATION_SECONDS" \
      '{token:$token,start_time_utc:$start,replicas:$replicas,rps:$rps,duration_seconds:$duration,status:"in_progress"}' > "$probe_dir/probe.json"
    k6_runner_run calibration.js "$probe_dir" "PROBE_RPS=$rps" \
      "PROBE_DURATION_SECONDS=$PROBE_DURATION_SECONDS" "PROBE_TOKEN=$token"
    end_epoch=$(date +%s)
    kube get pods -l run=php-apache -o json > "$probe_dir/pods-after.json"
    kube get endpointslices -l kubernetes.io/service-name=php-apache -o json > "$probe_dir/endpoints-after.json"
    while IFS= read -r pod; do
      kube logs "$pod" -c php-apache --since-time="$start_utc" > "$probe_dir/${pod}.log"
    done < <(jq -r '.items[].metadata.name' "$probe_dir/pods-before.json")
    query='sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="default",pod=~"php-apache-.*",container="php-apache"}[1m]))'
    curl -fsS --max-time 20 --get "$PROM_URL/api/v1/query_range" \
      --data-urlencode "query=$query" --data-urlencode "start=$start_epoch" \
      --data-urlencode "end=$end_epoch" --data-urlencode 'step=15s' > "$probe_dir/cpu-by-pod.json"
    jq -e '.status == "success" and (.data.result | length > 0)' "$probe_dir/cpu-by-pod.json" >/dev/null || die "Missing per-Pod CPU data"
    runner_pod=$(jq -r '.pod' "$probe_dir/k6-runner.json")
    for metric in cpu memory; do
      if [[ "$metric" = cpu ]]; then
        query="sum(rate(container_cpu_usage_seconds_total{namespace=\"default\",pod=\"$runner_pod\",container=\"k6\"}[1m]))"
      else
        query="sum(container_memory_working_set_bytes{namespace=\"default\",pod=\"$runner_pod\",container=\"k6\"})"
      fi
      curl -fsS --max-time 20 --get "$PROM_URL/api/v1/query_range" \
        --data-urlencode "query=$query" --data-urlencode "start=$start_epoch" \
        --data-urlencode "end=$end_epoch" --data-urlencode 'step=15s' > "$probe_dir/load-generator-${metric}.json"
      jq -e '.status == "success"' "$probe_dir/load-generator-${metric}.json" >/dev/null || die "Load-generator $metric query failed"
    done
    jq --argjson end "$end_epoch" '.status="success" | .end_time_unix=$end' "$probe_dir/probe.json" > "$probe_dir/probe-final.json"
    mv "$probe_dir/probe-final.json" "$probe_dir/probe.json"
    sleep 60
  done
done
"$PYTHON" "$SCRIPT_DIR/analyze/calibration.py" "$RUN_DIR"
echo "Calibration evidence: $RUN_DIR (routing and capacity require separate review)"
