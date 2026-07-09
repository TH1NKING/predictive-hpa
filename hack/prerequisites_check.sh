#!/usr/bin/env bash
# hack/prerequisites_check.sh
#
# 验证 Phase 3 benchmark 环境的基础设施前置条件。
# 任何基础设施检查失败立即 fail-fast 退出（exit 1），便于 run_benchmark.sh
# 用 "prerequisites_check.sh || exit 1" 短路实验流程。
#
# Controller 进程状态作为 info 报告，不影响退出码——它由 run_benchmark.sh
# 根据实验类型（phpa vs native_hpa）负责切换。
#
# 依赖工具:
#   - kubectl (v1.35.x)
#   - curl
#   - pgrep
#   - k6 v1.3.0+ (Phase 3 压测工具，安装方式见 docs/PHASE3_BENCHMARK_DESIGN.md)

set -euo pipefail

EXPECTED_CONTEXT="kind-hpa-dev"
PROMETHEUS_HEALTH_URL="http://localhost:9090/-/healthy"

pass() { echo "  [OK]   $1"; }
fail() { echo "  [FAIL] $1" >&2; exit 1; }

# ============================================================
# 1. kubectl context
# ============================================================
echo "[1/8] kubectl context"
CURRENT_CTX=$(kubectl config current-context 2>/dev/null || echo "")
if [ "$CURRENT_CTX" != "$EXPECTED_CONTEXT" ]; then
  fail "expected context '$EXPECTED_CONTEXT', got '$CURRENT_CTX'"
fi
pass "$CURRENT_CTX"

# ============================================================
# 2. cluster reachable
# ============================================================
echo "[2/8] cluster reachability"
if ! kubectl cluster-info --request-timeout=5s &>/dev/null; then
  fail "kubectl cluster-info failed (cluster unreachable?)"
fi
pass "cluster reachable"

# ============================================================
# 3. monitoring pods Running
# ============================================================
echo "[3/8] monitoring pods running"
TOTAL_PODS=$(kubectl get pods -n monitoring --no-headers 2>/dev/null | wc -l)
RUNNING_PODS=$(kubectl get pods -n monitoring --no-headers 2>/dev/null | awk '$3=="Running"' | wc -l)
if [ "$TOTAL_PODS" -eq 0 ]; then
  fail "no monitoring pods found"
fi
if [ "$TOTAL_PODS" -ne "$RUNNING_PODS" ]; then
  kubectl get pods -n monitoring >&2
  fail "expected $TOTAL_PODS pods Running, got $RUNNING_PODS"
fi
pass "$RUNNING_PODS/$TOTAL_PODS pods Running"

# ============================================================
# 4. PHPA CRD registered
# ============================================================
echo "[4/8] PHPA CRD registered"
if ! kubectl get crd predictivehpas.autoscaling.brian.io &>/dev/null; then
  fail "CRD predictivehpas.autoscaling.brian.io not found (run 'make install')"
fi
pass "CRD registered"

# ============================================================
# 5. port-forward process
# ============================================================
echo "[5/8] port-forward to prometheus"
if ! pgrep -f "port-forward.*prometheus-server" >/dev/null; then
  fail "port-forward not running. Start with:
       nohup kubectl port-forward -n monitoring svc/prometheus-server 9090:80 \\
             --address 0.0.0.0 > /tmp/pf.log 2>&1 & disown"
fi
PF_PID=$(pgrep -f "port-forward.*prometheus-server" | head -1)
pass "port-forward pid=$PF_PID"

# ============================================================
# 6. Prometheus HTTP reachable
# ============================================================
echo "[6/8] Prometheus HTTP reachable"
if ! curl -sf --max-time 5 "$PROMETHEUS_HEALTH_URL" >/dev/null; then
  fail "Prometheus health check failed at $PROMETHEUS_HEALTH_URL"
fi
pass "$PROMETHEUS_HEALTH_URL"

# ============================================================
# 7. k6 in PATH
# ============================================================
echo "[7/8] k6 in PATH"
if ! command -v k6 >/dev/null; then
  fail "k6 not found in PATH (see docs/PHASE3_BENCHMARK_DESIGN.md for install)"
fi
K6_VERSION=$(k6 version | head -1)
pass "$K6_VERSION"

# ============================================================
# 8. php-apache reachable (k6 load target via port-forward :8080)
# ============================================================
echo "[8/8] php-apache reachable (k6 target)"
if ! curl -sf --max-time 5 "http://localhost:8080/" >/dev/null; then
  fail "php-apache not reachable at http://localhost:8080/ (k6 load target).
       Start the port-forward:
       nohup kubectl port-forward svc/php-apache 8080:80 --address 0.0.0.0 > /tmp/pf-apache.log 2>&1 & disown"
fi
pass "http://localhost:8080/ reachable"

# ============================================================
# Informational: controller process state (does not affect exit code)
# ============================================================
echo ""
echo "[info] controller process state"
if pgrep -f "go-build.*predictive-hpa" >/dev/null 2>&1 || \
   pgrep -f "go run.*cmd/main.go" >/dev/null 2>&1; then
  echo "       RUNNING (required for phpa experiments; must be stopped for native_hpa)"
else
  echo "       NOT running (must be started before phpa experiments)"
fi

echo ""
echo "=== prerequisites OK ==="
