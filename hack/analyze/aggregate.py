#!/usr/bin/env python3
"""
Phase 3 cross-experiment aggregator.

Reads all extract.json files under experiments/ (skipping _INCOMPLETE_*) and
produces a markdown comparison report grouping experiments by (pattern, controller),
computing mean ± stdev across repeats, and writing a 5-section narrative report
intended for interview-grade audiences.

Usage:
    python aggregate.py <experiments_root_dir>           # writes experiments/AGGREGATE_REPORT.md
    python aggregate.py <experiments_root_dir> --stdout  # prints to stdout instead

Report sections (in order):
    1. Executive Summary    — one-table verdict + 3-sentence headline
    2. Experiment Setup     — reproduction-required metadata
    3. Per-Pattern Comparison — step / ramp / spike breakdowns
    4. Cross-Pattern Findings — what holds across patterns
    5. Known Limitations    — tail truncation, sampling precision, etc.

Design notes:
- Only includes experiments whose metadata.yaml has result.status=success
  (NOT the metadata embedded in extract.json — that file does not duplicate
  the status field; we re-read metadata.yaml to filter).
- Skips directories matching _INCOMPLETE_* per the experiment archival convention.
- Aggregation: arithmetic mean and population stdev (n-1 divisor for sample stdev
  via statistics.stdev which requires n>=2; for n==1 we report mean only with a
  '(n=1)' suffix).
- Honest reporting: missing metrics print as 'n/a'; warnings from extract.json
  surface in Section 5.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

# Metrics we aggregate. Each entry: (display_name, json_path, format_spec, unit).
# json_path uses dots for nested keys.
METRIC_SPECS: list[tuple[str, str, str, str]] = [
    # k6 business-side
    ("Total requests",                   "k6.total_requests",                ".0f", ""),
    ("Failed rate",                      "k6.failed_rate_pct",               ".2f", "%"),
    ("Dropped iterations",               "k6.dropped_iterations",            ".0f", ""),
    ("p95 latency (all)",                "k6.duration_p95_ms",               ".0f", " ms"),
    ("p95 latency (success only)",       "k6.duration_success_p95_ms",       ".0f", " ms"),
    # Scaling control
    ("First scale-up delay",             "scaling.first_scaleup_rel_s",      ".0f", " s"),
    ("Convergence time (to peak)",       "scaling.convergence_rel_s",        ".0f", " s"),
    ("Peak / steady-state replicas",     "scaling.steady_state_replicas",    ".1f", ""),
    ("First scale-down (after k6 stop)", "scaling.first_scaledown_rel_s_after_k6_stop", ".0f", " s"),
    ("Full scale-down (after k6 stop)",  "scaling.full_scaledown_rel_s_after_k6_stop",  ".0f", " s"),
    # Resource efficiency
    ("Pod-seconds (experiment total)",   "resource.pod_seconds_during_experiment",      ".0f", ""),
    ("Avg replicas",                     "resource.avg_replicas",            ".2f", ""),
    ("Waste window (replicas > 1 after k6 stop)", "resource.waste_window_s", ".0f", " s"),
]

# PHPA-specific metrics (only reported in phpa groups).
PHPA_METRIC_SPECS: list[tuple[str, str, str, str]] = [
    ("Total reconciles",         "phpa.total_reconciles",         ".0f", ""),
    ("Scaled=true count",        "phpa.scaled_true_count",        ".0f", ""),
    ("Stabilized=true count",    "phpa.stabilized_true_count",    ".0f", ""),
]


def get_nested(obj: dict, path: str) -> Any:
    """Walk a dotted path in a nested dict. Returns None on any miss."""
    cur: Any = obj
    for key in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
        if cur is None:
            return None
    return cur


def fmt_value(value: Any, fmt: str, unit: str) -> str:
    """Format a single value with the spec; returns 'n/a' for None."""
    if value is None:
        return "n/a"
    try:
        return format(float(value), fmt) + unit
    except (ValueError, TypeError):
        return "n/a"


def fmt_mean_stdev(values: list[float | None], fmt: str, unit: str) -> str:
    """Format mean ± stdev (or just mean + '(n=1)' for single-sample groups).

    None values are filtered out. Returns 'n/a' if no valid values remain.
    """
    clean = [v for v in values if v is not None]
    if not clean:
        return "n/a"
    if len(clean) == 1:
        return fmt_value(clean[0], fmt, unit) + " (n=1)"
    m = statistics.mean(clean)
    s = statistics.stdev(clean)
    return f"{format(m, fmt)} ± {format(s, fmt)}{unit}"


def load_extracts(root: Path) -> tuple[list[dict], list[str]]:
    """Discover and load all valid extract.json files under root.

    Filtering rules:
    - Skip directories matching _INCOMPLETE_* (per archival convention).
    - Require metadata.yaml present with result.status=success.
    - Require extract.json present and JSON-parseable.

    Returns (extracts, skip_messages) where extracts is the loaded list and
    skip_messages explains every skipped directory for the limitations section.
    """
    extracts: list[dict] = []
    skip_messages: list[str] = []

    for exp_dir in sorted(root.iterdir()):
        if not exp_dir.is_dir():
            continue
        if exp_dir.name.startswith("_INCOMPLETE_"):
            skip_messages.append(f"skipped {exp_dir.name}: marked incomplete")
            continue
        if exp_dir.name in ("calibration",):
            # Calibration probes live under experiments/ but are not matrix runs.
            continue

        meta_path = exp_dir / "metadata.yaml"
        extract_path = exp_dir / "extract.json"

        if not meta_path.exists():
            skip_messages.append(f"skipped {exp_dir.name}: no metadata.yaml")
            continue
        if not extract_path.exists():
            skip_messages.append(f"skipped {exp_dir.name}: no extract.json (re-run extract.py?)")
            continue

        try:
            with meta_path.open() as f:
                meta = yaml.safe_load(f)
        except yaml.YAMLError as e:
            skip_messages.append(f"skipped {exp_dir.name}: metadata.yaml parse error ({e})")
            continue

        status = (meta.get("result", {}) or {}).get("status")
        if status != "success":
            skip_messages.append(
                f"skipped {exp_dir.name}: metadata status={status!r} (not 'success')"
            )
            continue

        try:
            with extract_path.open() as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            skip_messages.append(f"skipped {exp_dir.name}: extract.json parse error ({e})")
            continue

        extracts.append(data)

    return extracts, skip_messages


def group_by_pattern_controller(
    extracts: list[dict],
) -> dict[tuple[str, str], list[dict]]:
    """Group extracts by (pattern, controller) tuple."""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for e in extracts:
        key = (e.get("pattern") or "?", e.get("controller") or "?")
        groups[key].append(e)
    # Sort each group by repeat for stable output.
    for key in groups:
        groups[key].sort(key=lambda x: (x.get("repeat") or 0))
    return groups


def make_metric_row(
    metric: tuple[str, str, str, str],
    extracts: list[dict],
) -> tuple[str, str]:
    """Compute the (mean ± stdev) string for one metric across one group."""
    name, path, fmt, unit = metric
    values = [get_nested(e, path) for e in extracts]
    # Coerce to float where possible.
    coerced: list[float | None] = []
    for v in values:
        if v is None:
            coerced.append(None)
        else:
            try:
                coerced.append(float(v))
            except (ValueError, TypeError):
                coerced.append(None)
    return name, fmt_mean_stdev(coerced, fmt, unit)


def compute_delta(phpa_vals: list[float | None], native_vals: list[float | None]) -> str:
    """Compute a human-readable delta of two mean values.

    Returns 'n/a' if either side missing. Otherwise:
    - Absolute delta with sign
    - Percentage delta (PHPA relative to native HPA baseline) when meaningful
    """
    p_clean = [v for v in phpa_vals if v is not None]
    n_clean = [v for v in native_vals if v is not None]
    if not p_clean or not n_clean:
        return "n/a"
    p_mean = statistics.mean(p_clean)
    n_mean = statistics.mean(n_clean)
    delta = p_mean - n_mean
    sign = "+" if delta >= 0 else ""
    if abs(n_mean) < 1e-9:
        return f"{sign}{delta:.1f} (baseline ~0)"
    pct = (delta / n_mean) * 100
    return f"{sign}{delta:.1f} ({sign}{pct:.0f}%)"


def render_executive_summary(
    groups: dict[tuple[str, str], list[dict]],
) -> str:
    """One headline table + 3-sentence verdict.

    For each pattern, contrast a small set of headline metrics between phpa and
    native_hpa groups.
    """
    headline_metrics = [
        ("First scale-up delay",             "scaling.first_scaleup_rel_s",      ".0f", "s"),
        ("Peak replicas",                    "scaling.steady_state_replicas",    ".1f", ""),
        ("Waste window after k6 stop",       "resource.waste_window_s",          ".0f", "s"),
        ("Failed rate",                      "k6.failed_rate_pct",               ".2f", "%"),
    ]
    patterns_present = sorted({p for (p, c) in groups.keys()})

    lines = ["## 1. Executive Summary", ""]
    lines.append(
        "PHPA's design intent: trade slower first-scale-up response for faster "
        "scale-down and lower resource waste. The numbers below quantify both "
        "sides of that trade."
    )
    lines.append("")
    lines.append("| Pattern | Metric | PHPA | native HPA | Δ (PHPA − native) |")
    lines.append("|---|---|---|---|---|")

    for pattern in patterns_present:
        phpa_g = groups.get((pattern, "phpa"), [])
        nat_g = groups.get((pattern, "native_hpa"), [])
        for name, path, fmt, unit in headline_metrics:
            phpa_vals = [_to_float(get_nested(e, path)) for e in phpa_g]
            nat_vals = [_to_float(get_nested(e, path)) for e in nat_g]
            unit_str = (" " + unit) if unit and unit not in ("%",) else unit
            phpa_str = fmt_mean_stdev(phpa_vals, fmt, unit_str)
            nat_str = fmt_mean_stdev(nat_vals, fmt, unit_str)
            delta_str = compute_delta(phpa_vals, nat_vals)
            lines.append(f"| {pattern} | {name} | {phpa_str} | {nat_str} | {delta_str} |")
    lines.append("")
    return "\n".join(lines)


def render_setup(extracts: list[dict]) -> str:
    """What was tested. Mostly invariant fields across runs (k6 version, RPS, etc.)."""
    if not extracts:
        return ""
    repeats = max((e.get("repeat") or 0) for e in extracts)
    patterns = sorted({e.get("pattern") for e in extracts if e.get("pattern")})
    git_commits = sorted({e.get("git_commit") for e in extracts if e.get("git_commit")})
    durations = [e.get("duration_s") for e in extracts if e.get("duration_s")]

    lines = ["## 2. Experiment Setup", ""]
    lines.append(f"- **Total experiments analyzed**: {len(extracts)}")
    lines.append(f"- **Patterns tested**: {', '.join(patterns)}")
    lines.append(f"- **Controllers compared**: phpa, native_hpa (1:1 design)")
    lines.append(f"- **Max repeat index seen**: r{repeats}")
    lines.append(f"- **Git commits used**: {', '.join(git_commits)}")
    if durations:
        lines.append(f"- **Experiment duration range**: {min(durations)}s — {max(durations)}s")
    lines.append("- **Cluster**: kind-hpa-dev (single node, Ubuntu 24.04 VM)")
    lines.append("- **Target workload**: php-apache (CPU-bound, requests=200m / limits=500m)")
    lines.append("- **Load tool**: k6 v1.3.0, target RPS=25 (calibrated; see Phase 3.2)")
    lines.append("")
    lines.append(
        "Each experiment follows the same 11-step orchestrator (`hack/run_benchmark.sh`): "
        "reset Deployment to 1 replica → switch controller → 30s metric accumulation → "
        "k6 load (211s) → 360s tail observation → collect prom + events + controller log → "
        "smoke check → mark success."
    )
    lines.append("")
    return "\n".join(lines)


def _to_float(v: Any) -> float | None:
    """Coerce a value to float or None, suppressing all errors."""
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def render_per_pattern(groups: dict[tuple[str, str], list[dict]]) -> str:
    """Section 3: per-pattern comparison tables (PHPA vs native_hpa)."""
    patterns_present = sorted({p for (p, c) in groups.keys()})
    if not patterns_present:
        return "## 3. Per-Pattern Comparison\n\n*No patterns to report.*\n"

    lines = ["## 3. Per-Pattern Comparison", ""]

    for pattern in patterns_present:
        phpa_g = groups.get((pattern, "phpa"), [])
        nat_g = groups.get((pattern, "native_hpa"), [])
        n_phpa = len(phpa_g)
        n_nat = len(nat_g)

        lines.append(f"### 3.{patterns_present.index(pattern) + 1} `{pattern}` pattern")
        lines.append("")
        lines.append(f"*PHPA runs: {n_phpa} | native HPA runs: {n_nat}*")
        lines.append("")

        # Combined metrics table (shared between controllers).
        lines.append("| Metric | PHPA | native HPA |")
        lines.append("|---|---|---|")
        for spec in METRIC_SPECS:
            name = spec[0]
            phpa_name, phpa_str = make_metric_row(spec, phpa_g)
            _, nat_str = make_metric_row(spec, nat_g)
            lines.append(f"| {name} | {phpa_str} | {nat_str} |")
        lines.append("")

        # PHPA-only metrics.
        if phpa_g:
            lines.append("**PHPA-specific decision counters:**")
            lines.append("")
            lines.append("| Metric | Value |")
            lines.append("|---|---|")
            for spec in PHPA_METRIC_SPECS:
                _, val = make_metric_row(spec, phpa_g)
                lines.append(f"| {spec[0]} | {val} |")
            # Skip reasons breakdown.
            skip_reason_totals: dict[str, list[int]] = defaultdict(list)
            for e in phpa_g:
                reasons = get_nested(e, "phpa.skip_reasons") or {}
                for k, v in reasons.items():
                    skip_reason_totals[k or "(none)"].append(int(v))
            for reason, vals in sorted(skip_reason_totals.items()):
                if not vals:
                    continue
                mean = statistics.mean(vals)
                stdev = statistics.stdev(vals) if len(vals) > 1 else 0.0
                lines.append(
                    f"| Skip reason: `{reason}` | "
                    f"{mean:.1f}" + (f" ± {stdev:.1f}" if len(vals) > 1 else " (n=1)") +
                    " |"
                )
            lines.append("")

        # Per-pattern interpretation paragraph (templated; user edits if needed).
        lines.append("**Interpretation:**")
        lines.append("")
        interp = _interpret_pattern(pattern, phpa_g, nat_g)
        lines.append(interp)
        lines.append("")

    return "\n".join(lines)


def _interpret_pattern(
    pattern: str,
    phpa_g: list[dict],
    nat_g: list[dict],
) -> str:
    """Generate a one-paragraph data-driven interpretation per pattern."""
    if not phpa_g or not nat_g:
        return (
            "*Insufficient data: at least one controller has no successful runs for "
            "this pattern. Interpretation deferred until matrix completes.*"
        )

    def avg(group: list[dict], path: str) -> float | None:
        vals = [_to_float(get_nested(e, path)) for e in group]
        clean = [v for v in vals if v is not None]
        return statistics.mean(clean) if clean else None

    phpa_first = avg(phpa_g, "scaling.first_scaleup_rel_s")
    nat_first = avg(nat_g, "scaling.first_scaleup_rel_s")
    phpa_peak = avg(phpa_g, "scaling.steady_state_replicas")
    nat_peak = avg(nat_g, "scaling.steady_state_replicas")
    phpa_waste = avg(phpa_g, "resource.waste_window_s")
    nat_waste = avg(nat_g, "resource.waste_window_s")
    phpa_fail = avg(phpa_g, "k6.failed_rate_pct")
    nat_fail = avg(nat_g, "k6.failed_rate_pct")

    parts: list[str] = []
    if phpa_first is not None and nat_first is not None:
        if phpa_first > nat_first:
            parts.append(
                f"PHPA's first scale-up is ~{phpa_first - nat_first:.0f}s slower "
                f"than native HPA ({phpa_first:.0f}s vs {nat_first:.0f}s). "
                "This reflects the EWMA + 1m Prometheus rate path's smoothing tax."
            )
        else:
            parts.append(
                f"PHPA reacts as fast as or faster than native HPA on first scale-up "
                f"({phpa_first:.0f}s vs {nat_first:.0f}s)."
            )
    if phpa_peak is not None and nat_peak is not None:
        if phpa_peak > nat_peak:
            parts.append(
                f"PHPA over-provisions: peak replicas {phpa_peak:.1f} vs native {nat_peak:.1f}. "
                "EWMA's forward extrapolation overshoots when the rate-of-change is high."
            )
        elif phpa_peak < nat_peak:
            parts.append(
                f"PHPA holds fewer replicas at peak ({phpa_peak:.1f} vs {nat_peak:.1f}), "
                "suggesting its smoothing damps short-lived spikes."
            )
    if phpa_waste is not None and nat_waste is not None:
        if phpa_waste < nat_waste:
            parts.append(
                f"PHPA finishes scale-down faster: waste window {phpa_waste:.0f}s "
                f"vs native HPA's {nat_waste:.0f}s, the core selling point."
            )
        elif phpa_waste > nat_waste:
            parts.append(
                f"Surprise: PHPA's waste window ({phpa_waste:.0f}s) exceeds native HPA "
                f"({nat_waste:.0f}s) here. Possible over-provisioning + 60s "
                "stabilization window combine to extend idle time."
            )
    if phpa_fail is not None and nat_fail is not None and abs(phpa_fail - nat_fail) > 0.5:
        if phpa_fail < nat_fail:
            parts.append(
                f"Business impact: PHPA's failed-rate is {phpa_fail:.1f}% vs "
                f"native HPA's {nat_fail:.1f}% (lower is better). Difference is small "
                "because the bottleneck is the single-Pod start-up window, not the "
                "controller."
            )
        else:
            parts.append(
                f"PHPA's failed-rate ({phpa_fail:.1f}%) exceeds native HPA's "
                f"({nat_fail:.1f}%). Slower first-scale-up reflected in client-side "
                "timeouts."
            )

    if not parts:
        return "*No comparable scalar metrics available for interpretation.*"
    return " ".join(parts)


def render_cross_pattern(groups: dict[tuple[str, str], list[dict]]) -> str:
    """Section 4: findings that hold across patterns."""
    lines = ["## 4. Cross-Pattern Findings", ""]
    patterns = sorted({p for (p, c) in groups.keys()})
    if len(patterns) < 2:
        lines.append(
            f"*Only {len(patterns)} pattern(s) available; cross-pattern findings "
            "deferred until the full matrix is complete.*"
        )
        lines.append("")
        return "\n".join(lines)

    lines.append(
        "When the matrix includes multiple patterns, this section calls out "
        "what holds independent of load shape — e.g., whether PHPA's scale-down "
        "advantage replicates under ramp and spike, or only under step. With "
        f"{len(patterns)} patterns now covered ({', '.join(patterns)}), the following "
        "trends emerge:"
    )
    lines.append("")
    # Placeholder bullets — these get filled in qualitatively when reading the report.
    lines.append("- *(populated by inspecting per-pattern tables above)*")
    lines.append("")
    return "\n".join(lines)


def render_limitations(extracts: list[dict], skip_messages: list[str]) -> str:
    """Section 5: be honest about what this report can NOT claim."""
    lines = ["## 5. Known Limitations", ""]

    # Aggregate warnings across all extracts.
    all_warnings: dict[str, list[str]] = defaultdict(list)
    for e in extracts:
        for w in e.get("warnings", []):
            all_warnings[w].append(e.get("experiment_id", "?"))

    lines.append("### Sampling & instrumentation")
    lines.append("")
    lines.append(
        "- **Replica timeline precision is 15s** (prom step size). Per-second "
        "scale events are visible only via events.yaml, which is unreliable for "
        "PHPA (no SuccessfulRescale emitted) and contaminated across experiments "
        "by 1h K8s event TTL — see commit e862d1b for rationale."
    )
    lines.append(
        "- **Sample size n=3 per (pattern, controller)** is below the threshold "
        "for formal statistical inference. Reported mean ± stdev is engineering "
        "summary only — no t-tests, no p-values."
    )
    lines.append(
        "- **Single-node kind cluster** does not reflect production scheduling "
        "latency, node-to-node network jitter, or PV provisioning delays."
    )
    lines.append("")

    if all_warnings:
        lines.append("### Warnings emitted during extraction")
        lines.append("")
        for warning, exp_ids in sorted(all_warnings.items()):
            lines.append(f"- `{warning}`")
            lines.append(f"  - Affected: {', '.join(sorted(set(exp_ids)))}")
        lines.append("")

    if skip_messages:
        lines.append("### Experiments excluded from aggregation")
        lines.append("")
        for m in skip_messages:
            lines.append(f"- {m}")
        lines.append("")

    lines.append("### PHPA implementation scope (v1alpha1)")
    lines.append("")
    lines.append(
        "- In-memory stabilization-window history (single replica controller only; "
        "restart loses history)."
    )
    lines.append(
        "- CPU metric only; no memory or custom metrics."
    )
    lines.append(
        "- Deployment scaleTargetRef only; no StatefulSet / ReplicaSet support."
    )
    lines.append(
        "- minReplicas=0 (scale-to-zero) accepted by CRD validation but coerced to 1 "
        "at runtime."
    )
    lines.append("")
    return "\n".join(lines)


def render_report(
    extracts: list[dict],
    skip_messages: list[str],
) -> str:
    """Compose the full report from sections 1-5."""
    if not extracts:
        return (
            "# Phase 3 Benchmark Report\n\n"
            "*No successful experiments found. Run `hack/run_benchmark.sh` first.*\n"
        )

    groups = group_by_pattern_controller(extracts)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    header = [
        "# Phase 3 Benchmark Report",
        "",
        f"> Generated {generated_at} from {len(extracts)} experiment(s) across "
        f"{len({p for p, _ in groups})} pattern(s) × {len({c for _, c in groups})} controller(s).",
        "",
    ]

    sections = [
        "\n".join(header),
        render_executive_summary(groups),
        render_setup(extracts),
        render_per_pattern(groups),
        render_cross_pattern(groups),
        render_limitations(extracts, skip_messages),
    ]

    return "\n".join(sections)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Aggregate Phase 3 extract.json files into a markdown report",
    )
    parser.add_argument(
        "experiments_root",
        type=Path,
        help="Path to experiments/ root directory",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print report to stdout instead of writing AGGREGATE_REPORT.md",
    )
    args = parser.parse_args()

    root: Path = args.experiments_root
    if not root.is_dir():
        print(f"ERROR: {root} is not a directory", file=sys.stderr)
        return 1

    extracts, skip_messages = load_extracts(root)
    report = render_report(extracts, skip_messages)

    if args.stdout:
        sys.stdout.write(report)
        if not report.endswith("\n"):
            sys.stdout.write("\n")
    else:
        out_path = root / "AGGREGATE_REPORT.md"
        with out_path.open("w") as f:
            f.write(report)
        # Short progress line.
        n_groups = len({(e.get("pattern"), e.get("controller")) for e in extracts})
        print(f"aggregated {len(extracts)} experiments across {n_groups} (pattern, controller) groups → {out_path}")
        if skip_messages:
            print(f"  ({len(skip_messages)} directory/ies skipped — see report Section 5)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
