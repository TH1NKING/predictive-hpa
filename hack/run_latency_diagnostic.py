#!/usr/bin/env python3
"""Print the frozen latency plan or observe one existing Current benchmark run.

The existing benchmark owns fixture mutations, load and controller lifetime.
This process owns only its observation files, gate receipt and log followers.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlencode
from urllib.request import urlopen


PROTOCOL = "latency-diagnostic-v1"
INTERVAL_SECONDS = 2
REQUEST_TIMEOUT_SECONDS = 12
GATE_TIMEOUT_SECONDS = 180
CPU_SELECTOR = 'container_cpu_usage_seconds_total{namespace="default",pod=~"php-apache-.*",container!=""}'
REQUEST_SELECTOR = 'kube_pod_container_resource_requests{namespace="default",pod=~"php-apache-.*",resource="cpu"}'
CPU_QUERY = f"(avg(rate({CPU_SELECTOR}[1m]))/avg({REQUEST_SELECTOR}))*100"
PROMETHEUS_URL = "http://localhost:9090"


def utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def frozen_plan() -> dict:
    return {"protocol_version": PROTOCOL, "controller": "phpa_current", "decision_mode": "Current",
            "pattern": "step", "rps": 25, "requeue_seconds": 30, "quiet_seconds": 30,
            "offered_duration_seconds": 181, "post_load_tail_seconds": 360,
            "observation_interval_seconds": INTERVAL_SECONDS,
            "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS, "phase_tolerance_seconds": 2,
            "source_range_seconds": 90, "threshold_cpu_percent": 55,
            "gate_timeout_seconds": GATE_TIMEOUT_SECONDS,
            "anchor_idle_cpu_below_percent": 5,
            "anchor_condition": "Next finished one-replica reconcile after gate readiness, no error, 30s requeue",
            "gate_launch_rounding": "ceil integer Unix seconds; retain unrounded target",
            "gate_receipt_precision_seconds": 1,
            "onset_definition": "k6 scenario.startTime + 30 seconds; separate first request attempt",
            "slots": [{"slot": index + 1, "block": index // 3 + 1, "requested_offset_seconds": offset}
                      for index, offset in enumerate((0, 10, 20, 20, 10, 0))],
            "rerun_policy": "Retain every assigned slot and phase miss; stop on runtime or collection failure",
            "queries": {"prom_cpu_raw": CPU_SELECTOR + "[90s]",
                        "prom_requests_raw": REQUEST_SELECTOR + "[90s]", "prom_cpu_evaluated": CPU_QUERY}}


class Observer:
    def __init__(self, directory: Path, context: str, offset: int) -> None:
        self.directory = directory.resolve()
        self.context = context
        self.offset = offset
        self.kube = ["kubectl", "--context", context, "--namespace", "default", "--request-timeout=10s"]
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.errors: list[dict] = []
        self.followers: dict[str, dict] = {}
        self.cycles = 0
        self.gate_released = False
        self.runner_name = None
        self.started = utc()
        self.plan = {**frozen_plan(), "requested_offset_seconds": offset, "context": context,
                     "observer_started_at": self.started}
        self.stream = None

    def record(self, row: dict) -> None:
        with self.lock:
            self.stream.write(json.dumps(row, allow_nan=False) + "\n")
            self.stream.flush()
            if row.get("status") != "success":
                self.errors.append(row)

    def request(self, kind: str, operation, **details: object) -> dict | None:
        started, monotonic_start = utc(), time.monotonic()
        try:
            response = operation()
            row = {"kind": kind, "request_started_at": started, "request_finished_at": utc(),
                   "duration_seconds": time.monotonic() - monotonic_start, "status": "success",
                   **details, "response": response}
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            row = {"kind": kind, "request_started_at": started, "request_finished_at": utc(),
                   "duration_seconds": time.monotonic() - monotonic_start, "status": "error",
                   **details, "error": repr(error)}
        self.record(row)
        return row.get("response")

    def kubectl(self, *arguments: str) -> str:
        # Keep container paths intact under Git Bash without changing file paths
        # for other callers. subprocess arguments never pass through a host shell.
        environment = {**os.environ, "MSYS_NO_PATHCONV": "1"}
        result = subprocess.run(self.kube + list(arguments), capture_output=True, text=True,
                                timeout=REQUEST_TIMEOUT_SECONDS, env=environment)
        if result.returncode:
            raise RuntimeError(f"kubectl {' '.join(arguments[:3])}: {result.stderr.strip()}")
        return result.stdout

    def prom_query(self, kind: str, query: str) -> None:
        evaluation = time.time()
        def execute() -> dict:
            url = PROMETHEUS_URL + "/api/v1/query?" + urlencode({"query": query, "time": f"{evaluation:.6f}"})
            with urlopen(url, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                value = json.load(response)
            if value.get("status") != "success":
                raise RuntimeError(f"Prometheus query failed: {value}")
            return value
        self.request(kind, execute, query=query, evaluation_time_unix=evaluation)

    def resource(self, kind: str, arguments: list[str]) -> dict | None:
        return self.request(kind, lambda: json.loads(self.kubectl("get", *arguments, "-o", "json")))

    def follow_pods(self, response: dict | None) -> None:
        if response is None:
            return
        for pod in response.get("items", []):
            metadata = pod["metadata"]
            uid, name = metadata["uid"], metadata["name"]
            status = next((item for item in pod.get("status", {}).get("containerStatuses", [])
                           if item.get("name") == "php-apache"), {})
            if uid in self.followers:
                follower = self.followers[uid]
                identity = (status.get("restartCount"), status.get("containerID"))
                original = (follower["restart_count"], follower["container_id"])
                if status and identity != original and identity != follower.get("last_changed_identity"):
                    follower["last_changed_identity"] = identity
                    self.record({"kind": "pod_log_stream", "status": "error", "request_started_at": utc(),
                                 "request_finished_at": utc(), "pod": name,
                                 "original_container": original, "observed_container": identity,
                                 "error": "Container identity changed after log attachment; request evidence has a gap"})
                continue
            if not (status.get("state", {}).get("running") or status.get("state", {}).get("terminated")):
                continue
            if not re.fullmatch(r"[a-zA-Z0-9.-]+", name) or not re.fullmatch(r"[a-zA-Z0-9-]+", uid):
                raise ValueError("Unexpected Pod name or UID in log filename")
            stdout = (self.directory / "workload-access" / f"{name}_{uid}.log").open("wb")
            stderr = (self.directory / "workload-access" / f"{name}_{uid}.stderr").open("wb")
            try:
                process = subprocess.Popen(["kubectl", "--context", self.context, "--namespace", "default",
                    "--request-timeout=0", "logs", "--follow", "--timestamps", f"--since-time={self.started}",
                    name, "-c", "php-apache"], stdout=stdout, stderr=stderr)
            except OSError:
                stdout.close()
                stderr.close()
                raise
            self.followers[uid] = {"process": process, "stdout": stdout, "stderr": stderr,
                                   "name": name, "uid": uid, "first_followed_at": utc(),
                                   "restart_count": status.get("restartCount", 0), "container_id": status.get("containerID"),
                                   "image_id": status.get("imageID")}
            if status.get("restartCount", 0):
                self.record({"kind": "pod_log_stream", "status": "error", "request_started_at": utc(),
                             "request_finished_at": utc(), "pod": name,
                             "error": "Container restarted; current log stream may omit previous-container requests"})

    def snapshot(self, executor: ThreadPoolExecutor) -> None:
        self.cycles += 1
        futures = [executor.submit(self.prom_query, kind, query) for kind, query in self.plan["queries"].items()]
        pods = executor.submit(self.resource, "pods", ["pods", "-l", "run=php-apache"])
        futures.extend((executor.submit(self.resource, "endpoints", ["endpointslices", "-l", "kubernetes.io/service-name=php-apache"]),
                        executor.submit(self.resource, "deployment", ["deployment", "php-apache"])))
        # Each request records its own interval. These parallel observations are
        # not claimed to be an atomic cross-resource snapshot.
        for future in futures:
            future.result()
        self.follow_pods(pods.result())

    def gate_operation(self, kind: str, *arguments: str) -> str | None:
        return self.request(kind, lambda: self.kubectl(*arguments))

    def coordinate_gate(self) -> None:
        sys.path.insert(0, str(Path(__file__).parent / "analyze"))
        from latency import controller_cycles, epoch
        deadline = time.monotonic() + GATE_TIMEOUT_SECONDS
        gate_ready_at = None
        try:
            while not self.stop.is_set() and time.monotonic() < deadline:
                runner = self.directory / "k6-runner.json"
                if self.runner_name is None and runner.exists():
                    try:
                        self.runner_name = json.loads(runner.read_text(encoding="utf-8"))["pod"]
                    except json.JSONDecodeError:
                        self.stop.wait(0.2)
                        continue
                    if not re.fullmatch(r"phpa-k6-[a-z0-9-]+", self.runner_name):
                        raise ValueError("Unexpected k6 resource identity")
                if self.runner_name is None:
                    self.stop.wait(0.2)
                    continue
                if gate_ready_at is None:
                    # Absence/Pending is expected during creation and is not an
                    # API failure. Return the complete GET List as interval evidence.
                    result = self.request("gate_pod", lambda: json.loads(self.kubectl(
                        "get", "pods", "--field-selector", f"metadata.name={self.runner_name}", "-o", "json")))
                    if result is None:
                        raise RuntimeError("Could not observe diagnostic gate Pod")
                    ready = any(any(condition.get("type") == "Ready" and condition.get("status") == "True"
                                    for condition in pod.get("status", {}).get("conditions", []))
                                for pod in result.get("items", []))
                    if not ready:
                        self.stop.wait(1)
                        continue
                    receipt = self.gate_operation("gate_ready", "exec", self.runner_name, "-c", "k6", "--", "sh", "-c",
                        "if [ -f /results/latency-gate-ready ]; then cat /results/latency-gate-ready; fi")
                    if receipt is None:
                        raise RuntimeError("Could not read diagnostic gate readiness")
                    if not receipt.strip():
                        self.stop.wait(0.2)
                        continue
                    gate_ready_at = time.time()
                    self.plan["gate_ready_observed_at"] = utc()
                    self.plan["gate_ready_receipt_unix"] = int(receipt.strip())
                try:
                    cycles = controller_cycles(self.directory / "controller.log")
                except json.JSONDecodeError:
                    self.stop.wait(0.1)
                    continue  # Logger may be in the middle of its final line.
                eligible = []
                for cycle in cycles:
                    finish, decision = cycle.get("finish", {}), cycle.get("decision", {})
                    if (finish and epoch(finish["reconcileFinishedAt"]) > gate_ready_at
                            and finish.get("requeueAfterSeconds") == 30 and not finish.get("reconcileError")
                            and decision.get("currentReplicas") == 1 and decision.get("finalDesired") == 1
                            and float(decision.get("currentCPU%", 100)) < 5):
                        eligible.append(cycle)
                if not eligible:
                    self.stop.wait(0.1)
                    continue
                anchor = eligible[0]
                target = epoch(anchor["finish"]["reconcileFinishedAt"]) + 30 + self.offset
                launch = math.ceil(target)
                if launch <= time.time() + 1:
                    raise RuntimeError("Selected idle gate anchor was already too old to schedule the assigned phase")
                self.plan.update({"anchor": anchor, "unrounded_process_launch_unix": target,
                                  "planned_process_launch_unix": launch, "planned_load_onset_unix": launch + 30,
                                  "gate_rounding_seconds": launch - target})
                atomic_json(self.directory / "latency-plan.json", self.plan)
                delivered = self.gate_operation("gate_schedule", "exec", self.runner_name, "-c", "k6", "--", "sh", "-c",
                    'printf "%s\\n" "$1" > /results/latency-launch-at-unix', "gate", str(launch))
                if delivered is None:
                    raise RuntimeError("Could not schedule diagnostic gate")
                while not self.stop.is_set() and time.monotonic() < deadline:
                    receipt = self.gate_operation("gate_release", "exec", self.runner_name, "-c", "k6", "--", "sh", "-c",
                        "cat /results/latency-gate-status; if [ -f /results/latency-gate-release-unix ]; then cat /results/latency-gate-release-unix; fi")
                    if receipt is None:
                        raise RuntimeError("Could not observe diagnostic gate release")
                    values = receipt.splitlines()
                    if values and values[0] == "released" and len(values) == 2:
                        self.gate_released = True
                        self.plan["gate_release_receipt_unix"] = int(values[1])
                        self.plan["gate_release_observed_at"] = utc()
                        atomic_json(self.directory / "latency-plan.json", self.plan)
                        return
                    if values and values[0] not in ("waiting", "released"):
                        raise RuntimeError(f"Diagnostic gate failed: {values[0]}")
                    self.stop.wait(1)
                break
            if not self.stop.is_set():
                raise RuntimeError("Diagnostic gate exceeded its 180-second deadline")
        except Exception as error:
            self.record({"kind": "gate", "request_started_at": utc(), "request_finished_at": utc(),
                         "status": "error", "error": repr(error)})
            if self.runner_name:
                self.gate_operation("gate_cancel", "exec", self.runner_name, "-c", "k6", "--", "sh", "-c",
                                    "touch /results/latency-gate-cancelled")
            self.stop.set()

    def run(self) -> int:
        if not self.directory.is_dir():
            raise ValueError("Observer requires the existing benchmark run directory")
        for filename in ("latency-observations.ndjson", "latency-observer-status.json", "latency-observer-ready"):
            if (self.directory / filename).exists():
                raise ValueError(f"Observer refuses to overwrite existing {filename}")
        (self.directory / "workload-access").mkdir(exist_ok=False)
        self.stream = (self.directory / "latency-observations.ndjson").open("x", encoding="utf-8")
        atomic_json(self.directory / "latency-plan.json", self.plan)
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda _number, _frame: self.stop.set())
        gate = threading.Thread(target=self.coordinate_gate, name="latency-gate")
        cleaned = True
        follower_status = []
        try:
            with ThreadPoolExecutor(max_workers=6, thread_name_prefix="latency-observer") as executor:
                self.snapshot(executor)
                (self.directory / "latency-observer-ready").write_text(utc() + "\n", encoding="utf-8")
                gate.start()
                self.stop.wait(INTERVAL_SECONDS)
                while not self.stop.is_set():
                    started = time.monotonic()
                    self.snapshot(executor)
                    self.stop.wait(max(0, INTERVAL_SECONDS - (time.monotonic() - started)))
        except Exception as error:
            self.record({"kind": "observer", "request_started_at": utc(), "request_finished_at": utc(),
                         "status": "error", "error": repr(error)})
        finally:
            self.stop.set()
            if gate.ident is not None:
                gate.join(timeout=REQUEST_TIMEOUT_SECONDS * 2 + 2)
                cleaned = not gate.is_alive()
            for follower in self.followers.values():
                process = follower["process"]
                natural_exit = process.poll()
                if natural_exit not in (None, 0):
                    self.record({"kind": "pod_log_stream", "request_started_at": utc(), "request_finished_at": utc(),
                                 "status": "error", "pod": follower["name"], "error": f"Unexpected exit {natural_exit}"})
                if natural_exit is None:
                    process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        cleaned = False
                follower["stdout"].close()
                follower["stderr"].close()
                follower_status.append({key: value for key, value in follower.items() if key not in ("process", "stdout", "stderr")}
                                       | {"natural_exit_code": natural_exit, "final_exit_code": process.returncode})
            success = not self.errors and cleaned and self.gate_released
            atomic_json(self.directory / "latency-observer-status.json", {"status": "success" if success else "failed",
                "started_at": self.started, "finished_at": utc(), "snapshot_count": self.cycles,
                "observation_interval_seconds": INTERVAL_SECONDS, "errors": self.errors,
                "owned_processes_stopped": cleaned, "gate_released": self.gate_released, "log_streams": follower_status})
            self.stream.close()
        return 0 if success else 3


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", action="store_true", help="Print the frozen plan without contacting any cluster")
    commands = parser.add_subparsers(dest="command")
    observe = commands.add_parser("observe")
    observe.add_argument("--run-dir", type=Path, required=True)
    observe.add_argument("--context", required=True)
    observe.add_argument("--offset-seconds", type=int, choices=(0, 10, 20), required=True)
    args = parser.parse_args()
    if args.plan and args.command is None:
        print(json.dumps(frozen_plan(), indent=2))
        return 0
    if args.plan or args.command != "observe":
        parser.error("Choose --plan or observe")
    if not re.fullmatch(r"kind-[a-z0-9][a-z0-9-]*", args.context):
        parser.error("Observer requires an explicit dedicated Kind context")
    try:
        return Observer(args.run_dir, args.context, args.offset_seconds).run()
    except (OSError, ValueError) as error:
        print(f"Latency observer failed: {error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
