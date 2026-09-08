#!/usr/bin/env python3
"""Explain diagnostic timing from retained raw evidence; never contact a cluster."""
from __future__ import annotations

import argparse
from bisect import bisect_right
from datetime import datetime
import json
import math
from pathlib import Path
import re
import sys

import yaml


EVENTS = {"Queried CPU utilization": "query", "Evaluated PredictiveHPA scaling decision": "decision",
          "Scaled Deployment": "scale", "Finished PredictiveHPA reconciliation": "finish"}


def epoch(value: object) -> float:
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    else:
        raise ValueError(f"Invalid timestamp: {value!r}")
    if not math.isfinite(result):
        raise ValueError("Non-finite timestamp")
    return result


def records(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def controller_cycles(path: Path) -> list[dict]:
    """Accept the controller's console or JSON logger without trusting display time."""
    cycles = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not any(message in line for message in EVENTS):
            continue
        if line.startswith("{"):
            row = json.loads(line)
            message = row.get("msg")
        else:
            parts = line.split("\t", 3)
            if len(parts) != 4 or parts[2] not in EVENTS:
                continue
            message, row = parts[2], json.loads(parts[3])
        if message not in EVENTS:
            continue
        start = row["reconcileStartedAt"]
        key = (row.get("reconcileID"), start)
        cycle = cycles.setdefault(key, {"reconcileID": key[0], "reconcileStartedAt": start})
        if EVENTS[message] in cycle:
            raise ValueError(f"Duplicate {message} in one reconciliation")
        cycle[EVENTS[message]] = row
    return sorted(cycles.values(), key=lambda row: epoch(row["reconcileStartedAt"]))


def controller_evidence(cycles: list[dict], onset: float, plan: dict) -> tuple[dict, dict, list[str]]:
    timing = {"first_scale_write_seconds": None, "first_expansion_decision_seconds": None,
              "first_controller_above_threshold_query_seconds": None,
              "first_new_pod_ready_seconds": None, "first_new_pod_request_seconds": None}
    eligible = []
    for cycle in cycles:
        finish, decision, query, scale = (cycle.get(name, {}) for name in ("finish", "decision", "query", "scale"))
        if finish and not finish.get("reconcileError") and not finish.get("error") and decision.get("finalDesired") == 1:
            finished = epoch(finish["reconcileFinishedAt"])
            if finished <= onset:
                eligible.append(finished)
        if decision and epoch(decision["decisionAt"]) >= onset:
            if (timing["first_expansion_decision_seconds"] is None
                    and decision.get("finalDesired", 0) > decision.get("currentReplicas", 0)
                    and not decision.get("skipReason")):
                timing["first_expansion_decision_seconds"] = epoch(decision["decisionAt"]) - onset
            if (timing["first_controller_above_threshold_query_seconds"] is None and query
                    and float(decision.get("currentCPU%", 0)) > 55 and not query.get("queryError")):
                timing["first_controller_above_threshold_query_seconds"] = epoch(query["queryFinishedAt"]) - onset
        if scale and epoch(scale["scaleWriteFinishedAt"]) >= onset:
            if scale["finalDesired"] > scale["previousDesiredReplicas"] and timing["first_scale_write_seconds"] is None:
                timing["first_scale_write_seconds"] = epoch(scale["scaleWriteFinishedAt"]) - onset
    requested = plan.get("requested_offset_seconds")
    gap = onset - max(eligible) if eligible else None
    cadence, tolerance = plan.get("requeue_seconds", 30), plan.get("phase_tolerance_seconds", 2)
    long_gap = gap is not None and gap > cadence + tolerance
    error = None if gap is None or requested is None or long_gap else min(
        abs(gap - requested), abs(gap - requested - cadence), abs(gap - requested + cadence))
    flags = []
    if not cycles:
        flags.append("missing_controller_diagnostic_events")
    if gap is None:
        flags.append("missing_pre_onset_idle_reconcile")
    if long_gap:
        flags.append("reconcile_gap_exceeds_nominal_interval")
    if error is not None and error > tolerance:
        flags.append("requested_phase_missed")
    return timing, {"requested_offset_seconds": requested, "requeue_seconds": cadence, "actual_reconcile_gap_seconds": gap,
                    "phase_error_seconds": error, "within_tolerance": error is not None and error <= tolerance,
                    "tolerance_seconds": tolerance}, flags


def source_evidence(observations: list[dict], onset: float) -> dict:
    samples = []
    seen = set()
    previous = None
    signal = None
    for row in sorted(observations, key=lambda r: epoch(r["request_started_at"])):
        if row.get("status") != "success" or row["kind"] not in ("prom_cpu_raw", "prom_cpu_evaluated"):
            continue
        started, finished = epoch(row["request_started_at"]), epoch(row["request_finished_at"])
        result = row["response"].get("data", {}).get("result", [])
        if row["kind"] == "prom_cpu_raw":
            for series in result:
                labels = series["metric"]
                for stamp, value in series.get("values", []):
                    stamp = epoch(stamp)
                    key = (json.dumps(labels, sort_keys=True), stamp)
                    if key in seen:
                        continue
                    seen.add(key)
                    if stamp <= onset:
                        continue
                    lower = None
                    if previous is not None and previous[1] - 90 < stamp <= previous[1]:
                        lower = previous[0] - onset
                    samples.append({"labels": labels, "sample_unix": stamp,
                                    "sample_seconds": stamp - onset, "counter_value": float(value),
                                    "visibility_interval_seconds": [lower, finished - onset]})
            previous = (started, float(row.get("evaluation_time_unix", started)))
        elif row["kind"] == "prom_cpu_evaluated" and signal is None:
            if len(result) == 1 and "value" in result[0]:
                stamp, value = result[0]["value"]
                if float(stamp) >= onset and math.isfinite(float(value)) and float(value) > 55:
                    signal = {"evaluation_seconds": float(stamp) - onset, "cpu_percent": float(value),
                              "observation_interval_seconds": [started - onset, finished - onset]}
    samples.sort(key=lambda sample: sample["sample_unix"])
    return {"threshold_cpu_percent": 55,
            "first_post_onset_cpu_sample": samples[0] if samples else None,
            "first_above_threshold_cpu_observation": signal,
            "post_onset_cpu_samples": samples}


def attach_query_source_evidence(cycles: list[dict], observations: list[dict]) -> None:
    source_rows = [row for row in observations if row["kind"] in ("prom_cpu_raw", "prom_requests_raw")]
    for cycle in cycles:
        if not cycle.get("query"):
            continue
        query_start = epoch(cycle["query"]["queryStartedAt"])
        evidence = {"cpu": [], "requests": [], "overlapping_observer_query_count": sum(
            epoch(row["request_started_at"]) <= query_start < epoch(row["request_finished_at"]) for row in source_rows),
            "interpretation": "Latest separately observed source samples; the controller may see newer data"}
        for kind, name in (("prom_cpu_raw", "cpu"), ("prom_requests_raw", "requests")):
            candidates = [row for row in source_rows if row["kind"] == kind and row.get("status") == "success"
                          and epoch(row["request_finished_at"]) <= query_start]
            if not candidates:
                continue
            latest = max(candidates, key=lambda row: epoch(row["request_finished_at"]))
            for series in latest["response"].get("data", {}).get("result", []):
                if not series.get("values"):
                    continue
                stamp, value = max(series["values"], key=lambda pair: epoch(pair[0]))
                evidence[name].append({"labels": series["metric"], "sample_unix": epoch(stamp),
                    "sample_age_at_query_seconds": query_start - epoch(stamp), "value": float(value),
                    "observer_response_finished_at": latest["request_finished_at"]})
        cycle["source_before_query"] = evidence


def pod_evidence(directory: Path, observations: list[dict], onset: float, marker: str) -> list[dict]:
    pods = {}
    successful = sorted((row for row in observations if row.get("status") == "success"),
                        key=lambda row: epoch(row["request_started_at"]))
    for row in successful:
        if row["kind"] != "pods":
            continue
        for pod in row["response"].get("items", []):
            metadata = pod["metadata"]
            created = epoch(metadata["creationTimestamp"])
            if created < onset:
                continue
            record = pods.setdefault(metadata["uid"], {"uid": metadata["uid"], "name": metadata["name"],
                "created_seconds": created - onset, "ready_seconds": None,
                "first_observed_interval_seconds": [epoch(row["request_started_at"]) - onset,
                                                     epoch(row["request_finished_at"]) - onset],
                "endpoint_ready_interval_seconds": None, "first_request_received_seconds": None,
                "first_access_log_emitted_seconds": None, "first_request_status": None,
                "request_timestamp_precision_seconds": 1})
            for condition in pod.get("status", {}).get("conditions", []):
                if condition.get("type") == "Ready" and condition.get("status") == "True":
                    ready = epoch(condition["lastTransitionTime"]) - onset
                    if record["ready_seconds"] is None or ready < record["ready_seconds"]:
                        record["ready_seconds"] = ready
                        record["first_ready_observation_interval_seconds"] = [
                            epoch(row["request_started_at"]) - onset, epoch(row["request_finished_at"]) - onset]
    for uid, pod in pods.items():
        previous = None
        for row in successful:
            if row["kind"] != "endpoints":
                continue
            ready = any(endpoint.get("targetRef", {}).get("uid") == uid and endpoint.get("conditions", {}).get("ready") is True
                        and bool(endpoint.get("addresses"))
                        for item in row["response"].get("items", []) for endpoint in item.get("endpoints", []))
            if ready:
                pod["endpoint_ready_interval_seconds"] = [previous, epoch(row["request_finished_at"]) - onset]
                break
            previous = epoch(row["request_started_at"]) - onset
        logfile = directory / "workload-access" / f"{pod['name']}_{uid}.log"
        if not logfile.exists():
            continue
        for line in logfile.read_text(encoding="utf-8", errors="replace").splitlines():
            if f'"phpa-benchmark/{marker}"' not in line:
                continue
            match = re.search(r'\[([^\]]+)\] "[^"\n]+" (\d{3}) ', line)
            if not match:
                continue
            received = datetime.strptime(match[1], "%d/%b/%Y:%H:%M:%S %z").timestamp() - onset
            emitted = epoch(line.split(" ", 1)[0]) - onset
            if received < -1:
                continue
            if pod["first_request_received_seconds"] is None or received < pod["first_request_received_seconds"]:
                pod["first_request_received_seconds"] = received
                pod["first_request_status"] = int(match[2])
            if pod["first_access_log_emitted_seconds"] is None or emitted < pod["first_access_log_emitted_seconds"]:
                pod["first_access_log_emitted_seconds"] = emitted
    return sorted(pods.values(), key=lambda row: row["created_seconds"])


def scenario_occupancy(directory: Path, onset: float, metadata: dict) -> dict:
    """Integrate the recorded 15s replica grid over the actual scenario window."""
    end, offered_end = onset + 541, onset + 181
    legacy_onset = metadata.get("load_start_time_unix")
    report = {"window_valid": False, "window_reference": "k6 scenario.startTime + 30 seconds",
              "start_time_unix": onset, "end_time_unix": end, "duration_seconds": 541,
              "sampling_precision_seconds": 15, "pod_seconds": None, "post_load_pod_seconds": None,
              "onset_offset_from_legacy_window_seconds": onset - legacy_onset if legacy_onset is not None else None,
              "legacy_window_reference": "integer process start + 30 seconds; original extract.json unchanged"}
    try:
        data = json.loads((directory / "prom.json").read_text(encoding="utf-8"))
        series = data["replicas"]["data"]["result"]
        if len(series) != 1:
            raise ValueError("Expected exactly one replica series")
        values = [(epoch(stamp), float(value)) for stamp, value in series[0]["values"]]
        if any(not math.isfinite(value) or value < 0 or value != int(value) for _, value in values):
            raise ValueError("Replica counts must be finite nonnegative integers")
        if any(right[0] <= left[0] for left, right in zip(values, values[1:])):
            raise ValueError("Replica timestamps must be unique and strictly increasing")
        stamps = [stamp for stamp, _ in values]
        first, last = bisect_right(stamps, onset) - 1, bisect_right(stamps, end)
        if first < 0 or last >= len(stamps):
            raise ValueError("Replica series must cover both scenario window boundaries")
        relevant = values[first:last + 1]
        if any(right[0] - left[0] > 15.001 for left, right in zip(relevant, relevant[1:])):
            raise ValueError("Replica coverage gap exceeds 15 seconds")
        total = post = 0.0
        first_up = None
        previous = relevant[0][1]
        for (start, value), (finish, _) in zip(relevant, relevant[1:]):
            if value > previous and first_up is None and start >= onset:
                first_up = start - onset
            previous = value
            total += value * max(0, min(finish, end) - max(start, onset))
            post += value * max(0, min(finish, end) - max(start, offered_end))
        report.update({"window_valid": True, "pod_seconds": total, "post_load_pod_seconds": post,
                       "first_sampled_replica_increase_seconds": first_up})
    except (OSError, ValueError, KeyError, TypeError) as error:
        report["error"] = str(error)
    return report


def scenario_schedule(directory: Path) -> dict:
    """Read the workload's actual schedule before post-load collection is complete."""
    attempts = [row["data"] for row in records(directory / "k6.json")
                if row.get("type") == "Point" and row.get("metric") == "latency_request_attempt"]
    if not attempts:
        raise ValueError("Missing latency_request_attempt evidence; process start is not scenario start")
    starts = {epoch(float(row["value"]) / 1000) for row in attempts}
    if any(start <= 0 for start in starts):
        raise ValueError("Scenario start must be a positive Unix epoch")
    if len(starts) != 1:
        raise ValueError("Diagnostic request points disagree on scenario.startTime")
    onset = starts.pop() + 30
    first_attempt = min(epoch(row["time"]) for row in attempts)
    if first_attempt < onset:
        raise ValueError("Diagnostic request attempt precedes the scheduled load onset")
    return {"load_onset_unix": onset, "offered_load_end_unix": onset + 181,
            "observation_end_unix": onset + 541, "first_request_attempt_unix": first_attempt}


def analyze(directory: Path) -> dict:
    metadata = yaml.safe_load((directory / "metadata.yaml").read_text(encoding="utf-8"))
    schedule = scenario_schedule(directory)
    onset = schedule["load_onset_unix"]
    observations = records(directory / "latency-observations.ndjson")
    plan = json.loads((directory / "latency-plan.json").read_text(encoding="utf-8"))
    cycles = controller_cycles(directory / "controller.log")
    attach_query_source_evidence(cycles, observations)
    timing, phase, flags = controller_evidence(cycles, onset, plan)
    phase["planned_load_onset_unix"] = plan.get("planned_load_onset_unix")
    phase["actual_onset_error_seconds"] = (onset - plan["planned_load_onset_unix"]
                                           if plan.get("planned_load_onset_unix") is not None else None)
    phase["gate_rounding_seconds"] = plan.get("gate_rounding_seconds")
    pods = pod_evidence(directory, observations, onset, metadata["experiment_id"])
    for target, source in (("ready", "ready_seconds"), ("request", "first_request_received_seconds"),
                           ("created", "created_seconds"), ("access_log_emitted", "first_access_log_emitted_seconds")):
        known = [row[source] for row in pods if row[source] is not None]
        timing[f"first_new_pod_{target}_seconds"] = min(known) if known else None
        if not known:
            flags.append(f"missing_new_pod_{target}")
    errors = [row for row in observations if row.get("status") != "success"]
    if errors:
        flags.append("observer_request_errors")
    occupancy = scenario_occupancy(directory, onset, metadata)
    if not occupancy["window_valid"]:
        flags.append("incomplete_scenario_replica_window")
    return {"protocol_version": "latency-diagnostic-v1", "experiment_id": metadata["experiment_id"],
            "timing": {"load_onset_unix": onset,
                       "first_request_attempt_seconds": schedule["first_request_attempt_unix"] - onset, **timing},
            "source_visibility": source_evidence(observations, onset), "phase": phase,
            "reconciliations": cycles, "new_pods": pods,
            "scenario_window_occupancy": occupancy,
            "quality": {"flags": flags, "observation_error_count": len(errors), "observation_errors": errors},
            "limitations": ["Visibility intervals describe separate observer queries, not exact controller ingestion time",
                            "A post-onset CPU counter sample alone does not prove increased demand",
                            "Apache request time has one-second precision; server log output is not a client success",
                            "Ready conditions, Service eligibility and tagged request evidence have different meanings"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--schedule-only", action="store_true",
                        help="Read actual k6 schedule boundaries without requiring post-load evidence")
    args = parser.parse_args()
    try:
        report = scenario_schedule(args.run_dir) if args.schedule_only else analyze(args.run_dir)
        destination = args.output or args.run_dir / ("latency-schedule.json" if args.schedule_only else "latency.json")
        destination.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        print(destination)
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Latency analysis failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
