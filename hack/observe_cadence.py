#!/usr/bin/env python3
"""Launch a frozen cadence slot only after a complete new verified CPU sample."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import hashlib
import math
import os
from pathlib import Path
import sys
import re
import shutil
import signal
import subprocess
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def epoch(value) -> float:
    if not isinstance(value, str):
        raise ValueError("Timestamp must be an explicit string")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamps require a timezone")
    return result.timestamp()


def exclusive_json(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, indent=2, allow_nan=False) + "\n")


def source_set(row: dict, target_uid: str) -> dict:
    if row["protocol_version"] != "cadence-cpu-v1" or row["target_uid"] != target_uid:
        raise ValueError("CPU observation target or protocol changed")
    if row["status"] != "success" or row.get("error"):
        raise ValueError("Accepted CPU observation must not contain an error")
    started, finished, evaluated = (epoch(row[name]) for name in
        ("observation_started_at", "observation_finished_at", "evaluated_at"))
    if not started - .001 <= evaluated <= finished or finished < started:
        raise ValueError("Invalid CPU observation interval")
    value = row["utilization_percent"]
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
        raise ValueError("Invalid CPU utilization")
    sources = {}
    for container in row["containers"]:
        key = tuple(container[name] for name in ("pod", "pod_uid", "container", "runtime_id"))
        if any(not isinstance(part, str) or not part for part in key) or key in sources:
            raise ValueError("Incomplete or duplicate container identity")
        source = epoch(container["source_timestamp"])
        if source > evaluated or not 0 <= finished - source <= 45:
            raise ValueError("Stale or future container source sample")
        sources[key] = source
    if not sources or epoch(row["source_timestamp"]) != min(sources.values()):
        raise ValueError("Oldest source timestamp disagrees with complete container set")
    return sources


def select_anchor(rows: list[dict], target_uid: str, after: float, offset: int) -> dict | None:
    previous = None
    previous_sources = None
    last_sequence, last_finished = 0, float("-inf")
    for row in rows:
        if row.get("kind") != "cpu_observation":
            continue
        if row["target_uid"] != target_uid:
            raise ValueError("CPU observation target changed")
        sequence = row["sequence"]
        started, finished = epoch(row["observation_started_at"]), epoch(row["observation_finished_at"])
        if type(sequence) is not int or sequence != last_sequence + 1 or started < last_finished or finished < started:
            raise ValueError("CPU observation stream is incomplete or out of order")
        last_sequence, last_finished = sequence, finished
        if row["status"] != "success":
            previous, previous_sources = None, None
            continue
        current = source_set(row, target_uid)
        if (previous is not None and started >= after and current.keys() == previous_sources.keys()
                and all(current[key] > previous_sources[key] for key in current)):
            return {"protocol_version": "cadence-pilot-v1", "previous_observation": previous,
                    "anchor": row, "planned_onset_unix": finished + offset}
        previous, previous_sources = row, current
    return None


def read_rows(path: Path) -> list[dict]:
    # The final row may still be in the producer's write buffer. Complete invalid
    # rows are errors; an unfinished tail is never used as an observation.
    content = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in content.splitlines(keepends=True) if line.endswith("\n") and line.strip()]


def k6_request(url: str, method: str = "GET") -> dict:
    parts = urlsplit(url)
    if parts.scheme != "http" or parts.hostname != "127.0.0.1" or parts.username or parts.password or parts.path:
        raise ValueError("k6 control must use the owned localhost port-forward")
    body = None if method == "GET" else json.dumps(
        {"data": {"type": "status", "id": "default", "attributes": {"paused": False}}}).encode()
    with urlopen(Request(url + "/v1/status", data=body, method=method,
                         headers={"Content-Type": "application/json"}), timeout=2) as response:
        return json.load(response)


def runner_is_ready(response: dict) -> bool:
    attributes = response["data"]["attributes"]
    # k6 1.3.0 ExecutionStatusPausedBeforeRun is 4, set after VU/executor init.
    return (attributes.get("status") == 4 and attributes.get("paused") is True
            and attributes.get("running") is False and attributes.get("stopped") is False)


def release_gate(observations: Path, target_uid: str, after: float, offset: int, url: str,
                 timeout: float, stop: threading.Event, check=None, attempt_path: Path | None = None) -> dict:
    deadline = time.monotonic() + timeout
    if not runner_is_ready(k6_request(url)):
        raise ValueError("k6 has not completed initialization in its paused state")
    while not stop.is_set() and time.monotonic() < deadline:
        if check:
            check()
        gate = select_anchor(read_rows(observations), target_uid, after, offset)
        if gate is None:
            stop.wait(.05)
            continue
        if gate["planned_onset_unix"] <= time.time():
            raise ValueError("Assigned source anchor is too old to meet its frozen phase")
        target_monotonic = time.monotonic() + gate["planned_onset_unix"] - time.time()
        while not stop.is_set() and time.monotonic() < min(target_monotonic, deadline):
            stop.wait(min(.05, max(0, target_monotonic - time.monotonic())))
        if stop.is_set():
            break
        if time.monotonic() >= deadline:
            raise TimeoutError("Cadence source gate exceeded its deadline before release")
        if check:
            check(gate)
        # Never retry an uncertain release: the HTTP failure may follow a real start.
        gate["release_request_started_at"] = utc()
        if attempt_path is not None:
            exclusive_json(attempt_path, gate)
        gate["release_response"] = k6_request(url, "PATCH")
        gate["release_request_finished_at"] = utc()
        attributes = gate["release_response"]["data"]["attributes"]
        if attributes.get("paused") is not False or attributes.get("stopped") is not False:
            raise ValueError("k6 did not acknowledge its assigned release")
        gate["status"] = "released"
        return gate
    if stop.is_set():
        raise RuntimeError("Cadence source gate was interrupted")
    raise TimeoutError("Cadence source gate exceeded its deadline")


def observe(args) -> int:
    directory = args.run_dir
    names = ("cadence-plan.json", "cadence-status.json", "cadence-gate.json", "cadence-cpu.ndjson",
             "cadence-cpu.log", "cadence-observer-ready", "cadence-stop", "cadence-cpu-stop",
             "cadence-forward.log", "cadence-control.ndjson", "cadence-gate-attempt.json")
    if not directory.is_dir() or any((directory / name).exists() for name in names):
        raise ValueError("Cadence observer requires a run directory without old cadence evidence")
    plan = {"protocol_version": "cadence-pilot-v1", "pair": args.pair, "slot": args.slot,
            "requeue_seconds": args.requeue_seconds, "offset_seconds": args.offset_seconds,
            "target_uid": args.target_uid, "phpa_uid": args.phpa_uid, "interval_seconds": 1,
            "gate_timeout_seconds": 120, "phase_tolerance_seconds": 1,
            "cpu_binary_sha256": args.cpu_sha256, "started_at": utc(), "context": args.context}
    exclusive_json(directory / "cadence-plan.json", plan)
    stop = threading.Event()
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: stop.set())
    errors, processes, gate_released, cleaned = [], [], False, True
    cpu = None
    try:
        if (not re.fullmatch(r"[a-f0-9]{64}", args.cpu_sha256)
                or hashlib.sha256(args.cpu_binary.read_bytes()).hexdigest() != args.cpu_sha256):
            raise ValueError("Frozen CPU observer binary identity is missing or changed")
        base = json.loads((directory / "live-baseline-plan.json").read_text())
        command = json.loads((directory / "controller-command.json").read_text())
        if (base["protocol_version"] != "cadence-pilot-v1" or base["requeue_seconds"] != args.requeue_seconds
                or command["requeue_seconds"] != args.requeue_seconds
                or [v for v in command["args"] if v.startswith("--requeue-interval=")] != [f"--requeue-interval={args.requeue_seconds}s"]
                or command["binary_sha256"] != base["controller_binary_sha256"]
                or base["target_uid"] != args.target_uid or base["phpa_uid"] != args.phpa_uid):
            raise ValueError("Declared cadence or identity differs from the running controller")
        warm = json.loads((directory / "live-baseline-gate.json").read_text())
        warm_time = epoch(warm["released_at"])
        with ExitStack() as stack:
            stdout = stack.enter_context((directory / "cadence-cpu.ndjson").open("x", encoding="utf-8"))
            stderr = stack.enter_context((directory / "cadence-cpu.log").open("x", encoding="utf-8"))
            forward_log = stack.enter_context((directory / "cadence-forward.log").open("x", encoding="utf-8"))
            controls = stack.enter_context((directory / "cadence-control.ndjson").open("x", encoding="utf-8"))
            cpu = subprocess.Popen([str(args.cpu_binary.resolve()), "--context", args.context,
                "--target-uid", args.target_uid, "--prometheus-address", args.prometheus_address,
                "--stop-file", str((directory / "cadence-cpu-stop").resolve())], stdout=stdout, stderr=stderr)
            processes.append(cpu)
            exclusive_json(directory / "cadence-observer-ready", {"started_at": utc()})
            kube = [shutil.which("kubectl") or "kubectl", "--context", args.context, "--namespace", "default"]
            deadline = time.monotonic() + 180
            runner, forward, url = None, None, None
            ready_at = None
            while not stop.is_set() and time.monotonic() < deadline:
                if cpu.poll() is not None:
                    raise RuntimeError("Verified CPU observer exited before the launch gate")
                receipt = directory / "k6-runner.json"
                if runner is None and receipt.exists():
                    try:
                        runner = json.loads(receipt.read_text())
                    except json.JSONDecodeError:
                        stop.wait(.05)
                        continue
                    name = runner["pod"]
                    if not re.fullmatch(r"phpa-k6-[a-z0-9-]+", name) or runner.get("script") != "cadence.js":
                        raise ValueError("Unexpected cadence k6 runner identity")
                    # Bind a port selected by kubectl; the exact forwarding process
                    # and observed port stay owned by this run.
                    forward = subprocess.Popen(kube + ["--request-timeout=0", "port-forward", "pod/" + name,
                        ":6565", "--address=127.0.0.1", "--pod-running-timeout=120s"],
                        stdout=forward_log, stderr=subprocess.STDOUT)
                    processes.append(forward)
                if forward is not None:
                    if forward.poll() is not None:
                        raise RuntimeError("Owned k6 control port-forward exited")
                    match = re.search(r"Forwarding from 127\.0\.0\.1:(\d+) -> 6565",
                                      (directory / "cadence-forward.log").read_text())
                    if match:
                        url = "http://127.0.0.1:" + match[1]
                        started = utc()
                        try:
                            response = k6_request(url)
                            controls.write(json.dumps({"method": "GET", "started_at": started,
                                "finished_at": utc(), "response": response}) + "\n")
                            controls.flush()
                            if runner_is_ready(response):
                                ready_at = utc()
                                break
                        except OSError as error:
                            controls.write(json.dumps({"method": "GET", "started_at": started,
                                "finished_at": utc(), "error": str(error)}) + "\n")
                            controls.flush()
                stop.wait(.2)
            if ready_at is None:
                raise RuntimeError("k6 did not initialize within the bounded startup period")
            after = max(warm_time + 30, epoch(ready_at))
            sys.path.insert(0, str(Path(__file__).parent))
            from observe_live_baseline import ready_receipt

            def check(gate=None):
                if cpu.poll() is not None or forward.poll() is not None:
                    raise RuntimeError("An owned observer or control process exited")
                state_rows = read_rows(directory / "live-observations.ndjson")
                if not state_rows:
                    raise ValueError("No current Kubernetes state evidence")
                state = state_rows[-1]
                if (state["status"] != "success" or time.time() - epoch(state["request_finished_at"]) > 4
                    or ready_receipt(state["response"], directory / "controller.log", "Current",
                                         args.target_uid, args.phpa_uid, args.requeue_seconds,
                                         allow_inflight=True) is None):
                    raise ValueError("Warm target readiness changed before release")
                if gate is not None:
                    expected = source_set(gate["anchor"], args.target_uid).keys()
                    if gate["anchor"]["utilization_percent"] >= 5:
                        raise ValueError("Verified CPU source anchor is no longer idle")
                    for row in read_rows(directory / "cadence-cpu.ndjson"):
                        if row.get("kind") != "cpu_observation" or row["sequence"] <= gate["anchor"]["sequence"]:
                            continue
                        if (row["status"] != "success" or source_set(row, args.target_uid).keys() != expected
                                or row["utilization_percent"] >= 5):
                            raise ValueError("Verified CPU coverage or idle condition changed while waiting for the assigned phase")

            while time.time() < after and not stop.is_set():
                check()
                stop.wait(.1)
            gate = release_gate(directory / "cadence-cpu.ndjson", args.target_uid, after, args.offset_seconds,
                                url, 120, stop, check, directory / "cadence-gate-attempt.json")
            gate.update(warm_ready_at=warm["released_at"], runner_ready_at=ready_at)
            exclusive_json(directory / "cadence-gate.json", gate)
            gate_released = True
            while not stop.is_set() and not (directory / "cadence-stop").exists():
                if cpu.poll() is not None:
                    raise RuntimeError("CPU observer exited before collection completed")
                # k6 may have finished; its control listener can close normally.
                stop.wait(.1)
            if stop.is_set():
                raise RuntimeError("Cadence observer was interrupted")
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
        errors.append(str(error))
    finally:
        if cpu is not None and cpu.poll() is None:
            exclusive_json(directory / "cadence-cpu-stop", {"stopped_at": utc()})
            try:
                if cpu.wait(timeout=12) != 0:
                    errors.append("CPU observer did not stop cleanly")
            except subprocess.TimeoutExpired:
                errors.append("CPU observer exceeded cleanup deadline")
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                    cleaned = False
        exclusive_json(directory / "cadence-status.json", {"protocol_version": "cadence-pilot-v1",
            "status": "success" if not errors and gate_released and cleaned else "failed", "errors": errors,
            "gate_released": gate_released, "owned_processes_stopped": cleaned,
            "started_at": plan["started_at"], "finished_at": utc()})
    return 0 if not errors and gate_released and cleaned else 3


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("check-anchor", "release"):
        check = commands.add_parser(command)
        check.add_argument("--observations", type=Path, required=True)
        check.add_argument("--target-uid", required=True)
        check.add_argument("--after", required=True)
        check.add_argument("--offset-seconds", type=int, choices=(2, 7, 12), required=True)
        check.add_argument("--output", type=Path, required=True)
        if command == "release":
            check.add_argument("--k6-url", required=True)
            check.add_argument("--timeout-seconds", type=float, default=120)
    run = commands.add_parser("observe")
    run.add_argument("--run-dir", type=Path, required=True)
    run.add_argument("--context", required=True)
    run.add_argument("--cpu-binary", type=Path, required=True)
    for name in ("cpu-sha256", "prometheus-address", "target-uid", "phpa-uid"):
        run.add_argument("--" + name, required=True)
    run.add_argument("--requeue-seconds", type=int, choices=(15, 30), required=True)
    run.add_argument("--offset-seconds", type=int, choices=(2, 7, 12), required=True)
    run.add_argument("--pair", type=int, choices=(1, 2, 3), required=True)
    run.add_argument("--slot", type=int, choices=range(1, 7), required=True)
    args = parser.parse_args()
    try:
        if args.command == "observe":
            if not re.fullmatch(r"kind-phpa-cadence-[a-z0-9][a-z0-9-]*", args.context):
                raise ValueError("Use an explicit kind-phpa-cadence-* dedicated context")
            return observe(args)
        if args.output.exists():
            raise ValueError("Refusing to overwrite gate evidence")
        if args.command == "release":
            if not 0 < args.timeout_seconds <= 120:
                raise ValueError("Gate timeout must be positive and at most 120 seconds")
            stop = threading.Event()
            for number in (signal.SIGINT, signal.SIGTERM):
                signal.signal(number, lambda *_: stop.set())
            try:
                result = release_gate(args.observations, args.target_uid, epoch(args.after), args.offset_seconds,
                                      args.k6_url, args.timeout_seconds, stop)
            except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
                exclusive_json(args.output, {"protocol_version": "cadence-pilot-v1", "status": "failed",
                                            "error": str(error), "finished_at": utc()})
                raise
            exclusive_json(args.output, result)
            return 0
        result = select_anchor(read_rows(args.observations), args.target_uid, epoch(args.after), args.offset_seconds)
        if result is None:
            print("No complete new source sample is available", file=sys.stderr)
            return 4
        exclusive_json(args.output, result)
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        print(f"Cadence observer failed: {error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
