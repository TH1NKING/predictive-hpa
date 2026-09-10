#!/usr/bin/env python3
"""Run a frozen nine-slot warm baseline in an explicitly owned dedicated Kind cluster."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
MODES = (("Current", "phpa_current"), ("Predictive", "phpa"), ("Hybrid", "phpa_hybrid"))


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, indent=2, allow_nan=False) + "\n")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_identity() -> dict:
    paths = {ROOT / "go.mod", ROOT / "go.sum"}
    for folder, suffixes in (("api", (".go",)), ("cmd", (".go",)), ("internal", (".go",)),
                             ("config", (".yaml",)), ("hack", (".sh", ".py", ".js"))):
        for path in (ROOT / folder).rglob("*"):
            if (path.is_file() and path.suffix in suffixes and not path.name.startswith("test_")
                    and not path.name.endswith("_test.go") and not any(part.startswith(".") for part in path.relative_to(ROOT).parts)
                    and "tests" not in path.relative_to(ROOT).parts):
                paths.add(path)
    return {path.relative_to(ROOT).as_posix(): digest(path) for path in sorted(paths)}


class Campaign:
    def __init__(self, args: argparse.Namespace, output: Path) -> None:
        self.args, self.output, self.sequence = args, output, 0
        self.status = {**plan(args.pattern, args.rps), "context": args.context, "started_at": now(),
                       "status": "in_progress", "error": None, "cleanup_error": None}
        self.environment = {**os.environ, "KUBECONFIG": str(args.kubeconfig.resolve()),
            "BENCHMARK_CONTEXT": args.context, "LIVE_BASELINE": "true", "LIVE_BASELINE_STARTUP_MODE": "warm",
            "LATENCY_DIAGNOSTIC": "false", "METRIC_PIPELINE_DIAGNOSTIC": "false", "METRIC_PIPELINE_SOURCE_NODE": "",
            "BENCHMARK_PATTERNS": args.pattern, "BENCHMARK_CONTROLLERS": "phpa_current phpa phpa_hybrid",
            "BENCHMARK_REPEATS": "3", "BENCHMARK_PYTHON": sys.executable, "RPS": str(args.rps),
            "EXPERIMENTS_ROOT": str(output / "runs"), "CAMPAIGN": output.name}
        self.kube = [shutil.which("kubectl") or "kubectl", "--context", args.context, "--request-timeout=20s"]
        self.active = None

    def command(self, label: str, arguments: list[str], timeout: int = 60) -> str:
        self.sequence += 1
        stem = self.output / "commands" / f"{self.sequence:03d}-{label}"
        with stem.with_suffix(".stdout").open("x", encoding="utf-8") as stdout, stem.with_suffix(".stderr").open("x", encoding="utf-8") as stderr:
            self.active = subprocess.Popen(arguments, cwd=ROOT, env=self.environment, stdout=stdout, stderr=stderr,
                                           start_new_session=os.name != "nt")
            try:
                code = self.active.wait(timeout=timeout)
                if os.name != "nt":
                    try:
                        os.killpg(self.active.pid, 0)
                    except ProcessLookupError:
                        pass
                    else:
                        # A completed leader does not prove that its children
                        # stopped. Retain the group handle until cleanup ends.
                        self.stop_active()
                        raise RuntimeError(f"{label} left descendants after its direct process exited")
                self.active = None
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                self.stop_active()
                raise
        if code:
            raise RuntimeError(f"{label} exited {code}; see {stem.name}.stderr")
        return stem.with_suffix(".stdout").read_text(encoding="utf-8")

    def stop_active(self) -> None:
        if self.active is None:
            return
        process = self.active

        def group_alive() -> bool:
            process.poll()  # Reap the direct child before checking its group.
            try:
                os.killpg(process.pid, 0)
                return True
            except ProcessLookupError:
                return False

        try:
            if os.name == "nt":
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=35)
            else:
                if group_alive():
                    os.killpg(process.pid, signal.SIGTERM)
                deadline = time.monotonic() + 35
                while group_alive() and time.monotonic() < deadline:
                    time.sleep(0.1)
                if group_alive():
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                    raise RuntimeError("Owned campaign process group required forced termination")
        except subprocess.TimeoutExpired:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
                raise RuntimeError("Owned campaign command required forced termination")
        except ProcessLookupError:
            process.wait(timeout=5)
        finally:
            self.active = None

    def get(self, label: str, *arguments: str) -> dict:
        return json.loads(self.command(label, self.kube + ["get", *arguments, "-o", "json"]))

    def snapshot(self) -> dict:
        namespace = self.get("namespace", "namespace", "default")
        nodes = self.get("nodes", "nodes")
        kind_nodes = [name.strip() for name in self.command("kind-nodes",
            [shutil.which("kind") or "kind", "get", "nodes", "--name", self.args.context.removeprefix("kind-")]).splitlines()
                      if name.strip()]
        api_nodes = [node["metadata"]["name"] for node in nodes["items"]]
        if (not api_nodes or not kind_nodes or len(set(api_nodes)) != len(api_nodes)
                or len(set(kind_nodes)) != len(kind_nodes) or sorted(api_nodes) != sorted(kind_nodes)):
            raise ValueError("API node roster does not match the nonempty node roster of the assigned local Kind cluster")
        deploy = self.get("deployment", "deployment", "php-apache", "-n", "default")
        service = self.get("service", "service", "php-apache", "-n", "default")
        hpas = self.get("hpas", "hpa", "-A")
        phpas = self.get("phpas", "predictivehpas", "-A")
        pods = self.get("pods", "pods", "-A")
        monitoring = self.get("monitoring-config", "configmaps", "-n", "monitoring")
        def targeting(item: dict) -> bool:
            return item["metadata"].get("namespace", "default") == "default" and item.get("spec", {}).get("scaleTargetRef", {}).get("name") == "php-apache"
        if any(targeting(item) for item in hpas["items"]):
            raise ValueError("A native HPA competes for the baseline target")
        policies = phpas["items"]
        if len(policies) > 1 or any(not targeting(item) or item["metadata"]["name"] != "predictivehpa-sample"
                                   for item in policies):
            raise ValueError("The dedicated campaign permits only its single intended PredictiveHPA")
        for pod in pods["items"]:
            metadata = pod["metadata"]
            if pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
                continue
            labels = metadata.get("labels", {})
            if (labels.get("control-plane") == "controller-manager"
                    or labels.get("app.kubernetes.io/name") == "predictive-hpa"
                    or metadata.get("namespace") == "predictive-hpa-system"):
                raise ValueError("An in-cluster PredictiveHPA manager may compete with the owned process")
        spec = dict(deploy["spec"])
        spec.pop("replicas", None)
        workload_images = sorted({status["imageID"] for pod in pods["items"]
            if pod["metadata"].get("namespace") == "default" and pod["metadata"].get("labels", {}).get("run") == "php-apache"
            for status in pod.get("status", {}).get("containerStatuses", []) if status.get("imageID")})
        if not workload_images:
            raise ValueError("Workload runtime image identity is unavailable")
        return {"namespace_uid": namespace["metadata"]["uid"],
            "kind_nodes": sorted(kind_nodes),
            "nodes": sorted([{"uid": node["metadata"]["uid"], "name": node["metadata"]["name"],
                "node_info": node["status"]["nodeInfo"], "capacity": node["status"]["capacity"],
                "allocatable": node["status"]["allocatable"], "spec": node["spec"]} for node in nodes["items"]], key=lambda item: item["name"]),
            "deployment_uid": deploy["metadata"]["uid"], "deployment_spec": spec,
            "workload_image_ids": workload_images, "service_uid": service["metadata"]["uid"], "service_spec": service["spec"],
            "monitoring_configs": sorted([{"name": cm["metadata"]["name"], "uid": cm["metadata"]["uid"], "data": cm.get("data", {})}
                for cm in monitoring["items"]], key=lambda item: item["name"]),
            "phpa": {"uid": policies[0]["metadata"]["uid"], "spec": policies[0]["spec"]} if policies else None}

    def run(self) -> int:
        self.output.mkdir()
        (self.output / "commands").mkdir()
        (self.output / "runs").mkdir()
        (self.output / "analysis").mkdir()
        write_json(self.output / "campaign-plan.json", self.status)
        initial = None
        try:
            active_context = self.command("context", self.kube + ["config", "current-context"]).strip()
            if active_context != self.args.context:
                raise ValueError("Isolated kubeconfig active context differs from the assigned cluster")
            names = self.command("kind-clusters", [shutil.which("kind") or "kind", "get", "clusters"]).splitlines()
            if self.args.context.removeprefix("kind-") not in names:
                raise ValueError("Context does not identify a local Kind cluster")
            initial = self.snapshot()
            write_json(self.output / "initial-state.json", initial)
            expected = None
            if self.args.expected_phpa_receipt:
                previous = json.loads(self.args.expected_phpa_receipt.read_text(encoding="utf-8"))
                if previous.get("deployment_uid") != initial["deployment_uid"] or previous.get("namespace_uid") != initial["namespace_uid"]:
                    raise ValueError("Previous fixture receipt belongs to a different cluster or target")
                expected = previous.get("phpa")
            if initial["phpa"] != expected:
                raise ValueError("Unexpected initial PHPA fixture; supply its exact previous terminal-state receipt")
            frozen_source = source_identity()
            write_json(self.output / "source-sha256.json", frozen_source)
            with tempfile.TemporaryDirectory(prefix="phpa-live-campaign-") as private:
                binary = Path(private) / ("controller.exe" if os.name == "nt" else "controller")
                self.command("build-controller", [shutil.which("go") or "go", "build", "-trimpath", "-o", str(binary), "./cmd"], 600)
                self.environment["LIVE_BASELINE_CONTROLLER_BINARY"] = str(binary)
                self.environment["LIVE_BASELINE_CONTROLLER_SHA256"] = digest(binary)
                write_json(self.output / "controller-binary.json", {"sha256": digest(binary), "build_flags": ["-trimpath"]})
                previous = initial
                for slot in self.status["slots"]:
                    if source_identity() != frozen_source or digest(binary) != self.environment["LIVE_BASELINE_CONTROLLER_SHA256"]:
                        raise ValueError("Frozen source or controller binary changed between slots")
                    current = self.snapshot()
                    if current != previous:
                        raise ValueError("Cluster, monitoring, workload or expected fixture changed between slots")
                    slot["status"], slot["started_at"] = "running", now()
                    write_json(self.output / f"slot-{slot['slot']:02d}-before.json", current)
                    before = set((self.output / "runs").iterdir())
                    try:
                        self.command(f"slot-{slot['slot']:02d}-benchmark", [shutil.which("bash") or "bash", "hack/run_benchmark.sh",
                            self.args.pattern, slot["controller"], str(slot["repeat"])], 1200)
                        created = set((self.output / "runs").iterdir()) - before
                        if len(created) != 1:
                            raise ValueError("Expected exactly one new benchmark run directory")
                        run = created.pop()
                        slot["run_dir"] = run.relative_to(self.output).as_posix()
                        if source_identity() != frozen_source:
                            raise ValueError("Frozen source changed during a slot")
                        analysis = self.output / "analysis" / f"slot-{slot['slot']:02d}.json"
                        self.command(f"slot-{slot['slot']:02d}-analysis", [sys.executable, "hack/analyze/live_baseline.py", str(run), "--output", str(analysis)])
                        slot["analysis"] = analysis.relative_to(self.output).as_posix()
                        previous = self.snapshot()
                        if {k: v for k, v in previous.items() if k != "phpa"} != {k: v for k, v in initial.items() if k != "phpa"}:
                            raise ValueError("Frozen cluster or workload identity changed during a slot")
                        applied = json.loads((run / "phpa-after-apply.json").read_text(encoding="utf-8"))
                        if previous["phpa"] != {"uid": applied["metadata"]["uid"], "spec": applied["spec"]}:
                            raise ValueError("Terminal PHPA differs from the fixture owned by this slot")
                        write_json(self.output / f"slot-{slot['slot']:02d}-after.json", previous)
                        slot["status"], slot["finished_at"] = "success", now()
                    except Exception:
                        slot["status"], slot["finished_at"] = "failed", now()
                        slot["partial_run_dirs"] = [path.relative_to(self.output).as_posix() for path in (set((self.output / "runs").iterdir()) - before)]
                        raise
                write_json(self.output / "terminal-state.json", previous)
            self.status["status"] = "success"
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError, KeyboardInterrupt) as error:
            self.status.update(status="failed", error=str(error) or "Interrupted")
        finally:
            try:
                self.stop_active()
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                self.status.update(status="failed", cleanup_error=str(error))
            for slot in self.status["slots"]:
                if slot["status"] == "running":
                    slot["status"] = "failed"
            self.status["finished_at"] = now()
            write_json(self.output / "campaign-status.json", self.status)
        return 0 if self.status["status"] == "success" else 3


def plan(pattern: str, rps: int) -> dict:
    return {"protocol_version": "live-baseline-v1", "pattern": pattern, "rps": rps, "startup_mode": "warm",
            "service_criteria": {"http_200_ratio_min": 0.99, "all_request_p95_ms_max": 500, "dropped_iterations_max": 0},
            "slots": [{"slot": repeat * 3 + offset + 1, "repeat": repeat + 1,
                       "decision_mode": MODES[(repeat + offset) % 3][0],
                       "controller": MODES[(repeat + offset) % 3][1], "status": "not_run"}
                      for repeat in range(3) for offset in range(3)]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", action="store_true", help="Print the offline assignment plan")
    parser.add_argument("--pattern", choices=("step", "ramp"), required=True)
    parser.add_argument("--rps", type=int, choices=range(1, 1001), default=25)
    parser.add_argument("--context")
    parser.add_argument("--kubeconfig", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--expected-phpa-receipt", type=Path, help="Previous terminal-state.json authorizing an existing benchmark fixture")
    args = parser.parse_args()
    if args.plan:
        print(json.dumps(plan(args.pattern, args.rps), indent=2))
        return 0
    if not args.context or not re.fullmatch(r"kind-phpa-live-baseline-[a-z0-9][a-z0-9-]*", args.context):
        parser.error("Use an explicit kind-phpa-live-baseline-* dedicated cluster")
    if args.kubeconfig is None or not args.kubeconfig.is_file() or args.output is None:
        parser.error("An existing isolated --kubeconfig and fresh --output directory are required")
    output = args.output.resolve()
    if output.exists() or args.kubeconfig.resolve().is_relative_to(output):
        parser.error("Output must be new and must not contain the private kubeconfig")
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def interrupted(number: int, _frame: object) -> None:
        # A second termination request must not bypass the bounded cleanup.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt(f"Received signal {number}")

    signal.signal(signal.SIGTERM, interrupted)
    try:
        return Campaign(args, output).run()
    except OSError as error:
        print(f"Live campaign failed: {error}", file=sys.stderr)
        return 3
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    raise SystemExit(main())
