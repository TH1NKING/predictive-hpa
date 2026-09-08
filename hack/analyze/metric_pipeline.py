#!/usr/bin/env python3
"""Compare retained metric pipeline snapshots without querying a live or post-run TSDB."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import sys

from latency import controller_cycles, epoch, records, scenario_schedule


EVALUATIONS = {"30": "prom_cpu_evaluated_30s", "60": "prom_cpu_evaluated"}
QUERY_KINDS = {"prom_cpu_raw", "prom_requests_raw", "prom_scrape_raw", *EVALUATIONS.values()}
METRIC_KINDS = QUERY_KINDS | {"prom_scrape_targets", "source_cadvisor"}


def evaluated(row: dict, onset: float) -> dict:
    value = None
    if row["status"] != "success" or row.get("response", {}).get("status") == "error":
        status = "error"
    else:
        result = row["response"]["data"]["result"]
        status = "empty" if not result else "multiple" if len(result) != 1 else "valid"
        if status == "valid":
            value = float(result[0]["value"][1])
            if not math.isfinite(value):
                status, value = "nonfinite", None
    return {"status": status, "cpu_percent": value,
            "evaluation_seconds": epoch(row["evaluation_time_unix"]) - onset,
            "observation_interval_seconds": [epoch(row["request_started_at"]) - onset,
                                             epoch(row["request_finished_at"]) - onset],
            "error": row.get("error", row.get("response", {}).get("error"))}


def raw_values(row: dict) -> list[tuple]:
    return [(series["metric"], epoch(stamp), float(value))
            for series in row["response"]["data"]["result"] for stamp, value in series["values"]]


def raw_evidence(rows: list[dict], onset: float, width: float, flags: set) -> tuple:
    snapshots, samples, request_values = [], {}, set()
    for row in sorted(rows, key=lambda value: epoch(value["request_finished_at"])):
        kind = row["kind"]
        if kind not in ("prom_cpu_raw", "prom_requests_raw"):
            continue
        if row["status"] != "success" or row.get("response", {}).get("status") == "error":
            flags.add(f"{kind}_error")
            continue
        values = raw_values(row)
        if not values:
            flags.add(f"{kind}_empty")
        if kind == "prom_requests_raw":
            for _, _, value in values:
                if math.isfinite(value):
                    request_values.add(value)
                else:
                    flags.add("nonfinite_request")
            continue
        keys = set()
        for labels, stamp, value in values:
            key = (json.dumps(labels, sort_keys=True), stamp)
            keys.add(key)
            finite = value if math.isfinite(value) else None
            if finite is None:
                flags.add("nonfinite_counter")
            if key in samples:
                if samples[key]["counter_value"] != finite:
                    samples[key]["conflicting_values"] = True
                    flags.add("conflicting_sample_values")
                continue
            lower = [epoch(previous["request_started_at"]) - onset for previous, prior in snapshots
                     if epoch(previous["request_finished_at"]) <= epoch(row["request_started_at"])
                     and epoch(previous["evaluation_time_unix"]) - width < stamp <= epoch(previous["evaluation_time_unix"])
                     and key not in prior]
            samples[key] = {"labels": labels, "sample_seconds": stamp - onset, "counter_value": finite,
                            "conflicting_values": False,
                            "visibility_interval_seconds": [max(lower) if lower else None,
                                                            epoch(row["request_finished_at"]) - onset]}
        snapshots.append((row, keys))
    for label in {key[0] for key in samples}:
        series = sorted((stamp, sample) for (key, stamp), sample in samples.items() if key == label)
        for (_, previous), (_, current) in zip(series, series[1:]):
            if (previous["counter_value"] is not None and current["counter_value"] is not None
                    and not previous["conflicting_values"] and not current["conflicting_values"]
                    and current["counter_value"] < previous["counter_value"]):
                current["counter_reset_from_previous"] = True
                flags.add("counter_reset")
    return sorted(samples.values(), key=lambda row: row["sample_seconds"]), sorted(request_values)


def window_counts(rows: list[dict], onset: float) -> list[dict]:
    windows = []
    for row in rows:
        if row["kind"] != "prom_cpu_raw" or row["status"] != "success" or row["response"].get("status") == "error":
            continue
        evaluation = epoch(row["evaluation_time_unix"])
        series = {}
        for labels, stamp, value in raw_values(row):
            series.setdefault(json.dumps(labels, sort_keys=True), {})[stamp] = value
        for labels, values in series.items():
            windows.append({"labels": json.loads(labels), "evaluation_seconds": evaluation - onset,
                            "counts": {str(width): sum(evaluation - width < stamp <= evaluation for stamp in values)
                                       for width in (30, 60)},
                            "nonfinite_counts": {str(width): sum(evaluation - width < stamp <= evaluation and not math.isfinite(value)
                                                                 for stamp, value in values.items()) for width in (30, 60)}})
    return windows


def source_evidence(rows: list[dict], raw: list[dict], onset: float, flags: set) -> tuple:
    samples, matches = [], []
    for row in rows:
        if row["kind"] != "source_cadvisor":
            continue
        if row["status"] != "success":
            flags.add("source_cadvisor_error")
            continue
        if not row["response"]["lines"]:
            flags.add("source_cadvisor_empty")
        for line in row["response"]["lines"]:
            item = {"cycle_id": row["cycle_id"], "source_node": row.get("source_node"), "line": line,
                    "observation_interval_seconds": [epoch(row["request_started_at"]) - onset,
                                                     epoch(row["request_finished_at"]) - onset]}
            parsed = re.fullmatch(r'([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)(?:\s+([-+0-9.eE]+))?\s*', line)
            if parsed is None:
                item["status"] = "unparsed"
                samples.append(item)
                flags.add("unparsed_source_line")
                continue
            name, label_text, value_text, stamp_text = parsed.groups()
            labels, remainder = {}, label_text or ""
            while remainder:
                label = re.match(r'\s*([a-zA-Z_][a-zA-Z0-9_]*)=("(?:[^"\\]|\\.)*")\s*(?:,\s*|$)', remainder)
                if label is None:
                    raise ValueError(f"Invalid source labels: {label_text}")
                labels[label[1]] = json.loads(label[2])
                remainder = remainder[label.end():]
            value = float(value_text)
            finite = value if math.isfinite(value) else None
            stamp = epoch(float(stamp_text) / 1000) if stamp_text is not None else None
            item.update({"status": "valid" if finite is not None else "nonfinite", "metric": name,
                         "labels": labels, "value": finite, "explicit_timestamp_milliseconds": float(stamp_text) if stamp_text else None,
                         "explicit_sample_seconds": stamp - onset if stamp is not None else None,
                         "last_seen_seconds": finite - onset if name == "container_last_seen" and finite is not None else None,
                         "timestamp_interpretation": "Explicit exposition sample timestamp; not proven exact CPU update time"})
            if name == "container_last_seen":
                item["timestamp_interpretation"] = "Container last-seen value and its exposition timestamp; not CPU update time"
            if finite is None:
                flags.add("nonfinite_source_value")
            samples.append(item)
            if name != "container_cpu_usage_seconds_total":
                continue
            candidates = [sample for sample in raw if finite is not None and sample["counter_value"] == finite
                          and not sample["conflicting_values"]
                          and all(sample["labels"].get(key) == value for key, value in labels.items() if key != "__name__")
                          and (stamp is None or sample["sample_seconds"] == stamp - onset)]
            status = "no_matching_raw_sample" if not candidates else (
                "ambiguous_sample_match" if stamp is not None and len(candidates) > 1 else (
                "exact_sample_match" if stamp is not None else (
                "ambiguous_counter_value" if len(candidates) > 1 else "counter_value_only")))
            matches.append({"source_sample_index": len(samples) - 1, "status": status, "candidates": candidates,
                            "ingestion_time_seconds": None,
                            "interpretation": "Label, timestamp and counter agreement is identity evidence; observer bounds are not ingestion timestamps"})
            if status != "exact_sample_match":
                flags.add(f"source_raw_{status}")
    return samples, matches


def scrape_evidence(rows: list[dict], onset: float, flags: set) -> list[dict]:
    scrapes = {}
    for row in rows:
        if row["kind"] != "prom_scrape_targets":
            continue
        if row["status"] != "success" or row.get("response", {}).get("status") == "error":
            flags.add("prom_scrape_targets_error")
            continue
        targets = row["response"]["data"]["activeTargets"]
        if not targets:
            flags.add("prom_scrape_targets_empty")
        for target in targets:
            identity = {name: target.get(name) for name in ("labels", "discoveredLabels", "scrapeUrl")}
            stamp = epoch(target["lastScrape"])
            key = (json.dumps(identity, sort_keys=True), stamp)
            item = scrapes.setdefault(key, {"target": identity, "reported_scrape_seconds": stamp - onset,
                                          "reported_scrape_time": target["lastScrape"], "reports": [],
                                          "observation_count": 0, "first_observation_interval_seconds": [
                                              epoch(row["request_started_at"]) - onset, epoch(row["request_finished_at"]) - onset]})
            report = {"reported_duration_seconds": float(target["lastScrapeDuration"]),
                      "health": target["health"], "last_error": target["lastError"]}
            if report not in item["reports"]:
                item["reports"].append(report)
            item["observation_count"] += 1
            if len(item["reports"]) > 1:
                flags.add("inconsistent_scrape_reports")
            if report["health"] != "up" or report["last_error"]:
                flags.add("scrape_reported_error")
    return sorted(scrapes.values(), key=lambda item: item["reported_scrape_seconds"])


def scrape_metric_evidence(rows: list[dict], onset: float, flags: set) -> tuple:
    observations, samples = [], {}
    for row in rows:
        if row["kind"] != "prom_scrape_raw":
            continue
        status = "error" if row["status"] != "success" or row.get("response", {}).get("status") == "error" else "success"
        values = raw_values(row) if status == "success" else []
        if status == "success" and not values:
            status = "empty"
        if status != "success":
            flags.add(f"prom_scrape_raw_{status}")
        observations.append({"cycle_id": row["cycle_id"], "evaluation_seconds": epoch(row["evaluation_time_unix"]) - onset,
                             "status": status, "response": row.get("response"), "error": row.get("error"),
                             "observation_interval_seconds": [epoch(row["request_started_at"]) - onset,
                                                              epoch(row["request_finished_at"]) - onset]})
        for labels, stamp, value in values:
            finite = value if math.isfinite(value) else None
            key = (json.dumps(labels, sort_keys=True), stamp)
            if finite is None:
                flags.add("nonfinite_scrape_metric")
            if labels.get("__name__") == "up" and finite == 0:
                flags.add("scrape_up_zero")
            if key in samples and samples[key]["value"] != finite:
                samples[key]["conflicting_values"] = True
                flags.add("conflicting_scrape_metric_values")
            samples.setdefault(key, {"labels": labels, "sample_seconds": stamp - onset, "value": finite,
                                     "conflicting_values": False})
    return observations, sorted(samples.values(), key=lambda item: item["sample_seconds"])


def overhead_evidence(rows: list[dict]) -> dict:
    cycles = []
    requests = [row for row in rows if row["kind"] in METRIC_KINDS]
    for row in rows:
        if row["kind"] != "observer_cycle":
            continue
        duration = float(row["duration_seconds"])
        interval = float(row["observation_interval_seconds"])
        cycles.append({"cycle_id": row["cycle_id"], "duration_seconds": duration,
                       "interval_seconds": interval, "overrun_seconds": max(0, duration - interval),
                       "reported_overrun_seconds": row.get("overrun_seconds")})
    return {"scope": "All retained observer cycles, including outside the pre-expansion comparison",
            "metric_request_count": len(requests),
            "prometheus_query_count": sum(row["kind"] in QUERY_KINDS for row in requests),
            "failed_metric_request_count": sum(row["status"] != "success" for row in requests),
            "total_metric_request_duration_seconds": sum(float(row["duration_seconds"]) for row in requests),
            "total_cycle_duration_seconds": sum(row["duration_seconds"] for row in cycles),
            "overrun_cycle_count": sum(row["overrun_seconds"] > 0 for row in cycles), "cycles": cycles}


def validate_observations(rows: list[dict], onset: float) -> None:
    """Validate all retained metric evidence, even when no expansion gives a cutoff."""
    for row in rows:
        if row["cycle_id"] is None:
            raise ValueError("Missing observer cycle id")
        epoch(row["evaluation_time_unix"])
        started, finished = epoch(row["request_started_at"]), epoch(row["request_finished_at"])
        duration = float(row["duration_seconds"])
        if finished < started or not math.isfinite(duration) or duration < 0:
            raise ValueError("Invalid observer request interval or duration")
        if row["kind"] == "observer_cycle":
            interval = float(row["observation_interval_seconds"])
            if not math.isfinite(interval) or interval <= 0:
                raise ValueError("Invalid observer cycle interval")
        elif row["status"] == "success" and row.get("response", {}).get("status") != "error":
            if row["kind"] in EVALUATIONS.values():
                evaluated(row, onset)
            elif row["kind"] in QUERY_KINDS:
                raw_values(row)
            elif row["kind"] == "source_cadvisor":
                if not isinstance(row["response"]["lines"], list) or any(not isinstance(line, str) for line in row["response"]["lines"]):
                    raise ValueError("Invalid source exposition lines")
            elif row["kind"] == "prom_scrape_targets":
                for target in row["response"]["data"]["activeTargets"]:
                    epoch(target["lastScrape"])
                    duration = float(target["lastScrapeDuration"])
                    if not math.isfinite(duration) or duration < 0:
                        raise ValueError("Invalid reported scrape duration")
                    target["health"], target["lastError"]


def analyze(directory: Path, batch: str) -> dict:
    flags = set()
    onset = scenario_schedule(directory)["load_onset_unix"]
    controller = controller_cycles(directory / "controller.log")
    plan = json.loads((directory / "latency-plan.json").read_text(encoding="utf-8"))
    observations = records(directory / "latency-observations.ndjson")
    observations = [row for row in observations if row["kind"] in METRIC_KINDS | {"observer_cycle"}]
    source_range = float(plan["source_range_seconds"])
    if not math.isfinite(source_range) or source_range < 60:
        raise ValueError("Source range must cover both 30-second and 60-second windows")
    validate_observations(observations, onset)
    expansion = min((cycle["scale"] for cycle in controller if cycle.get("scale")
                     and cycle["scale"]["finalDesired"] > cycle["scale"]["previousDesiredReplicas"]
                     and epoch(cycle["scale"]["scaleWriteStartedAt"]) >= onset),
                    key=lambda row: epoch(row["scaleWriteStartedAt"]), default=None)
    cutoff = epoch(expansion["scaleWriteStartedAt"]) if expansion else None
    if expansion is None:
        flags.add("missing_successful_expansion")
    observed = [row for row in observations if cutoff is not None and epoch(row["request_finished_at"]) < cutoff]
    raw_samples, request_values = raw_evidence(observed, onset, source_range, flags)
    eligible = [row for row in observed if epoch(row["evaluation_time_unix"]) >= onset]
    source_samples, source_matches = source_evidence(eligible, raw_samples, onset, flags)
    scrapes = scrape_evidence(eligible, onset, flags)
    scrape_observations, scrape_samples = scrape_metric_evidence(eligible, onset, flags)
    overhead = overhead_evidence(observations)
    grouped = {}
    for row in observed:
        if row["evaluation_time_unix"] >= onset:
            grouped.setdefault(row["cycle_id"], []).append(row)
    cycles = []
    for cycle_id, rows in grouped.items():
        times = {epoch(row["evaluation_time_unix"]) for row in rows}
        evaluation = min(times)
        values = {}
        for width, kind in EVALUATIONS.items():
            matches = [evaluated(row, onset) for row in rows if row["kind"] == kind]
            if len(matches) == 1:
                values[width] = matches[0]
            else:
                values[width] = {"status": "missing" if not matches else "duplicate", "cpu_percent": None,
                                 "observations": matches}
            if values[width]["status"] != "valid":
                flags.add(f'evaluated_{width}_{values[width]["status"]}')
        pair = "duplicate" if any(v["status"] == "duplicate" for v in values.values()) else (
            "missing" if any(v["status"] == "missing" for v in values.values()) else (
            "mismatched" if len(times) != 1 else "matched"))
        if len(times) != 1:
            flags.add("evaluation_time_mismatch")
        cycles.append({"cycle_id": cycle_id, "evaluation_seconds": evaluation - onset,
                       "evaluation_times_seconds": sorted(stamp - onset for stamp in times),
                       "pair_status": pair, "evaluations": values, "window_samples": window_counts(rows, onset)})
    cycles.sort(key=lambda row: row["evaluation_seconds"])
    first = {width: next((row["evaluations"][width] for row in cycles
                          if row["pair_status"] == "matched" and row["evaluations"][width]["status"] == "valid"
                          and row["evaluations"][width]["cpu_percent"] > 55), None) for width in EVALUATIONS}
    difference = first["60"]["evaluation_seconds"] - first["30"]["evaluation_seconds"] if all(first.values()) else None
    return {"batch": batch, "run": directory.name, "input_directory": str(directory.resolve()),
            "load_onset_unix": onset, "cutoff_seconds": cutoff - onset if cutoff is not None else None,
            "cycles": cycles, "first_above_threshold": first,
            "first_crossing_60_minus_30_seconds": difference,
            "raw_samples": raw_samples, "request_values_cores": request_values,
            "source_samples": source_samples, "source_raw_matches": source_matches,
            "scrapes": scrapes, "overhead": overhead,
            "scrape_metric_observations": scrape_observations, "scrape_metric_samples": scrape_samples,
            "scrape_interpretation": "Reported scrape time may be aligned; duration covers scrape and append work, not commit; their sum is not an ingestion timestamp",
            "snapshot_interpretation": "Equal evaluation times align the windows; sequential HTTP requests do not provide an atomic TSDB snapshot",
            "crossing_interpretation": "Difference between first observed threshold crossings in matched cycles, not a causal estimate",
            "quality_flags": sorted(flags)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", required=True)
    parser.add_argument("run_dirs", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, help="New file outside every input run; otherwise print JSON")
    args = parser.parse_args()
    try:
        if args.output and any(args.output.resolve().is_relative_to(path.resolve()) for path in args.run_dirs):
            raise ValueError("Output must be outside every input run directory")
        report = {"protocol_version": "metric-pipeline-v1", "runs": [analyze(path, args.batch) for path in args.run_dirs]}
        encoded = json.dumps(report, indent=2, allow_nan=False) + "\n"
        if args.output:
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(encoded)
        else:
            print(encoded, end="")
        return 0
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        print(f"Metric pipeline analysis failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
