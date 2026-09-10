#!/usr/bin/env python3
"""Observe verified controller readiness and Kubernetes replica state."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).parent / "analyze"))
from latency import controller_cycles, epoch


def utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def exclusive_json(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def read_state(context: str) -> dict:
    started = utc()
    resources = {"deployment": ["deployment", "php-apache"],
                 "phpa": ["predictivehpa", "predictivehpa-sample"],
                 "pods": ["pods", "-l", "run=php-apache"]}

    def read(arguments: list[str]) -> tuple[dict, dict | None]:
        receipt = {"request_started_at": utc()}
        value = None
        try:
            result = subprocess.run([shutil.which("kubectl") or "kubectl", "--context", context,
                "--namespace", "default", "--request-timeout=10s", "get", *arguments, "-o", "json"],
                capture_output=True, text=True, timeout=12, env={**os.environ, "MSYS_NO_PATHCONV": "1"})
            if result.returncode:
                raise RuntimeError(result.stderr.strip())
            value = json.loads(result.stdout)
            receipt["status"] = "success"
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            receipt.update(status="error", error=str(error))
        receipt["request_finished_at"] = utc()
        return receipt, value

    with ThreadPoolExecutor(max_workers=3) as executor:
        pending = {key: executor.submit(read, arguments) for key, arguments in resources.items()}
        results = {key: future.result() for key, future in pending.items()}
    requests = {key: value[0] for key, value in results.items()}
    return {"kind": "state", "request_started_at": started, "request_finished_at": utc(),
            "status": "success" if all(row["status"] == "success" for row in requests.values()) else "error",
            "requests": requests, "response": {key: value[1] for key, value in results.items() if value[1] is not None}}


def ready_receipt(state: dict, log: Path, mode: str, target_uid: str, phpa_uid: str) -> dict | None:
    deploy, phpa = state["deployment"], state["phpa"]
    if deploy["metadata"]["uid"] != target_uid or phpa["metadata"]["uid"] != phpa_uid:
        raise ValueError("Target or PredictiveHPA UID changed")
    cycles = controller_cycles(log)
    if not cycles:
        return None
    cycle = cycles[-1]
    query, decision, finish = (cycle.get(key, {}) for key in ("query", "decision", "finish"))
    conditions = phpa.get("status", {}).get("conditions", [])
    metrics_ready = any(row.get("type") == "MetricsReady" and row.get("status") == "True"
                        and row.get("observedGeneration") == phpa["metadata"].get("generation") for row in conditions)
    if (not finish or finish.get("reconcileError") or not query or query.get("queryError")
            or not decision or decision.get("decisionMode") != mode or phpa.get("spec", {}).get("decisionMode") != mode
            or decision.get("samples", 0) < 2 or query.get("samples", 0) < 2 or not query.get("sourceTimestamp")
            or decision.get("currentReplicas") != 1 or decision.get("finalDesired") != 1
            or not 0 <= float(decision.get("currentCPU%", 100)) < 5
            or decision.get("coldStartProtection") is not False or not metrics_ready
            or deploy.get("spec", {}).get("replicas") != 1 or deploy.get("status", {}).get("readyReplicas") != 1
            or deploy.get("status", {}).get("replicas") != 1):
        return None
    if (epoch(decision["stabilizationEvaluatedAt"]) <= epoch(decision["coldStartProtectedUntil"])
            or not 0 <= time.time() - epoch(finish["reconcileFinishedAt"]) <= 72):
        return None
    return {"protocol_version": "live-baseline-v1", "startup_mode": "warm", "released_at": utc(),
            "target_uid": target_uid, "phpa_uid": phpa_uid, "anchor": cycle}


def observe(args: argparse.Namespace) -> int:
    directory = args.run_dir
    names = ("live-baseline-plan.json", "live-observations.ndjson", "live-baseline-gate.json",
             "live-baseline-status.json", "live-baseline-stop")
    if any((directory / name).exists() for name in names):
        raise ValueError("Refusing to overwrite live baseline evidence or consume an old stop receipt")
    for digest in (args.source_sha256, args.binary_sha256):
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Source and controller binary SHA256 must be explicit")
    epoch(args.controller_started_at)
    started = utc()
    plan = {"protocol_version": "live-baseline-v1", "startup_mode": args.startup_mode,
            "decision_mode": args.decision_mode, "pattern": args.pattern, "rps": args.rps,
            "requeue_seconds": 30, "interval_seconds": 2, "quiet_seconds": 30,
            "offered_duration_seconds": 181 if args.pattern == "step" else 240, "post_load_tail_seconds": 360,
            "controller_started_at": args.controller_started_at, "observer_started_at": started,
            "target_uid": args.target_uid, "phpa_uid": args.phpa_uid, "context": args.context,
            "source_sha256": args.source_sha256, "controller_binary_sha256": args.binary_sha256,
            "gate_timeout_seconds": 240, "observer_timeout_seconds": 1500}
    exclusive_json(directory / "live-baseline-plan.json", plan)
    stop = threading.Event()
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: stop.set())
    deadline, gate_deadline = time.monotonic() + 1500, time.monotonic() + 240
    errors, count, gate_released = [], 0, False
    try:
        with (directory / "live-observations.ndjson").open("x", encoding="utf-8") as stream:
            while not stop.is_set() and not (directory / "live-baseline-stop").exists():
                cycle_start = time.monotonic()
                row = read_state(args.context)
                count += 1
                row["cycle_id"] = count
                stream.write(json.dumps(row, allow_nan=False) + "\n")
                stream.flush()
                if row["status"] != "success":
                    raise RuntimeError("Kubernetes state observation failed; see retained request receipts")
                state = row["response"]
                if (state["deployment"]["metadata"]["uid"] != args.target_uid
                        or state["phpa"]["metadata"]["uid"] != args.phpa_uid):
                    raise ValueError("Target or PredictiveHPA UID changed during observation")
                if not gate_released:
                    if args.startup_mode == "cold":
                        receipt = {"protocol_version": "live-baseline-v1", "startup_mode": "cold",
                                   "released_at": utc(), "target_uid": args.target_uid, "phpa_uid": args.phpa_uid,
                                   "anchor": None, "controller_started_at": args.controller_started_at}
                    else:
                        try:
                            receipt = ready_receipt(state, directory / "controller.log", args.decision_mode,
                                                    args.target_uid, args.phpa_uid)
                        except json.JSONDecodeError:
                            receipt = None  # A logger can be partway through its final record.
                    if receipt is not None:
                        exclusive_json(directory / "live-baseline-gate.json", receipt)
                        gate_released = True
                    elif time.monotonic() >= gate_deadline:
                        raise RuntimeError("Verified history readiness exceeded 240 seconds")
                if time.monotonic() >= deadline:
                    raise RuntimeError("Observer exceeded its 1500-second collection deadline")
                stop.wait(max(0, 2 - (time.monotonic() - cycle_start)))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        errors.append({"at": utc(), "error": str(error)})
    success = not errors and gate_released
    exclusive_json(directory / "live-baseline-status.json", {"protocol_version": "live-baseline-v1",
        "status": "success" if success else "failed", "started_at": started, "finished_at": utc(),
        "errors": errors, "observation_count": count, "owned_processes_stopped": True,
        "gate_released": gate_released})
    return 0 if success else 3


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check-ready", help="Check saved state and controller logs without contacting a cluster")
    check.add_argument("--state", type=Path, required=True)
    check.add_argument("--controller-log", type=Path, required=True)
    check.add_argument("--decision-mode", choices=("Current", "Predictive", "Hybrid"), required=True)
    check.add_argument("--target-uid", required=True)
    check.add_argument("--phpa-uid", required=True)
    check.add_argument("--output", type=Path, required=True)
    sample = commands.add_parser("sample", help="Record one read-only Kubernetes observation, including failures")
    sample.add_argument("--context", required=True)
    sample.add_argument("--output", type=Path, required=True)
    run = commands.add_parser("observe", help="Observe until signalled or live-baseline-stop is created")
    run.add_argument("--run-dir", type=Path, required=True)
    run.add_argument("--context", required=True)
    run.add_argument("--startup-mode", choices=("warm", "cold"), required=True)
    run.add_argument("--decision-mode", choices=("Current", "Predictive", "Hybrid"), required=True)
    run.add_argument("--pattern", choices=("step", "ramp"), required=True)
    run.add_argument("--rps", type=int, choices=range(1, 1001), required=True)
    for name in ("controller-started-at", "target-uid", "phpa-uid", "source-sha256", "binary-sha256"):
        run.add_argument("--" + name, required=True)
    args = parser.parse_args()
    try:
        if args.command in ("sample", "observe") and not re.fullmatch(r"kind-[a-z0-9][a-z0-9-]*", args.context):
            raise ValueError("An explicit dedicated Kind context is required")
        if args.command == "observe":
            return observe(args)
        if args.output.exists():
            raise ValueError("Refusing to overwrite output")
        if args.command == "sample":
            row = read_state(args.context)
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(row, allow_nan=False) + "\n")
            return 0 if row["status"] == "success" else 3
        receipt = ready_receipt(json.loads(args.state.read_text(encoding="utf-8")), args.controller_log,
                                args.decision_mode, args.target_uid, args.phpa_uid)
        if receipt is None:
            print("Verified history or cold-start protection is not ready", file=sys.stderr)
            return 4
        exclusive_json(args.output, receipt)
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Live baseline observer failed: {error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
