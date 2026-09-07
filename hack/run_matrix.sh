#!/usr/bin/env bash
# run_matrix.sh — stabilization-window ablation matrix automation wrapper.
#
# Drives hack/run_benchmark.sh through a configurable matrix; the default is
# 27 experiments (3 patterns × 3 controllers × 3 repeats). Controller order rotates on each
# repeat to reduce run-order bias. After each successful
# experiment, immediately runs hack/analyze/extract.py so extract.json is
# always up to date.
#
# Behavior:
# - Fail-fast: any failed experiment (run_benchmark.sh exits non-zero)
#   aborts the matrix immediately.
# - Resumable: at startup, scans only $EXPERIMENTS_ROOT for successful runs
#   matching <pattern>_<controller>_r<idx> AND the effective source/configuration
#   identity and skips them. Re-running the
#   wrapper after a partial completion picks up where it left off.
# - A controlled run is complete only when its extraction validates. Extraction
#   failures stop the matrix and remain pending on resume, with evidence kept.
# - AGGREGATE_REPORT.md is NOT regenerated per-run. Run hack/analyze/aggregate.py
#   manually when you want a fresh report.
#
# Usage:
#   hack/run_matrix.sh           # run all remaining experiments
#   hack/run_matrix.sh --dry-run # show plan, don't run anything
#   EXPERIMENTS_ROOT=experiments/service-routing-v1 hack/run_matrix.sh --dry-run
#   RPS=25 BENCHMARK_PATTERNS=step BENCHMARK_CONTROLLERS='native_hpa_60 phpa' \
#     BENCHMARK_REPEATS=1 EXPERIMENTS_ROOT=experiments/pilot hack/run_matrix.sh --dry-run
#
# Manual interruption:
#   Ctrl+C once: wrapper exits AFTER current experiment finishes
#   Ctrl+C twice: hard abort (current experiment artifacts may be incomplete)

set -euo pipefail

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
source "$REPO_ROOT/hack/lib/benchmark_config.sh"
benchmark_config_init
k6_runner_validate_config
benchmark_config_fingerprint
EXPERIMENTS_DIR="$EXPERIMENTS_ROOT"
RUN_BENCHMARK="$REPO_ROOT/hack/run_benchmark.sh"
EXTRACT_PY="$REPO_ROOT/hack/analyze/extract.py"
EXTRACT_VENV="$REPO_ROOT/hack/analyze/.venv/bin/python"
if [ -x "$EXTRACT_VENV" ]; then
  BENCHMARK_PYTHON="${BENCHMARK_PYTHON:-$EXTRACT_VENV}"
else
  BENCHMARK_PYTHON="${BENCHMARK_PYTHON:-${PYTHON:-python3}}"
fi

# === Argument parsing ===
DRY_RUN=false
case "${1:-}" in
  --dry-run) DRY_RUN=true ;;
  "") ;;
  *) echo "USAGE: $0 [--dry-run]" >&2; exit 1 ;;
esac
[ $# -le 1 ] || { echo "USAGE: $0 [--dry-run]" >&2; exit 1; }

# === Matrix definition ===
# Rotate the selected controller order on each repeat, including subsets.
MATRIX=()
for pattern in "${BENCHMARK_PATTERN_VALUES[@]}"; do
  for ((repeat_idx=1; repeat_idx<=BENCHMARK_REPEATS; repeat_idx++)); do
    rotation=$((repeat_idx - 1))
    for ((offset=0; offset<${#BENCHMARK_CONTROLLER_VALUES[@]}; offset++)); do
      controller_idx=$(((rotation + offset) % ${#BENCHMARK_CONTROLLER_VALUES[@]}))
      MATRIX+=("${pattern}:${BENCHMARK_CONTROLLER_VALUES[controller_idx]}:${repeat_idx}")
    done
  done
done
TOTAL="${#MATRIX[@]}"

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
  local require_extraction="${4:-true}"
  local decision_mode
  decision_mode=$(benchmark_decision_mode "$controller")
  local matches
  if [ ! -d "$EXPERIMENTS_DIR" ]; then
    return 1
  fi
  matches=$(find "$EXPERIMENTS_DIR" -maxdepth 1 -type d \
    -name "*_${pattern}_${controller}_r${idx}" \
    ! -name "_INCOMPLETE_*" 2>/dev/null | LC_ALL=C sort -r)
  if [ -z "$matches" ]; then
    return 1
  fi
  # Missing identity fields deliberately reject older successes, including the
  # old default 25 RPS runs whose VU settings and measurement protocol differed.
  while IFS= read -r dir; do
    if [ -f "$dir/metadata.yaml" ] && \
       grep -q '^  status: success[[:space:]]*$' "$dir/metadata.yaml" 2>/dev/null &&
       grep -Fxq "campaign: \"$CAMPAIGN\"" "$dir/metadata.yaml" &&
       grep -Fxq "traffic_path: \"$K6_TRAFFIC_PATH\"" "$dir/metadata.yaml" &&
       grep -Fxq "load_generator: \"$K6_EXECUTION_MODE\"" "$dir/metadata.yaml" &&
       grep -Fxq "endpoint: \"$K6_BASE_URL\"" "$dir/metadata.yaml" &&
       grep -Fxq "k6_image: \"$K6_IMAGE\"" "$dir/metadata.yaml" &&
       grep -Fxq "protocol_version: \"$BENCHMARK_PROTOCOL_VERSION\"" "$dir/metadata.yaml" &&
       grep -Fxq "rps: $RPS" "$dir/metadata.yaml" &&
       grep -Fxq "benchmark_source_sha256: \"$BENCHMARK_SOURCE_SHA256\"" "$dir/metadata.yaml" &&
       grep -Fxq "benchmark_config_sha256: \"$BENCHMARK_CONFIG_SHA256\"" "$dir/metadata.yaml" &&
       grep -Fxq "pre_allocated_vus: $BENCHMARK_PRE_ALLOCATED_VUS" "$dir/metadata.yaml" &&
       grep -Fxq "max_vus: $BENCHMARK_MAX_VUS" "$dir/metadata.yaml" &&
       grep -Fxq "decision_mode: \"$decision_mode\"" "$dir/metadata.yaml" &&
       grep -Fxq 'connection_reuse: false' "$dir/metadata.yaml"; then
      if [ "$require_extraction" = false ] || validate_extraction "$dir"; then
        echo "$dir"
        return 0
      fi
    fi
  done <<<"$matches"
  return 1
}

validate_extraction() {
  "$BENCHMARK_PYTHON" - "$REPO_ROOT/hack/analyze" "$1" <<'PY'
import json
from pathlib import Path
import sys
import yaml
sys.path.insert(0, sys.argv[1])
from aggregate import validate_pilot_run
directory = Path(sys.argv[2])
try:
    metadata = yaml.safe_load((directory / 'metadata.yaml').read_text())
    extracted = json.loads((directory / 'extract.json').read_text())
    validate_pilot_run(metadata, extracted, directory.name)
except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError) as error:
    print(f'Pending controlled measurement in {directory.name}: {error}', file=sys.stderr)
    sys.exit(1)
PY
}

# Preserve failed artifacts, but never count invalid measurement as completion.
run_extract() {
  local exp_dir="$1"
  if "$BENCHMARK_PYTHON" "$EXTRACT_PY" "$exp_dir" 2>&1 | sed 's/^/[wrapper]   /' &&
      validate_extraction "$exp_dir"; then
    return 0
  fi
  # Overall outcome includes measurement validity. Keep the raw artifacts while
  # excluding this failed attempt from aggregation after a successful rerun.
  sed -i 's|^  status: success|  status: failed|' "$exp_dir/metadata.yaml"
  sed -i 's|^  failure_reason: ""|  failure_reason: "controlled extraction failed; raw collection retained"|' "$exp_dir/metadata.yaml"
  echo "[wrapper]   ERROR: controlled extraction failed; evidence retained in $exp_dir" >&2
  return 1
}

# === Pre-flight: print plan, count skips ===
echo "=== Matrix wrapper: $TOTAL experiments planned ==="
echo "Experiments root: $EXPERIMENTS_DIR"
echo "Protocol: $BENCHMARK_PROTOCOL_VERSION"
echo "Offered RPS: $RPS"
echo "Virtual users: $BENCHMARK_PRE_ALLOCATED_VUS preallocated, $BENCHMARK_MAX_VUS maximum"
echo "Source SHA256: $BENCHMARK_SOURCE_SHA256"
echo "Configuration SHA256: $BENCHMARK_CONFIG_SHA256"
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

# Include each selected pattern's schedule, tail and approximate setup time.
EST_SECONDS=0
for i in "${!MATRIX[@]}"; do
  [ "${PLAN_STATUS[i]}" = PENDING ] || continue
  case "${MATRIX[i]%%:*}" in
    step) EST_SECONDS=$((EST_SECONDS + 611)) ;;
    ramp) EST_SECONDS=$((EST_SECONDS + 670)) ;;
    spike) EST_SECONDS=$((EST_SECONDS + 641)) ;;
  esac
done
EST_MIN=$(( (EST_SECONDS + 59) / 60 ))
echo "Estimated runtime for pending: ~${EST_MIN} minutes."

if [ "$DRY_RUN" = true ]; then
  echo ""
  echo "Dry-run mode: not executing. Exiting."
  exit 0
fi

# === Pre-flight checks before launching first experiment ===
echo ""
echo "=== Pre-flight check ==="
"$BENCHMARK_PYTHON" -c 'import yaml' || {
  echo "ERROR: BENCHMARK_PYTHON must identify Python with PyYAML installed" >&2
  exit 1
}
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
    # Locate the just-created experiment directory.
    new_dir=$(already_succeeded "$pattern" "$controller" "$idx" false || true)
    if [ -n "$new_dir" ]; then
      echo "[wrapper] running extract.py on $new_dir"
      if ! run_extract "$new_dir"; then
        FAILED="$pattern $controller r$idx (controlled extraction failed)"
        break
      fi
      COMPLETED=$((COMPLETED + 1))
    else
      FAILED="$pattern $controller r$idx (completed collection directory missing)"
      break
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
