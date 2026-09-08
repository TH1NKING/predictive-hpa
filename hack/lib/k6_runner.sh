#!/usr/bin/env bash
# Shared single-attempt in-cluster k6 runner. Source this file from orchestrators.
# k6_runner_render <script relative to hack/k6> <output dir> [NAME=value ...]
# writes a reviewable plan without contacting Kubernetes. k6_runner_run has the
# same interface, verifies an explicitly selected dedicated Kind cluster, and
# saves the original k6 NDJSON plus diagnostics before removing its own objects.

K6_RUNNER_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
K6_IMAGE="${K6_IMAGE:-grafana/k6:1.3.0}"
K6_BASE_URL="http://php-apache.default.svc:80"
K6_EXECUTION_MODE="in-cluster-service"
K6_TRAFFIC_PATH="service-clusterip"
K6_RUNNER_TIMEOUT_SECONDS="${K6_RUNNER_TIMEOUT_SECONDS:-900}"
K6_RUNNER_STARTUP_TIMEOUT_SECONDS="${K6_RUNNER_STARTUP_TIMEOUT_SECONDS:-120}"
K6_RUNNER_CPU_REQUEST="500m"
K6_RUNNER_MEMORY_REQUEST="512Mi"
K6_RUNNER_MEMORY_LIMIT="1Gi"

k6_runner_validate_config() {
  if ! [[ "$K6_IMAGE" =~ ^grafana/k6:1\.3\.0(@sha256:[a-f0-9]{64})?$ ]]; then
    echo "ERROR: K6_IMAGE must be grafana/k6:1.3.0, optionally pinned with @sha256:<64 hex digits>" >&2
    return 1
  fi
  local value
  for value in "$K6_RUNNER_TIMEOUT_SECONDS" "$K6_RUNNER_STARTUP_TIMEOUT_SECONDS"; do
    if ! [[ "$value" =~ ^[1-9][0-9]{0,4}$ ]]; then
      echo "ERROR: runner timeout values must be positive integers <= 99999" >&2
      return 1
    fi
  done
}

k6_runner_require_kind_context() {
  k6_runner_validate_config || return 1
  if ! [[ "${BENCHMARK_CONTEXT:-}" =~ ^kind-[a-z0-9][a-z0-9-]*$ ]]; then
    echo "ERROR: set BENCHMARK_CONTEXT=kind-<dedicated-benchmark-cluster> explicitly" >&2
    return 1
  fi
  local current clusters
  current=$(kubectl config current-context) || return 1
  if [ "$current" != "$BENCHMARK_CONTEXT" ]; then
    echo "ERROR: active context '$current' differs from BENCHMARK_CONTEXT '$BENCHMARK_CONTEXT'" >&2
    return 1
  fi
  clusters=$(kind get clusters) || return 1
  if ! grep -Fxq -- "${BENCHMARK_CONTEXT#kind-}" <<<"$clusters"; then
    echo "ERROR: selected context does not identify a local Kind cluster" >&2
    return 1
  fi
  k6_runner_kubectl cluster-info --request-timeout=5s >/dev/null || return 1
}

k6_runner_kubectl() {
  if [ "${1:-}" = exec ]; then
    # Git Bash must not translate the container's /results paths into Windows
    # paths when kubectl is a native executable. Keep conversion for -f paths.
    MSYS_NO_PATHCONV=1 kubectl --context="${BENCHMARK_CONTEXT:?BENCHMARK_CONTEXT is required}" \
      --namespace=default --request-timeout=30s "$@"
    return $?
  fi
  kubectl --context="${BENCHMARK_CONTEXT:?BENCHMARK_CONTEXT is required}" \
    --namespace=default --request-timeout=30s "$@"
}

k6_runner_render() {
  local script="$1" output_dir="$2"
  shift 2
  k6_runner_validate_config || return 1
  if ! [[ "$script" =~ ^[a-z][a-z0-9_-]*\.js$ ]] || \
     [ ! -f "$K6_RUNNER_REPO_ROOT/hack/k6/$script" ]; then
    echo "ERROR: expected an existing script filename under hack/k6" >&2
    return 1
  fi
  if [ "$script" = latency-step.js ] && [ "${LATENCY_DIAGNOSTIC:-false}" != true ]; then
    echo "ERROR: latency-step.js requires LATENCY_DIAGNOSTIC=true" >&2
    return 1
  fi
  command -v jq >/dev/null || { echo "ERROR: jq is required to render runner manifests" >&2; return 1; }
  mkdir -p "$output_dir" || return 1
  local name="${K6_RUNNER_NAME:-phpa-k6-$(date -u +%Y%m%d%H%M%S)-$$-$RANDOM}"
  if ! [[ "$name" =~ ^phpa-k6-[a-z0-9-]+$ ]] || [ "${#name}" -gt 63 ]; then
    echo "ERROR: invalid runner resource name" >&2
    return 1
  fi
  local data='{}' items='[]' entry key path env_json gate_enabled=false
  # ConfigMap keys cannot contain '/', while volume item paths can. Preserve
  # the script import tree explicitly rather than relying on an image's tar.
  while IFS= read -r path; do
    key="${path//\//__}"
    data=$(jq --arg key "$key" --rawfile content "$K6_RUNNER_REPO_ROOT/hack/k6/$path" \
      '. + {($key): $content}' <<<"$data") || return 1
    items=$(jq --arg key "$key" --arg path "$path" '. + [{key:$key,path:$path}]' <<<"$items") || return 1
  done < <(cd "$K6_RUNNER_REPO_ROOT/hack/k6" && find . -type f -name '*.js' | sed 's|^./||' | sort)
  env_json=$(jq -n --arg url "$K6_BASE_URL" '[
    {name:"BASE_URL",value:$url},
    {name:"K6_NO_CONNECTION_REUSE",value:"true"},
    {name:"K6_NO_USAGE_REPORT",value:"true"}
  ]') || return 1
  for entry in "$@"; do
    # Only workload parameters are accepted; endpoint and k6 execution options
    # are fixed so an inherited shell variable cannot redirect benchmark load.
    if [ "$entry" = LATENCY_GATE_TIMEOUT_SECONDS=180 ] && \
        [ "$script" = latency-step.js ] && [ "${LATENCY_DIAGNOSTIC:-false}" = true ]; then
      gate_enabled=true
    elif ! [[ "$entry" =~ ^(RPS|PROBE_RPS|PROBE_DURATION_SECONDS|PROBE_REPLICAS|CALIBRATION_RPS|CALIBRATION_DURATION_SECONDS)=[1-9][0-9]*$ ]] && \
       ! [[ "$entry" =~ ^PROBE_TOKEN=[A-Za-z0-9_-]+$ ]]; then
      echo "ERROR: unsupported runner environment argument '$entry'" >&2
      return 1
    fi
    env_json=$(jq --arg name "${entry%%=*}" --arg value "${entry#*=}" \
      '. + [{name:$name,value:$value}]' <<<"$env_json") || return 1
  done
  if [ "$script" = latency-step.js ] && [ "$gate_enabled" != true ]; then
    echo "ERROR: latency-step.js requires its bounded 180-second launch gate" >&2
    return 1
  fi
  jq -n --arg name "$name" --argjson data "$data" '{
    apiVersion:"v1",kind:"ConfigMap",
    metadata:{name:$name,namespace:"default",labels:{"app.kubernetes.io/name":"phpa-k6","benchmark-run":$name}},
    data:$data
  }' > "$output_dir/k6-configmap.json" || return 1

  local command
  command=$(cat <<'RUNNER'
set -u
touch /results/k6.json /results/k6-warnings.log /results/k6-stdout.log
k6 version > /results/k6-version.txt 2>&1
if [ "${LATENCY_GATE_TIMEOUT_SECONDS:-0}" != 0 ]; then
  latency_wait_for_launch() {
    gate_started=$(date +%s)
    gate_deadline=$((gate_started + LATENCY_GATE_TIMEOUT_SECONDS))
    printf '%s\n' waiting > /results/latency-gate-status
    printf '%s\n' "$gate_started" > /results/latency-gate-ready
    launch_at=""
    while [ "$(date +%s)" -lt "$gate_deadline" ]; do
      if [ -f /results/latency-gate-cancelled ]; then
        printf '%s\n' cancelled > /results/latency-gate-status
        return 125
      fi
      if [ -z "$launch_at" ] && [ -f /results/latency-launch-at-unix ]; then
        launch_at=$(cat /results/latency-launch-at-unix)
        case "$launch_at" in
          ''|*[!0-9]*|0*)
            printf '%s\n' invalid_launch > /results/latency-gate-status
            return 125
            ;;
        esac
        if [ "${#launch_at}" -gt 12 ]; then
          printf '%s\n' invalid_launch > /results/latency-gate-status
          return 125
        fi
      fi
      if [ -n "$launch_at" ] && [ "$(date +%s)" -ge "$launch_at" ]; then
        date +%s > /results/latency-gate-release-unix
        printf '%s\n' released > /results/latency-gate-status
        return 0
      fi
      sleep 0.1
    done
    printf '%s\n' timed_out > /results/latency-gate-status
    return 124
  }
  latency_wait_for_launch
  gate_code=$?
  if [ "$gate_code" -ne 0 ]; then
    printf '%s\n' "$gate_code" > /results/k6-exit-code
    # Retain the gate failure receipts without starting the workload.
    while [ ! -f /results/collected ]; do sleep 2; done
    exit "$gate_code"
  fi
fi
date -u +%Y-%m-%dT%H:%M:%SZ > /results/k6-start-time-utc
date +%s > /results/k6-start-time-unix
k6 run --out json=/results/k6.json --summary-export=/results/k6-summary.json --log-output=file=/results/k6-warnings.log "$1" > /results/k6-stdout.log 2>&1 &
load_pid=$!
printf '%s\n' "$load_pid" > /results/k6-pid
trap 'kill -TERM "$load_pid" 2>/dev/null || true; wait "$load_pid" 2>/dev/null || true; exit 143' TERM INT
# Close the race where host cancellation arrived between gate release and PID
# publication. The unchanged workload still begins with its 30-second quiet stage.
if [ -f /results/latency-gate-cancelled ]; then kill -TERM "$load_pid" 2>/dev/null || true; fi
wait "$load_pid"
code=$?
date -u +%Y-%m-%dT%H:%M:%SZ > /results/k6-end-time-utc
date +%s > /results/k6-end-time-unix
printf '%s\n' "$code" > /results/k6-exit-code.tmp
mv /results/k6-exit-code.tmp /results/k6-exit-code
# Keep emptyDir readable through kubectl exec until the host collects files.
while [ ! -f /results/collected ]; do sleep 2; done
exit "$code"
RUNNER
) || return 1
  jq -n --arg name "$name" --arg image "$K6_IMAGE" --arg script "$script" \
    --arg command "$command" --argjson env "$env_json" --argjson items "$items" \
    --arg cpu "$K6_RUNNER_CPU_REQUEST" --arg memory "$K6_RUNNER_MEMORY_REQUEST" \
    --arg memory_limit "$K6_RUNNER_MEMORY_LIMIT" \
    --argjson deadline "$((K6_RUNNER_TIMEOUT_SECONDS + K6_RUNNER_STARTUP_TIMEOUT_SECONDS + 600))" '{
    apiVersion:"v1",kind:"Pod",
    metadata:{name:$name,namespace:"default",labels:{"app.kubernetes.io/name":"phpa-k6","benchmark-run":$name}},
    spec:{restartPolicy:"Never",activeDeadlineSeconds:$deadline,terminationGracePeriodSeconds:30,
      automountServiceAccountToken:false,
      securityContext:{runAsNonRoot:true,runAsUser:12345,runAsGroup:12345,fsGroup:12345},
      containers:[{name:"k6",image:$image,imagePullPolicy:"IfNotPresent",
        command:["/bin/sh","-c"],args:[$command,"runner",("/scripts/" + $script)],env:$env,
        securityContext:{allowPrivilegeEscalation:false,capabilities:{drop:["ALL"]}},
        resources:{requests:{cpu:$cpu,memory:$memory},limits:{memory:$memory_limit}},
        volumeMounts:[{name:"scripts",mountPath:"/scripts",readOnly:true},{name:"results",mountPath:"/results"}]}],
      volumes:[{name:"scripts",configMap:{name:$name,items:$items}},{name:"results",emptyDir:{sizeLimit:"2Gi"}}]
    }
  }' > "$output_dir/k6-pod.json" || return 1
  jq -n --arg context "${BENCHMARK_CONTEXT:-unset-offline-plan}" --arg name "$name" \
    --arg endpoint "$K6_BASE_URL" --arg mode "$K6_EXECUTION_MODE" --arg path "$K6_TRAFFIC_PATH" \
    --arg image "$K6_IMAGE" --arg script "$script" --argjson env "$env_json" \
    --argjson gate_enabled "$gate_enabled" '{
    context:$context,namespace:"default",pod:$name,configmap:$name,
    endpoint:$endpoint,execution_mode:$mode,traffic_path:$path,image:$image,script:$script,
    connection_reuse:false,environment:$env,status:"planned"
  } + (if $gate_enabled then {latency_gate:{timeout_seconds:180,
    clock_precision_seconds:1,launch_epoch_unit:"integer_unix_seconds",
    launch_rounding:"ceil",poll_interval_seconds:0.1}} else {} end)' > "$output_dir/k6-runner.json" || return 1
}

k6_runner_validate_artifacts() {
  local output_dir="$1" file start end
  for file in k6.json k6-summary.json k6-version.txt k6-start-time-utc \
    k6-start-time-unix k6-end-time-utc k6-end-time-unix k6-exit-code; do
    if [ ! -s "$output_dir/$file" ]; then
      echo "ERROR: required artifact $file is empty or missing" >&2
      return 1
    fi
  done
  # Parse each NDJSON line without loading the whole stream into memory. A
  # syntactically valid stream with no request points is not a completed probe.
  if ! jq -Rne 'reduce inputs as $line (false;
      if ($line | test("^\\s*$")) then . else
        ($line | fromjson) as $record |
        if ($record | type) != "object" then error("expected NDJSON object") else
          . or ($record.type == "Point" and $record.metric == "http_reqs" and
            ($record.data.value | type) == "number" and $record.data.value > 0)
        end
      end)' "$output_dir/k6.json" >/dev/null; then
    echo "ERROR: k6.json must contain valid NDJSON and positive http_reqs points" >&2
    return 1
  fi
  # --summary-export uses the legacy flat counter shape; handleSummary uses
  # values.count. Accept either explicit shape, while rejecting absent/zero
  # counters instead of confusing a copied file with successful collection.
  if ! jq -e '.metrics.http_reqs | (.count // .values.count) |
      type == "number" and . > 0' "$output_dir/k6-summary.json" >/dev/null; then
    echo "ERROR: k6-summary.json must contain a valid summary with completed requests" >&2
    return 1
  fi
  # Official images can report SemVer build metadata (for example +dirty).
  # Keep the release exact and reject prereleases or malformed build suffixes.
  grep -Eq '^k6 v1\.3\.0(\+[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?([[:space:]]|$)' "$output_dir/k6-version.txt" || {
    echo "ERROR: collected k6 version does not match the pinned image version" >&2; return 1;
  }
  for file in k6-start-time-utc k6-end-time-utc; do
    grep -Exq '[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z' "$output_dir/$file" || {
      echo "ERROR: invalid UTC timestamp in $file" >&2; return 1;
    }
  done
  start=$(cat "$output_dir/k6-start-time-unix")
  end=$(cat "$output_dir/k6-end-time-unix")
  if ! [[ "$start" =~ ^[1-9][0-9]{0,11}$ ]] || ! [[ "$end" =~ ^[1-9][0-9]{0,11}$ ]] || \
      [ "$end" -lt "$start" ]; then
    echo "ERROR: invalid or reversed k6 Unix timestamps" >&2
    return 1
  fi
  grep -Exq '[0-9]{1,3}' "$output_dir/k6-exit-code" || {
    echo "ERROR: invalid k6 exit code artifact" >&2; return 1;
  }
}

k6_runner_run() (
  # Subshell scopes the lifecycle trap and resource ownership to one probe.
  set -euo pipefail
  local script="$1" output_dir="$2"
  shift 2
  k6_runner_require_kind_context || exit 1
  if [ -e "$output_dir/k6-runner.json" ]; then
    echo "ERROR: runner output already exists; use a fresh output directory to preserve earlier evidence" >&2
    exit 1
  fi
  k6_runner_render "$script" "$output_dir" "$@" || exit 1
  local name created_cm=false created_pod=false collected=false finished=false
  local failure_reason="runner setup failed" run_code=1
  name=$(jq -r '.pod' "$output_dir/k6-runner.json")

  finish_runner() {
    local status=$? file copy_failed=false
    trap - EXIT INT TERM
    if [ "$created_pod" = true ]; then
      k6_runner_kubectl get pod "$name" -o json > "$output_dir/k6-pod-status.json" 2>> "$output_dir/k6-collection-errors.log" || true
      k6_runner_kubectl get events --field-selector="involvedObject.name=$name" -o yaml \
        > "$output_dir/k6-pod-events.yaml" 2>> "$output_dir/k6-collection-errors.log" || true
      k6_runner_kubectl logs "$name" -c k6 > "$output_dir/k6-container.log" 2>> "$output_dir/k6-collection-errors.log" || true
      # A timeout/interruption must stop actual load before copying partial files.
      if [ "$finished" = false ]; then
        local stop_command='if [ -f /results/k6-pid ]; then kill -TERM "$(cat /results/k6-pid)" 2>/dev/null || true; fi'
        if [ "$script" = latency-step.js ]; then
          stop_command="touch /results/latency-gate-cancelled; $stop_command"
        fi
        k6_runner_kubectl exec "$name" -c k6 -- sh -c "$stop_command" \
          >> "$output_dir/k6-collection-errors.log" 2>&1 || true
      fi
      for file in k6.json k6-stdout.log k6-warnings.log k6-version.txt k6-summary.json \
        k6-start-time-utc k6-start-time-unix k6-end-time-utc k6-end-time-unix k6-exit-code; do
        if k6_runner_kubectl exec "$name" -c k6 -- cat "/results/$file" \
            > "$output_dir/$file.partial" 2>> "$output_dir/k6-collection-errors.log"; then
          mv "$output_dir/$file.partial" "$output_dir/$file"
        else
          copy_failed=true
        fi
      done
      if [ "$script" = latency-step.js ]; then
        for file in latency-gate-ready latency-launch-at-unix latency-gate-release-unix latency-gate-status; do
          if k6_runner_kubectl exec "$name" -c k6 -- cat "/results/$file" \
              > "$output_dir/$file.partial" 2>> "$output_dir/k6-collection-errors.log"; then
            mv "$output_dir/$file.partial" "$output_dir/$file"
          else
            copy_failed=true
          fi
        done
        if [ "$finished" = false ]; then
          k6_runner_kubectl exec "$name" -c k6 -- cat /results/latency-gate-cancelled \
            > "$output_dir/latency-gate-cancelled" 2>> "$output_dir/k6-collection-errors.log" || true
        fi
        if ! grep -Fxq released "$output_dir/latency-gate-status" 2>/dev/null; then
          copy_failed=true
        fi
      fi
      if [ "$copy_failed" = false ] && \
          k6_runner_validate_artifacts "$output_dir" 2>> "$output_dir/k6-collection-errors.log"; then
        collected=true
      fi
      if [ "$collected" = true ]; then
        k6_runner_kubectl delete pod "$name" --wait=false --ignore-not-found=true \
          >> "$output_dir/k6-collection-errors.log" 2>&1 || status=3
      else
        # Preserve emptyDir for manual recovery. The Pod deadline bounds its
        # lifetime; no automatic rerun is permitted after collection failure.
        echo "ERROR: partial artifacts retained in $output_dir; recover pod/$name before deleting it and configmap/$name" >&2
        status=3
        failure_reason="artifact collection incomplete or validation failed; runner resources retained"
      fi
    fi
    if [ "$created_cm" = true ] && { [ "$created_pod" = false ] || [ "$collected" = true ]; }; then
      k6_runner_kubectl delete configmap "$name" --wait=false --ignore-not-found=true \
        >> "$output_dir/k6-collection-errors.log" 2>&1 || status=3
    fi
    local state=failed
    if [ "$status" -eq 0 ]; then state=success; failure_reason=""; fi
    jq --arg state "$state" --arg reason "$failure_reason" --argjson code "$run_code" \
      '.status=$state | .failure_reason=$reason | .k6_exit_code=$code' \
      "$output_dir/k6-runner.json" > "$output_dir/k6-runner.json.tmp" && \
      mv "$output_dir/k6-runner.json.tmp" "$output_dir/k6-runner.json"
    exit "$status"
  }
  trap finish_runner EXIT
  trap 'failure_reason="runner interrupted"; exit 130' INT
  trap 'failure_reason="runner terminated"; exit 143' TERM

  # create (never apply) ensures an existing object cannot become ours. Flags
  # are set only after success, so cleanup cannot delete a colliding object.
  k6_runner_kubectl create -f "$output_dir/k6-configmap.json" >/dev/null || exit 1
  created_cm=true
  k6_runner_kubectl create -f "$output_dir/k6-pod.json" >/dev/null || exit 1
  created_pod=true
  failure_reason="runner did not become ready"
  k6_runner_kubectl wait --for=condition=Ready "pod/$name" \
    --timeout="${K6_RUNNER_STARTUP_TIMEOUT_SECONDS}s" --request-timeout="${K6_RUNNER_STARTUP_TIMEOUT_SECONDS}s" || exit 2

  local started=$SECONDS marker phase
  failure_reason="runner exceeded ${K6_RUNNER_TIMEOUT_SECONDS}s timeout"
  while [ "$((SECONDS - started))" -lt "$K6_RUNNER_TIMEOUT_SECONDS" ]; do
    marker=$(k6_runner_kubectl exec "$name" -c k6 -- sh -c \
      'if [ -f /results/k6-exit-code ]; then cat /results/k6-exit-code; fi' 2>> "$output_dir/k6-collection-errors.log") || marker=""
    if [[ "$marker" =~ ^[0-9]+$ ]]; then
      run_code="$marker"
      finished=true
      if [ "$run_code" -eq 0 ]; then exit 0; fi
      failure_reason="k6 exited with status $run_code"
      exit 2
    fi
    phase=$(k6_runner_kubectl get pod "$name" -o jsonpath='{.status.phase}') || exit 2
    if [ "$phase" = Failed ] || [ "$phase" = Succeeded ]; then
      failure_reason="runner Pod terminated before artifact collection"
      exit 2
    fi
    sleep 2
  done
  exit 2
)
