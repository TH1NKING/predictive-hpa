#!/usr/bin/env python3
"""Analyze retained live-observation baseline evidence without contacting a cluster."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sys

import yaml

from extract import load_k6
from latency import controller_cycles, epoch, records


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path.name}")
    return value


def validate_plan(plan: dict) -> None:
    if plan["protocol_version"] != "live-baseline-v1" or plan["startup_mode"] not in ("warm", "cold"):
        raise ValueError("Unsupported live baseline protocol or startup mode")
    if plan["decision_mode"] not in ("Current", "Predictive", "Hybrid"):
        raise ValueError("Unsupported decision mode")
    if plan["pattern"] not in ("step", "ramp"):
        raise ValueError("Unsupported load pattern")
    for name in ("rps", "interval_seconds", "requeue_seconds", "quiet_seconds", "offered_duration_seconds", "post_load_tail_seconds"):
        value = plan[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"Plan {name} must be a finite positive number")
    if plan["offered_duration_seconds"] != {"step": 181, "ramp": 240}[plan["pattern"]]:
        raise ValueError("Offered duration does not match the frozen load pattern")
    if plan["quiet_seconds"] != 30 or plan["post_load_tail_seconds"] != 360:
        raise ValueError("Quiet period and observation tail must match live-baseline-v1")
    epoch(plan["controller_started_at"])


def schedule(directory: Path, plan: dict) -> dict:
    candidates = []
    for name in ("k6-warnings.log", "k6-stdout.log"):
        path = directory / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            # k6 text logging may quote and escape the console message.
            normalized = line.replace('\\"', '"')
            match = re.search(r"PHPA_BASELINE_SCHEDULE\s+(\{.*?\})", normalized)
            if match:
                candidates.append(json.loads(match[1]))
    if not candidates:
        raise ValueError("Missing actual k6 PHPA_BASELINE_SCHEDULE evidence")
    first = candidates[0]
    start, onset, end = (epoch(first[key]) for key in ("scenario_start_unix", "onset_unix", "offered_end_unix"))
    if any(row != first for row in candidates):
        raise ValueError("Conflicting k6 schedule markers")
    if start <= 0 or abs(onset - start - plan["quiet_seconds"]) > 0.001 or abs(
            end - onset - plan["offered_duration_seconds"]) > 0.001:
        raise ValueError("Actual k6 schedule disagrees with plan")
    if first.get("pattern", plan["pattern"]) != plan["pattern"]:
        raise ValueError("Actual k6 pattern disagrees with plan")
    return {"scenario_start_unix": start, "load_onset_unix": onset, "offered_load_end_unix": end,
        "observation_end_unix": end + plan["post_load_tail_seconds"]}


def business_evidence(directory: Path, actual: dict) -> dict:
    completion = completion_evidence(directory, actual)
    points = [row for row in records(directory / "k6.json") if row.get("type") == "Point"]
    counts = {name: 0 for name in ("http_reqs", "http_req_duration", "http_req_failed", "baseline_request_attempt")}
    for point in points:
        name, data = point.get("metric"), point["data"]
        if name not in (*counts, "dropped_iterations"):
            continue
        value = data["value"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("k6 metric values must be finite and nonnegative")
        point_time = epoch(data["time"])
        if point_time < actual["load_onset_unix"] - completion["attempt_timestamp_tolerance_seconds"]:
            raise ValueError("k6 request evidence precedes actual onset")
        if point_time > completion["process_end_unix"] + completion["process_timestamp_precision_seconds"]:
            raise ValueError("k6 request evidence follows the recorded process completion")
        if name == "baseline_request_attempt" and point_time > actual["offered_load_end_unix"] + completion["attempt_timestamp_tolerance_seconds"]:
            raise ValueError("k6 request attempt follows the offered load window")
        if name in counts:
            counts[name] += int(value) if name == "http_reqs" else 1
        if name == "http_req_duration" and "status" not in data.get("tags", {}):
            raise ValueError("k6 duration evidence lacks HTTP status")
        if name == "http_req_failed" and value not in (0, 1):
            raise ValueError("Invalid k6 failure indicator")
        if name in ("http_reqs", "dropped_iterations") and int(value) != value:
            raise ValueError("Invalid k6 request/drop count")
        if name == "baseline_request_attempt" and abs(value / 1000 - actual["scenario_start_unix"]) > 0.001:
            raise ValueError("k6 request marker disagrees with actual scenario start")
    if counts["http_reqs"] == 0 or len(set(counts.values())) != 1:
        raise ValueError("Incomplete k6 requests, durations, failures or attempt markers")
    result, warnings = load_k6(directory / "k6.json")
    if warnings:
        raise ValueError("; ".join(warnings))
    result["integrity"] = {**summary_evidence(directory, result, counts["baseline_request_attempt"]), **completion}
    return result


def completion_evidence(directory: Path, actual: dict) -> dict:
    runner = read_json(directory / "k6-runner.json")
    exit_code = (directory / "k6-exit-code").read_text(encoding="utf-8").strip()
    if (exit_code != "0" or runner["status"] != "success" or type(runner["k6_exit_code"]) is not int
            or runner["k6_exit_code"] != 0 or runner["failure_reason"] != ""):
        raise ValueError("k6 runner did not report successful execution and cleanup")
    times = []
    for name in ("k6-start-time-unix", "k6-end-time-unix"):
        value = (directory / name).read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"[1-9][0-9]{0,11}", value):
            raise ValueError(f"Invalid integer process timestamp in {name}")
        times.append(int(value))
    start, end = times
    # The shell records date +%s around k6. Its end timestamp can be rounded
    # down by one second. Allow bounded runtime drain (10s request policy plus
    # 30s executor grace), but use the actual process end to bound responses:
    # under load a timeout callback need not run at the exact policy deadline.
    if (start > actual["scenario_start_unix"] or end < start
            or end + 1 < actual["offered_load_end_unix"]
            or end > actual["offered_load_end_unix"] + 40 + 1):
        raise ValueError("k6 process timestamps do not cover the offered load window and bounded drain")
    return {"process_start_unix": start, "process_end_unix": end, "process_timestamp_precision_seconds": 1,
        "attempt_timestamp_tolerance_seconds": 0.01, "maximum_drain_seconds": 40}


def summary_evidence(directory: Path, raw: dict, attempt_count: int) -> dict:
    metrics = read_json(directory / "k6-summary.json")["metrics"]
    if not isinstance(metrics, dict):
        raise ValueError("k6 summary metrics must be an object")

    def values(name: str) -> dict:
        metric = metrics[name]
        if not isinstance(metric, dict):
            raise ValueError(f"Invalid k6 summary metric {name}")
        value = metric.get("values", metric)
        if not isinstance(value, dict):
            raise ValueError(f"Invalid k6 summary metric {name}")
        return value

    def number(value: object, label: str, *, integer: bool = False) -> float:
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                or value < 0 or (integer and int(value) != value)):
            raise ValueError(f"Invalid k6 summary {label}")
        return value

    requests = number(values("http_reqs")["count"], "request count", integer=True)
    failed = values("http_req_failed")
    # In a k6 Rate, passes count nonzero observations: for http_req_failed
    # those are failures, despite the field's counterintuitive name.
    failures = number(failed["passes"], "failure count", integer=True)
    nonfailures = number(failed["fails"], "nonfailure count", integer=True)
    rate = number(failed.get("value", failed.get("rate")), "failure rate")
    dropped = number(values("dropped_iterations")["count"], "dropped count", integer=True) if "dropped_iterations" in metrics else 0
    if (requests != raw["total_requests"] or failures != raw["failed_count"] or failures + nonfailures != requests
            or not math.isclose(rate, failures / requests, rel_tol=0, abs_tol=1e-12)
            or dropped != raw["dropped_iterations"]):
        raise ValueError("k6 summary request, failure or dropped counts disagree with raw evidence")
    has_attempt_count = "baseline_request_attempt" in metrics and "count" in values("baseline_request_attempt")
    if has_attempt_count and number(values("baseline_request_attempt")["count"], "attempt count", integer=True) != attempt_count:
        raise ValueError("k6 summary attempt count disagrees with raw evidence")
    p95 = number(values("http_req_duration")["p(95)"], "all-request p95")
    # extract.load_k6 rounds milliseconds to two decimal places; this only
    # accommodates that serialization precision, not different percentile rules.
    if not math.isclose(p95, raw["duration_p95_ms"], rel_tol=0, abs_tol=0.01):
        raise ValueError("k6 summary all-request p95 disagrees with raw evidence")
    return {"summary_requests": requests, "summary_failures": failures, "summary_dropped_iterations": dropped,
        "summary_all_request_p95_ms": p95, "summary_attempt_count_available": has_attempt_count}


def validate_evidence(metadata: dict, plan: dict, gate: dict, status: dict, rows: list[dict],
                      cycles: list[dict], actual: dict) -> float:
    if not isinstance(metadata, dict) or not isinstance(metadata.get("experiment_id"), str) or not metadata["experiment_id"]:
        raise ValueError("Metadata must identify the experiment")
    if metadata["result"]["status"] != "success" or metadata["result"].get("failure_reason"):
        raise ValueError("Runner did not report successful completion and cleanup")
    if metadata.get("live_baseline") is not True or metadata["decision_mode"] != plan["decision_mode"]:
        raise ValueError("Metadata baseline identity or mode disagrees with plan")
    if metadata.get("pattern", plan["pattern"]) != plan["pattern"]:
        raise ValueError("Metadata pattern disagrees with plan")
    if status["status"] != "success" or status["owned_processes_stopped"] is not True or status["errors"] or status["gate_released"] is not True:
        raise ValueError("Observer did not finish successfully and stop its owned processes")
    if gate["startup_mode"] != plan["startup_mode"] or not gate["target_uid"] or not gate["phpa_uid"]:
        raise ValueError("Gate startup mode or resource identity is missing")
    if metadata.get("startup_mode", plan["startup_mode"]) != plan["startup_mode"]:
        raise ValueError("Metadata startup mode disagrees with plan")
    for key in ("target_uid", "phpa_uid"):
        if key in plan and plan[key] != gate[key]:
            raise ValueError(f"Plan {key} disagrees with gate")
    if "source_sha256" in plan and plan["source_sha256"] != metadata.get("benchmark_source_sha256"):
        raise ValueError("Plan source SHA256 disagrees with metadata")
    if "source_sha256" in plan and not re.fullmatch(r"[a-f0-9]{64}", plan["source_sha256"]):
        raise ValueError("Plan source SHA256 must be a lowercase SHA256 digest")
    release = epoch(gate["released_at"])
    controller_start = epoch(plan["controller_started_at"])
    if not controller_start <= release <= actual["scenario_start_unix"]:
        raise ValueError("Controller start, gate release and scenario start are inconsistent")
    if not cycles or not any(row.get("kind") == "state" and row.get("status") == "success" for row in rows):
        raise ValueError("Missing controller cycles or successful state observations")
    for row in rows:
        if row.get("kind") != "state":
            continue
        for resource, key in (("deployment", "target_uid"), ("phpa", "phpa_uid")):
            obj = row.get("response", {}).get(resource)
            if obj is None:
                if row.get("status") == "success":
                    raise ValueError(f"Successful state lacks {resource}")
                continue
            if obj["metadata"]["uid"] != gate[key]:
                raise ValueError(f"{resource} UID disagrees with gate")
            if resource == "phpa" and obj["spec"].get("decisionMode") != plan["decision_mode"]:
                raise ValueError("PHPA decision mode disagrees with plan")
        if row.get("status") == "success":
            response = row["response"]
            target = response["phpa"]["spec"]["scaleTargetRef"]
            if target["kind"] != "Deployment" or target["name"] != response["deployment"]["metadata"]["name"]:
                raise ValueError("PHPA target disagrees with sampled Deployment")
    log_start = controller_start
    if plan["startup_mode"] == "warm":
        anchor = gate["anchor"]
        if not isinstance(anchor, dict):
            raise ValueError("Warm gate requires a verified anchor reconciliation")
        matching = [item for item in cycles if item["reconcileStartedAt"] == anchor["reconcileStartedAt"]]
        if len(matching) != 1 or any(matching[0].get(key) != anchor.get(key) for key in ("query", "decision", "finish")):
            raise ValueError("Warm gate anchor disagrees with retained controller log")
        query, decision, finish = (anchor[key] for key in ("query", "decision", "finish"))
        if (query.get("queryError") or query["samples"] < 2 or decision["samples"] < 2
                or finish.get("reconcileError") or decision["currentReplicas"] != 1 or decision["finalDesired"] != 1
                or not 0 <= float(decision["currentCPU%"]) < 5):
            raise ValueError("Warm gate lacks accepted history or successful reconciliation")
        if (epoch(decision["stabilizationEvaluatedAt"]) <= epoch(decision["coldStartProtectedUntil"])
                or decision["coldStartProtection"] is not False
                or not 0 <= release - epoch(finish["reconcileFinishedAt"]) <= 2 * plan["requeue_seconds"] + 12):
            raise ValueError("Warm gate released before the initial protection window completed")
        log_start = epoch(anchor["reconcileStartedAt"])
        prior = [item for item in samples(rows, "phpa", "metrics") if item["finish"] <= actual["load_onset_unix"]]
        if not prior or prior[-1]["value"] != 0 or actual["load_onset_unix"] - prior[-1]["finish"] > 2 * plan["interval_seconds"]:
            raise ValueError("Warm onset lacks fresh current-generation MetricsReady=True evidence")
    elif gate.get("anchor") is not None:
        raise ValueError("Cold gate must keep warm anchor absent")
    for item in cycles:
        if epoch(item["reconcileStartedAt"]) < log_start:
            continue
        for name in ("decision", "scale"):
            if item.get(name) and item[name]["decisionMode"] != plan["decision_mode"]:
                raise ValueError("Controller decision mode disagrees with plan")
    return log_start


def read_interval(row: dict, resource: str) -> tuple[float, float]:
    timing = row.get("requests", {}).get(resource, row)
    start, end = (epoch(timing[key]) for key in ("request_started_at", "request_finished_at"))
    if end < start:
        raise ValueError("Observer request finishes before it starts")
    return start, end


def samples(rows: list[dict], resource: str, field: str) -> list[dict]:
    result = []
    for row in rows:
        if row.get("kind") != "state":
            continue
        start, finish = read_interval(row, resource)
        value = None
        if row.get("status") == "success":
            obj = row["response"][resource]
            if field == "requested":
                value = obj["spec"]["replicas"]
            elif field == "ready":
                value = obj.get("status", {}).get("readyReplicas", 0)
            else:
                conditions = [c for c in obj.get("status", {}).get("conditions", []) if c.get("type") == "MetricsReady"]
                if len(conditions) == 1 and conditions[0].get("observedGeneration") == obj["metadata"].get("generation"):
                    value = {"True": 0, "False": 1}.get(conditions[0].get("status"))
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0 or int(value) != value):
                raise ValueError("Replica counts must be finite nonnegative integers")
        result.append({"start": start, "finish": finish, "value": value})
    if any(right["start"] < left["finish"] or right["finish"] <= left["finish"]
            for left, right in zip(result, result[1:])):
        raise ValueError("Observer resource reads must be ordered without duplicate or overlapping intervals")
    return result


def integrate(values: list[dict], onset: float, end: float, offered_end: float, max_gap: float) -> dict:
    total = post = covered = 0.0
    intervals = []
    for left, right in zip(values, values[1:]):
        begin, finish = max(onset, left["finish"]), min(end, right["finish"])
        if finish <= begin:
            continue
        if left["value"] is None or right["value"] is None or right["finish"] - left["finish"] > max_gap:
            continue
        covered += finish - begin
        total += left["value"] * (finish - begin)
        post += left["value"] * max(0, finish - max(begin, offered_end))
        intervals.append([begin, finish])
    unknown = []
    cursor = onset
    for begin, finish in intervals:
        if begin > cursor:
            unknown.append([cursor - onset, begin - onset])
        cursor = finish
    if cursor < end:
        unknown.append([cursor - onset, end - onset])
    complete = abs(covered - (end - onset)) < 0.000001
    return {"pod_seconds": total if complete else None, "covered_pod_seconds": total,
        "post_load_pod_seconds": post if complete else None, "covered_seconds": covered,
        "unknown_seconds": end - onset - covered, "unknown_intervals_seconds": unknown,
        "coverage_complete": complete}


def readiness_evidence(values: list[dict], onset: float, end: float, max_gap: float) -> dict:
    coverage = integrate(values, onset, end, end, max_gap)
    false_intervals = []
    for left, right in zip(values, values[1:]):
        begin, finish = max(onset, left["finish"]), min(end, right["finish"])
        if finish <= begin or left["value"] != 1 or right["value"] is None or right["finish"] - left["finish"] > max_gap:
            continue
        if false_intervals and false_intervals[-1][1] == begin - onset:
            false_intervals[-1][1] = finish - onset
        else:
            false_intervals.append([begin - onset, finish - onset])
    return {"sampled_false_seconds": coverage["covered_pod_seconds"],
        "sampled_false_intervals_seconds": false_intervals,
        "unknown_seconds": coverage["unknown_seconds"], "unknown_intervals_seconds": coverage["unknown_intervals_seconds"],
        "coverage_complete": coverage["coverage_complete"],
        "method": "Hold observed current-generation MetricsReady status until the next bounded successful read; missing/stale conditions are unknown",
        "samples": [{"observation_interval_seconds": [row["start"] - onset, row["finish"] - onset],
            "status": {0: "True", 1: "False", None: "Unknown"}[row["value"]]} for row in values
            if onset - max_gap <= row["finish"] <= end + max_gap]}


def analyze(directory: Path, plan: dict) -> dict:
    metadata = yaml.safe_load((directory / "metadata.yaml").read_text(encoding="utf-8"))
    gate = read_json(directory / "live-baseline-gate.json")
    state = read_json(directory / "live-baseline-status.json")
    runner = read_json(directory / "live-runner-status.json")
    if (runner["protocol_version"] != "live-baseline-v1" or runner["status"] != "success"
            or runner["exit_code"] != 0 or runner["controller_process_stopped"] is not True):
        raise ValueError("Runner cleanup receipt does not confirm successful completion")
    actual = schedule(directory, plan)
    onset, end, offered_end = (actual[key] for key in ("load_onset_unix", "observation_end_unix", "offered_load_end_unix"))
    rows = records(directory / "live-observations.ndjson")
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("Observation rows must be JSON objects")
    cycles = controller_cycles(directory / "controller.log")
    log_start = validate_evidence(metadata, plan, gate, state, rows, cycles, actual)
    binary_receipt = directory / "controller-binary.sha256"
    if "controller_binary_sha256" in plan:
        digest = plan["controller_binary_sha256"]
        if not re.fullmatch(r"[a-f0-9]{64}", digest) or binary_receipt.read_text(encoding="utf-8").split()[0] != digest:
            raise ValueError("Controller binary SHA256 disagrees with recorded build receipt")
    k6 = business_evidence(directory, actual)
    max_gap = 2 * plan["interval_seconds"]
    requested, ready = (samples(rows, "deployment", field) for field in ("requested", "ready"))
    if not requested or requested[0]["finish"] > onset or requested[-1]["finish"] < end:
        raise ValueError("State observations must cover actual onset and the complete post-load tail")
    occupancy = {name: integrate(values, onset, end, offered_end, max_gap)
        for name, values in (("requested", requested), ("ready", ready))}
    metrics_samples = samples(rows, "phpa", "metrics")
    readiness = readiness_evidence(metrics_samples, onset, end, max_gap)
    first_growth = None
    before = [row for row in ready if row["finish"] <= onset and row["value"] is not None]
    if before:
        baseline = before[-1]
        previous = baseline
        for row in ready:
            if row["finish"] <= onset or row["value"] is None:
                continue
            if row["value"] > baseline["value"]:
                first_growth = [previous["start"] - onset, row["finish"] - onset]
                break
            previous = row
    writes = []
    accepted = []
    for item in cycles:
        if epoch(item["reconcileStartedAt"]) < log_start:
            continue
        query, decision, scale = (item.get(name, {}) for name in ("query", "decision", "scale"))
        if decision and (not query or query.get("queryError")):
            raise ValueError("Accepted controller decision lacks its successful source observation")
        if scale and (not decision or scale.get("scaled") is not True):
            raise ValueError("Successful Scale evidence lacks its decision or success flag")
        if query and decision and not query.get("queryError"):
            age = epoch(query["observationFinishedAt"]) - epoch(query["sourceTimestamp"])
            if age < 0 or query["samples"] < 2:
                raise ValueError("Accepted observation has invalid source age or insufficient history")
            accepted.append({"reconcile_started_at": item["reconcileStartedAt"],
                "evaluation_unix": epoch(query["latestEvaluationAt"]), "source_unix": epoch(query["sourceTimestamp"]),
                "observation_finished_unix": epoch(query["observationFinishedAt"]), "samples": query["samples"],
                "source_age_at_observation_finish_seconds": age})
        if scale and onset <= epoch(scale["scaleWriteFinishedAt"]) <= end:
            writes.append({"finished_seconds": epoch(scale["scaleWriteFinishedAt"]) - onset,
                "previous_desired_replicas": scale["previousDesiredReplicas"], "desired_replicas": scale["finalDesired"]})
    increases = [write for write in writes if write["desired_replicas"] > write["previous_desired_replicas"]]
    metrics_ready_interval = None
    previous = None
    for row in metrics_samples:
        if row["value"] == 0:
            metrics_ready_interval = [previous["start"] - onset if previous else None, row["finish"] - onset]
            break
        if row["value"] == 1:
            previous = row
    return {"protocol_version": "live-baseline-v1", "experiment_id": metadata["experiment_id"],
        "decision_mode": plan["decision_mode"], "pattern": plan["pattern"], "schedule": actual, "k6": k6,
        "startup": {"mode": plan["startup_mode"], "controller_started_at": plan["controller_started_at"], "gate": gate,
            "controller_start_to_onset_seconds": onset - epoch(plan["controller_started_at"]),
            "first_accepted_controller_observation_seconds": accepted[0]["observation_finished_unix"] - onset if accepted else None,
            "first_observed_metrics_ready_interval_seconds": metrics_ready_interval},
        "timing": {"first_scale_increase_seconds": increases[0]["finished_seconds"] if increases else None,
            "scale_outcome": "increase_observed" if increases else "no_increase_observed",
            "first_observed_ready_growth_interval_seconds": first_growth, "successful_scale_writes": writes},
        "controller_observations": {"accepted": accepted},
        "lineage": {"declared_source_sha256": plan.get("source_sha256"),
            "declared_controller_binary_sha256": plan.get("controller_binary_sha256"),
            "input_files": input_manifest(directory)},
        "metrics_readiness": readiness,
        "replica_time": {"window_seconds": end - onset, "maximum_sampling_gap_seconds": max_gap,
            "method": "Left response-finish sample held to next successful response; long gaps remain unknown", **occupancy},
        "quality": {"observer_status": state, "runner_status": runner,
            "observation_error_count": sum(row.get("status") != "success" for row in rows),
            "complete_sampling_coverage": readiness["coverage_complete"] and all(row["coverage_complete"] for row in occupancy.values())},
        "limitations": ["Replica time is a sampled estimate, not exact Pod lifetime, CPU consumption or cost",
            "Deployment and PHPA reads are not atomic; per-resource request intervals are retained when available",
            "Ready growth is bounded by API reads, not an exact transition or proof of Service traffic eligibility",
            "Source age comes from accepted controller observations, not an independent name-prefix query"]}


def input_manifest(directory: Path) -> dict:
    names = ("metadata.yaml", "live-baseline-plan.json", "live-baseline-gate.json", "live-baseline-status.json",
        "live-runner-status.json", "live-observations.ndjson", "controller.log", "k6.json", "k6-summary.json",
        "k6-runner.json", "k6-exit-code", "k6-start-time-unix", "k6-end-time-unix",
        "k6-warnings.log", "k6-stdout.log", "controller-binary.sha256")
    result = {}
    for name in names:
        path = directory / name
        if path.exists():
            with path.open("rb") as stream:
                digest = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            result[name] = {"sha256": digest.hexdigest(), "size_bytes": path.stat().st_size}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--schedule-only", action="store_true")
    args = parser.parse_args()
    try:
        plan = read_json(args.run_dir / "live-baseline-plan.json")
        validate_plan(plan)
        report = schedule(args.run_dir, plan) if args.schedule_only else analyze(args.run_dir, plan)
        destination = args.output or args.run_dir / ("live-baseline-schedule.json" if args.schedule_only else "live-baseline.json")
        rendered = json.dumps(report, indent=2, allow_nan=False) + "\n"
        with destination.open("x", encoding="utf-8") as stream:
            stream.write(rendered)
        print(destination)
        return 0
    except (OSError, ValueError, KeyError, TypeError, IndexError, yaml.YAMLError) as error:
        print(f"Live baseline analysis failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
