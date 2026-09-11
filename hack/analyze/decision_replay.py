#!/usr/bin/env python3
"""Prepare same-input replay from retained logs; this command never contacts a cluster."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys

import yaml

from latency import controller_cycles, epoch as parse_epoch, records
from live_baseline import read_interval, schedule as actual_schedule


LIMITATIONS = [
    "Fixed recorded replica inputs and separate per-mode recommendation histories; not a closed-loop simulation",
    "Recorded forecasts do not verify prediction when the CPU history contains an unknown value",
    "Source timestamps do not identify exporter refresh or actual scrape execution times",
    "Ready observations are bounded reads and do not establish when a new Pod served a request",
    "API sampling cannot exclude an external writer changing and restoring state between reads",
    "No counterfactual HTTP success, p95 or replica-time benefit is calculated",
]
FILES = ("controller.log", "live-baseline-plan.json", "live-baseline-gate.json",
         "live-baseline-schedule.json", "phpa-after-apply.json", "deployment-before.json",
         "metadata.yaml", "controller-binary.sha256", "live-observations.ndjson", "k6-stdout.log", "k6-warnings.log")


def epoch(value):
    if isinstance(value, bool) or (isinstance(value, str) and not re.search(r"(?:Z|[+-][0-9]{2}:[0-9]{2})$", value)):
        raise ValueError("Timestamp must have an explicit timezone or finite epoch seconds")
    return parse_epoch(value)


def ordered_times(*values):
    times = [epoch(value) for value in values]
    if any(right < left for left, right in zip(times, times[1:])):
        raise ValueError("Recorded event times are out of order")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def duration(value):
    """Support the positive Go-duration units used by recorded configurations."""
    parts = re.findall(r"([0-9]+(?:\.[0-9]+)?)(ms|s|m|h)", value)
    if not parts or "".join(number + unit for number, unit in parts) != value:
        raise ValueError(f"Unsupported recorded duration: {value}")
    seconds = sum(float(number) * {"ms": .001, "s": 1, "m": 60, "h": 3600}[unit] for number, unit in parts)
    if not seconds.is_integer():
        raise ValueError("Replay accepts recorded whole-second configuration durations")
    return int(seconds)


def configuration(spec):
    prediction = spec["prediction"]
    return {"minReplicas": spec.get("minReplicas", 1), "maxReplicas": spec["maxReplicas"],
            "targetCPU": spec["targetCPUUtilizationPercentage"], "alphaPercent": prediction["alphaPercent"],
            "windowSeconds": duration(prediction["window"]), "horizonSeconds": duration(prediction["horizon"]),
            "stabilizationSeconds": spec.get("scaleDownStabilizationWindowSeconds", 60)}


def manifest(directory):
    return {name: {"bytes": (directory / name).stat().st_size,
                   "sha256": hashlib.sha256((directory / name).read_bytes()).hexdigest()} for name in FILES}


def validate_identity(plan, gate, phpa, before, metadata, states, binary_digest):
    if plan.get("protocol_version") != "live-baseline-v1":
        raise ValueError("Unsupported baseline protocol")
    for resource, obj, uid_key in [("target", before, "target_uid"), ("phpa", phpa, "phpa_uid")]:
        if not plan.get(uid_key) or obj["metadata"]["uid"] != plan[uid_key] or gate[uid_key] != plan[uid_key]:
            raise ValueError(f"{resource} identity disagrees with plan/gate")
    if (metadata.get("live_baseline") is not True or metadata["result"]["status"] != "success"
            or metadata["result"].get("failure_reason") or metadata["git"]["dirty"] is not False):
        raise ValueError("Baseline source/run was not recorded as clean and successful")
    if (not re.fullmatch(r"[a-f0-9]{40}", metadata["git"]["commit"])
            or not re.fullmatch(r"[a-f0-9]{64}", plan["source_sha256"])
            or metadata["benchmark_source_sha256"] != plan["source_sha256"]
            or not re.fullmatch(r"[a-f0-9]{64}", plan["controller_binary_sha256"])
            or binary_digest != plan["controller_binary_sha256"]):
        raise ValueError("Recorded source or binary identity disagrees")
    if not (metadata["decision_mode"] == plan["decision_mode"] == phpa["spec"]["decisionMode"]):
        raise ValueError("Recorded decision modes disagree")
    if (phpa["spec"]["prediction"]["algorithm"] != "EWMA"
            or phpa["spec"]["scaleTargetRef"]["kind"] != "Deployment"
            or phpa["spec"]["scaleTargetRef"]["name"] != before["metadata"]["name"]):
        raise ValueError("Unsupported or inconsistent workload configuration")
    successful = 0
    for row in states:
        if row.get("kind") != "state":
            continue
        for resource, reference in [("deployment", before), ("phpa", phpa)]:
            obj = row.get("response", {}).get(resource)
            if obj is None:
                if row.get("status") == "success":
                    raise ValueError(f"Successful observation lacks {resource} identity")
                continue
            if obj["metadata"]["uid"] != reference["metadata"]["uid"]:
                raise ValueError(f"Observed {resource} identity changed")
            if resource == "phpa" and (obj["metadata"]["generation"] != phpa["metadata"]["generation"]
                                       or obj["spec"] != phpa["spec"]):
                raise ValueError("Observed PHPA configuration or generation changed")
        if row.get("status") == "success":
            successful += 1
    if successful == 0:
        raise ValueError("No successful API identity observations")


def history_series(anchors, query, decision, window):
    at = query["latestEvaluationAt"]
    instant = epoch(at)
    anchors[:] = [point for point in anchors if instant - window <= epoch(point["timestamp"]) < instant]
    live = {"timestamp": at, "value": decision["currentCPU%"] if decision else None}
    if not anchors or instant - epoch(anchors[-1]["timestamp"]) >= query["observationSpacingSeconds"]:
        anchors.append(live)
        returned = list(anchors)
    else:
        # A frequent event replaces the returned tail, not the stored anchor.
        returned = anchors[:-1] + [live]
    if len(returned) != query["samples"] or (decision and len(returned) != decision["samples"]):
        raise ValueError("CPU history count disagrees with recorded observation; prefix may be missing")
    return returned


def explanation(cycle, requested, target):
    query, decision = cycle.get("query", {}), cycle.get("decision", {})
    if query.get("queryError"):
        return "metrics_rejected_before_query" if not query.get("queryStartedAt") else "metrics_rejected_after_query"
    if not decision:
        return "insufficient_history" if query.get("samples") == 1 else "unknown_no_decision"
    if cycle.get("scale"):
        return "scale_written"
    if decision["skipReason"] == "WithinToleranceBand":
        return "within_tolerance"
    if decision["stabilized"]:
        return "scale_down_stabilized"
    if decision["currentCPU%"] <= target:
        return "current_input_at_or_below_target"
    if decision["decisionCPU%"] < decision["currentCPU%"] and decision["finalDesired"] <= requested:
        return "lower_forecast_did_not_expand"
    return "no_replica_change" if decision["skipReason"] else "decision_without_successful_write"


def ready_interval(rows, onset, writes, initial):
    first_scale = next((write for write in writes if write["finished"] >= onset and write["after"] > write["before"]), None)
    if first_scale is None:
        return None
    previous = None
    for row in rows:
        if row.get("kind") != "state" or row.get("status") != "success":
            continue
        start, end = read_interval(row, "deployment")
        ready = row["response"]["deployment"].get("status", {}).get("readyReplicas", 0)
        if end >= first_scale["finished"] and ready > initial:
            return [previous - onset if previous is not None else None, end - onset]
        if ready <= initial:
            previous = start
    return None


def check_requested_observations(states, writes, initial):
    checked, overlapping = 0, 0
    last_start = -math.inf
    for row in states:
        if row.get("kind") != "state" or row.get("status") != "success":
            continue
        start, end = read_interval(row, "deployment")
        if start < last_start or end < start:
            raise ValueError("API read intervals are reversed or out of order")
        last_start = start
        preceding = initial
        for write in writes:
            if write["finished"] < start:
                preceding = write["after"]
        candidates = {preceding}
        overlaps = [write for write in writes if write["started"] <= end and write["finished"] >= start]
        for write in overlaps:
            candidates.update((write["before"], write["after"]))
        if row["response"]["deployment"]["spec"]["replicas"] not in candidates:
            raise ValueError("Observed requested replicas disagree with successful write chain")
        checked += 1
        overlapping += bool(overlaps)
    return {"checked": checked, "overlapping_write_intervals": overlapping}


def validate_cycles(cycles, plan, phpa, log_text):
    if not cycles or log_text.count("Starting manager") != 1:
        raise ValueError("Missing startup prefix or multiple controller processes")
    previous_finish = epoch(plan["controller_started_at"])
    first_decision = True
    for cycle in cycles:
        start = epoch(cycle["reconcileStartedAt"])
        query, decision, finish = (cycle.get(name, {}) for name in ("query", "decision", "finish"))
        if not query or not finish or finish["reconcileError"]:
            raise ValueError("Incomplete or failed reconciliation; complete decision history cannot be established")
        if start < previous_finish:
            raise ValueError("Overlapping reconciliations or a missing process boundary")
        previous_finish = epoch(finish["reconcileFinishedAt"])
        if previous_finish < start:
            raise ValueError("Reconciliation finish precedes its start")
        for event in (query, decision, finish, cycle.get("scale", {})):
            if event and (event.get("namespace") != phpa["metadata"]["namespace"] or event.get("name") != phpa["metadata"]["name"]):
                raise ValueError("Controller log identity disagrees with PHPA")
        if query["cpuRateWindowSeconds"] != 60 or query["observationSpacingSeconds"] != 15:
            raise ValueError("Unsupported recorded sampling configuration")
        ordered_times(cycle["reconcileStartedAt"], query["observationStartedAt"],
                      query["observationFinishedAt"], finish["reconcileFinishedAt"])
        if bool(query.get("queryStartedAt")) != bool(query.get("queryFinishedAt")):
            raise ValueError("Query time boundary is missing")
        if query.get("queryStartedAt"):
            ordered_times(query["observationStartedAt"], query["queryStartedAt"],
                          query["queryFinishedAt"], query["observationFinishedAt"])
        if query["queryError"]:
            if decision or cycle.get("scale"):
                raise ValueError("Rejected observation unexpectedly has a decision or write")
            continue
        if not decision:
            if query["samples"] != 1:
                raise ValueError("Missing decision after a successful multi-sample observation")
            continue
        if decision["decisionMode"] != plan["decision_mode"] or decision["latestEvaluationAt"] != query["latestEvaluationAt"]:
            raise ValueError("Decision mode or observation time disagrees with query")
        ordered_times(query["observationFinishedAt"], decision["stabilizationEvaluatedAt"],
                      decision["decisionAt"], finish["reconcileFinishedAt"])
        if cycle.get("scale"):
            scale = cycle["scale"]
            ordered_times(decision["decisionAt"], scale["scaleWriteStartedAt"], scale["scaleWriteFinishedAt"],
                          finish["reconcileFinishedAt"])
        if first_decision:
            at = epoch(decision["stabilizationEvaluatedAt"])
            window = phpa["spec"].get("scaleDownStabilizationWindowSeconds", 60)
            if (decision["stabilizationHistoryEntries"] != 1 or epoch(decision["stabilizationHistoryOldestAt"]) != at
                    or abs(epoch(decision["coldStartProtectedUntil"]) - at - window) > .000001):
                raise ValueError("First decision lacks a complete policy-history prefix")
            first_decision = False
        if not decision["skipReason"] and not cycle.get("scale"):
            raise ValueError("Decision lacks either a skip reason or a successful write")


def prepare(directory):
    plan = read_json(directory / "live-baseline-plan.json")
    gate = read_json(directory / "live-baseline-gate.json")
    schedule = read_json(directory / "live-baseline-schedule.json")
    if schedule != actual_schedule(directory, plan):
        raise ValueError("Recorded schedule disagrees with actual k6 load marker")
    phpa = read_json(directory / "phpa-after-apply.json")
    before = read_json(directory / "deployment-before.json")
    metadata = yaml.safe_load((directory / "metadata.yaml").read_text(encoding="utf-8"))
    config = configuration(phpa["spec"])
    cycles = controller_cycles(directory / "controller.log")
    states = records(directory / "live-observations.ndjson")
    validate_identity(plan, gate, phpa, before, metadata, states,
                      (directory / "controller-binary.sha256").read_text().split()[0])
    validate_cycles(cycles, plan, phpa, (directory / "controller.log").read_text(encoding="utf-8"))
    onset = schedule["load_onset_unix"]
    ordered_times(plan["controller_started_at"], gate["released_at"], schedule["load_onset_unix"],
                  schedule["offered_load_end_unix"], schedule["observation_end_unix"])
    initial = before["spec"]["replicas"]
    requested = initial
    anchors, replay, timeline, writes = [], [], [], []
    counts = {"computed": 0, "recorded": 0, "unknown_cpu_observations": 0}
    previous_finished = None
    for cycle in cycles:
        query, decision, finish = (cycle.get(name, {}) for name in ("query", "decision", "finish"))
        start = epoch(cycle["reconcileStartedAt"])
        series = []
        if query and not query["queryError"]:
            series = history_series(anchors, query, decision, config["windowSeconds"])
            if not decision:
                counts["unknown_cpu_observations"] += 1
        elif query and query["queryError"]:
            preserve = ("metricsprovider: incomplete data", "metricsprovider: no data", "metricsprovider: target changed")
            if not any(reason in query["queryError"] for reason in preserve):
                anchors.clear()
        if decision:
            complete = bool(series) and all(point["value"] is not None for point in series)
            source = "computed-history" if complete else "recorded-forecast"
            counts["computed" if complete else "recorded"] += 1
            item = {"at": decision["stabilizationEvaluatedAt"], "observedReplicas": decision["currentReplicas"],
                    "requestedReplicas": requested, "currentCPU": decision["currentCPU%"],
                    "predictionSource": source, "actualMode": decision["decisionMode"],
                    "expected": {"rawPrediction": decision["rawPredictedCPU%"], "decisionCPU": decision["decisionCPU%"],
                                 "boundedPrediction": decision["predictedCPU%"], "desiredReplicas": decision["desiredReplicas"],
                                 "finalDesired": decision["finalDesired"], "skipReason": decision["skipReason"],
                                 "stabilized": decision["stabilized"], "coldStartProtection": decision["coldStartProtection"],
                                 "protectedUntil": decision["coldStartProtectedUntil"],
                                 "historyEntries": decision["stabilizationHistoryEntries"],
                                 "historyOldestAt": decision["stabilizationHistoryOldestAt"]}}
            item["samples" if complete else "rawPrediction"] = series if complete else decision["rawPredictedCPU%"]
            replay.append(item)
        record = {"reconcile_id": cycle.get("reconcileID"), "reconcile_started_seconds": start - onset,
                  "query_error": query.get("queryError"), "samples": query.get("samples"),
                  "reason": explanation(cycle, requested, config["targetCPU"]), "requested_replicas": requested,
                  "replay_index": len(replay) - 1 if decision else None,
                  "unobserved_since_previous_finish_seconds": start - previous_finished if previous_finished is not None else None}
        for name, field in [("query_started", "queryStartedAt"), ("query_finished", "queryFinishedAt"),
                            ("source_sample", "sourceTimestamp"), ("evaluation", "latestEvaluationAt"),
                            ("observation_finished", "observationFinishedAt")]:
            record[name + "_seconds"] = epoch(query[field]) - onset if query.get(field) else None
        record["source_age_at_observation_seconds"] = (epoch(query["observationFinishedAt"]) - epoch(query["sourceTimestamp"])) if query.get("sourceTimestamp") else None
        record["decision_seconds"] = epoch(decision["decisionAt"]) - onset if decision else None
        record["prediction_source"] = replay[-1]["predictionSource"] if decision else None
        record["current_cpu"] = decision.get("currentCPU%")
        record["recorded_forecast"] = decision.get("rawPredictedCPU%")
        if cycle.get("scale"):
            scale = cycle["scale"]
            if (not decision or decision["skipReason"] or not scale.get("scaled")
                    or scale["previousDesiredReplicas"] != requested
                    or scale["finalDesired"] != decision["finalDesired"]
                    or scale["currentReplicas"] != decision["currentReplicas"]):
                raise ValueError("Successful Scale write chain disagrees with recorded decision")
            writes.append({"started": epoch(scale["scaleWriteStartedAt"]), "finished": epoch(scale["scaleWriteFinishedAt"]),
                           "before": scale["previousDesiredReplicas"], "after": scale["finalDesired"]})
            requested = scale["finalDesired"]
        record["scale_write_finished_seconds"] = writes[-1]["finished"] - onset if cycle.get("scale") else None
        previous_finished = epoch(finish["reconcileFinishedAt"]) if finish else None
        timeline.append(record)
    replica_checks = check_requested_observations(states, writes, initial)
    inputs = {"schemaVersion": 1, "policyHistoryCompleteness": "complete", "config": config, "cycles": replay}
    first_write = next((write["finished"] - onset for write in writes if write["finished"] >= onset and write["after"] > write["before"]), None)
    report = {"schema_version": 1, "experiment_id": metadata["experiment_id"], "actual_mode": plan["decision_mode"],
              "verification": "prepared-only", "history": counts, "identity": {key: plan[key] for key in ("target_uid", "phpa_uid")},
              "requested_replica_checks": replica_checks,
              "source": {"git_commit": metadata["git"]["commit"], "source_sha256": plan["source_sha256"],
                         "controller_binary_sha256": plan["controller_binary_sha256"], "files": manifest(directory)},
              "schedule": schedule, "timing": {"first_scale_seconds": first_write,
                  "first_ready_interval_seconds": ready_interval(states, onset, writes, initial)},
              "cycles": timeline, "reason_counts": dict(Counter(row["reason"] for row in timeline)), "limitations": LIMITATIONS}
    return inputs, report


def write_timeline(path, report, result):
    columns = list(report["cycles"][0])
    if result:
        columns += [mode + suffix for mode in ("Current", "Predictive", "Hybrid")
                    for suffix in ("_final_desired", "_skip_reason")]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for cycle in report["cycles"]:
            row = dict(cycle)
            if result and cycle["replay_index"] is not None:
                for mode, outcome in result["cycles"][cycle["replay_index"]]["modes"].items():
                    row[mode + "_final_desired"] = outcome["finalDesired"]
                    row[mode + "_skip_reason"] = outcome["skipReason"]
            writer.writerow(row)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--replay-binary", type=Path)
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise ValueError("Output already exists; preserve previous results and choose a new directory")
        inputs, report = prepare(args.run)
        result = None
        if args.replay_binary:
            process = subprocess.run([str(args.replay_binary.resolve()), "-input", "-"], input=json.dumps(inputs),
                                     capture_output=True, text=True, timeout=120)
            if process.returncode:
                raise ValueError(f"Replay failed ({process.returncode}): {process.stderr.strip()}")
            result = json.loads(process.stdout)
            if (result.get("verified") is not True or result.get("schemaVersion") != 1
                    or result.get("replicaInput") != "fixed-observed-and-requested"
                    or len(result.get("cycles", [])) != len(inputs["cycles"])):
                raise ValueError("Replay did not verify recorded decisions")
            report["verification"] = "recorded-decisions-matched"
            report["replay_binary_sha256"] = hashlib.sha256(args.replay_binary.read_bytes()).hexdigest()
        args.output.mkdir(parents=True, exist_ok=False)
        for name, value in [("replay-input.json", inputs), ("report.json", report), ("replay-result.json", result)]:
            if value is not None:
                (args.output / name).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        write_timeline(args.output / "timeline.csv", report, result)
        print(json.dumps({"experiment_id": report["experiment_id"], "verification": report["verification"], "history": report["history"]}))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print(f"decision replay analysis: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
