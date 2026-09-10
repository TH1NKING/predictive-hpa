#!/usr/bin/env python3
"""Build and run metrics-safety acceptance in a newly owned Kind cluster."""

import argparse
from contextlib import ExitStack
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
from urllib.parse import urlencode


ROOT = Path(__file__).resolve().parents[1]
NODE_IMAGE = "kindest/node:v1.35.0@sha256:452d707d4862f52530247495d180205e029056831160e22870e37e3f6c1ac31f"
OWNER_MOUNT = "/run/phpa-acceptance-owner"
PROXY = "/api/v1/namespaces/metrics-safety/services/prometheus:9090/proxy"


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Runner:
    def __init__(self, args, output):
        self.args = args
        self.output = output
        self.private = Path(tempfile.mkdtemp(prefix="phpa-metrics-safety-"))
        self.kubeconfig = self.private / "cluster.kubeconfig"
        self.owner = self.private / "owner"
        self.owner.write_text(self.private.name, encoding="utf-8")
        self.context = "kind-" + args.cluster_name
        self.node_name = args.cluster_name + "-control-plane"
        self.node_id = None
        self.cluster_uid = None
        self.created = False
        self.sequence = 0
        self.deadline = time.monotonic() + args.timeout_seconds
        self.finalizing = False
        self.summary = {"cluster_name": args.cluster_name, "context": self.context,
                        "started_at": now(), "run_error": None, "diagnostic_errors": [],
                        "cleanup_error": None, "passed": False, "cluster_deleted": False}

    def write(self, name, value):
        (self.output / name).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def process_group_alive(process):
        # poll() also reaps the direct child. Its descendants can keep the
        # original group alive after that child has already exited.
        process.poll()
        if os.name != "posix":
            return process.returncode is None
        try:
            os.killpg(process.pid, 0)
            return True
        except ProcessLookupError:
            return False

    def stop_process_group(self, process, receipt):
        termination = {"process_group_id": process.pid, "grace_seconds": 3,
                       "kill_wait_seconds": 5, "forced_kill": False, "group_stopped": False}
        receipt["termination"] = termination

        def send(force=False):
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
                elif force:
                    process.kill()
                else:
                    process.terminate()
            except ProcessLookupError:
                pass

        send()
        deadline = time.monotonic() + termination["grace_seconds"]
        while self.process_group_alive(process) and time.monotonic() < deadline:
            time.sleep(0.05)
        if self.process_group_alive(process):
            termination["forced_kill"] = True
            send(force=True)
            deadline = time.monotonic() + termination["kill_wait_seconds"]
            while self.process_group_alive(process) and time.monotonic() < deadline:
                time.sleep(0.05)
        process.wait(timeout=5)
        termination["group_stopped"] = not self.process_group_alive(process)
        if not termination["group_stopped"]:
            raise RuntimeError("Owned command process group remained after bounded termination")

    def command(self, label, argv, timeout=45, raw_stdout=False, stdin_path=None):
        if not self.finalizing:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Acceptance run deadline expired")
            timeout = min(timeout, remaining)
        self.sequence += 1
        stem = f"{self.sequence:03d}-{label}"
        receipt = {"at": now(), "command": [str(x) for x in argv], "timeout_seconds": timeout}
        stdout_path, stderr_path = (self.private / (stem + suffix) for suffix in (".stdout", ".stderr"))
        process = None
        try:
            # Files prevent inherited stdout/stderr pipe descriptors from making
            # communicate() wait forever when a verifier leaves descendants.
            with ExitStack() as streams:
                stdout = streams.enter_context(stdout_path.open("w", encoding="utf-8", newline=""))
                stderr = streams.enter_context(stderr_path.open("w", encoding="utf-8", newline=""))
                stdin = streams.enter_context(Path(stdin_path).open("rb")) if stdin_path is not None else None
                if stdin is not None:
                    receipt.update(stdin_file=str(stdin_path), stdin_sha256=hashlib.file_digest(stdin, "sha256").hexdigest(),
                                   stdin_size_bytes=os.fstat(stdin.fileno()).st_size)
                    stdin.seek(0)
                process = subprocess.Popen(argv, cwd=ROOT, stdin=stdin, stdout=stdout, stderr=stderr,
                                           start_new_session=os.name == "posix")
                try:
                    code = process.wait(timeout=timeout)
                except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
                    receipt["error"] = str(error) or "Interrupted"
                    self.stop_process_group(process, receipt)
                    raise
                if self.process_group_alive(process):
                    self.stop_process_group(process, receipt)
                    raise RuntimeError(f"{label} left descendants after its direct process exited")
            if code:
                raise RuntimeError(f"{label} failed with exit {code}: {stderr_path.read_text(encoding='utf-8', errors='replace').strip()}")
            return stdout_path.read_bytes() if raw_stdout else stdout_path.read_text(encoding="utf-8", errors="replace")
        except (subprocess.TimeoutExpired, OSError, RuntimeError) as error:
            receipt["error"] = str(error)
            raise
        except KeyboardInterrupt as error:
            receipt["error"] = str(error) or "Interrupted"
            raise
        finally:
            receipt["returncode"] = process.returncode if process is not None else None
            for name, path in (("stdout", stdout_path), ("stderr", stderr_path)):
                receipt[name] = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
            self.write(stem + ".json", receipt)

    def kubectl(self, label, *argv, timeout=45):
        return self.command(label, ["kubectl", "--kubeconfig", str(self.kubeconfig),
                                   "--context", self.context, *argv], timeout)

    def helm(self, label, *argv, timeout=240):
        return self.command(label, ["helm", "--kubeconfig", str(self.kubeconfig),
                                   "--kube-context", self.context, *argv], timeout)

    def image_content(self, label, content_digest, expected_size=None):
        raw = self.command(label, ["docker", "exec", self.node_id, "ctr", "-n", "k8s.io",
                                   "content", "get", content_digest], raw_stdout=True)
        # Keep exact bytes: JSON reserialization or newline conversion changes
        # a content digest even when the parsed document appears equivalent.
        (self.output / (label + ".json")).write_bytes(raw)
        if len(raw) > 8 * 1024 * 1024 or (expected_size is not None and len(raw) != expected_size):
            raise RuntimeError("Loaded image content size does not match its descriptor")
        if "sha256:" + hashlib.sha256(raw).hexdigest() != content_digest:
            raise RuntimeError("Loaded image content SHA256 does not match its digest")
        document = json.loads(raw)
        if not isinstance(document, dict):
            raise RuntimeError("Loaded image content is not a JSON object")
        return document

    def verify_image_identity(self, image_id, runtime):
        config_id = runtime["status"]["id"]
        if any(not isinstance(value, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", value)
               for value in (image_id, config_id)):
            raise RuntimeError("Loaded manager image has an invalid Docker or CRI digest")
        accepted_ids, manifest_ids = {config_id}, []
        if image_id != config_id:
            # The build ID may identify a manifest while CRI identifies its
            # config. Prove that link from content, not an arbitrary repoDigest
            # alias or a mutable image tag.
            manifest = self.image_content("image-manifest", image_id)
            if (type(manifest.get("schemaVersion")) is not int or manifest["schemaVersion"] != 2
                    or manifest.get("mediaType") not in {
                    "application/vnd.oci.image.manifest.v1+json",
                    "application/vnd.docker.distribution.manifest.v2+json"}):
                raise RuntimeError("Loaded image is not a supported version 2 image manifest")
            config = manifest.get("config")
            if (not isinstance(config, dict) or config.get("digest") != config_id
                    or config.get("mediaType") not in {"application/vnd.oci.image.config.v1+json",
                                                       "application/vnd.docker.container.image.v1+json"}
                    or type(config.get("size")) is not int or not 0 < config["size"] <= 8 * 1024 * 1024):
                raise RuntimeError("Loaded manager image config does not match the built Docker manifest")
            self.image_content("image-config", config_id, config["size"])
            accepted_ids.add(image_id)
            manifest_ids.append(image_id)
        self.write("image-identity.json", {"docker_image_id": image_id, "runtime_config_digest": config_id,
                                           "verified_manifest_digests": manifest_ids,
                                           "accepted_pod_image_ids": sorted(accepted_ids)})
        return accepted_ids

    def cached_fixture_images(self):
        requested = list(dict.fromkeys(self.args.preload_fixture_image))
        if not requested:
            return []
        allowed = set()
        for name in ("metrics-safety-monitoring.yaml", "metrics-safety-workloads.yaml"):
            contents = (self.source / "config/benchmark" / name).read_text(encoding="utf-8")
            allowed.update(ref.strip("\"'") for ref in re.findall(r"^\s*image:\s*([^\s#]+)", contents, re.MULTILINE))
        if any(ref not in allowed or not re.fullmatch(r"[^@\s]+@sha256:[a-f0-9]{64}", ref) for ref in requested):
            raise RuntimeError("Preload requires an exact pinned image reference declared by the frozen acceptance fixtures")
        records = []
        for index, ref in enumerate(requested, 1):
            images = json.loads(self.command(f"fixture-cache-{index}", ["docker", "image", "inspect", ref]))
            if not isinstance(images, list) or len(images) != 1:
                raise RuntimeError("Expected one cached fixture image for the pinned reference")
            cached = images[0]
            pinned = ref.rsplit("@", 1)[1]
            known = {cached.get("Id"), (cached.get("Descriptor") or {}).get("digest")}
            known.update(value.rsplit("@", 1)[-1] for value in cached.get("RepoDigests", []) if isinstance(value, str))
            if pinned not in known:
                raise RuntimeError("Cached fixture image does not match its pinned digest")
            records.append({"image": ref, "pinned_digest": pinned, "docker_image_id": cached["Id"]})
        self.write("fixture-images.json", {"images": records})
        return requested

    def load_fixture_image(self, index, ref, platform):
        archive = self.private / f"fixture-{index}.tar"
        self.command(f"fixture-save-{index}", ["docker", "image", "save", "--output", str(archive), ref], timeout=180)
        archive_hash, archive_size = digest(archive), archive.stat().st_size
        repository = ref.rsplit("@", 1)[0]
        if "/" not in repository:
            repository = "docker.io/library/" + repository
        elif not any(character in repository.split("/", 1)[0] for character in (".", ":")) and not repository.startswith("localhost/"):
            repository = "docker.io/" + repository
        # A Docker cache may contain only the host platform of a multi-platform
        # index. Preserve that index and import the actual node's platform;
        # Kind's all-platform import would require unrelated cached platforms.
        # Stream the private archive directly, avoiding an intermediate
        # container path that the import process might not see.
        self.command(f"fixture-import-{index}", ["docker", "exec", "-i", self.node_id, "ctr", "-n", "k8s.io",
                                                "images", "import", "--local", "--platform", platform,
                                                "--digests", "--base-name", repository, "-"], timeout=180, stdin_path=archive)
        runtime = json.loads(self.command(f"fixture-runtime-{index}", ["docker", "exec", self.node_id,
                                                                      "crictl", "inspecti", ref]))
        runtime_id = runtime.get("status", {}).get("id")
        if not isinstance(runtime_id, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", runtime_id):
            raise RuntimeError("Imported fixture reference has no valid CRI image identity")
        self.write(f"fixture-import-{index}.json", {"image": ref, "platform": platform,
                                                   "archive_sha256": archive_hash, "archive_size_bytes": archive_size,
                                                   "runtime_config_digest": runtime_id, "node_container_id": self.node_id})

    def inspect_node(self):
        nodes = json.loads(self.command("node-identity", ["docker", "inspect", self.node_name]))
        if len(nodes) != 1:
            raise RuntimeError("Expected exactly one dedicated node container")
        node = nodes[0]
        if (node["Config"].get("Labels", {}).get("io.x-k8s.kind.cluster") != self.args.cluster_name
                or node.get("Name") != "/" + self.node_name
                or not any(m.get("Destination") == OWNER_MOUNT
                           and Path(m.get("Source", "")).resolve() == self.owner.resolve()
                           for m in node.get("Mounts", []))):
            raise RuntimeError("Node ownership marker does not match; refusing mutations")
        if self.node_id is not None and node["Id"] != self.node_id:
            raise RuntimeError("Node identity changed; refusing mutations")
        self.node_id = node["Id"]
        return node

    def guard(self):
        self.inspect_node()
        uid = json.loads(self.kubectl("cluster-identity", "get", "namespace", "kube-system", "-o", "json"))["metadata"]["uid"]
        if self.cluster_uid is not None and uid != self.cluster_uid:
            raise RuntimeError("Cluster identity changed; refusing mutations")
        self.cluster_uid = uid

    def freeze_source(self):
        head = self.command("git-head", ["git", "rev-parse", "HEAD"]).strip()
        status = self.command("git-status", ["git", "status", "--porcelain=v1"])
        names = self.command("git-files", ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"])
        source = self.private / "source"
        source.mkdir()
        files = {}
        for name in sorted(set(names.split("\0")) - {""}):
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError("Source file escapes the repository")
            path = ROOT / relative
            if path.is_symlink() or not path.resolve().is_relative_to(ROOT.resolve()):
                raise RuntimeError("Source snapshot does not accept symlinks or paths outside the repository")
            if not path.is_file():
                continue
            destination = source / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(path.read_bytes())
            files[name] = digest(destination)
        for required in ("Dockerfile", ".dockerignore", "go.mod", "go.sum", "cmd/main.go"):
            if required not in files:
                raise RuntimeError(f"Source snapshot is missing {required}")
        source_hash = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        self.write("source.json", {"head": head, "git_status": status, "files": files,
                                   "snapshot_sha256": source_hash,
                                   "scope": "Existing tracked and nonignored untracked repository files"})
        self.source = source
        self.source_files = files
        self.image = "metrics-safety/predictive-hpa:source-" + source_hash[:20]

    def run(self):
        clusters = self.command("clusters-before", ["kind", "get", "clusters"]).splitlines()
        if self.args.cluster_name in clusters:
            raise RuntimeError("Dedicated Kind cluster already exists; refusing reuse")
        conflict = self.command("containers-before", ["docker", "ps", "-aq", "--filter", "name=^/" + self.node_name + "$"])
        if conflict.strip():
            raise RuntimeError("Dedicated node container already exists; refusing reuse")
        for label, command in (("kind-version", ["kind", "version"]),
                               ("helm-version", ["helm", "version", "--short"]),
                               ("docker-version", ["docker", "version"]),
                               ("kubectl-version", ["kubectl", "version", "--client", "-o", "json"])):
            self.command(label, command)
        self.freeze_source()
        fixture_images = self.cached_fixture_images()
        iidfile = self.private / "image-id"
        self.command("docker-build", ["docker", "build", "--provenance=false", "--file", str(self.source / "Dockerfile"),
                                       "--iidfile", str(iidfile), "--tag", self.image, str(self.source)], timeout=1200)
        image_id = iidfile.read_text().strip()
        image = json.loads(self.command("docker-image", ["docker", "image", "inspect", self.image]))[0]
        if image["Id"] != image_id:
            raise RuntimeError("Built Docker image identity changed")
        self.write("image.json", {"image": self.image, "docker_image_id": image_id,
                                  "dockerfile_sha256": digest(self.source / "Dockerfile")})
        config = {"kind": "Cluster", "apiVersion": "kind.x-k8s.io/v1alpha4", "nodes": [
            {"role": "control-plane", "extraMounts": [{"hostPath": str(self.owner),
                                                        "containerPath": OWNER_MOUNT, "readOnly": True}]}]}
        config_path = self.private / "kind.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        self.created = True
        self.command("kind-create", ["kind", "create", "cluster", "--name", self.args.cluster_name,
                                      "--image", NODE_IMAGE, "--config", str(config_path), "--retain",
                                      "--kubeconfig", str(self.kubeconfig), "--wait", "180s"], timeout=300)
        self.guard()
        self.write("ownership.json", {"node_container_id": self.node_id, "cluster_uid": self.cluster_uid,
                                      "node_name": self.node_name, "node_image": NODE_IMAGE})
        nodes = json.loads(self.kubectl("nodes-ready", "get", "nodes", "-o", "json"))["items"]
        if [n["metadata"]["name"] for n in nodes] != [self.node_name]:
            raise RuntimeError("Unexpected node roster in dedicated cluster")
        if fixture_images:
            info = nodes[0].get("status", {}).get("nodeInfo", {})
            architecture = info.get("architecture")
            if (info.get("operatingSystem") != "linux" or not isinstance(architecture, str)
                    or not re.fullmatch(r"[a-z0-9_]+", architecture)):
                raise RuntimeError("Fixture import requires the dedicated node's Linux OS and architecture")
            fixture_platform = "linux/" + architecture
        self.command("kind-load", ["kind", "load", "docker-image", self.image, "--name", self.args.cluster_name], timeout=180)
        runtime = json.loads(self.command("runtime-image", ["docker", "exec", self.node_name, "crictl", "inspecti", self.image]))
        runtime_ids = self.verify_image_identity(image_id, runtime)
        self.guard()
        for index, ref in enumerate(fixture_images, 1):
            self.load_fixture_image(index, ref, fixture_platform)
            self.guard()
        self.kubectl("monitoring-apply", "apply", "-f", str(self.source / "config/benchmark/metrics-safety-monitoring.yaml"))
        for deployment in ("prometheus", "kube-state-metrics"):
            self.kubectl(deployment + "-ready", "-n", "metrics-safety", "rollout", "status",
                         "deployment/" + deployment, "--timeout=180s", timeout=200)
        self.monitoring_ready()
        chart = str(self.source / "deploy/charts/predictive-hpa")
        values = ["--set", "replicaCount=2", "--set", "image.repository=metrics-safety/predictive-hpa",
                  "--set-string", "image.tag=" + self.image.rsplit(":", 1)[1],
                  "--set", "image.pullPolicy=Never", "--set-string",
                  "prometheus.url=http://prometheus.metrics-safety.svc:9090"]
        self.helm("helm-lint", "lint", chart, *values)
        self.helm("helm-template", "template", "metrics-safety", chart, "--namespace", "metrics-safety-manager", *values)
        self.guard()
        self.helm("helm-install", "install", "metrics-safety", chart, "--namespace", "metrics-safety-manager",
                  "--create-namespace", "--wait", "--timeout", "180s", *values)
        self.kubectl("managers-ready", "-n", "metrics-safety-manager", "rollout", "status",
                     "deployment/metrics-safety-predictive-hpa", "--timeout=180s", timeout=200)
        pods = json.loads(self.kubectl("manager-images", "-n", "metrics-safety-manager", "get", "pods",
                                     "-l", "app.kubernetes.io/instance=metrics-safety", "-o", "json"))["items"]
        statuses = [c for p in pods for c in p.get("status", {}).get("containerStatuses", []) if c["name"] == "manager"]
        if len(statuses) != 2 or any(not c.get("ready") or c["imageID"].split("://")[-1].split("@")[-1] not in runtime_ids for c in statuses):
            raise RuntimeError("Two Ready manager Pods must run the verified Dockerfile image")
        self.command("acceptance", [sys.executable, str(self.source / "hack/verify_metrics_safety.py"),
                                    "--context", self.context, "--kubeconfig", str(self.kubeconfig),
                                    "--output", str(self.output / "acceptance")], timeout=1800)
        accepted = json.loads((self.output / "acceptance/summary.json").read_text(encoding="utf-8"))
        if not accepted.get("passed") or len(accepted.get("checks", [])) != 11 or not all(c.get("passed") for c in accepted["checks"]):
            raise RuntimeError("Acceptance did not report all 11 successful checks")
        if any(digest(self.source / name) != value for name, value in self.source_files.items()):
            raise RuntimeError("Frozen source changed during the run")

    def monitoring_ready(self):
        deadline = time.monotonic() + 150
        while True:
            targets = json.loads(self.kubectl("prometheus-targets", "get", "--raw", PROXY + "/api/v1/targets"))
            active = targets.get("data", {}).get("activeTargets", [])
            if ({t.get("labels", {}).get("job") for t in active} == {"kubernetes-nodes-cadvisor", "kube-state-metrics"}
                    and len(active) == 2 and all(t.get("health") == "up" for t in active)):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Prometheus scrape targets did not become healthy")
            time.sleep(5)
        for name, query in (("cadvisor", 'container_cpu_usage_seconds_total{namespace="metrics-safety",container!="",container!="POD"}'),
                            ("pod-owner", 'kube_pod_owner{namespace="metrics-safety"}'),
                            ("replicaset-owner", 'kube_replicaset_owner{namespace="metrics-safety"}'),
                            ("cpu-requests", 'kube_pod_container_resource_requests{namespace="metrics-safety",resource="cpu"}')):
            while True:
                response = json.loads(self.kubectl(name, "get", "--raw", PROXY + "/api/v1/query?" + urlencode({"query": query})))
                if response.get("status") == "success" and response.get("data", {}).get("result"):
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("Monitoring source is missing " + name)
                time.sleep(5)

    def diagnostics(self):
        self.guard()
        commands = [
            ("final-pods", ["get", "pods", "-A", "-o", "json"]),
            ("final-deployments", ["get", "deployments", "-A", "-o", "json"]),
            ("final-phpas", ["get", "phpa", "-A", "-o", "json"]),
            ("final-events", ["get", "events", "-A", "-o", "json"]),
            ("final-leases", ["-n", "metrics-safety-manager", "get", "leases", "-o", "json"]),
            ("final-manager-logs", ["-n", "metrics-safety-manager", "logs", "deployment/metrics-safety-predictive-hpa",
                                    "--all-pods=true", "-c", "manager"]),
        ]
        commands += [("final-prometheus-" + name, ["get", "--raw", PROXY + "/api/v1/" + route])
                     for name, route in (("targets", "targets"), ("buildinfo", "status/buildinfo"), ("config", "status/config"))]
        for label, argv in commands:
            try:
                self.kubectl(label, *argv)
            except Exception as error:
                self.summary["diagnostic_errors"].append(f"{label}: {error}")

    def cleanup(self):
        remaining = self.command("containers-cleanup", ["docker", "ps", "-aq", "--filter", "name=^/" + self.node_name + "$"])
        if not remaining.strip() and self.node_id is None:
            clusters = self.command("clusters-cleanup", ["kind", "get", "clusters"]).splitlines()
            if self.args.cluster_name in clusters:
                raise RuntimeError("Unexpected dedicated cluster resources; refusing cleanup without ownership")
            self.summary["cleanup_not_needed"] = True
            return
        self.inspect_node()
        if self.cluster_uid is not None:
            try:
                current = json.loads(self.kubectl("cleanup-cluster-identity", "get", "namespace", "kube-system", "-o", "json"))["metadata"]["uid"]
            except Exception as error:
                self.summary["cleanup_identity_fallback"] = "Owned node container; Kubernetes API unavailable: " + str(error)
            else:
                if current != self.cluster_uid:
                    raise RuntimeError("Cluster identity changed; refusing cleanup")
        self.command("kind-delete", ["kind", "delete", "cluster", "--name", self.args.cluster_name,
                                      "--kubeconfig", str(self.kubeconfig)], timeout=120)
        clusters = self.command("clusters-after", ["kind", "get", "clusters"]).splitlines()
        remaining = self.command("containers-after", ["docker", "ps", "-aq", "--filter", "name=^/" + self.node_name + "$"])
        if self.args.cluster_name in clusters or remaining.strip():
            raise RuntimeError("Dedicated Kind cluster remained after cleanup")
        self.summary["cluster_deleted"] = True

    def finish(self):
        try:
            self.run()
        except (Exception, KeyboardInterrupt) as error:
            self.summary["run_error"] = str(error) or type(error).__name__
        finally:
            self.finalizing = True
            if self.created:
                try:
                    self.diagnostics()
                except Exception as error:
                    self.summary["diagnostic_errors"].append(str(error))
                try:
                    self.cleanup()
                except Exception as error:
                    self.summary["cleanup_error"] = str(error)
            self.summary.update(finished_at=now(), node_container_id=self.node_id, cluster_uid=self.cluster_uid)
            if not self.summary["cleanup_error"]:
                try:
                    shutil.rmtree(self.private)
                except OSError as error:
                    self.summary["cleanup_error"] = "Could not remove private run files: " + str(error)
            if self.summary["cleanup_error"]:
                self.summary["private_workspace"] = str(self.private)
            self.summary["passed"] = not (self.summary["run_error"] or self.summary["diagnostic_errors"] or self.summary["cleanup_error"])
            self.write("summary.json", self.summary)
            self.write("artifact-manifest.json", {"files": {p.relative_to(self.output).as_posix(): digest(p)
                                                           for p in sorted(self.output.rglob("*")) if p.is_file()}})
        print(json.dumps(self.summary, indent=2))
        return 0 if self.summary["passed"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster-name", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--preload-fixture-image", action="append", default=[],
                        help="load an already cached, exact fixture digest into Kind before deployment (repeatable)")
    parser.add_argument("--timeout-seconds", type=int, default=2700,
                        help="total run budget before bounded diagnostics and cleanup (default: 2700)")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"phpa-metrics-safety-[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", args.cluster_name):
        parser.error("cluster name must start with phpa-metrics-safety- and use lowercase DNS characters")
    if len(args.cluster_name) > 45:
        parser.error("cluster name must be at most 45 characters")
    if args.timeout_seconds <= 0:
        parser.error("timeout-seconds must be positive")
    output = Path(args.output).resolve()
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        parser.error("output directory already exists; retain failed evidence and choose a new path")
    previous = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
    runner = None

    def interrupted(signum, frame):
        # A second signal must not interrupt bounded diagnostics, resource
        # cleanup, or the final summary after the run has already stopped.
        if runner is not None and runner.finalizing:
            return
        raise KeyboardInterrupt("Received " + signal.Signals(signum).name)

    for number in previous:
        signal.signal(number, interrupted)
    try:
        runner = Runner(args, output)
        return runner.finish()
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    raise SystemExit(main())
