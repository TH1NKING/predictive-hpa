#!/usr/bin/env python3
"""Describe pre-expansion metric evidence from retained files, without network access."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

from latency import controller_cycles, epoch, records, scenario_schedule


def result_rows(row):
    return row["response"]["data"]["result"]


def sample_map(row, conflicts=None):
    values = {}
    for series in result_rows(row):
        for stamp, value in series.get("values", []):
            number = float(value)
            number = number if math.isfinite(number) else None
            key = (json.dumps(series["metric"], sort_keys=True), epoch(stamp))
            if key in values and values[key] != number and conflicts is not None:
                conflicts.add(key)
            values[key] = number
    return values


def analyze(directory: Path, batch: str) -> dict:
    flags = set()
    onset = scenario_schedule(directory)["load_onset_unix"]
    cycles = controller_cycles(directory / "controller.log")
    # An absent expansion is a valid outcome; unreadable evidence is not.
    plan = json.loads((directory / "latency-plan.json").read_text(encoding="utf-8"))
    window = plan["source_range_seconds"]
    observations = records(directory / "latency-observations.ndjson")
    expansion = min((cycle["scale"] for cycle in cycles if cycle.get("scale")
                     and cycle["scale"]["finalDesired"] > cycle["scale"]["previousDesiredReplicas"]
                     and epoch(cycle["scale"]["scaleWriteStartedAt"]) >= onset),
                    key=lambda row: epoch(row["scaleWriteStartedAt"]), default=None)
    if expansion is None:
        return {"batch": batch, "run": directory.name, "input_directory": str(directory.resolve()),
                "load_onset_unix": onset, "cutoff_seconds": None, "scale_response_seconds": None,
                "samples": [], "pairs": [], "request_values_cores": [], "evaluations": [], "window_samples": [],
                "empty_raw_observations": [],
                "first_above_threshold": None, "controller_queries": [], "quality_flags": ["missing_successful_expansion"]}
    cutoff = epoch(expansion["scaleWriteStartedAt"])
    observed = [row for row in observations
                if row["kind"] in ("prom_cpu_raw", "prom_requests_raw", "prom_cpu_evaluated")
                and epoch(row["request_finished_at"]) < cutoff]
    flags.update(f'{row["kind"]}_error' for row in observed
                 if row["kind"] != "prom_cpu_evaluated" and row["status"] != "success")
    empty_raw = [{"kind": row["kind"], "evaluation_seconds": row["evaluation_time_unix"] - onset,
                  "observation_interval_seconds": [epoch(row["request_started_at"]) - onset,
                                                   epoch(row["request_finished_at"]) - onset]}
                 for row in observed if row["kind"] != "prom_cpu_evaluated"
                 and row["status"] == "success" and not result_rows(row)]
    flags.update(f'{row["kind"]}_empty' for row in empty_raw)
    raw = sorted((row for row in observed if row["kind"] == "prom_cpu_raw" and row["status"] == "success"),
                 key=lambda row: epoch(row["request_finished_at"]))
    conflicts = set()
    snapshots = [(row, sample_map(row, conflicts)) for row in raw]
    if conflicts:
        flags.add("conflicting_sample_values")
    window_samples = []
    for row, values in snapshots:
        evaluation = row["evaluation_time_unix"]
        if evaluation < onset:
            continue
        for label in sorted({key[0] for key in values}):
            stamps = [stamp for (key, stamp) in values if key == label]
            window_samples.append({"evaluation_seconds": evaluation - onset, "labels": json.loads(label),
                                   "counts": {str(width): sum(evaluation - width < stamp <= evaluation for stamp in stamps)
                                              for width in (30, 60)}})
    samples = {}
    for row, values in snapshots:
        for key, value in values.items():
            if value is None:
                flags.add("nonfinite_counter")
            if key in samples:
                if samples[key]["counter_value"] != value:
                    samples[key]["conflicting_values"] = True
                    flags.add("conflicting_sample_values")
                continue
            labels, stamp = key
            lower = [epoch(previous["request_started_at"]) - onset for previous, prior in snapshots
                     if epoch(previous["request_finished_at"]) <= epoch(row["request_started_at"])
                     and previous["evaluation_time_unix"] - window < stamp <= previous["evaluation_time_unix"]
                     and key not in prior]
            samples[key] = {"labels": json.loads(labels), "sample_seconds": stamp - onset,
                            "counter_value": value, "conflicting_values": key in conflicts,
                            "visibility_interval_seconds": [max(lower) if lower else None,
                                                            epoch(row["request_finished_at"]) - onset]}
    pairs = []
    for label in sorted({key[0] for key in samples}):
        series = sorted((stamp, sample) for (key, stamp), sample in samples.items() if key == label)
        for (left, a), (right, b) in zip(series, series[1:]):
            if right <= onset:
                continue
            gap = right - left
            delta = b["counter_value"] - a["counter_value"] if a["counter_value"] is not None and b["counter_value"] is not None else None
            status = "nonfinite_counter" if delta is None else "counter_reset" if delta < 0 else "valid"
            if a["conflicting_values"] or b["conflicting_values"]:
                status = "conflicting_sample_values"
            if status != "valid":
                flags.add(status)
            pairs.append({"labels": b["labels"], "left_seconds": left - onset, "right_seconds": right - onset,
                          "sample_gap_seconds": gap, "counter_delta": delta,
                          "slope_cores": delta / gap if status == "valid" else None, "status": status,
                          "right_visibility_interval_seconds": b["visibility_interval_seconds"]})
    evaluations = []
    for row in sorted(observed, key=lambda row: epoch(row["request_started_at"])):
        if row["kind"] != "prom_cpu_evaluated" or row["evaluation_time_unix"] < onset:
            continue
        value = None
        if row["status"] != "success":
            status = "error"
        else:
            result = result_rows(row)
            status = "empty" if not result else "multiple_series" if len(result) != 1 else "valid"
            if status == "valid":
                value = float(result[0]["value"][1])
                if not math.isfinite(value):
                    status, value = "nonfinite", None
        if status != "valid":
            flags.add(f"evaluated_{status}")
        evaluations.append({"evaluation_seconds": row["evaluation_time_unix"] - onset,
                            "cpu_percent": value, "status": status,
                            "observation_interval_seconds": [epoch(row["request_started_at"]) - onset,
                                                             epoch(row["request_finished_at"]) - onset]})
    request_values = {value for row in observed if row["kind"] == "prom_requests_raw" and row["status"] == "success"
                      for value in sample_map(row).values()}
    if None in request_values:
        flags.add("nonfinite_request")
    requests = sorted(value for value in request_values if value is not None)
    queries = [{"query_interval_seconds": [epoch(cycle["query"]["queryStartedAt"]) - onset,
                                            epoch(cycle["query"]["queryFinishedAt"]) - onset],
                "cpu_percent": cycle.get("decision", {}).get("currentCPU%"),
                "query_error": cycle["query"].get("queryError", "")}
               for cycle in cycles if cycle.get("query")
               and onset <= epoch(cycle["query"]["queryStartedAt"])
               and epoch(cycle["query"]["queryFinishedAt"]) < cutoff]
    return {"batch": batch, "run": directory.name, "input_directory": str(directory.resolve()),
            "load_onset_unix": onset, "cutoff_seconds": cutoff - onset,
            "scale_response_seconds": epoch(expansion["scaleWriteFinishedAt"]) - onset,
            "samples": sorted(samples.values(), key=lambda row: row["sample_seconds"]), "pairs": pairs,
            "request_values_cores": requests, "evaluations": evaluations, "window_samples": window_samples,
            "empty_raw_observations": empty_raw,
            "first_above_threshold": next((row for row in evaluations if row["status"] == "valid" and row["cpu_percent"] > 55), None),
            "controller_queries": queries, "quality_flags": sorted(flags)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", required=True)
    parser.add_argument("run_dirs", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, help="New file outside every input run; otherwise print JSON")
    args = parser.parse_args()
    try:
        if args.output and any(args.output.resolve().is_relative_to(path.resolve()) for path in args.run_dirs):
            raise ValueError("Output must be outside every input run directory")
        report = {"protocol_version": "metric-visibility-v1", "runs": [analyze(path, args.batch) for path in args.run_dirs]}
        encoded = json.dumps(report, indent=2, allow_nan=False) + "\n"
        if args.output:
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(encoded)
        else:
            print(encoded, end="")
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Metric visibility analysis failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
