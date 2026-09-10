#!/usr/bin/env python3
"""Render the complete, independently audited 18-run baseline; never synthesize data.

Usage (only after both formal campaigns finish):
  python hack/analyze/plot_live_baseline.py docs/benchmarks/assets/live-baseline-inputs-20260910.json
  python hack/analyze/plot_live_baseline.py docs/benchmarks/assets/live-baseline-inputs-20260910.json --check-only

Input shape:
  {"calibration": {"qualified_rps": 25, ...}, "campaigns": [
    {"pattern": "step", "slots": [nine rows from independent_audit.py]},
    {"pattern": "ramp", "slots": [nine rows from independent_audit.py]}]}

The original compact input is read without modification. A Markdown table on
stdout includes every run's requested and Ready replica integrals and drops.
PNG/SVG default to docs/benchmarks/assets/live-baseline-summary-20260910.*.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
from io import BytesIO
import json
import math
from pathlib import Path
import statistics
import sys


ROOT = Path(__file__).resolve().parents[2]
MODES = ("Current", "Predictive", "Hybrid")
COLORS = {"Current": "#2475B0", "Predictive": "#DE7D24", "Hybrid": "#258554"}
WINDOWS = {"step": 541, "ramp": 600}
CRITERIA = {"http200_pct": 99, "p95_ms": 500}


def finite(value: object, label: str, *, integer: bool = False) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or value < 0 or (integer and value != int(value))):
        raise ValueError(f"{label} must be a finite nonnegative {'integer' if integer else 'number'}")
    return value


def validate(document: dict) -> dict[str, list[dict]]:
    if not isinstance(document, dict) or not isinstance(document["calibration"], dict):
        raise ValueError("A compact input with calibration and both campaigns is required")
    rps = finite(document["calibration"]["qualified_rps"], "qualified_rps", integer=True)
    if not 1 <= rps <= 1000:
        raise ValueError("Calibration must qualify the campaign RPS")
    campaigns = document["campaigns"]
    if not isinstance(campaigns, list) or len(campaigns) != 2:
        raise ValueError("Exactly two complete formal campaigns are required")
    result = {}
    experiment_ids = set()
    for campaign in campaigns:
        pattern = campaign["pattern"]
        if pattern not in WINDOWS or pattern in result:
            raise ValueError("Exactly one step and one ramp campaign are required")
        if "campaign_status" in campaign and campaign["campaign_status"] != "success":
            raise ValueError(f"{pattern} campaign has not completed successfully")
        rows = campaign["slots"]
        if not isinstance(rows, list) or len(rows) != 9:
            raise ValueError(f"{pattern} must retain all nine assigned slots")
        slots = [finite(row["slot"], f"{pattern} slot", integer=True) for row in rows]
        if sorted(slots) != list(range(1, 10)):
            raise ValueError(f"{pattern} slot IDs must be exactly 1 through 9")
        if Counter(row["mode"] for row in rows) != Counter({mode: 3 for mode in MODES}):
            raise ValueError(f"{pattern} must have exactly three runs per decision mode")
        for row in rows:
            identity = f"{pattern} slot {row['slot']}"
            if (row["status"] != "success" or row["startup"] != "warm" or row["pattern"] != pattern
                    or row["assigned_mode"] != row["mode"]):
                raise ValueError(f"{identity}: only complete, correctly assigned warm runs can be plotted")
            experiment_id = row["experiment_id"]
            if not isinstance(experiment_id, str) or not experiment_id or experiment_id in experiment_ids:
                raise ValueError(f"{identity}: experiment identity must be present and globally unique")
            experiment_ids.add(experiment_id)
            total = finite(row["requests"], f"{identity} requests", integer=True)
            good = finite(row["http200"], f"{identity} HTTP 200", integer=True)
            success = finite(row["http200_pct"], f"{identity} HTTP 200 percent")
            p95 = finite(row["p95_ms"], f"{identity} all-request p95")
            drops = finite(row["dropped"], f"{identity} dropped iterations", integer=True)
            if not total or good > total or not math.isclose(success, 100 * good / total, rel_tol=0, abs_tol=1e-8):
                raise ValueError(f"{identity}: inconsistent request counts or success percentage")
            expected_pass = success >= CRITERIA["http200_pct"] and p95 <= CRITERIA["p95_ms"] and drops == 0
            if not isinstance(row["criteria_passed"], bool) or row["criteria_passed"] != expected_pass:
                raise ValueError(f"{identity}: criteria receipt disagrees with measured service results")
            if finite(row["input_hashes_verified"], f"{identity} verified inputs", integer=True) < 1:
                raise ValueError(f"{identity}: independent input verification is missing")
            for resource in ("requested", "ready"):
                replicas = row["replicas"][resource]
                covered = finite(replicas["covered_seconds"], f"{identity} {resource} coverage")
                finite(replicas["covered_pod_seconds"], f"{identity} {resource} replica time")
                if replicas["complete"] is not True or not math.isclose(covered, WINDOWS[pattern], rel_tol=0, abs_tol=1e-5):
                    raise ValueError(f"{identity}: {resource} coverage must span all {WINDOWS[pattern]} seconds")
            if row["first_scale_seconds"] is not None:
                if finite(row["first_scale_seconds"], f"{identity} first Scale time") > WINDOWS[pattern]:
                    raise ValueError(f"{identity}: Scale increase lies outside the observation window")
        result[pattern] = sorted(rows, key=lambda row: row["slot"])
    return result


def markdown_table(campaigns: dict[str, list[dict]]) -> str:
    lines = ["| Pattern | Slot | Mode | HTTP 200 (%) | All-request p95 (ms) | Drops | Requested Pod-s | Ready Pod-s | First Scale increase (s) | Service criteria |",
             "|---|---:|---|---:|---:|---:|---:|---:|---:|---|"]
    for pattern in WINDOWS:
        for row in campaigns[pattern]:
            scale = "No increase" if row["first_scale_seconds"] is None else f"{row['first_scale_seconds']:.2f}"
            lines.append(f"| {pattern} | {row['slot']} | {row['mode']} | {row['http200_pct']:.2f} | {row['p95_ms']:.2f} | "
                f"{row['dropped']} | {row['replicas']['requested']['covered_pod_seconds']:.2f} | "
                f"{row['replicas']['ready']['covered_pod_seconds']:.2f} | {scale} | "
                f"{'Pass' if row['criteria_passed'] else 'Fail'} |")
    return "\n".join(lines)


def render(document: dict, campaigns: dict[str, list[dict]], source: Path, digest: str) -> dict[str, bytes]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from matplotlib.ticker import MaxNLocator, StrMethodFormatter

    metrics = (("http200_pct", "HTTP 200 success", "%"),
               ("p95_ms", "All-request p95", "ms; includes failed requests"),
               ("requested_pod_seconds", "Requested replica time", "Pod-seconds"),
               ("first_scale_seconds", "First successful Scale increase", "seconds after load onset"))

    def value(row: dict, metric: str) -> float | None:
        return row["replicas"]["requested"]["covered_pod_seconds"] if metric == "requested_pod_seconds" else row[metric]

    all_rows = [row for rows in campaigns.values() for row in rows]
    limits = {}
    for key, _, _ in metrics:
        known = [value(row, key) for row in all_rows if value(row, key) is not None]
        maximum = max([*known, CRITERIA.get(key, 0), 1])
        limits[key] = (0, 102 if key == "http200_pct" else maximum * 1.22)

    with plt.rc_context({"font.family": "DejaVu Sans", "font.size": 10, "axes.labelcolor": "#344054",
            "text.color": "#172B4D", "axes.edgecolor": "#C8D1DA", "xtick.color": "#344054",
            "ytick.color": "#344054", "svg.fonttype": "none", "savefig.facecolor": "white"}):
        fig, axes = plt.subplots(2, 4, figsize=(16, 9), sharex="col")
        fig.subplots_adjust(left=0.085, right=0.975, bottom=0.205, top=0.80, wspace=0.30, hspace=0.44)
        fig.text(0.045, 0.955, "Verified-observation baseline", fontsize=22, weight="bold", ha="left")
        fig.text(0.045, 0.916, f"Current / Predictive / Hybrid  |  {document['calibration']['qualified_rps']} RPS  |  "
            "Warm start  |  3 runs per mode and pattern", fontsize=11, color="#52657A", ha="left")
        handles = [Line2D([], [], marker="o", linestyle="none", color="#52657A", markersize=6,
                         label="Individual run (horizontal offset only)"),
                   Line2D([], [], color="#172B4D", linewidth=2.5, label="Arithmetic mean of observed values"),
                   Line2D([], [], color="#8291A3", linestyle="--", linewidth=1.2, label="Predeclared service criterion")]
        fig.legend(handles=handles, loc="upper left", bbox_to_anchor=(0.040, 0.887), frameon=False, ncol=3, fontsize=9)

        for row_index, pattern in enumerate(WINDOWS):
            row_axes = axes[row_index]
            row_axes[0].text(-0.30, 0.5, f"{pattern.upper()}\n{WINDOWS[pattern]} s window", transform=row_axes[0].transAxes,
                rotation=90, ha="center", va="center", fontsize=11, weight="bold", color="#344054")
            for column, (key, title, unit) in enumerate(metrics):
                ax = row_axes[column]
                ax.set_title(title, fontsize=11, loc="left", pad=18, weight="bold")
                ax.set_ylabel(unit, fontsize=9)
                ax.set_xlim(-0.55, 2.55)
                ax.set_ylim(*limits[key])
                ax.set_xticks(range(3), MODES)
                ax.tick_params(axis="x", labelbottom=True, length=0, pad=9, labelsize=9)
                ax.tick_params(axis="y", length=0, labelsize=9)
                ax.yaxis.set_major_locator(MaxNLocator(nbins=5))
                ax.yaxis.set_major_formatter(StrMethodFormatter("{x:,.0f}"))
                ax.grid(axis="y", color="#E7ECF1", linewidth=0.8)
                ax.set_axisbelow(True)
                ax.spines[["top", "right"]].set_visible(False)
                if key in CRITERIA:
                    threshold = CRITERIA[key]
                    ax.axhline(threshold, color="#8291A3", linestyle="--", linewidth=1.2, zorder=2)
                    ax.text(0.98, threshold, "99% minimum" if key == "http200_pct" else "500 ms maximum",
                        transform=ax.get_yaxis_transform(), ha="right", va="bottom", fontsize=8,
                        color="#52657A", bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.85, "pad": 1})
                for x, mode in enumerate(MODES):
                    group = [row for row in campaigns[pattern] if row["mode"] == mode]
                    known = []
                    for offset, row in zip((-0.15, 0, 0.15), group):
                        observed = value(row, key)
                        if observed is not None:
                            known.append(observed)
                            ax.scatter(x + offset, observed, s=45, color=COLORS[mode], edgecolors="white",
                                linewidths=0.7, zorder=4)
                    if known:
                        average = statistics.mean(known)
                        ax.plot([x - 0.25, x + 0.25], [average, average], color="#172B4D", linewidth=2.3, zorder=5)
                    if key == "first_scale_seconds" and len(known) < 3:
                        ax.text(x, 0.96, f"No increase: {3 - len(known)}/3\nMean n={len(known)}",
                            transform=ax.get_xaxis_transform(), ha="center", va="top", fontsize=8, color=COLORS[mode])
        fig.text(0.045, 0.139, "All 18 assigned formal runs are included. n=3 summaries are descriptive; no confidence intervals or significance claims.",
                 fontsize=10, ha="left", color="#344054")
        fig.text(0.045, 0.109, "Replica time uses complete sampled coverage: step 541 s; ramp 600 s. It is not CPU consumption or billing. "
                 "Ready replica time and dropped iterations remain in the source/table.", fontsize=9, ha="left", color="#52657A")
        fig.text(0.045, 0.078, "Service criteria: HTTP 200 >=99%, all-request p95 <=500 ms, and zero dropped iterations. "
                 "No Scale increase is retained explicitly, without substituting zero seconds.", fontsize=9, ha="left", color="#52657A")
        fig.text(0.045, 0.044, f"Source: {source.name}  |  SHA256 {digest[:16]}...  |  Independent retained-input verification",
                 fontsize=8, ha="left", color="#65778A")
        rendered = {}
        for extension in ("png", "svg"):
            buffer = BytesIO()
            fig.savefig(buffer, format=extension, dpi=180, metadata={"Creator": "predictive-hpa baseline report"}
                        if extension == "svg" else {"Software": "predictive-hpa baseline report"})
            payload = buffer.getvalue()
            if extension == "svg":
                payload = b"\n".join(line.rstrip(b" \t") for line in payload.split(b"\n"))
            rendered[extension] = payload
        plt.close(fig)
        return rendered


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output-prefix", type=Path,
        default=ROOT / "docs/benchmarks/assets/live-baseline-summary-20260910")
    parser.add_argument("--check-only", action="store_true", help="Validate all final inputs and print the complete table without rendering")
    parser.add_argument("--overwrite", action="store_true", help="Explicitly replace existing PNG/SVG outputs")
    args = parser.parse_args()
    try:
        data = args.input.read_bytes()
        document = json.loads(data)
        campaigns = validate(document)
        digest = hashlib.sha256(data).hexdigest()
        if not args.check_only:
            destinations = {extension: args.output_prefix.with_suffix("." + extension) for extension in ("png", "svg")}
            for destination in destinations.values():
                if destination.resolve() == args.input.resolve():
                    raise ValueError("A figure cannot replace its compact source input")
                if destination.exists() and not args.overwrite:
                    raise ValueError(f"Output already exists: {destination}; use --overwrite only for an intended regeneration")
                if not destination.parent.is_dir():
                    raise ValueError(f"Output directory does not exist: {destination.parent}")
            rendered = render(document, campaigns, args.input, digest)
            for extension, destination in destinations.items():
                with destination.open("wb" if args.overwrite else "xb") as stream:
                    stream.write(rendered[extension])
                print(f"Wrote {destination}", file=sys.stderr)
        print(markdown_table(campaigns))
        print(f"\nVerified complete input: 18/18 runs; SHA256 {digest}", file=sys.stderr)
        return 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        print(f"Baseline figure generation failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
