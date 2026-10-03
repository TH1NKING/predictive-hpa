#!/usr/bin/env python3
"""Run the scaffold E2E suite in a new, privately configured, owned Kind cluster."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import uuid

try:
    from .run_metrics_safety import NODE_IMAGE, OWNER_MOUNT, ROOT, Runner
except ImportError:
    from run_metrics_safety import NODE_IMAGE, OWNER_MOUNT, ROOT, Runner


def guarded_command(state_path, tool, arguments):
    """Validate ownership at every kubectl/kind boundary, including nested make."""
    state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    kubeconfig = Path(state["kubeconfig"])
    if hashlib.sha256(kubeconfig.read_bytes()).hexdigest() != state["kubeconfig_sha256"]:
        raise RuntimeError("Private kubeconfig changed; refusing cluster command")
    if tool == "kubectl":
        overrides = ("--kubeconfig", "--context", "--cluster", "--server", "--user", "--token",
                     "--certificate-authority", "--client-certificate", "--client-key")
        if any(arg.split("=", 1)[0] in overrides or arg.startswith("-s") for arg in arguments):
            raise RuntimeError("Cluster identity overrides are forbidden in E2E commands")
    elif tool != "kind" or (len(arguments) != 5 or arguments[:2] != ["load", "docker-image"]
                            or arguments[3:] != ["--name", state["cluster_name"]]):
        raise RuntimeError("Only image loading into the owned Kind cluster is allowed")

    def query(argv):
        result = subprocess.run(argv, check=True, capture_output=True, text=True, timeout=20)
        return json.loads(result.stdout)

    nodes = query([state["docker"], "inspect", state["node_container_id"]])
    if len(nodes) != 1:
        raise RuntimeError("Owned node container is absent")
    node = nodes[0]
    if (node.get("Id") != state["node_container_id"]
            or node.get("Name") != "/" + state["node_name"]
            or node.get("Config", {}).get("Labels", {}).get("io.x-k8s.kind.cluster") != state["cluster_name"]
            or not any(m.get("Destination") == OWNER_MOUNT and m.get("Source") == state["owner"]
                       for m in node.get("Mounts", []))):
        raise RuntimeError("Node ownership changed; refusing cluster command")
    prefix = [state["kubectl"], "--kubeconfig", str(kubeconfig), "--context", state["context"],
              "--request-timeout=20s"]
    cluster = query([*prefix, "get", "namespace", "kube-system", "-o", "json"])
    if cluster.get("metadata", {}).get("uid") != state["cluster_uid"]:
        raise RuntimeError("Cluster identity changed; refusing cluster command")
    return [*prefix, *arguments] if tool == "kubectl" else [state["kind"], *arguments]


class E2ERunner(Runner):
    def __init__(self, args, output):
        super().__init__(args, output)
        self.tools = {}

    def command(self, label, argv, *args, **kwargs):
        # Resolve real executables once, before placing the guarded wrappers on PATH.
        argv = [self.tools.get(str(argv[0]), argv[0]), *argv[1:]]
        environment = dict(kwargs.pop("env", None) or os.environ)
        environment.update(KIND_EXPERIMENTAL_PROVIDER="docker", KUBECONFIG=str(self.kubeconfig))
        kwargs["env"] = environment
        return super().command(label, argv, *args, **kwargs)

    def guarded_environment(self):
        state = {"cluster_name": self.args.cluster_name, "node_name": self.node_name,
                 "context": self.context, "node_container_id": self.node_id,
                 "cluster_uid": self.cluster_uid, "owner": str(self.owner),
                 "kubeconfig": str(self.kubeconfig),
                 "kubeconfig_sha256": hashlib.sha256(self.kubeconfig.read_bytes()).hexdigest(),
                 **self.tools}
        state_path = self.private / "e2e-owner.json"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        wrappers = self.private / "bin"
        wrappers.mkdir()
        for tool in ("kubectl", "kind"):
            wrapper = wrappers / tool
            command = [sys.executable, str(Path(__file__).resolve()), "--guarded-tool", tool, str(state_path)]
            wrapper.write_text("#!/bin/sh\nexec " + shlex.join(command) + ' "$@"\n', encoding="utf-8")
            wrapper.chmod(0o700)
        environment = dict(os.environ)
        # MAKEFLAGS can forward a command-line KUBECTL override to nested make.
        # A child must use the privately generated wrapper instead.
        for name in ("MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES", "GNUMAKEFLAGS"):
            environment.pop(name, None)
        environment.update(PHPA_E2E_STATE=str(state_path), KUBECONFIG=str(self.kubeconfig),
                           KIND_CLUSTER=self.args.cluster_name, KIND=str(wrappers / "kind"),
                           KUBECTL=str(wrappers / "kubectl"),
                           CONTAINER_TOOL=self.tools["docker"],
                           PATH=str(wrappers) + os.pathsep + environment.get("PATH", ""))
        # Default manifests use controller-runtime's self-signed metrics TLS
        # and enable no webhook. Install cert-manager only when explicitly opted in.
        environment.setdefault("CERT_MANAGER_INSTALL_SKIP", "true")
        return environment

    def run(self):
        for name in ("kind", "kubectl", "docker", "go", "make"):
            executable = shutil.which(os.environ.get(name.upper(), name))
            if not executable:
                raise RuntimeError(f"Required E2E executable is unavailable: {name}")
            self.tools[name] = str(Path(executable).resolve())
        clusters = self.command("clusters-before", ["kind", "get", "clusters"]).splitlines()
        if self.args.cluster_name in clusters:
            raise RuntimeError("Dedicated Kind cluster already exists; refusing reuse")
        conflict = self.command("containers-before", ["docker", "ps", "-aq", "--filter", "name=^/" + self.node_name + "$"])
        if conflict.strip():
            raise RuntimeError("Dedicated node container already exists; refusing reuse")
        # make deploy edits its Kustomize image. Run it against a recorded private
        # snapshot so successful and failed E2E runs preserve the user's files.
        self.freeze_source()
        curl_image = self.prepare_curl_image()
        config = {"kind": "Cluster", "apiVersion": "kind.x-k8s.io/v1alpha4", "nodes": [
            {"role": "control-plane", "extraMounts": [{"hostPath": str(self.owner),
                "containerPath": OWNER_MOUNT, "readOnly": True}]}]}
        config_path = self.private / "kind.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        self.created = True
        self.command("kind-create", ["kind", "create", "cluster", "--name", self.args.cluster_name,
            "--image", NODE_IMAGE, "--config", str(config_path), "--retain", "--kubeconfig", str(self.kubeconfig),
            "--wait", "180s"], timeout=300)
        self.guard()
        self.write("ownership.json", {"node_container_id": self.node_id, "cluster_uid": self.cluster_uid,
                                      "node_name": self.node_name, "node_image": NODE_IMAGE})
        nodes = json.loads(self.kubectl("curl-node-platform", "get", "nodes", "-o", "json"))["items"]
        if len(nodes) != 1 or nodes[0]["metadata"]["name"] != self.node_name:
            raise RuntimeError("Curl image import requires the single owned node")
        info = nodes[0].get("status", {}).get("nodeInfo", {})
        architecture = info.get("architecture")
        if info.get("operatingSystem") != "linux" or not isinstance(architecture, str) or not re.fullmatch(r"[a-z0-9_]+", architecture):
            raise RuntimeError("Curl image import requires the owned node's Linux architecture")
        self.load_fixture_image(1, curl_image, "linux/" + architecture)
        self.guard()
        self.command("go-e2e", ["go", "test", "-tags=e2e", "./test/e2e/", "-v", "-count=1",
            "-timeout=25m", "-ginkgo.v"], timeout=1500, env=self.guarded_environment(), cwd=self.source)

    def prepare_curl_image(self):
        image = (self.source / "test/e2e/curl-image.txt").read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"docker\.io/curlimages/curl@sha256:[a-f0-9]{64}", image):
            raise RuntimeError("E2E curl fixture must use its pinned Docker Hub digest")
        try:
            contents = self.command("curl-image-cache", ["docker", "image", "inspect", image])
        except RuntimeError:
            self.command("curl-image-pull", ["docker", "pull", image], timeout=300)
            contents = self.command("curl-image-inspect", ["docker", "image", "inspect", image])
        images = json.loads(contents)
        if len(images) != 1:
            raise RuntimeError("Expected one cached pinned curl image")
        cached = images[0]
        pinned = image.rsplit("@", 1)[1]
        known = {cached.get("Id"), (cached.get("Descriptor") or {}).get("digest")}
        known.update(value.rsplit("@", 1)[-1] for value in cached.get("RepoDigests", []) if isinstance(value, str))
        if pinned not in known:
            raise RuntimeError("Cached curl image does not match the pinned digest")
        self.write("curl-image.json", {"image": image, "docker_image_id": cached["Id"]})
        return image

    def diagnostics(self):
        self.guard()
        for label, arguments in (
            ("final-pods", ["get", "pods", "-A", "-o", "json"]),
            ("final-events", ["get", "events", "-A", "-o", "json"]),
            ("final-manager-logs", ["logs", "-n", "predictive-hpa-system", "deployment/predictive-hpa-controller-manager",
                                    "-c", "manager", "--tail=2000"]),
        ):
            try:
                self.kubectl(label, *arguments, timeout=20)
            except Exception as error:
                # Missing deployment after an early setup failure is diagnostic,
                # while the original failed suite remains the authoritative error.
                self.summary["diagnostic_errors"].append(f"{label}: {error}")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--guarded-tool":
        try:
            command = guarded_command(argv[2], argv[1], argv[3:])
            os.execv(command[0], command)
        except (Exception, KeyboardInterrupt) as error:
            print(f"E2E cluster guard: {error}", file=sys.stderr)
            return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster-name", default="predictive-hpa-test-e2e")
    parser.add_argument("--output", help="fresh evidence directory; defaults to benchmark-runs/e2e-<unique ID>")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    args = parser.parse_args(argv)
    if os.name != "posix":
        parser.error("E2E requires Linux/WSL so timed-out command process groups can be terminated safely")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,43}[a-z0-9])?", args.cluster_name):
        parser.error("cluster name must use lowercase DNS characters and be at most 45 characters")
    if args.timeout_seconds <= 0:
        parser.error("timeout-seconds must be positive")
    output = Path(args.output).resolve() if args.output else ROOT / "benchmark-runs" / ("e2e-" + uuid.uuid4().hex[:12])
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        parser.error("output directory already exists; retain evidence and choose a new path")
    previous = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
    runner = None

    def interrupted(signum, frame):
        if runner is not None and runner.finalizing:
            return
        raise KeyboardInterrupt("Received " + signal.Signals(signum).name)

    for number in previous:
        signal.signal(number, interrupted)
    try:
        runner = E2ERunner(args, output)
        print("E2E evidence: " + str(output), flush=True)
        return runner.finish()
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    raise SystemExit(main())
