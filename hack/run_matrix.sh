#!/usr/bin/env bash
# run_matrix.sh — stabilization-window ablation matrix automation wrapper.
#
# Drives hack/run_benchmark.sh through the 27-experiment matrix
# (3 patterns × 3 controllers × 3 repeats). Controller order rotates on each
# repeat to reduce run-order bias. After each successful
# experiment, immediately runs hack/analyze/extract.py so extract.json is
# always up to date.
#
# Behavior:
# - Fail-fast: any failed experiment (run_benchmark.sh exits non-zero)
#   aborts the matrix immediately.
# - Resumable: at startup, scans only $EXPERIMENTS_ROOT for successful runs
#   matching <pattern>_<controller>_r<idx> and skips them. Re-running the
#   wrapper after a partial completion picks up where it left off.
# - Extract is best-effort: if extract.py fails for one experiment, the
#   matrix continues but logs a warning. Run extract.py manually later.
# - AGGREGATE_REPORT.md is NOT regenerated per-run. Run hack/analyze/aggregate.py
#   manually when you want a fresh report.
#
# Usage:
#   hack/run_matrix.sh           # run all remaining experiments
#   hack/run_matrix.sh --dry-run # show plan, don't run anything
#   EXPERIMENTS_ROOT=experiments/service-routing-v1 hack/run_matrix.sh --dry-run
#
# Manual interruption:
#   Ctrl+C once: wrapper exits AFTER current experiment finishes
#   Ctrl+C twice: hard abort (current experiment artifacts may be incomplete)

set -euo pipefail

# === Matrix definition ===
# Format: <pattern>:<controller>:<repeat_idx>
# Latin rotation per repeat:
#   r1: native_hpa_300, native_hpa_60, phpa
#   r2: native_hpa_60, phpa, native_hpa_300
#   r3: phpa, native_hpa_300, native_hpa_60
PATTERNS=(step ramp spike)
CONTROLLERS=(native_hpa_300 native_hpa_60 phpa)
MATRIX=()
for pattern in "${PATTERNS[@]}"; do
  for repeat_idx in 1 2 3; do
    rotation=$((repeat_idx - 1))
    for offset in 0 1 2; do
      controller_idx=$(((rotation + offset) % ${#CONTROLLERS[@]}))
      MATRIX+=("${pattern}:${CONTROLLERS[controller_idx]}:${repeat_idx}")
    done
  done
done
TOTAL="${#MATRIX[@]}"

# === Configuration ===
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXPERIMENTS_ROOT="${EXPERIMENTS_ROOT:-experiments/service-routing-v1}"
case "$EXPERIMENTS_ROOT" in
  /*) ;;
  *) EXPERIMENTS_ROOT="$REPO_ROOT/$EXPERIMENTS_ROOT" ;;
esac
export EXPERIMENTS_ROOT
CAMPAIGN="${CAMPAIGN:-service-routing-v1}"
export CAMPAIGN
source "$REPO_ROOT/hack/lib/k6_runner.sh"
k6_runner_validate_config
EXPERIMENTS_DIR="$EXPERIMENTS_ROOT"
RUN_BENCHMARK="$REPO_ROOT/hack/run_benchmark.sh"
EXTRACT_PY="$REPO_ROOT/hack/analyze/extract.py"
EXTRACT_VENV="$REPO_ROOT/hack/analyze/.venv/bin/python"

# === Argument parsing ===
DRY_RUN=false
case "${1:-}" in
  --dry-run) DRY_RUN=true ;;
  "") ;;
  *) echo "USAGE: $0 [--dry-run]" >&2; exit 1 ;;
esac
[ $# -le 1 ] || { echo "USAGE: $0 [--dry-run]" >&2; exit 1; }

# === Helpers ===

# Soft Ctrl+C: after current experiment, exit.
INTERRUPTED=false
on_interrupt() {
  echo ""
  echo "[wrapper] Ctrl+C received. Will exit after current experiment finishes."
  echo "[wrapper] Press Ctrl+C again to hard-abort (current artifacts may be incomplete)."
  INTERRUPTED=true
  trap - INT
}
trap on_interrupt INT

# Check if an experiment with the given (pattern, controller, repeat) already
# has a successful run on disk. Returns 0 (success/found) or 1 (not found).
already_succeeded() {
  local pattern="$1" controller="$2" idx="$3"
  local matches
  if [ ! -d "$EXPERIMENTS_DIR" ]; then
    return 1
  fi
  matches=$(find "$EXPERIMENTS_DIR" -maxdepth 1 -type d \
    -name "*_${pattern}_${controller}_r${idx}" \
    ! -name "_INCOMPLETE_*" 2>/dev/null)
  if [ -z "$matches" ]; then
    return 1
  fi
  # Resume only compatible Service-path evidence from this campaign and image.
  # Missing fields deliberately reject archived v2 successes even in this root.
  while IFS= read -r dir; do
    if [ -f "$dir/metadata.yaml" ] && \
       grep -q '^  status: success[[:space:]]*$' "$dir/metadata.yaml" 2>/dev/null &&
       grep -Fxq "campaign: \"$CAMPAIGN\"" "$dir/metadata.yaml" &&
       grep -Fxq "traffic_path: \"$K6_TRAFFIC_PATH\"" "$dir/metadata.yaml" &&
       grep -Fxq "load_generator: \"$K6_EXECUTION_MODE\"" "$dir/metadata.yaml" &&
       grep -Fxq "endpoint: \"$K6_BASE_URL\"" "$dir/metadata.yaml" &&
       grep -Fxq "k6_image: \"$K6_IMAGE\"" "$dir/metadata.yaml" &&
       grep -Fxq 'connection_reuse: false' "$dir/metadata.yaml"; then
      echo "$dir"
      return 0
    fi
  done <<<"$matches"
  return 1
}

# Run extract.py on a successful experiment directory. Best-effort:
# logs warnings but never aborts the matrix.
run_extract() {
  local exp_dir="$1"
  if [ ! -x "$EXTRACT_VENV" ]; then
    echo "[wrapper]   WARN: venv python not found at $EXTRACT_VENV; skipping extract"
    return 0
  fi
  if [ ! -f "$EXTRACT_PY" ]; then
    echo "[wrapper]   WARN: extract.py not found at $EXTRACT_PY; skipping extract"
    return 0
  fi
  if "$EXTRACT_VENV" "$EXTRACT_PY" "$exp_dir" 2>&1 | sed 's/^/[wrapper]   /'; then
    return 0
  else
    echo "[wrapper]   WARN: extract.py failed for $exp_dir (matrix continues)"
    return 0
  fi
}

# === Pre-flight: print plan, count skips ===
echo "=== Matrix wrapper: $TOTAL experiments planned ==="
echo "Experiments root: $EXPERIMENTS_DIR"
echo ""
SKIP_COUNT=0
PENDING_COUNT=0
declare -a PLAN_STATUS
for i in "${!MATRIX[@]}"; do
  IFS=":" read -r pattern controller idx <<<"${MATRIX[i]}"
  num=$((i + 1))
  if existing=$(already_succeeded "$pattern" "$controller" "$idx"); then
    printf "[%2d/%d] %-6s %-14s r%-2d  SKIP (already in %s)\n" \
      "$num" "$TOTAL" "$pattern" "$controller" "$idx" "$(basename "$existing")"
    PLAN_STATUS[i]="SKIP"
    SKIP_COUNT=$((SKIP_COUNT + 1))
  else
    printf "[%2d/%d] %-6s %-14s r%-2d  PENDING\n" \
      "$num" "$TOTAL" "$pattern" "$controller" "$idx"
    PLAN_STATUS[i]="PENDING"
    PENDING_COUNT=$((PENDING_COUNT + 1))
  fi
done
echo ""
echo "Summary: $SKIP_COUNT already done, $PENDING_COUNT pending."

if [ "$PENDING_COUNT" -eq 0 ]; then
  echo "Matrix already complete. Nothing to do."
  exit 0
fi

# Time estimate: ~613s per experiment (k6 211s + tail 360s + setup ~40s).
EST_MIN=$(( PENDING_COUNT * 613 / 60 ))
echo "Estimated runtime for pending: ~${EST_MIN} minutes."

if [ "$DRY_RUN" = true ]; then
  echo ""
  echo "Dry-run mode: not executing. Exiting."
  exit 0
fi

# === Pre-flight checks before launching first experiment ===
echo ""
echo "=== Pre-flight check ==="
mkdir -p "$EXPERIMENTS_DIR"
if [ ! -x "$RUN_BENCHMARK" ]; then
  echo "ERROR: $RUN_BENCHMARK not executable" >&2
  exit 1
fi
if "$REPO_ROOT/hack/prerequisites_check.sh" >/dev/null 2>&1; then
  echo "  prerequisites OK"
else
  echo "ERROR: prerequisites_check.sh failed. Run it manually to diagnose." >&2
  exit 1
fi

# Confirm before starting the long run.
echo ""
echo "Starting matrix in 5 seconds... (Ctrl+C now to abort)"
sleep 5
echo ""

# === Run the matrix ===
COMPLETED=0
FAILED=""
START_EPOCH=$(date +%s)

for i in "${!MATRIX[@]}"; do
  IFS=":" read -r pattern controller idx <<<"${MATRIX[i]}"
  num=$((i + 1))

  if [ "${PLAN_STATUS[i]}" = "SKIP" ]; then
    continue
  fi

  echo ""
  echo "============================================================"
  echo "[$num/$TOTAL] $pattern $controller r$idx — starting"
  echo "============================================================"

  if "$RUN_BENCHMARK" "$pattern" "$controller" "$idx"; then
    COMPLETED=$((COMPLETED + 1))
    # Locate the just-created experiment directory.
    new_dir=$(already_succeeded "$pattern" "$controller" "$idx" || true)
    if [ -n "$new_dir" ]; then
      echo "[wrapper] running extract.py on $new_dir"
      run_extract "$new_dir"
    else
      echo "[wrapper]   WARN: could not locate experiment directory after run; skipping extract"
    fi
  else
    FAILED="$pattern $controller r$idx"
    echo ""
    echo "============================================================"
    echo "[$num/$TOTAL] FAILED: $FAILED"
    echo "============================================================"
    break
  fi

  if [ "$INTERRUPTED" = true ]; then
    echo ""
    echo "[wrapper] Soft interrupt acknowledged. Stopping after $num/$TOTAL."
    break
  fi
done

END_EPOCH=$(date +%s)
ELAPSED_MIN=$(( (END_EPOCH - START_EPOCH) / 60 ))

# === Summary ===
echo ""
echo "============================================================"
echo "Matrix summary"
echo "============================================================"
echo "Completed this session: $COMPLETED"
echo "Skipped (already done): $SKIP_COUNT"
echo "Elapsed: ${ELAPSED_MIN} min"
if [ -n "$FAILED" ]; then
  echo "FAILED at: $FAILED"
  echo ""
  echo "After fixing the underlying issue, re-run hack/run_matrix.sh to"
  echo "continue from where the matrix stopped."
  exit 2
fi

if [ "$INTERRUPTED" = true ]; then
  echo ""
  echo "Interrupted. Re-run hack/run_matrix.sh to continue."
  exit 0
fi

echo ""
echo "All planned experiments complete."
echo "Next step: regenerate the aggregate report:"
echo "  cd hack/analyze && source .venv/bin/activate"
echo "  python aggregate.py \"$EXPERIMENTS_DIR\""
