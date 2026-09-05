#!/usr/bin/env bash
# Read-only preflight for a dedicated Kind cluster. Benchmark traffic stays
# inside the cluster; only Prometheus needs a host-side forwarding endpoint.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/k6_runner.sh"
PROMETHEUS_HEALTH_URL="http://localhost:9090/-/healthy"
pass() { echo "  [OK]   $1"; }
fail() { echo "  [FAIL] $1" >&2; exit 1; }

echo "[1/7] local tools and pinned runner configuration"
for tool in kubectl kind jq curl; do
  command -v "$tool" >/dev/null || fail "$tool not found in PATH"
done
k6_runner_validate_config || exit 1
pass "$K6_IMAGE (local k6 installation is unnecessary)"

echo "[2/7] explicit dedicated Kind context and cluster reachability"
k6_runner_require_kind_context || exit 1
pass "$BENCHMARK_CONTEXT"

echo "[3/7] monitoring Pods ready"
MONITORING=$(k6_runner_kubectl get pods -n monitoring -o json)
if ! jq -e '.items | length > 0 and all(.[];
  .status.phase == "Running" and
  any(.status.conditions[]?; .type == "Ready" and .status == "True"))'   <<<"$MONITORING" >/dev/null; then
  fail "monitoring Pods are absent or not ready"
fi
pass "monitoring Pods ready"

echo "[4/7] PHPA CRD registered"
k6_runner_kubectl get crd predictivehpas.autoscaling.brian.io >/dev/null ||   fail "CRD predictivehpas.autoscaling.brian.io not found"
pass "CRD registered"

echo "[5/7] Prometheus HTTP reachable"
curl -sf --max-time 5 "$PROMETHEUS_HEALTH_URL" >/dev/null ||   fail "Prometheus health check failed; forward the dedicated cluster's monitoring/prometheus-server to localhost:9090"
pass "$PROMETHEUS_HEALTH_URL"

echo "[6/7] php-apache Service ClusterIP and port"
SERVICE=$(k6_runner_kubectl get service php-apache -o json)
if ! jq -e '.spec.clusterIP != null and .spec.clusterIP != "None" and
  any(.spec.ports[]; .port == 80 and (.protocol // "TCP") == "TCP")' <<<"$SERVICE" >/dev/null; then
  fail "php-apache must expose a ClusterIP Service on TCP port 80"
fi
pass "$K6_BASE_URL"

echo "[7/7] php-apache ready Service endpoints"
ENDPOINTS=$(k6_runner_kubectl get endpointslices -l kubernetes.io/service-name=php-apache -o json)
if ! jq -e 'any(.items[].endpoints[]?;
  .conditions.ready == true and (.addresses | length) > 0)' <<<"$ENDPOINTS" >/dev/null; then
  fail "php-apache has no ready EndpointSlice addresses"
fi
pass "ready endpoints found; DNS and HTTP are checked by the in-cluster probe"
echo "=== prerequisites OK ==="
