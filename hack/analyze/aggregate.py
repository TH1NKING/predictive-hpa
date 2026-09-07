#!/usr/bin/env python3
"""
Stabilization-window ablation cross-experiment aggregator.

Reads all extract.json files under an explicitly supplied experiments root
(skipping _INCOMPLETE_*) and produces a markdown comparison report grouping
experiments by (pattern, controller). The report keeps the three controller
variants in a fixed order and separates stabilization-window and prediction
effects so the two changes are not conflated.

Usage:
    python aggregate.py <experiments_root_dir>           # writes <root>/AGGREGATE_REPORT.md
    python aggregate.py <experiments_root_dir> --stdout  # prints to stdout instead

Report sections (in order):
    1. Executive Summary    — headline metrics and both ablation effects
    2. Experiment Setup     — reproduction-required metadata
    3. Per-Pattern Comparison — step / ramp / spike breakdowns
    4. Reading the Effects  — neutral definitions and data coverage
    5. Known Limitations    — tail truncation, sampling precision, etc.

Design notes:
- Only includes experiments whose metadata.yaml has result.status=success
  (NOT the metadata embedded in extract.json — that file does not duplicate
  the status field; we re-read metadata.yaml to filter).
- Skips directories matching _INCOMPLETE_* per the experiment archival convention.
- Aggregation: arithmetic mean and sample stdev (n-1 divisor via
  statistics.stdev, which requires n>=2; for n==1 we report mean only with a
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

from extract import validate_decision_mode


# Stable report order and user-facing labels for the three-way ablation.
CONTROLLER_COLUMNS: tuple[tuple[str, str], ...] = (
    ("native_hpa_300", "Native-300"),
    ("native_hpa_60", "Native-60"),
    ("phpa", "PHPA-60"),
)
CONTROLLER_LABELS = dict(CONTROLLER_COLUMNS)
DECISION_MODE_COLUMNS = (
    ("phpa_current", "PHPA-Current"), ("phpa", "PHPA-Predictive"),
    ("phpa_hybrid", "PHPA-Hybrid"),
)

WINDOW_BASELINE_CONTROLLER = "native_hpa_300"
WINDOW_CANDIDATE_CONTROLLER = "native_hpa_60"
PREDICTION_BASELINE_CONTROLLER = "native_hpa_60"
PREDICTION_CANDIDATE_CONTROLLER = "phpa"

WINDOW_EFFECT_LABEL = "Window effect (Native-60 - Native-300)"
PREDICTION_EFFECT_LABEL = "Prediction effect (PHPA-60 - Native-60)"

CENSORED_METRIC_PATHS = frozenset(
    {
        "scaling.first_scaledown_rel_s_after_k6_stop",
        "scaling.full_scaledown_rel_s_after_k6_stop",
        "resource.waste_window_s",
    }
)

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
    ("Peak replicas",                    "scaling.steady_state_replicas",    ".1f", ""),
    ("First scale-down relative to k6 stop", "scaling.first_scaledown_rel_s_after_k6_stop", ".0f", " s"),
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

CONTROLLED_PROTOCOL = "controlled-pilot-v1"
PILOT_IDENTITY_FIELDS = (
    "protocol_version", "campaign", "rps", "pre_allocated_vus", "max_vus",
    "benchmark_source_sha256", "benchmark_config_sha256", "post_load_tail_seconds",
)
PILOT_RUN_IDENTITY_FIELDS = PILOT_IDENTITY_FIELDS + (
    "experiment_id", "pattern", "controller", "repeat",
    "scale_down_stabilization_seconds", "prediction_variant",
)
PILOT_WINDOW_IDENTITIES = (
    ("load_start_time_unix", "measurement.load_onset_time_unix"),
    ("offered_load_end_time_unix", "measurement.offered_load_end_time_unix"),
    ("observation_end_time_unix", "measurement.tail_end_time_unix"),
)
PILOT_METRIC_SPECS = METRIC_SPECS[:5] + [
    ("HTTP 200 requests", "k6.successful_requests_http_200", ".0f", ""),
    ("HTTP 200 rate", "k6.successful_rate_http_200_pct", ".2f", "%"),
    ("First scale-up after load onset", "measurement.first_scaleup_after_load_onset_s", ".0f", " s"),
    ("First logged successful upscale after load onset", "phpa.first_upscale_decision_after_load_onset_s", ".3f", " s"),
    ("Peak replicas during observation", "measurement.peak_replicas", ".1f", ""),
    ("First scale-down after offered load end", "measurement.first_scaledown_after_offered_load_end_s", ".0f", " s"),
    ("Full scale-down after offered load end", "measurement.full_scaledown_after_offered_load_end_s", ".0f", " s"),
    ("Pod-seconds (load onset to fixed tail end)", "measurement.pod_seconds_load_onset_to_tail_end", ".0f", ""),
    ("Pod-seconds after offered load end", "measurement.pod_seconds_post_load", ".0f", ""),
    ("Excess Pod-seconds above minReplicas after offered load end", "measurement.excess_pod_seconds_post_load", ".0f", ""),
    ("Time above minReplicas after offered load end", "measurement.post_load_above_min_s", ".0f", " s"),
]


def validate_pilot_compatibility(extracts: list[dict]) -> None:
    """Refuse to pool different loads, sources or definitions in a new pilot."""
    if not any(e.get("protocol_version") == CONTROLLED_PROTOCOL for e in extracts):
        return
    for field in PILOT_IDENTITY_FIELDS:
        values = {str(e.get(field)) for e in extracts}
        if any(e.get(field) is None or e.get(field) == "" for e in extracts) or len(values) != 1:
            raise ValueError(f"incompatible controlled pilot: mixed or missing {field}: {sorted(values)}")
    variants: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    explicit_modes = any("decision_mode" in item for item in extracts)
    for item in extracts:
        validate_decision_mode(item)
        if explicit_modes and "decision_mode" not in item:
            raise ValueError("incompatible controlled pilot: missing decision_mode")
        if get_nested(item, "measurement.window_valid") is not True:
            raise ValueError(
                f"invalid controlled measurement in {item.get('experiment_id', '?')}; "
                "inspect extraction warnings before aggregation"
            )
        group = (item.get("pattern"), item.get("controller"))
        for field in ("scale_down_stabilization_seconds", "prediction_variant"):
            if item.get(field) is None:
                raise ValueError(f"incompatible controlled pilot: missing {field} in {group}")
            variants[group][field].add(str(item.get(field)))
    for group, fields in variants.items():
        for field, values in fields.items():
            if len(values) != 1:
                raise ValueError(f"incompatible controlled pilot: mixed {field} in {group}")


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


def validate_pilot_run(metadata: dict, extracted: dict, experiment_name: str = "?") -> None:
    """Validate a controlled run's metadata/extract pair without filling gaps.

    Public so matrix resume checks use the same contract as report loading.
    Raises ValueError for missing or stale identity/window fields or an invalid
    measurement; legacy loading deliberately does not call this function.
    """
    if (metadata.get("protocol_version") != CONTROLLED_PROTOCOL
            or extracted.get("protocol_version") != CONTROLLED_PROTOCOL):
        raise ValueError(
            f"metadata/extract protocol_version mismatch in {experiment_name}; re-run extract.py"
        )
    pairs = [(field, field) for field in PILOT_RUN_IDENTITY_FIELDS]
    if "decision_mode" in metadata or "decision_mode" in extracted:
        pairs.append(("decision_mode", "decision_mode"))
    pairs.extend((("git.commit", "git_commit"), *PILOT_WINDOW_IDENTITIES))
    for metadata_path, extracted_path in pairs:
        original = get_nested(metadata, metadata_path)
        derived = get_nested(extracted, extracted_path)
        if original is None or original == "" or derived is None or derived == "":
            raise ValueError(
                f"metadata/extract {metadata_path} missing in {experiment_name}; re-run extract.py"
            )
        if original != derived:
            raise ValueError(
                f"metadata/extract {metadata_path} mismatch in {experiment_name}; re-run extract.py"
            )
    validate_pilot_compatibility([extracted])


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
                loaded_meta = yaml.safe_load(f)
        except yaml.YAMLError as e:
            skip_messages.append(f"skipped {exp_dir.name}: metadata.yaml parse error ({e})")
            continue

        if not isinstance(loaded_meta, dict):
            skip_messages.append(
                f"skipped {exp_dir.name}: metadata.yaml must contain a mapping"
            )
            continue
        meta = loaded_meta

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

        if not isinstance(data, dict):
            skip_messages.append(
                f"skipped {exp_dir.name}: extract.json must contain an object"
            )
            continue

        if (meta.get("protocol_version") == CONTROLLED_PROTOCOL
                or data.get("protocol_version") == CONTROLLED_PROTOCOL):
            validate_pilot_run(meta, data, exp_dir.name)
        else:
            # Preserve historical enrichment for extracts predating these fields.
            data.setdefault("campaign", meta.get("campaign"))
            data.setdefault(
                "scale_down_stabilization_seconds",
                meta.get("scale_down_stabilization_seconds"),
            )
            data.setdefault("prediction_variant", meta.get("prediction_variant"))

        extracts.append(data)

    validate_pilot_compatibility(extracts)
    return extracts, skip_messages


def group_by_pattern_controller(
    extracts: list[dict],
) -> dict[tuple[str, str], list[dict]]:
    """Group extracts by (pattern, controller) tuple."""
    validate_pilot_compatibility(extracts)
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
    return name, fmt_mean_stdev(metric_values(extracts, path), fmt, unit)


def metric_values(extracts: list[dict], path: str) -> list[float | None]:
    """Return one numeric-or-None value per extract for a dotted metric path."""
    return [_to_float(get_nested(e, path)) for e in extracts]


def metric_group_is_censored(extracts: list[dict], path: str) -> bool:
    """Whether a scale-down metric includes a run that ended above minReplicas."""
    if path not in CENSORED_METRIC_PATHS:
        return False
    if path == "scaling.first_scaledown_rel_s_after_k6_stop":
        return any(get_nested(e, path) is None for e in extracts)
    if path == "scaling.full_scaledown_rel_s_after_k6_stop":
        return any(
            get_nested(e, path) is None
            or get_nested(e, "scaling.scaledown_completed") is False
            for e in extracts
        )
    return any(
        get_nested(e, "scaling.scaledown_completed") is False for e in extracts
    )


def format_shared_metric(
    extracts: list[dict],
    path: str,
    fmt: str,
    unit: str,
) -> str:
    """Format one shared metric and mark groups with censored scale-down data."""
    rendered = fmt_mean_stdev(metric_values(extracts, path), fmt, unit)
    if metric_group_is_censored(extracts, path):
        rendered += " †"
    return rendered


def compute_delta(
    candidate_vals: list[float | None],
    baseline_vals: list[float | None],
) -> str:
    """Compute candidate mean minus baseline mean.

    Returns 'n/a' if either side missing. Otherwise:
    - Absolute delta with sign
    - Percentage delta relative to the supplied baseline when meaningful
    """
    candidate_clean = [v for v in candidate_vals if v is not None]
    baseline_clean = [v for v in baseline_vals if v is not None]
    if not candidate_clean or not baseline_clean:
        return "n/a"
    candidate_mean = statistics.mean(candidate_clean)
    baseline_mean = statistics.mean(baseline_clean)
    delta = candidate_mean - baseline_mean
    if baseline_mean < 0:
        return f"{delta:+.1f} (negative baseline)"
    if abs(baseline_mean) < 1e-9:
        return f"{delta:+.1f} (baseline ~0)"
    pct = (delta / baseline_mean) * 100
    return f"{delta:+.1f} ({pct:+.0f}%)"


def controller_metric_values(
    groups: dict[tuple[str, str], list[dict]],
    pattern: str,
    path: str,
) -> dict[str, list[float | None]]:
    """Collect values for all report controller columns in stable order."""
    return {
        controller: metric_values(groups.get((pattern, controller), []), path)
        for controller, _ in CONTROLLER_COLUMNS
    }


def metric_effects(values: dict[str, list[float | None]]) -> tuple[str, str]:
    """Return window and prediction effects from controller metric values."""
    window_effect = compute_delta(
        values[WINDOW_CANDIDATE_CONTROLLER],
        values[WINDOW_BASELINE_CONTROLLER],
    )
    prediction_effect = compute_delta(
        values[PREDICTION_CANDIDATE_CONTROLLER],
        values[PREDICTION_BASELINE_CONTROLLER],
    )
    return window_effect, prediction_effect


def render_executive_summary(
    groups: dict[tuple[str, str], list[dict]],
) -> str:
    """Render headline metrics with both independent ablation effects."""
    headline_metrics = [
        ("First scale-up delay",             "scaling.first_scaleup_rel_s",      ".0f", "s"),
        ("Peak replicas",                    "scaling.steady_state_replicas",    ".1f", ""),
        ("Waste window after k6 stop",       "resource.waste_window_s",          ".0f", "s"),
        ("Failed rate",                      "k6.failed_rate_pct",               ".2f", "%"),
    ]
    patterns_present = sorted({p for (p, c) in groups.keys()})

    lines = ["## 1. Executive Summary", ""]
    lines.append(
        "The controller columns report group means across repeats. Effect columns "
        "are arithmetic differences between group means; their sign is not an "
        "automatic better/worse judgment."
    )
    lines.append("")
    lines.append(
        "| Pattern | Metric | Native-300 | Native-60 | PHPA-60 | "
        f"{WINDOW_EFFECT_LABEL} | {PREDICTION_EFFECT_LABEL} |"
    )
    lines.append("|---|---|---|---|---|---|---|")

    for pattern in patterns_present:
        for name, path, fmt, unit in headline_metrics:
            controller_groups = {
                controller: groups.get((pattern, controller), [])
                for controller, _ in CONTROLLER_COLUMNS
            }
            values = controller_metric_values(groups, pattern, path)
            unit_str = (" " + unit) if unit and unit not in ("%",) else unit
            formatted = [
                format_shared_metric(
                    controller_groups[controller], path, fmt, unit_str
                )
                for controller, _ in CONTROLLER_COLUMNS
            ]
            window_effect, prediction_effect = metric_effects(values)
            if metric_group_is_censored(
                controller_groups[WINDOW_BASELINE_CONTROLLER], path
            ) or metric_group_is_censored(
                controller_groups[WINDOW_CANDIDATE_CONTROLLER], path
            ):
                window_effect += " †"
            if metric_group_is_censored(
                controller_groups[PREDICTION_BASELINE_CONTROLLER], path
            ) or metric_group_is_censored(
                controller_groups[PREDICTION_CANDIDATE_CONTROLLER], path
            ):
                prediction_effect += " †"
            lines.append(
                f"| {pattern} | {name} | {' | '.join(formatted)} | "
                f"{window_effect} | {prediction_effect} |"
            )
    lines.append("")
    return "\n".join(lines)


def render_setup(extracts: list[dict]) -> str:
    """Summarize experiment metadata available in the loaded extracts."""
    if not extracts:
        return ""
    repeats = max((e.get("repeat") or 0) for e in extracts)
    patterns = sorted({e.get("pattern") for e in extracts if e.get("pattern")})
    git_commits = sorted({e.get("git_commit") for e in extracts if e.get("git_commit")})
    durations = [e.get("duration_s") for e in extracts if e.get("duration_s")]
    campaigns = sorted({e.get("campaign") for e in extracts if e.get("campaign")})
    windows = sorted(
        {
            e.get("scale_down_stabilization_seconds")
            for e in extracts
            if e.get("scale_down_stabilization_seconds") is not None
        }
    )
    prediction_variants = sorted(
        {
            str(e.get("prediction_variant"))
            for e in extracts
            if e.get("prediction_variant") is not None
        }
    )
    controllers_present = {e.get("controller") for e in extracts}
    controller_labels = [
        label
        for controller, label in CONTROLLER_COLUMNS
        if controller in controllers_present
    ]
    unknown_controllers = sorted(
        str(controller)
        for controller in controllers_present
        if controller and controller not in CONTROLLER_LABELS
    )
    controller_labels.extend(unknown_controllers)

    lines = ["## 2. Experiment Setup", ""]
    lines.append(f"- **Total experiments analyzed**: {len(extracts)}")
    lines.append(f"- **Patterns tested**: {', '.join(patterns)}")
    lines.append(f"- **Controllers compared**: {', '.join(controller_labels)}")
    if campaigns:
        lines.append(f"- **Campaigns**: {', '.join(campaigns)}")
    if windows:
        lines.append(
            "- **Scale-down stabilization values**: "
            + ", ".join(f"{value}s" for value in windows)
        )
    if prediction_variants:
        lines.append(
            f"- **Prediction variants**: {', '.join(prediction_variants)}"
        )
    lines.append(f"- **Max repeat index seen**: r{repeats}")
    if git_commits:
        lines.append(f"- **Git commits used**: {', '.join(git_commits)}")
    if durations:
        lines.append(f"- **Experiment duration range**: {min(durations)}s — {max(durations)}s")
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
    """Section 3: per-pattern three-way comparison and both effects."""
    patterns_present = sorted({p for (p, c) in groups.keys()})
    if not patterns_present:
        return "## 3. Per-Pattern Comparison\n\n*No patterns to report.*\n"

    lines = ["## 3. Per-Pattern Comparison", ""]

    for pattern in patterns_present:
        phpa_g = groups.get((pattern, "phpa"), [])
        group_counts = [
            f"{label} runs: {len(groups.get((pattern, controller), []))}"
            for controller, label in CONTROLLER_COLUMNS
        ]

        lines.append(f"### 3.{patterns_present.index(pattern) + 1} `{pattern}` pattern")
        lines.append("")
        lines.append(f"*{' | '.join(group_counts)}*")
        lines.append("")

        lines.append(
            "| Metric | Native-300 | Native-60 | PHPA-60 | "
            f"{WINDOW_EFFECT_LABEL} | {PREDICTION_EFFECT_LABEL} |"
        )
        lines.append("|---|---|---|---|---|---|")
        for spec in METRIC_SPECS:
            name, path, fmt, unit = spec
            controller_groups = {
                controller: groups.get((pattern, controller), [])
                for controller, _ in CONTROLLER_COLUMNS
            }
            values = controller_metric_values(groups, pattern, path)
            formatted = [
                format_shared_metric(controller_groups[controller], path, fmt, unit)
                for controller, _ in CONTROLLER_COLUMNS
            ]
            window_effect, prediction_effect = metric_effects(values)
            if metric_group_is_censored(
                controller_groups[WINDOW_BASELINE_CONTROLLER], path
            ) or metric_group_is_censored(
                controller_groups[WINDOW_CANDIDATE_CONTROLLER], path
            ):
                window_effect += " †"
            if metric_group_is_censored(
                controller_groups[PREDICTION_BASELINE_CONTROLLER], path
            ) or metric_group_is_censored(
                controller_groups[PREDICTION_CANDIDATE_CONTROLLER], path
            ):
                prediction_effect += " †"
            lines.append(
                f"| {name} | {' | '.join(formatted)} | "
                f"{window_effect} | {prediction_effect} |"
            )
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

    return "\n".join(lines)


def render_cross_pattern(groups: dict[tuple[str, str], list[dict]]) -> str:
    """Section 4: neutral effect definitions and controller coverage."""
    lines = ["## 4. Reading the Effects", ""]
    patterns = sorted({p for (p, c) in groups.keys()})
    lines.append(
        f"- **Window effect** is `{CONTROLLER_LABELS[WINDOW_CANDIDATE_CONTROLLER]} "
        f"- {CONTROLLER_LABELS[WINDOW_BASELINE_CONTROLLER]}` and changes only the "
        "scale-down stabilization window."
    )
    lines.append(
        f"- **Prediction effect** is `{CONTROLLER_LABELS[PREDICTION_CANDIDATE_CONTROLLER]} "
        f"- {CONTROLLER_LABELS[PREDICTION_BASELINE_CONTROLLER]}` and compares prediction "
        "against the matched 60-second native baseline."
    )
    expected_controllers = {controller for controller, _ in CONTROLLER_COLUMNS}
    complete_patterns = [
        pattern
        for pattern in patterns
        if expected_controllers.issubset(
            {
                controller
                for candidate_pattern, controller in groups
                if candidate_pattern == pattern
            }
        )
    ]
    lines.append(
        f"- **Complete three-controller coverage**: {len(complete_patterns)}/{len(patterns)} "
        "pattern(s). Missing groups appear as `n/a`."
    )
    lines.append(
        "- Effect signs are arithmetic only. Whether a positive or negative value is "
        "preferred depends on the metric."
    )
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
        "- **Replica timeline precision is 15s** (Prometheus query step). "
        "events.yaml is retained as a secondary signal because the three controllers "
        "do not emit identical event streams."
    )
    group_sizes = [
        len(group)
        for group in group_by_pattern_controller(extracts).values()
        if group
    ]
    sample_size_range = (
        f"{min(group_sizes)}–{max(group_sizes)}" if group_sizes else "0"
    )
    lines.append(
        f"- **Observed group sample sizes range from n={sample_size_range}.** "
        "Reported mean ± sample standard deviation and mean differences are "
        "engineering summaries only; no formal statistical inference is performed."
    )
    if any(
        get_nested(e, "scaling.scaledown_completed") is False for e in extracts
    ):
        lines.append(
            "- **† Censored group**: at least one run ended above minReplicas; "
            "marked waste-window values are lower bounds, and marked scale-down "
            "means omit unavailable runs."
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
            "# Stabilization Window Ablation Benchmark Report\n\n"
            "*No successful experiments found in the supplied experiments root.*\n"
        )

    groups = group_by_pattern_controller(extracts)
    if extracts[0].get("protocol_version") == CONTROLLED_PROTOCOL:
        return render_controlled_report(extracts, skip_messages, groups)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    header = [
        "# Stabilization Window Ablation Benchmark Report",
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


def render_controlled_report(
    extracts: list[dict], skip_messages: list[str],
    groups: dict[tuple[str, str], list[dict]],
) -> str:
    """Report explicit fixed-window metrics without relabelling archived v2 data."""
    ablation = any(item.get("controller") in ("phpa_current", "phpa_hybrid") for item in extracts)
    available_columns = CONTROLLER_COLUMNS[:2] + DECISION_MODE_COLUMNS if ablation else CONTROLLER_COLUMNS
    columns = [(key, label) for key, label in available_columns
               if any(e.get("controller") == key for e in extracts)]
    lines = ["# Controlled Pilot Benchmark Report", "", render_setup(extracts)]
    if ablation:
        mode_by_controller = {item["controller"]: item["decision_mode"] for item in extracts}
        lines.append("- **Decision modes**: " + ", ".join(
            f"{key}={mode_by_controller[key]}" for key, _ in columns))
    for field in PILOT_IDENTITY_FIELDS:
        lines.append(f"- **{field}**: `{extracts[0].get(field)}`")
    lines.extend([
        "", "## Fixed observation window", "",
        "Load onset is the recorded k6 runner start + 30s. Offered load end uses the "
        "declared pattern schedule (step 211s, ramp 270s, spike 241s from runner start). "
        "Every observation ends 360s after offered load end, excluding preparation and "
        "variable runner drain/collection time from resource comparisons.", "",
        "Pod-seconds estimate the integral of sampled Deployment status replicas with "
        "a piecewise constant hold, at 15s precision. They are not CPU-seconds or billing "
        "measurements. HTTP metrics include in-flight requests finishing after offered load end.", "",
        "| Experiment | Load onset (Unix) | Offered load end (Unix) | Fixed tail end (Unix) | Window (s) | Scale-down complete |",
        "|---|---|---|---|---|---|",
    ])
    for item in extracts:
        measurement = item["measurement"]
        values = [measurement.get(key) for key in (
            "load_onset_time_unix", "offered_load_end_time_unix", "tail_end_time_unix",
            "window_duration_s", "scaledown_completed",
        )]
        lines.append(f"| {item.get('experiment_id')} | " + " | ".join(map(str, values)) + " |")
    differences = [("Controller difference", "phpa", "native_hpa_60")]
    explanation = ("The difference is PHPA-60 minus Native-60 and compares complete controllers; "
                   "it does not isolate prediction as a causal effect.")
    if ablation:
        differences = [("Current - Predictive", "phpa_current", "phpa"),
                       ("Hybrid - Predictive", "phpa_hybrid", "phpa")]
        explanation = ("The treatments use the same controller, metric source and scaling settings; "
                       "only the selected decision mode differs. Differences are group means, "
                       "with Predictive as the baseline.")
    for pattern in sorted({pattern for pattern, _ in groups}):
        lines.extend(["", f"## {pattern} comparison", "",
                      "Values are mean ± sample standard deviation, or a single-run value with n=1. "
                      + explanation, "",
                      "| Metric | " + " | ".join(label for _, label in columns) + " | "
                      + " | ".join(label for label, _, _ in differences) + " |",
                      "|---|" + "---|" * (len(columns) + len(differences))])
        for name, path, fmt, unit in PILOT_METRIC_SPECS:
            formatted = []
            censored_groups = set()
            for controller, _ in columns:
                group = groups.get((pattern, controller), [])
                rendered = fmt_mean_stdev(metric_values(group, path), fmt, unit)
                censored = path in {
                    "measurement.full_scaledown_after_offered_load_end_s",
                    "measurement.post_load_above_min_s",
                } and any(get_nested(e, "measurement.scaledown_completed") is False for e in group)
                formatted.append(rendered + (" †" if censored else ""))
                if censored:
                    censored_groups.add(controller)
            for _, candidate, baseline in differences:
                difference = compute_delta(metric_values(groups.get((pattern, candidate), []), path),
                                           metric_values(groups.get((pattern, baseline), []), path))
                formatted.append(difference + (" †" if {candidate, baseline} & censored_groups else ""))
            lines.append(f"| {name} | " + " | ".join(formatted) + " |")
    lines.extend(["", "## Limitations and exclusions", "",
                  "- This small pilot provides descriptive evidence; no statistical significance is claimed.",
                  "- Replica changes have 15s sampling precision; a change straddling load onset cannot be timed more precisely.",
                  "- Logged successful upscales compare the submitted target with the previous desired replica count and use the timestamp immediately after a Scale update; this is separate from Pod readiness and sampled replica rise. Legacy Reconciled logs are a fallback measured after status persistence and compare with observed replicas, which may lag the desired count. Missing logged upscales remain n/a.",
                  "- † marks scale-down censored at the fixed tail end. Time above minReplicas is then a lower bound for complete recovery; missing full scale-down times are omitted from means.",
                  "- Fixed-window Pod-seconds remain observed window costs even if scale-down is censored.",
                  "- Failed rate follows k6 expected-response policy; HTTP 200 rate separately counts strict status 200."])
    for item in extracts:
        for warning in item.get("warnings", []):
            # Preserve source warnings while excluding timing warnings belonging
            # only to the legacy calculation retained separately in extract.json.
            if not warning.startswith(("scaledown_completed=false", "no scale-down events detected",
                                       "k6 start timestamp unavailable")):
                lines.append(f"- {item.get('experiment_id')}: {warning}")
    lines.extend(f"- {message}" for message in skip_messages)
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="Aggregate stabilization-window ablation extracts into markdown",
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

    try:
        extracts, skip_messages = load_extracts(root)
        report = render_report(extracts, skip_messages)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.stdout:
        sys.stdout.write(report)
        if not report.endswith("\n"):
            sys.stdout.write("\n")
    else:
        out_path = root / "AGGREGATE_REPORT.md"
        with out_path.open("w", encoding="utf-8", newline="\n") as f:
            f.write(report)
        # Short progress line.
        n_groups = len({(e.get("pattern"), e.get("controller")) for e in extracts})
        print(f"aggregated {len(extracts)} experiments across {n_groups} (pattern, controller) groups → {out_path}")
        if skip_messages:
            print(f"  ({len(skip_messages)} directory/ies skipped — see report Section 5)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
