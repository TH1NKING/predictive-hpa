#!/usr/bin/env bash
# hack/run_calibration.sh
#
# Phase 3.2 calibration: determine TARGET_RPS for the production benchmark.
#
# Approach: fix Deployment at 5 replicas (bypassing any HPA), drive 4 RPS
# levels, measure steady-state CPU%, derive RPS-to-CPU coefficient,
# back-calculate the RPS that drives maxReplicas=10 + target=50%.
#
# Note: this script does NOT kill the controller. PHPA sample is deleted
# during calibration so the controller's Reconcile loop becomes a no-op
# (IsNotFound -> return). Restored at the end.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

PHPA_SAMPLE="config/samples/autoscaling_v1alpha1_predictivehpa.yaml"
PROM_URL="http://localhost:9090"
CALIBRATION_DIR="experiments/calibration"
FIXED_REPLICAS=5
PROBE_DURATION_SECONDS=90
RPS_LEVELS=(5 15 30 45)

hack/prerequisites_check.sh

mkdir -p "$CALIBRATION_DIR"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RESULTS_MD="$CALIBRATION_DIR/calibration_${TIMESTAMP}.md"

echo "[setup] disabling autoscaling, fixing replicas=$FIXED_REPLICAS"
kubectl delete hpa php-apache --ignore-not-found=true >/dev/null
kubectl delete -f "$PHPA_SAMPLE" --ignore-not-found=true >/dev/null
kubectl scale deploy php-apache --replicas=$FIXED_REPLICAS >/dev/null
kubectl rollout status deploy/php-apache --timeout=120s >/dev/null
echo "[setup] Deployment stable at $FIXED_REPLICAS replicas"
sleep 30

# === Calibration markdown header ===
cat > "$RESULTS_MD" <<MD
# Phase 3.2 Calibration Results

- **Timestamp**: $(date -u +%Y-%m-%dT%H:%M:%SZ)
- **Fixed replicas**: $FIXED_REPLICAS (autoscaling disabled)
- **Per-Pod CPU request**: 200m
- **Total CPU capacity at fixed setup**: $((FIXED_REPLICAS * 200))m (= $(echo "scale=2; $FIXED_REPLICAS * 200 / 1000" | bc) cores)
- **Probe duration per RPS level**: ${PROBE_DURATION_SECONDS}s
- **Target HPA configuration to be calibrated against**: maxReplicas=10, targetCPUUtilizationPercentage=50

## Probe Results

| RPS | Steady CPU% | p50 latency | p95 latency | http_req_failed | dropped iterations |
|---|---|---|---|---|---|
MD

# === Run probes ===
for RPS in "${RPS_LEVELS[@]}"; do
  echo ""
  echo "[probe] RPS=$RPS for ${PROBE_DURATION_SECONDS}s"

  # Inline k6 script
  K6_TMP=$(mktemp /tmp/k6-calibrate-XXXXXX.js)
  cat > "$K6_TMP" <<JS
import http from 'k6/http';
export const options = {
  scenarios: {
    probe: {
      executor: 'constant-arrival-rate',
      rate: $RPS, timeUnit: '1s',
      duration: '${PROBE_DURATION_SECONDS}s',
      preAllocatedVUs: $((RPS * 2 + 10)),
      maxVUs: $((RPS * 4 + 20)),
    },
  },
};
export default function () { http.get('http://localhost:8080/', { timeout: '10s' }); }
JS

  START=$(date +%s)
  K6_OUT="$CALIBRATION_DIR/probe_${RPS}rps_${TIMESTAMP}.json"
  k6 run --out json="$K6_OUT" --quiet "$K6_TMP" > "$CALIBRATION_DIR/probe_${RPS}rps_${TIMESTAMP}.txt" 2>&1 || true
  END=$(date +%s)
  rm -f "$K6_TMP"

  # Use the last 60s of the probe for steady-state measurement (skip ramp-up)
  STEADY_START=$((END - 60))

  CPU_QUERY='(avg(rate(container_cpu_usage_seconds_total{namespace="default",pod=~"php-apache-.*",container!=""}[1m]))/avg(kube_pod_container_resource_requests{namespace="default",pod=~"php-apache-.*",resource="cpu"}))*100'
  CPU_ENCODED=$(printf '%s' "$CPU_QUERY" | jq -sRr @uri)
  CPU_RESULT=$(curl -sf --max-time 10 \
    "$PROM_URL/api/v1/query_range?query=${CPU_ENCODED}&start=${STEADY_START}&end=${END}&step=15s")
  CPU_AVG=$(echo "$CPU_RESULT" | jq -r \
    '[.data.result[0].values[]? | .[1] | tonumber] | add / length' 2>/dev/null || echo "N/A")

  # Parse k6 text output
  P50=$(grep -oP 'http_req_duration.*?med=\K[^\s]+' "$CALIBRATION_DIR/probe_${RPS}rps_${TIMESTAMP}.txt" | head -1 || echo "N/A")
  P95=$(grep -oP 'p\(95\)=\K[^\s]+' "$CALIBRATION_DIR/probe_${RPS}rps_${TIMESTAMP}.txt" | head -1 || echo "N/A")
  FAILED=$(grep -oP 'http_req_failed.*?:\s*\K[0-9.]+%' "$CALIBRATION_DIR/probe_${RPS}rps_${TIMESTAMP}.txt" | head -1 || echo "N/A")
  DROPPED=$(grep -oP 'dropped_iterations.*?:\s*\K[0-9]+' "$CALIBRATION_DIR/probe_${RPS}rps_${TIMESTAMP}.txt" | head -1 || echo "0")

  printf "| %d | %.2f | %s | %s | %s | %s |\n" "$RPS" "$CPU_AVG" "$P50" "$P95" "$FAILED" "$DROPPED" >> "$RESULTS_MD"
  echo "  CPU%=$CPU_AVG  p50=$P50  p95=$P95  failed=$FAILED  dropped=$DROPPED"

  # Cool-down between probes
  sleep 20
done

# === Footer / analysis hints ===
cat >> "$RESULTS_MD" <<MD

## Analysis

1. Plot RPS vs Steady CPU%.  Linear region's slope is the **per-RPS CPU coefficient** \`k\` (% per RPS at this fixed-replica setup).
2. Scale \`k\` to per-Pod-per-RPS:  \`k_pod = k * $FIXED_REPLICAS\` (% per RPS per Pod)
3. Target operating point: maxReplicas=10, target=50% → desired CPU per Pod = 50%
4. Per-Pod RPS capacity at 50%: \`RPS_per_pod = 50 / k_pod\`
5. **Final TARGET_RPS** = \`RPS_per_pod * 10\` → update \`hack/k6/lib/common.js\`

## Sanity checks

- Reject any RPS level where \`http_req_failed > 1%\` or \`dropped_iterations > 10\` (Pods are saturated; CPU% no longer linear with RPS)
- Keep at most 2-3 lowest-RPS unsaturated points for the linear regression
MD

# === Restore ===
echo ""
echo "[teardown] restoring PHPA sample"
kubectl apply -f "$PHPA_SAMPLE" >/dev/null
echo ""
echo "=== calibration done ==="
echo "Results:   $RESULTS_MD"
echo "Raw probes: $CALIBRATION_DIR/probe_*rps_${TIMESTAMP}.{json,txt}"
