#!/usr/bin/env python3
"""Analyze retained cadence-pilot receipts; never contact or change a cluster."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sys

import yaml

import live_baseline
from latency import controller_cycles, epoch, records

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from observe_cadence import select_anchor


PROTOCOL = "cadence-pilot-v1"
ALLOCATION = ((1, 1, 30, 2), (1, 2, 15, 2), (2, 3, 15, 7),
    (2, 4, 30, 7), (3, 5, 30, 12), (3, 6, 15, 12))


def number(value: object, label: str, minimum: float = 0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum:
        raise ValueError(f"Invalid {label}")
    return float(value)


def timestamp(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("Boolean timestamp")
    result = epoch(value)
    if result <= 0:
        raise ValueError("Timestamp must be positive")
    return result


def sources(row: dict) -> dict:
    containers = row["containers"]
    if not isinstance(containers, list) or not containers:
        raise ValueError("Accepted CPU observation lacks container sources")
    result = {}
    for container in containers:
        identity = tuple(container[name] for name in ("pod", "pod_uid", "container", "runtime_id"))
        if any(not isinstance(part, str) or not part for part in identity) or identity in result:
            raise ValueError("Empty or duplicate container identity")
        result[identity] = timestamp(container["source_timestamp"])
    return result


def validate_observation(row: dict, uid: str) -> None:
    start, finish = (timestamp(row[key]) for key in ("observation_started_at", "observation_finished_at"))
    if finish < start or row["status"] not in ("success", "rejected") or row["target_uid"] != uid:
        raise ValueError("Invalid CPU observation interval or status")
    if row["status"] == "success":
        if row["target_uid"] != uid or row.get("error"):
            raise ValueError("Accepted CPU observation has wrong target identity or an error")
        evaluated = timestamp(row["evaluated_at"])
        source = sources(row)
        if (evaluated < start - 0.001 or evaluated > finish + 0.001
                or any(value > evaluated or finish - value > 45 for value in source.values())
                or abs(timestamp(row["source_timestamp"]) - min(source.values())) > 0.001):
            raise ValueError("CPU observation has stale, future, or inconsistent source timestamps")
        number(row["utilization_percent"], "CPU utilization")
    elif not row.get("error"):
        raise ValueError("Rejected CPU observation lacks a reason")


def observation_queries(row: dict) -> list[dict]:
    queries = row["queries"]
    if not isinstance(queries, list):
        raise ValueError("CPU observation lacks actual HTTP query receipts")
    begin, end = (timestamp(row[key]) for key in ("observation_started_at", "observation_finished_at"))
    previous = begin
    result = []
    for query in queries:
        start, finish = (timestamp(query[key]) for key in ("started_at", "finished_at"))
        duration = number(query["duration_seconds"], "query duration")
        if (start < previous - 0.001 or finish < start or finish > end + 0.001
                or abs(duration - (finish - start)) > 0.002):
            raise ValueError("HTTP query interval or duration disagrees with its observation")
        if (query["query_kind"] not in ("rate", "timestamp") or query["method"] not in ("GET", "POST")
                or type(query["http_status"]) is not int or not 0 <= query["http_status"] <= 599
                or not isinstance(query.get("error", ""), str)):
            raise ValueError("Invalid HTTP query receipt")
        failed = bool(query.get("error")) or not 200 <= query["http_status"] < 300
        result.append({"started_unix": start, "finished_unix": finish, "duration_seconds": duration,
            "query_kind": query["query_kind"], "failed": failed, "http_status": query["http_status"]})
        previous = finish
    if row["status"] == "success":
        rate = [q for q in result if q["query_kind"] == "rate"]
        source = [q for q in result if q["query_kind"] == "timestamp"]
        # Prometheus may retry POST as GET after HTTP 405. The failed attempts
        # consume real HTTP work, but do not invalidate the eventual observation.
        if not rate or not source or rate[-1]["failed"] or source[-1]["failed"] or result != rate + source:
            raise ValueError("Accepted CPU observation lacks successful rate and timestamp HTTP receipts")
    return result


def query_totals(rows: list[dict], onset: float, end: float) -> dict:
    queries = [query for row in rows for query in observation_queries(row)]

    def total(selected: list[dict]) -> dict:
        return {"http_requests": len(selected), "duration_seconds": sum(q["duration_seconds"] for q in selected),
            "errors": sum(q["failed"] for q in selected),
            "rate_requests": sum(q["query_kind"] == "rate" for q in selected),
            "timestamp_requests": sum(q["query_kind"] == "timestamp" for q in selected)}

    return {"scenario_window": total([q for q in queries if onset <= q["started_unix"] < end]),
        "entire_capture": total(queries), "window_attribution": "HTTP request start in [load onset, observation end)"}


def signal_totals(rows: list[dict], onset: float, end: float) -> dict:
    entire = {"accepted_observations": 0, "rejected_observations": 0,
        "strictly_advanced_observations": 0, "repeated_or_partial_observations": 0,
        "initial_or_changed_membership_observations": 0}
    window = dict(entire)
    previous = None
    observations = []
    for row in rows:
        finish = timestamp(row["observation_finished_at"])
        keys = ["rejected_observations"]
        classification = "rejected"
        if row["status"] == "success":
            current = sources(row)
            if previous is None or current.keys() != previous.keys():
                classification = "initial_or_changed_membership_observations"
            elif all(current[key] > previous[key] for key in current):
                classification = "strictly_advanced_observations"
            else:
                classification = "repeated_or_partial_observations"
            keys = ["accepted_observations", classification]
            previous = current
        for key in keys:
            entire[key] += 1
            if onset <= finish < end:
                window[key] += 1
        observations.append({"finished_seconds": finish - onset, "classification": classification,
            "source_timestamps": row.get("containers", []), "error": row.get("error", "")})
    return {"scenario_window": window, "entire_capture": entire, "observations": observations,
        "method": "Every container in the unchanged UID/runtime membership must strictly advance against the previous accepted observation"}


def observer_sampling(rows: list[dict], onset: float, end: float, interval: float) -> dict:
    maximum_gap = 2 * interval
    intervals = []
    if timestamp(rows[0]["observation_started_at"]) > onset:
        intervals.append((onset, timestamp(rows[0]["observation_started_at"])))
    for previous, following in zip(rows, rows[1:]):
        finish = timestamp(previous["observation_finished_at"])
        start = timestamp(following["observation_started_at"])
        if start - finish > maximum_gap:
            intervals.append((finish, start))
    if timestamp(rows[-1]["observation_finished_at"]) < end:
        intervals.append((timestamp(rows[-1]["observation_finished_at"]), end))
    unknown = [[max(start, onset) - onset, min(finish, end) - onset] for start, finish in intervals
        if min(finish, end) > max(start, onset)]
    return {"coverage_complete": not unknown, "unknown_intervals_seconds": unknown,
        "unknown_seconds": sum(finish - start for start, finish in unknown), "maximum_gap_seconds": maximum_gap,
        "method": "Entire gaps from previous observation finish to next observation start exceeding the maximum allowed gap; in-flight request duration is not missing capture"}


def validate_configuration(directory: Path, plan: dict, baseline_plan: dict, cycles: list[dict]) -> dict:
    if plan["protocol_version"] != PROTOCOL:
        raise ValueError("Unsupported cadence protocol")
    allocation = tuple(plan[name] for name in ("pair", "slot", "requeue_seconds", "offset_seconds"))
    if allocation not in ALLOCATION or any(type(value) is not int for value in allocation):
        raise ValueError("Cadence plan differs from the frozen six-slot allocation")
    if any(plan[key] != value for key, value in (("interval_seconds", 1), ("gate_timeout_seconds", 120),
            ("phase_tolerance_seconds", 1))):
        raise ValueError("Cadence observation interval or gate limits differ from the frozen protocol")
    if not re.fullmatch(r"[a-f0-9]{64}", plan["cpu_binary_sha256"]):
        raise ValueError("Missing CPU observer binary fingerprint")
    metadata = yaml.safe_load((directory / "metadata.yaml").read_text(encoding="utf-8"))
    if (metadata.get("cadence_pilot") is not True or metadata.get("requeue_seconds") != plan["requeue_seconds"]
            or baseline_plan["protocol_version"] != PROTOCOL or baseline_plan["quiet_seconds"] != 0
            or baseline_plan["startup_mode"] != "warm" or baseline_plan["decision_mode"] != "Current"
            or baseline_plan["pattern"] != "step"
            or any(baseline_plan[key] != plan[key] for key in ("target_uid", "phpa_uid", "requeue_seconds"))):
        raise ValueError("Cadence metadata and live plan disagree")
    command = live_baseline.read_json(directory / "controller-command.json")
    args = command["args"]
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise ValueError("Invalid controller command receipt")
    values = []
    for index, arg in enumerate(args):
        if arg.startswith("--requeue-interval="):
            values.append(arg.split("=", 1)[1])
        elif arg == "--requeue-interval":
            values.append(args[index + 1])
    if (values != [f"{plan['requeue_seconds']}s"] or command["requeue_seconds"] != plan["requeue_seconds"]
            or command["binary_sha256"] != baseline_plan["controller_binary_sha256"]):
        raise ValueError("Controller command interval or binary fingerprint disagrees with plan")
    if not cycles:
        raise ValueError("No actual controller reconciliations")
    for cycle in cycles:
        finish = cycle.get("finish")
        if finish is None:
            raise ValueError("Incomplete retained controller reconciliation")
        if not finish.get("reconcileError") and finish.get("requeueAfterSeconds") != plan["requeue_seconds"]:
            raise ValueError("Controller log interval disagrees with plan")
    return command


def controller_observations(cycles: list[dict], uid: str) -> list[dict]:
    rows = []
    for cycle in cycles:
        query = cycle.get("query")
        if query is None:
            continue
        row = {"status": "rejected" if query.get("queryError") else "success", "error": query.get("queryError", ""),
            "target_uid": query.get("targetUID"), "observation_started_at": query["observationStartedAt"],
            "observation_finished_at": query["observationFinishedAt"], "evaluated_at": query.get("latestEvaluationAt"),
            "source_timestamp": query.get("sourceTimestamp"), "containers": query.get("containerSources"),
            "utilization_percent": query["currentCPU%"],
            "queries": query["queries"]}
        validate_observation(row, uid)
        if row["status"] == "success" and cycle.get("decision") and not math.isclose(
                number(cycle["decision"]["currentCPU%"], "decision CPU utilization"),
                row["utilization_percent"], rel_tol=1e-12, abs_tol=1e-9):
            raise ValueError("Controller decision CPU disagrees with its actual observation")
        rows.append(row)
    return rows


def analyze_run(directory: Path) -> dict:
    plan = live_baseline.read_json(directory / "cadence-plan.json")
    baseline_plan = live_baseline.read_json(directory / "live-baseline-plan.json")
    live_baseline.validate_plan(baseline_plan)
    cycles = controller_cycles(directory / "controller.log")
    validate_configuration(directory, plan, baseline_plan, cycles)
    status = live_baseline.read_json(directory / "cadence-status.json")
    if (status["status"] != "success" or status["owned_processes_stopped"] is not True
            or status["gate_released"] is not True or status["errors"]):
        raise ValueError("Cadence gate or cleanup did not complete successfully")
    cpu = records(directory / "cadence-cpu.ndjson")
    if (not cpu or cpu[-1].get("kind") != "cpu_observer_summary" or cpu[-1].get("status") != "completed"
            or any(row.get("kind") != "cpu_observation" for row in cpu[:-1])):
        raise ValueError("CPU observer stream lacks a single successful terminal receipt")
    summary, cpu = cpu[-1], cpu[:-1]
    counts = {"observations": len(cpu), "successful_observations": sum(row.get("status") == "success" for row in cpu),
        "rejected_observations": sum(row.get("status") == "rejected" for row in cpu)}
    if (summary["protocol_version"] != "cadence-cpu-v1" or summary["target_uid"] != plan["target_uid"]
            or summary.get("error") or not cpu
            or any(type(summary[key]) is not int or summary[key] != value for key, value in counts.items())
            or timestamp(summary["finished_at"]) < timestamp(cpu[-1]["observation_finished_at"])):
        raise ValueError("CPU stream disagrees with its independent completion receipt")
    previous_finish = 0
    for sequence, row in enumerate(cpu, 1):
        if row["sequence"] != sequence or type(row["sequence"]) is not int or row["protocol_version"] != "cadence-cpu-v1":
            raise ValueError("CPU stream sequence or protocol is inconsistent")
        validate_observation(row, plan["target_uid"])
        if timestamp(row["observation_started_at"]) < previous_finish:
            raise ValueError("CPU observations overlap or arrive out of order")
        previous_finish = timestamp(row["observation_finished_at"])
    baseline = live_baseline.analyze(directory, baseline_plan)
    onset, end = (baseline["schedule"][key] for key in ("load_onset_unix", "observation_end_unix"))
    gate = live_baseline.read_json(directory / "cadence-gate.json")
    if gate["protocol_version"] != PROTOCOL:
        raise ValueError("Unsupported cadence gate protocol")
    attempt = live_baseline.read_json(directory / "cadence-gate-attempt.json")
    if any(attempt[key] != gate[key] for key in ("protocol_version", "previous_observation", "anchor",
            "planned_onset_unix", "release_request_started_at")):
        raise ValueError("Gate acknowledgement disagrees with the retained release attempt")
    previous, anchor = (gate[key] for key in ("previous_observation", "anchor"))
    if previous not in cpu or anchor not in cpu or anchor["sequence"] != previous["sequence"] + 1:
        raise ValueError("Gate CPU observations disagree with retained stream")
    accepted_between = [row for row in cpu if previous["sequence"] < row["sequence"] < anchor["sequence"] and row["status"] == "success"]
    if previous["status"] != "success" or anchor["status"] != "success" or accepted_between:
        raise ValueError("Gate does not reference successive accepted CPU observations")
    before, current = sources(previous), sources(anchor)
    if before.keys() != current.keys() or not all(current[key] > before[key] for key in current):
        raise ValueError("Gate requires strictly newer source samples for the entire unchanged container membership")
    finish = timestamp(anchor["observation_finished_at"])
    earliest = max(timestamp(gate["warm_ready_at"]) + 30, timestamp(gate["runner_ready_at"]))
    selected = select_anchor(cpu, plan["target_uid"], earliest, plan["offset_seconds"])
    if selected is None or selected["previous_observation"] != previous or selected["anchor"] != anchor:
        raise ValueError("Recorded gate skipped or changed the first eligible source anchor")
    release_start, release_end = (timestamp(gate[key]) for key in ("release_request_started_at", "release_request_finished_at"))
    for row in cpu:
        if row["sequence"] > anchor["sequence"] and timestamp(row["observation_finished_at"]) <= release_start:
            if row["status"] != "success" or sources(row).keys() != current.keys():
                raise ValueError("Verified CPU coverage changed between the source anchor and gate release")
    planned = timestamp(gate["planned_onset_unix"])
    if (timestamp(anchor["observation_started_at"]) < earliest or finish - earliest > plan["gate_timeout_seconds"]
            or abs(planned - finish - plan["offset_seconds"]) > 0.001 or release_end < release_start
            or release_start < finish or release_start < planned - 0.002 or release_end > onset + 1
            or gate["release_response"]["data"]["attributes"]["paused"] is not False):
        raise ValueError("Cadence gate times, idle period, timeout, or release response are invalid")
    prior_finishes = [timestamp(cycle["finish"]["reconcileFinishedAt"]) for cycle in cycles
        if timestamp(cycle["finish"]["reconcileFinishedAt"]) <= onset]
    if not prior_finishes:
        raise ValueError("Missing completed reconciliation before load onset")
    actual_offset = onset - finish
    phase_valid = abs(actual_offset - plan["offset_seconds"]) <= plan["phase_tolerance_seconds"]
    controller = controller_observations(cycles, plan["target_uid"])
    complete = baseline["quality"]["complete_sampling_coverage"]
    sampling = observer_sampling(cpu, onset, end, plan["interval_seconds"])
    k6 = baseline["k6"]
    service = {"http_200_fraction": k6["successful_rate_http_200_pct"] / 100,
        "all_request_p95_ms": k6["duration_p95_ms"], "dropped_iterations": k6["dropped_iterations"],
        "thresholds": {"minimum_http_200_fraction": 0.99, "maximum_all_request_p95_ms": 500, "maximum_drops": 0}}
    service["passed"] = service["http_200_fraction"] >= 0.99 and service["all_request_p95_ms"] <= 500 and service["dropped_iterations"] == 0
    names = ("cadence-plan.json", "cadence-gate.json", "cadence-gate-attempt.json", "cadence-status.json",
        "cadence-cpu.ndjson", "controller-command.json")
    lineage = {name: {"sha256": hashlib.sha256((directory / name).read_bytes()).hexdigest(),
        "size_bytes": (directory / name).stat().st_size} for name in names}
    return {"protocol_version": PROTOCOL, "kind": "run", "pair": plan["pair"], "slot": plan["slot"],
        "requeue_seconds": plan["requeue_seconds"], "offset_seconds": plan["offset_seconds"],
        "configuration": {"rps": baseline_plan["rps"], "source_sha256": baseline_plan["source_sha256"],
            "controller_binary_sha256": baseline_plan["controller_binary_sha256"], "cpu_binary_sha256": plan["cpu_binary_sha256"]},
        "evidence_valid": complete, "comparison_eligible": complete and phase_valid and sampling["coverage_complete"],
        "phase_valid": phase_valid, "observer_sampling": sampling,
        "phase": {"planned_offset_seconds": plan["offset_seconds"], "actual_offset_seconds": actual_offset,
            "oldest_source_age_at_anchor_seconds": finish - min(current.values()),
            "oldest_source_age_at_onset_seconds": onset - min(current.values()),
            "onset_after_last_completed_reconcile_seconds": onset - max(prior_finishes)},
        "queries": {"controller": query_totals(controller, onset, end), "observer": query_totals(cpu, onset, end)},
        "signals": {"controller": signal_totals(controller, onset, end), "observer": signal_totals(cpu, onset, end)},
        "rejected_observation_intervals": {"method": "Sampled MetricsReady=False intervals; long gaps remain unknown, not a sum of query durations",
            **baseline["metrics_readiness"]}, "service_criteria": service, "baseline": baseline,
        "lineage": {"input_files": lineage},
        "limitations": ["Source timestamps identify stored counter samples, not exact exporter refresh times",
            "Phase failure and service failure remain in their assigned slots; neither is silently retried",
            "Three pairs describe mechanisms and feasibility, not production or population-wide gains"]}


def inside(directory: Path, value: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("Missing retained campaign path")
    path = (directory / value).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ValueError("Campaign evidence path escapes its directory")
    return path


def comparison_values(report: dict) -> dict:
    baseline = report["baseline"]
    window_queries = {actor: report["queries"][actor]["scenario_window"] for actor in ("controller", "observer")}
    result = {"http_200_fraction": report["service_criteria"]["http_200_fraction"],
        "all_request_p95_ms": report["service_criteria"]["all_request_p95_ms"],
        "dropped_iterations": report["service_criteria"]["dropped_iterations"],
        "first_scale_increase_seconds": baseline["timing"]["first_scale_increase_seconds"],
        "first_ready_growth_interval_seconds": baseline["timing"]["first_observed_ready_growth_interval_seconds"],
        "requested_replica_seconds": baseline["replica_time"]["requested"]["pod_seconds"],
        "ready_replica_seconds": baseline["replica_time"]["ready"]["pod_seconds"],
        "metrics_ready_false_sampled_seconds": baseline["metrics_readiness"]["sampled_false_seconds"],
        **report["phase"]}
    for actor, query in window_queries.items():
        result.update({f"{actor}_http_requests": query["http_requests"],
            f"{actor}_http_duration_seconds": query["duration_seconds"], f"{actor}_http_errors": query["errors"],
            f"{actor}_strictly_advanced_observations": report["signals"][actor]["scenario_window"]["strictly_advanced_observations"]})
    return result


def analyze_campaign(directory: Path) -> dict:
    state = live_baseline.read_json(directory / "campaign-status.json")
    if state["protocol_version"] != PROTOCOL or len(state["slots"]) != 6:
        raise ValueError("Campaign must retain all six assigned cadence slots")
    number(state["rps"], "campaign RPS", 1)
    slots = []
    for allocated, slot in zip(ALLOCATION, state["slots"]):
        if tuple(slot[key] for key in ("pair", "slot", "requeue_seconds", "offset_seconds")) != allocated:
            raise ValueError("Campaign slot order or assignment changed")
        if slot["status"] not in ("not_run", "running", "success", "failed"):
            raise ValueError("Unknown campaign slot status")
        retained = dict(slot)
        if slot["status"] == "success":
            stored = live_baseline.read_json(inside(directory, slot["analysis"]))
            # Recompute from retained inputs: a edited or stale success report must
            # not bypass the run CLI's identity, query, cleanup, or timing checks.
            report = analyze_run(inside(directory, slot["run_dir"]))
            if report != stored or tuple(report[key] for key in ("pair", "slot", "requeue_seconds", "offset_seconds")) != allocated:
                raise ValueError("Campaign analysis disagrees with its assigned slot or retained raw inputs")
            if report["configuration"]["rps"] != state["rps"]:
                raise ValueError("Run RPS differs from frozen campaign RPS")
            retained["result"] = report
        slots.append(retained)
    pairs = []
    for index in range(0, 6, 2):
        pair_slots = slots[index:index + 2]
        pair = {"pair": index // 2 + 1, "slots": [slot["slot"] for slot in pair_slots],
            "comparison_eligible": False, "reasons": [], "delta_15_minus_30": None,
            "values_30_seconds": None, "values_15_seconds": None, "service_passed_both": None}
        reports = {slot["requeue_seconds"]: slot.get("result") for slot in pair_slots}
        for cadence, report in reports.items():
            if report:
                pair[f"values_{cadence}_seconds"] = comparison_values(report)
        if not all(reports.values()):
            pair["reasons"].append("Both assigned slots have not produced complete run reports")
        else:
            slow, fast = reports[30], reports[15]
            pair["service_passed_both"] = slow["service_criteria"]["passed"] and fast["service_criteria"]["passed"]
            if not slow["comparison_eligible"] or not fast["comparison_eligible"]:
                pair["reasons"].append("One or both runs lack valid phase or complete state/CPU sampling coverage")
            if slow["configuration"] != fast["configuration"]:
                pair["reasons"].append("Frozen configuration or source/binary fingerprints differ")
            for key in ("actual_offset_seconds", "oldest_source_age_at_onset_seconds"):
                difference = abs(slow["phase"][key] - fast["phase"][key])
                pair[f"{key}_difference"] = difference
                if difference > 1:
                    pair["reasons"].append(f"Paired {key} differs by more than one second")
            pair["comparison_eligible"] = not pair["reasons"]
            if pair["comparison_eligible"]:
                # A missing expansion remains null; no fictitious deadline or zero
                # substitutes for the unobserved Scale/Ready event.
                slow_values, fast_values = pair["values_30_seconds"], pair["values_15_seconds"]
                pair["delta_15_minus_30"] = {key: fast_values[key] - value
                    if isinstance(value, (int, float)) and isinstance(fast_values[key], (int, float)) else None
                    for key, value in slow_values.items()}
        pairs.append(pair)
    return {"protocol_version": PROTOCOL, "kind": "campaign", "status": state["status"],
        "error": state.get("error", ""), "cleanup_error": state.get("cleanup_error", ""),
        "slots": slots, "pairs": pairs, "eligible_pairs": sum(pair["comparison_eligible"] for pair in pairs),
        "limitations": ["Deltas are 15-second minus 30-second values within each preassigned pair",
            "Service failure is an observed outcome independent of evidence validity",
            "No pooled causal or production-performance conclusion follows from three exploratory pairs"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--run-dir", type=Path)
    source.add_argument("--campaign", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = analyze_campaign(args.campaign) if args.campaign is not None else analyze_run(args.run_dir)
        rendered = json.dumps(report, indent=2, allow_nan=False) + "\n"
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(rendered)
        print(args.output)
        return 0
    except (OSError, ValueError, KeyError, TypeError, IndexError, yaml.YAMLError) as error:
        print(f"Cadence analysis failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
