#!/usr/bin/env python3
"""Exercise metrics isolation, source outage and leader restart in dedicated Kind.

Requires a running two-replica Helm manager and Prometheus in the dedicated
cluster. All commands carry an explicit context and kubeconfig. Outputs retain
failed checks as well as successful observations; this is acceptance evidence,
not a service-performance benchmark.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time


WORKLOAD_NS = "metrics-safety-workloads"


class Acceptance:
    def __init__(self, args):
        self.args = args
        self.output = Path(args.output).resolve()
        self.output.mkdir(parents=True, exist_ok=False)
        self.commands = self.output / "commands.ndjson"
        self.observations = self.output / "observations.ndjson"
        self.cluster_uid = None
        self.fixture_uid = None
        self.fixture_created = False
        self.source_stopped = False
        self.results = []

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()

    def record(self, path, value):
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": self.now(), **value}) + "\n")

    def kubectl(self, *arguments, namespace=None):
        command = ["kubectl", "--kubeconfig", self.args.kubeconfig,
                   "--context", self.args.context]
        if namespace:
            command += ["-n", namespace]
        command += list(arguments)
        result = subprocess.run(command, capture_output=True, text=True, timeout=45)
        self.record(self.commands, {"command": command[5:], "returncode": result.returncode,
                                   "stdout": result.stdout, "stderr": result.stderr})
        if result.returncode:
            raise RuntimeError(f"kubectl {arguments}: {result.stderr.strip()}")
        return result.stdout

    def get(self, kind, name=None, namespace=WORKLOAD_NS):
        args = ["get", kind]
        if name:
            args.append(name)
        return json.loads(self.kubectl(*args, "-o", "json", namespace=namespace))

    def guard(self):
        identity = self.get("namespace", "kube-system", namespace=None)["metadata"]["uid"]
        if self.cluster_uid is not None and identity != self.cluster_uid:
            raise RuntimeError("Cluster identity changed; refusing mutations")
        self.cluster_uid = identity
        nodes = self.get("nodes", namespace=None)["items"]
        cluster_name = self.args.context.removeprefix("kind-")
        if not nodes or any(not n["metadata"]["name"].startswith(cluster_name + "-") for n in nodes):
            raise RuntimeError("Context does not point to the named dedicated Kind nodes")

    def snapshot(self):
        snapshot = {"phpas": self.get("phpa")["items"],
                    "deployments": self.get("deployments")["items"]}
        self.record(self.observations, snapshot)
        return snapshot

    @staticmethod
    def item(snapshot, collection, name):
        return next(x for x in snapshot[collection] if x["metadata"]["name"] == name)

    @staticmethod
    def condition(phpa, name):
        return next((x for x in phpa.get("status", {}).get("conditions", [])
                     if x["type"] == name), {})

    def wait(self, label, predicate, timeout=240):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = self.snapshot()
            if predicate(current):
                self.results.append({"check": label, "at": self.now(), "passed": True})
                return current
            time.sleep(5)
        raise RuntimeError(f"Timed out: {label}")

    def scale(self, name, replicas, namespace):
        self.guard()
        self.kubectl("scale", "deployment", name, f"--replicas={replicas}", namespace=namespace)

    def source_ready(self, snapshot):
        return all(self.condition(p, "MetricsReady").get("status") == "True"
                   for p in snapshot["phpas"])

    def requested(self, snapshot, name="web"):
        return self.item(snapshot, "deployments", name)["spec"]["replicas"]

    def set_load(self, enabled):
        self.guard()
        pods = self.get("pods")["items"]
        for pod in pods:
            if pod["metadata"].get("labels", {}).get("app") != "metrics-safety-web":
                continue
            if pod["metadata"].get("deletionTimestamp"):
                continue
            status = next((c for c in pod.get("status", {}).get("containerStatuses", [])
                           if c["name"] == "app"), {})
            if not status.get("state", {}).get("running"):
                continue
            operation = ["touch", "/tmp/load"] if enabled else ["rm", "-f", "/tmp/load"]
            self.kubectl("exec", pod["metadata"]["name"], "-c", "app", "--", *operation,
                         namespace=WORKLOAD_NS)

    def check_isolation(self):
        def isolated(snapshot):
            if not self.source_ready(snapshot):
                return False
            target = self.item(snapshot, "phpas", "web")["status"]
            canary = self.item(snapshot, "phpas", "web-canary")["status"]
            return (target["currentCPUUtilizationPercentage"] < 20
                    and canary["currentCPUUtilizationPercentage"] > 100)
        self.wait("Idle web stays below 20% while same-prefix canary exceeds 100%", isolated)
        self.wait("Idle target eventually downscales to one after protection", lambda s: self.requested(s) == 1)

    def check_rollout(self):
        self.guard()
        before = {p["metadata"]["uid"] for p in self.get("pods")["items"]
                  if p["metadata"].get("labels", {}).get("app") == "metrics-safety-web"}
        self.kubectl("rollout", "restart", "deployment/web", namespace=WORKLOAD_NS)
        self.kubectl("rollout", "status", "deployment/web", "--timeout=40s", namespace=WORKLOAD_NS)
        after = {p["metadata"]["uid"] for p in self.get("pods")["items"]
                 if p["metadata"].get("labels", {}).get("app") == "metrics-safety-web"
                 and not p["metadata"].get("deletionTimestamp")}
        if not after or before & after:
            raise RuntimeError("Rollout did not replace the active target Pods")
        # Allow a complete normal reconciliation after the rollout, instead of
        # accepting a Ready condition left over from the old roster.
        time.sleep(35)
        self.wait("Rolling update returns complete fresh metrics", self.source_ready)
        self.wait("Rolling update remains isolated from canary CPU", lambda s:
                  self.condition(self.item(s, "phpas", "web"), "MetricsReady").get("status") == "True"
                  and self.item(s, "phpas", "web")["status"]["currentCPUUtilizationPercentage"] < 20)

    def check_restart(self):
        self.set_load(True)
        self.wait("Real target CPU triggers an actual Scale increase", lambda s: self.requested(s) >= 2)
        self.set_load(False)
        # Stop the old process promptly after demand drops. The successor must
        # establish its own protection, even if old status survives in the API.
        lease = self.get("lease", "8da91c08.brian.io", self.args.manager_namespace)
        holder = lease["spec"]["holderIdentity"]
        pods = self.get("pods", namespace=self.args.manager_namespace)["items"]
        leader = next(p["metadata"]["name"] for p in pods
                      if holder.startswith(p["metadata"]["name"] + "_"))
        self.kubectl("logs", leader, "-c", "manager", namespace=self.args.manager_namespace)
        self.guard()
        self.kubectl("delete", "pod", leader, "--wait=false", namespace=self.args.manager_namespace)
        def successor(snapshot):
            new_holder = self.get("lease", "8da91c08.brian.io", self.args.manager_namespace)["spec"].get("holderIdentity")
            condition = self.condition(self.item(snapshot, "phpas", "web"), "ScaleDownStabilized")
            return (new_holder and new_holder != holder
                    and condition.get("status") == "True"
                    and condition.get("reason") == "ColdStartProtection"
                    and self.source_ready(snapshot))
        after = self.wait("Successor leader establishes cold-start protection", successor)
        protected_replicas = self.requested(after)
        if protected_replicas < 2:
            raise RuntimeError("Successor lost the expanded capacity before protection")
        end = time.monotonic() + 30
        while time.monotonic() < end:
            if self.requested(self.snapshot()) < protected_replicas:
                raise RuntimeError("Scale decreased during observed cold-start protection")
            time.sleep(5)
        self.results.append({"check": "New leader retained capacity during 30s observation",
                             "at": self.now(), "passed": True, "replicas": protected_replicas})
        self.wait("Target can shrink after successor protection expires", lambda s: self.requested(s) == 1)

    def check_outage(self):
        self.source_stopped = True
        self.scale(self.args.prometheus_deployment, 0, self.args.prometheus_namespace)
        self.wait("Prometheus outage sets MetricsReady False", lambda s:
                  self.condition(self.item(s, "phpas", "web"), "MetricsReady").get("status") == "False")
        self.scale("web", 3, WORKLOAD_NS)
        before = self.item(self.snapshot(), "phpas", "web").get("status", {}).get("lastScaleTime")
        end = time.monotonic() + 35
        while time.monotonic() < end:
            snapshot = self.snapshot()
            status = self.item(snapshot, "phpas", "web").get("status", {})
            if self.requested(snapshot) != 3:
                raise RuntimeError("Unavailable source changed Scale")
            if status.get("currentCPUUtilizationPercentage") is not None:
                raise RuntimeError("Unavailable source retained an obsolete CPU display")
            if status.get("lastScaleTime") != before:
                raise RuntimeError("Unavailable source changed lastScaleTime")
            time.sleep(5)
        self.results.append({"check": "Outage holds Scale and clears obsolete CPU for 35s",
                             "at": self.now(), "passed": True})
        self.scale(self.args.prometheus_deployment, 1, self.args.prometheus_namespace)
        self.source_stopped = False
        self.wait("Fresh source recovers metrics and scaling", lambda s:
                  self.source_ready(s) and self.requested(s) == 1, timeout=300)

    def run(self):
        self.guard()
        namespaces = self.get("namespaces", namespace=None)["items"]
        if any(n["metadata"]["name"] == WORKLOAD_NS for n in namespaces):
            raise RuntimeError("Acceptance namespace already exists; refusing to reuse fixtures")
        managers = self.get("deployment", self.args.manager_deployment, self.args.manager_namespace)
        if managers["spec"]["replicas"] != 2 or managers.get("status", {}).get("readyReplicas") != 2:
            raise RuntimeError("Acceptance requires two Ready manager replicas")
        source = self.get("deployment", self.args.prometheus_deployment, self.args.prometheus_namespace)
        if source["spec"]["replicas"] != 1:
            raise RuntimeError("Acceptance requires a dedicated one-replica Prometheus source")
        self.record(self.observations, {"cluster_uid": self.cluster_uid, "manager": managers})
        fixture = Path(__file__).resolve().parents[1] / "config/benchmark/metrics-safety-workloads.yaml"
        self.fixture_created = True
        self.kubectl("apply", "-f", str(fixture))
        self.fixture_uid = self.get("namespace", WORKLOAD_NS, namespace=None)["metadata"]["uid"]
        self.check_isolation()
        self.check_rollout()
        self.check_restart()
        self.check_outage()

    def cleanup(self):
        self.guard()
        if self.source_stopped:
            self.scale(self.args.prometheus_deployment, 1, self.args.prometheus_namespace)
        if self.fixture_created:
            current = self.get("namespace", WORKLOAD_NS, namespace=None)["metadata"]["uid"]
            if self.fixture_uid is None or current != self.fixture_uid:
                raise RuntimeError("Fixture namespace identity is unconfirmed; leaving it for inspection")
            try:
                self.snapshot()
                self.kubectl("logs", "deployment/" + self.args.manager_deployment, "--all-pods=true",
                             "-c", "manager", namespace=self.args.manager_namespace)
            finally:
                self.kubectl("delete", "namespace", WORKLOAD_NS, "--wait=false")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--manager-namespace", default="metrics-safety-manager")
    parser.add_argument("--manager-deployment", default="metrics-safety-predictive-hpa")
    parser.add_argument("--prometheus-namespace", default="metrics-safety")
    parser.add_argument("--prometheus-deployment", default="prometheus")
    args = parser.parse_args()
    if not args.context.startswith("kind-phpa-metrics-safety-"):
        parser.error("context must name a dedicated kind-phpa-metrics-safety-* cluster")
    run = Acceptance(args)
    error = None
    cleanup_error = None
    try:
        run.run()
    except Exception as exc:
        error = str(exc)
    finally:
        try:
            run.cleanup()
        except Exception as exc:
            cleanup_error = str(exc)
        summary = {"finished_at": run.now(), "context": args.context,
                   "cluster_uid": run.cluster_uid, "checks": run.results,
                   "passed": error is None and cleanup_error is None,
                   "error": error, "cleanup_error": cleanup_error}
        (run.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(summary, indent=2))
    raise SystemExit(0 if summary["passed"] else 1)


if __name__ == "__main__":
    main()
