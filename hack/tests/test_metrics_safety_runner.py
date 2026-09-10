"""Public acceptance CLI behavior with the external command boundary simulated."""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("metrics_safety_runner", ROOT / "hack/run_metrics_safety.py")
REAL_RUN = subprocess.run
REAL_POPEN = subprocess.Popen


def command_processes(environment):
    """Expose the external command fixture through Popen's process boundary."""
    class CompletedCommand:
        pid = 2147483647

        def __init__(self, result):
            self.returncode = result.returncode

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

    def launch(argv, **kwargs):
        if argv[0] == "git":
            return REAL_POPEN(argv, **kwargs)
        result = environment(argv)
        kwargs["stdout"].write(result.stdout)
        kwargs["stderr"].write(result.stderr)
        return CompletedCommand(result)

    return launch


class CommandEnvironment:
    """A single external Kind/Docker/Kubernetes environment, not runner internals."""

    def __init__(self):
        self.cluster = None
        self.deleted = False
        self.commands = []
        self.image_id = "sha256:" + "1" * 64

    def __call__(self, argv, **kwargs):
        self.commands.append(argv)
        result = ""
        if argv[0] == "git":
            return REAL_RUN(argv, **kwargs)
        if argv[:3] == ["kind", "get", "clusters"]:
            result = "phpa-metrics-safety-test\n" if self.cluster else ""
        elif argv[:2] == ["docker", "build"]:
            Path(argv[argv.index("--iidfile") + 1]).write_text(self.image_id)
        elif argv[:3] == ["kind", "create", "cluster"]:
            config = json.loads(Path(argv[argv.index("--config") + 1]).read_text())
            self.cluster = {"Id": "node-original", "Name": "/phpa-metrics-safety-test-control-plane",
                            "Config": {"Labels": {"io.x-k8s.kind.cluster": "phpa-metrics-safety-test"}},
                            "Mounts": [{"Source": config["nodes"][0]["extraMounts"][0]["hostPath"],
                                        "Destination": "/run/phpa-acceptance-owner"}]}
            Path(argv[argv.index("--kubeconfig") + 1]).write_text("PRIVATE_KUBECONFIG_CANARY")
        elif argv[:3] == ["kind", "delete", "cluster"]:
            self.cluster = None
            self.deleted = True
        elif argv[:2] == ["docker", "inspect"]:
            result = json.dumps([self.cluster] if self.cluster else [])
        elif argv[:3] == ["docker", "ps", "-aq"]:
            result = self.cluster["Id"] if self.cluster else ""
        elif argv[:3] == ["docker", "image", "inspect"]:
            result = json.dumps([{"Id": self.image_id}])
        elif argv[0] == "docker" and "inspecti" in argv:
            result = json.dumps({"status": {"id": self.image_id, "repoDigests": []}})
        elif argv[0] == "kubectl":
            if "--raw" in argv:
                target = argv[argv.index("--raw") + 1]
                if "targets" in target:
                    result = json.dumps({"status": "success", "data": {"activeTargets": [
                        {"labels": {"job": "kubernetes-nodes-cadvisor"}, "health": "up"},
                        {"labels": {"job": "kube-state-metrics"}, "health": "up"}]}})
                else:
                    result = json.dumps({"status": "success", "data": {"result": [{"value": [0, "1"]}]}})
            elif "kube-system" in argv:
                result = json.dumps({"metadata": {"uid": "cluster-original"}})
            elif "nodes" in argv:
                result = json.dumps({"items": [{"metadata": {"name": "phpa-metrics-safety-test-control-plane"}}]})
            elif "pods" in argv:
                result = json.dumps({"items": [{"metadata": {"name": "manager-" + str(i)},
                    "spec": {"containers": [{"name": "manager", "image": "placeholder"}]},
                    "status": {"containerStatuses": [{"name": "manager", "ready": True,
                                                         "imageID": self.image_id}]}} for i in range(2)]})
            else:
                result = json.dumps({"items": []})
        elif len(argv) > 1 and argv[1].endswith("verify_metrics_safety.py"):
            target = Path(argv[argv.index("--output") + 1])
            target.mkdir()
            (target / "summary.json").write_text(json.dumps({"passed": True, "checks": [
                {"passed": True} for _ in range(11)]}))
        return subprocess.CompletedProcess(argv, 0, result, "")


class ManifestImageEnvironment(CommandEnvironment):
    """Docker reports a manifest, while the external CRI reports its config ID."""

    def __init__(self, media_type="application/vnd.oci.image.manifest.v1+json"):
        super().__init__()
        config_type = ("application/vnd.oci.image.config.v1+json" if "oci" in media_type
                       else "application/vnd.docker.container.image.v1+json")
        self.config_blob = json.dumps({"architecture": "amd64", "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": []}}, indent=2).replace("\n", "\r\n") + "\r\n"
        self.runtime_id = "sha256:" + hashlib.sha256(self.config_blob.encode()).hexdigest()
        self.manifest = {"schemaVersion": 2, "mediaType": media_type, "config": {
            "mediaType": config_type, "digest": self.runtime_id, "size": len(self.config_blob.encode())}, "layers": []}
        self.manifest_blob = json.dumps(self.manifest, indent=2).replace("\n", "\r\n") + "\r\n"
        self.image_id = "sha256:" + hashlib.sha256(self.manifest_blob.encode()).hexdigest()
        self.blobs = {self.image_id: self.manifest_blob, self.runtime_id: self.config_blob}
        self.repo_digests = []
        self.pod_ids = [self.image_id, self.runtime_id]

    def __call__(self, argv, **kwargs):
        result = super().__call__(argv, **kwargs)
        if argv[:3] == ["docker", "image", "inspect"]:
            result.stdout = json.dumps([{"Id": self.image_id, "Descriptor": {
                "digest": self.image_id, "mediaType": self.manifest["mediaType"],
                "annotations": {"config.digest": self.runtime_id}}}])
        elif argv[0] == "docker" and "inspecti" in argv:
            result.stdout = json.dumps({"status": {"id": self.runtime_id, "repoDigests": self.repo_digests}})
        elif argv[0] == "docker" and "content" in argv and "get" in argv:
            if argv[-1] not in self.blobs:
                result.returncode, result.stderr = 1, "Content digest was not found"
            else:
                result.stdout = self.blobs[argv[-1]]
        elif argv[0] == "kubectl" and "pods" in argv and "-A" not in argv:
            pods = json.loads(result.stdout)
            for pod, image_id in zip(pods["items"], self.pod_ids):
                pod["status"]["containerStatuses"][0]["imageID"] = image_id
            result.stdout = json.dumps(pods)
        return result


class MetricsSafetyRunnerTests(unittest.TestCase):
    def test_success_builds_current_dockerfile_records_evidence_and_deletes_owned_cluster(self):
        module = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(module)
        environment = CommandEnvironment()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence"
            with patch.object(subprocess, "Popen", side_effect=command_processes(environment)), contextlib.redirect_stdout(io.StringIO()):
                code = module.main(["--cluster-name", "phpa-metrics-safety-test", "--output", str(output)])
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(0, code, summary)
            self.assertTrue(summary["passed"])
            self.assertTrue(environment.deleted)
            proof = json.loads((output / "image-identity.json").read_text())
            self.assertEqual(environment.image_id, proof["runtime_config_digest"])
            self.assertEqual([], proof["verified_manifest_digests"])
            self.assertEqual([environment.image_id], proof["accepted_pod_image_ids"])
            manifest = json.loads((output / "artifact-manifest.json").read_text())
            self.assertIn("source.json", manifest["files"])
            for path in output.rglob("*"):
                if path.is_file():
                    self.assertNotIn("PRIVATE_KUBECONFIG_CANARY", path.read_text())
            source = json.loads((output / "source.json").read_text())
            self.assertIn("Dockerfile", source["files"])
            builds = [c for c in environment.commands if c[:2] == ["docker", "build"]]
            self.assertEqual(1, len(builds))
            self.assertIn("--file", builds[0])

    def test_manifest_and_cri_config_are_linked_even_when_repo_digests_are_empty(self):
        module = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(module)
        environment = ManifestImageEnvironment()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence"
            with patch.object(subprocess, "Popen", side_effect=command_processes(environment)), contextlib.redirect_stdout(io.StringIO()):
                code = module.main(["--cluster-name", "phpa-metrics-safety-test", "--output", str(output)])
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(0, code, summary)
            self.assertTrue(summary["passed"])
            self.assertTrue(environment.deleted)
            proof = json.loads((output / "image-identity.json").read_text())
            self.assertEqual(environment.image_id, proof["docker_image_id"])
            self.assertEqual(environment.runtime_id, proof["runtime_config_digest"])
            self.assertEqual([environment.image_id], proof["verified_manifest_digests"])
            self.assertEqual({environment.image_id, environment.runtime_id}, set(proof["accepted_pod_image_ids"]))
            self.assertEqual(environment.manifest_blob.encode(), (output / "image-manifest.json").read_bytes())
            self.assertEqual(environment.config_blob.encode(), (output / "image-config.json").read_bytes())
            reads = [argv for argv in environment.commands if argv[0] == "docker" and "content" in argv and "get" in argv]
            self.assertEqual({environment.image_id, environment.runtime_id}, {argv[-1] for argv in reads})
            self.assertTrue(all(argv[2] == "node-original" for argv in reads))

    def test_existing_cluster_is_rejected_and_never_deleted(self):
        module = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(module)
        commands = []

        def command(argv, **kwargs):
            commands.append(argv)
            output = "phpa-metrics-safety-test\n" if argv[:3] == ["kind", "get", "clusters"] else ""
            return subprocess.CompletedProcess(argv, 0, output, "")

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence"
            with patch.object(subprocess, "Popen", side_effect=command_processes(command)), contextlib.redirect_stdout(io.StringIO()):
                code = module.main(["--cluster-name", "phpa-metrics-safety-test", "--output", str(output)])
            summary = json.loads((output / "summary.json").read_text())
        self.assertEqual(1, code)
        self.assertIn("already exists", summary["run_error"])
        self.assertFalse(any(c[:3] == ["kind", "delete", "cluster"] for c in commands))

    def test_containerd_config_and_docker_manifest_ids_can_differ_when_digest_matches(self):
        module = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(module)
        environment = ManifestImageEnvironment("application/vnd.docker.distribution.manifest.v2+json")
        environment.repo_digests = ["docker.io/metrics-safety/predictive-hpa@" + environment.image_id]

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence"
            with patch.object(subprocess, "Popen", side_effect=command_processes(environment)), contextlib.redirect_stdout(io.StringIO()):
                code = module.main(["--cluster-name", "phpa-metrics-safety-test", "--output", str(output)])
            summary = json.loads((output / "summary.json").read_text())
        self.assertEqual(0, code, summary)
        self.assertTrue(environment.deleted)

    def test_unverified_image_content_is_rejected_before_helm_and_owned_cluster_is_deleted(self):
        for scenario in ("forged-manifest", "malformed-manifest", "config-mismatch", "forged-config",
                         "config-size-mismatch", "unsupported-media-type", "annotation-without-manifest"):
            with self.subTest(scenario=scenario):
                module = importlib.util.module_from_spec(SPEC)
                SPEC.loader.exec_module(module)
                environment = ManifestImageEnvironment()
                if scenario == "forged-manifest":
                    environment.blobs[environment.image_id] += " "
                elif scenario == "forged-config":
                    environment.blobs[environment.runtime_id] += " "
                elif scenario == "annotation-without-manifest":
                    del environment.blobs[environment.image_id]
                else:
                    if scenario == "config-mismatch":
                        environment.manifest["config"]["digest"] = "sha256:" + "2" * 64
                    elif scenario == "config-size-mismatch":
                        environment.manifest["config"]["size"] += 1
                    elif scenario == "unsupported-media-type":
                        environment.manifest["mediaType"] = "application/vnd.oci.image.index.v1+json"
                    blob = "{not-json" if scenario == "malformed-manifest" else json.dumps(environment.manifest)
                    environment.image_id = "sha256:" + hashlib.sha256(blob.encode()).hexdigest()
                    environment.blobs[environment.image_id] = blob
                with tempfile.TemporaryDirectory() as directory:
                    output = Path(directory) / "evidence"
                    with patch.object(subprocess, "Popen", side_effect=command_processes(environment)), contextlib.redirect_stdout(io.StringIO()):
                        code = module.main(["--cluster-name", "phpa-metrics-safety-test", "--output", str(output)])
                    summary = json.loads((output / "summary.json").read_text())
                    self.assertEqual(1, code, summary)
                    self.assertIsNotNone(summary["run_error"])
                    self.assertTrue(summary["cluster_deleted"])
                    self.assertTrue(environment.deleted)
                    self.assertIsNone(summary["cleanup_error"])
                    self.assertTrue((output / "artifact-manifest.json").is_file())
                    self.assertFalse(any(argv[0] == "helm" and any(action in argv for action in ("lint", "template", "install"))
                        for argv in environment.commands))

    def test_unrelated_cri_repo_digest_is_not_an_accepted_manager_pod_identity(self):
        module = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(module)
        environment = CommandEnvironment()
        unrelated = "sha256:" + "9" * 64

        def command(argv, **kwargs):
            result = environment(argv, **kwargs)
            if argv[0] == "docker" and "inspecti" in argv:
                result.stdout = json.dumps({"status": {"id": environment.image_id,
                    "repoDigests": ["docker.io/other/image@" + unrelated]}})
            elif argv[0] == "kubectl" and "pods" in argv and "-A" not in argv:
                pods = json.loads(result.stdout)
                pods["items"][0]["status"]["containerStatuses"][0]["imageID"] = unrelated
                result.stdout = json.dumps(pods)
            return result

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence"
            with patch.object(subprocess, "Popen", side_effect=command_processes(command)), contextlib.redirect_stdout(io.StringIO()):
                code = module.main(["--cluster-name", "phpa-metrics-safety-test", "--output", str(output)])
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(1, code, summary)
            self.assertTrue(summary["cluster_deleted"])
            self.assertTrue(environment.deleted)
            proof = json.loads((output / "image-identity.json").read_text())
            self.assertNotIn(unrelated, proof["accepted_pod_image_ids"])

    def test_failure_before_node_creation_preserves_error_without_inventing_cleanup_failure(self):
        module = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(module)
        environment = CommandEnvironment()

        def command(argv, **kwargs):
            if argv[:3] == ["kind", "create", "cluster"]:
                return subprocess.CompletedProcess(argv, 1, "", "simulated node image pull failure")
            return environment(argv, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence"
            with patch.object(subprocess, "Popen", side_effect=command_processes(command)), contextlib.redirect_stdout(io.StringIO()):
                code = module.main(["--cluster-name", "phpa-metrics-safety-test", "--output", str(output)])
            summary = json.loads((output / "summary.json").read_text())
        self.assertEqual(1, code)
        self.assertIn("node image pull failure", summary["run_error"])
        self.assertIsNone(summary["cleanup_error"])
        self.assertFalse(environment.deleted)

    def test_changed_kubernetes_identity_prevents_deletion_and_retains_private_recovery_state(self):
        module = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(module)
        environment = CommandEnvironment()
        acceptance_ran = False

        def command(argv, **kwargs):
            nonlocal acceptance_ran
            result = environment(argv, **kwargs)
            if len(argv) > 1 and argv[1].endswith("verify_metrics_safety.py"):
                acceptance_ran = True
            if argv[0] == "kubectl" and "kube-system" in argv and acceptance_ran:
                result.stdout = json.dumps({"metadata": {"uid": "replacement-cluster"}})
            return result

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence"
            with patch.object(subprocess, "Popen", side_effect=command_processes(command)), contextlib.redirect_stdout(io.StringIO()):
                code = module.main(["--cluster-name", "phpa-metrics-safety-test", "--output", str(output)])
            summary = json.loads((output / "summary.json").read_text())
        try:
            self.assertEqual(1, code)
            self.assertFalse(environment.deleted)
            self.assertIn("identity changed", summary["cleanup_error"])
            self.assertTrue(Path(summary["private_workspace"]).is_dir())
        finally:
            if summary.get("private_workspace"):
                shutil.rmtree(summary["private_workspace"])

    def test_run_deadline_still_collects_diagnostics_and_cleans_up(self):
        module = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(module)
        environment = CommandEnvironment()
        elapsed = [0.0]

        def command(argv, **kwargs):
            result = environment(argv, **kwargs)
            if argv[:3] == ["kind", "create", "cluster"]:
                elapsed[0] = 20.0
            return result

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence"
            with patch.object(subprocess, "Popen", side_effect=command_processes(command)), \
                    patch.object(module.time, "monotonic", side_effect=lambda: elapsed[0]), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = module.main(["--cluster-name", "phpa-metrics-safety-test", "--output", str(output),
                                    "--timeout-seconds", "10"])
            summary = json.loads((output / "summary.json").read_text())
        self.assertEqual(1, code)
        self.assertIn("deadline", summary["run_error"])
        self.assertTrue(environment.deleted)

    def test_first_healthy_scrape_may_precede_raw_series_readiness(self):
        module = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(module)
        environment = CommandEnvironment()
        query_count = 0

        def command(argv, **kwargs):
            nonlocal query_count
            result = environment(argv, **kwargs)
            if argv[0] == "kubectl" and any("api/v1/query?" in arg for arg in argv):
                query_count += 1
                if query_count == 1:
                    result.stdout = json.dumps({"status": "success", "data": {"result": []}})
            return result

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence"
            with patch.object(subprocess, "Popen", side_effect=command_processes(command)), patch.object(module.time, "sleep"), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = module.main(["--cluster-name", "phpa-metrics-safety-test", "--output", str(output)])
            summary = json.loads((output / "summary.json").read_text())
        self.assertEqual(0, code, summary)
        self.assertGreater(query_count, 4)

    def test_failures_keep_their_stage_and_cleanup_only_owned_resources(self):
        for scenario in ("acceptance", "diagnostics", "partial-create", "interrupted-create", "cleanup", "replacement-node"):
            with self.subTest(scenario=scenario):
                module = importlib.util.module_from_spec(SPEC)
                SPEC.loader.exec_module(module)
                environment = CommandEnvironment()

                def command(argv, **kwargs):
                    if scenario == "cleanup" and argv[:3] == ["kind", "delete", "cluster"]:
                        return subprocess.CompletedProcess(argv, 1, "", "cleanup failed")
                    result = environment(argv, **kwargs)
                    if argv[:3] == ["kind", "create", "cluster"]:
                        if scenario == "partial-create":
                            result.returncode, result.stderr = 1, "created node but bootstrap failed"
                        elif scenario == "interrupted-create":
                            raise KeyboardInterrupt("interrupted bootstrap")
                    if len(argv) > 1 and argv[1].endswith("verify_metrics_safety.py"):
                        if scenario == "acceptance":
                            result.returncode, result.stderr = 1, "acceptance failed"
                        elif scenario == "replacement-node":
                            environment.cluster["Id"] = "replacement-node"
                    if scenario == "diagnostics" and "--all-pods=true" in argv:
                        result.returncode, result.stderr = 1, "diagnostic log collection failed"
                    return result

                with tempfile.TemporaryDirectory() as directory:
                    output = Path(directory) / "evidence"
                    with patch.object(subprocess, "Popen", side_effect=command_processes(command)), contextlib.redirect_stdout(io.StringIO()):
                        code = module.main(["--cluster-name", "phpa-metrics-safety-test", "--output", str(output)])
                    summary = json.loads((output / "summary.json").read_text())
                    self.assertTrue((output / "artifact-manifest.json").is_file())
                try:
                    self.assertEqual(1, code, summary)
                    if scenario in ("acceptance", "partial-create", "interrupted-create"):
                        self.assertIsNotNone(summary["run_error"])
                        self.assertTrue(environment.deleted)
                    elif scenario == "diagnostics":
                        self.assertIsNone(summary["run_error"])
                        self.assertTrue(summary["diagnostic_errors"])
                        self.assertTrue(environment.deleted)
                    else:
                        self.assertIsNotNone(summary["cleanup_error"])
                        self.assertFalse(environment.deleted)
                finally:
                    if summary.get("private_workspace"):
                        shutil.rmtree(summary["private_workspace"])

    def test_existing_evidence_is_untouched(self):
        module = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence"
            output.mkdir()
            sentinel = output / "original.txt"
            sentinel.write_text("keep failed receipt")
            with patch.object(subprocess, "Popen") as command, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    module.main(["--cluster-name", "phpa-metrics-safety-test", "--output", str(output)])
            self.assertEqual(2, raised.exception.code)
            self.assertEqual("keep failed receipt", sentinel.read_text())
            command.assert_not_called()


@unittest.skipUnless(os.name == "posix", "Process-group acceptance is exercised on Linux")
class MetricsSafetyProcessTreeTests(unittest.TestCase):
    def run_interrupted_cli(self, mode):
        finalization_stage = {"diagnostics-sigterm": "diagnostics", "delete-sigterm": "delete"}.get(mode)
        nested_verifier = mode != "diagnostics-sigterm"
        with tempfile.TemporaryDirectory(prefix="fault-process-cli-") as directory:
            work = Path(directory)
            source, tools_dir = work / "source", work / "tools"
            (source / "hack").mkdir(parents=True)
            tools_dir.mkdir()
            shutil.copyfile(ROOT / "hack/run_metrics_safety.py", source / "hack/run_metrics_safety.py")
            for name in ("Dockerfile", ".dockerignore", "go.mod", "go.sum", "cmd/main.go",
                         "config/benchmark/metrics-safety-monitoring.yaml"):
                target = source / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("isolated command fixture\n")
            (source / "deploy/charts/predictive-hpa").mkdir(parents=True)
            verifier = '''import os, pathlib, signal, subprocess, sys, time
pathlib.Path(os.environ["PHPA_PARENT_PID"]).write_text(str(os.getpid()))
code = "import os,pathlib,signal,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); pathlib.Path(os.environ['PHPA_CHILD_PID']).write_text(str(os.getpid())); print('nested stdout retained',flush=True); print('nested stderr retained',file=sys.stderr,flush=True); time.sleep(300)"
subprocess.Popen([sys.executable, "-c", code])
while True:
    time.sleep(1)
'''
            if not nested_verifier:
                verifier = '''import json, pathlib, sys
output = pathlib.Path(sys.argv[sys.argv.index("--output") + 1])
output.mkdir()
(output / "summary.json").write_text(json.dumps({"passed": True, "checks": [{"passed": True}] * 11}))
'''
            (source / "hack/verify_metrics_safety.py").write_text(verifier)
            tool_body = '''#!/usr/bin/env python3
import importlib.util, json, os, pathlib, subprocess, sys, time
tool = pathlib.Path(sys.argv[0]).name
args = [tool, *sys.argv[1:]]
stage = os.environ.get("PHPA_FINALIZE_STAGE")
if ((stage == "diagnostics" and tool == "kubectl" and "pods" in args and "-A" in args)
        or (stage == "delete" and args[:3] == ["kind", "delete", "cluster"])):
    pathlib.Path(os.environ["PHPA_FINALIZE_STARTED"]).write_text(str(os.getpid()))
    deadline = time.monotonic() + 5
    while not pathlib.Path(os.environ["PHPA_FINALIZE_RELEASE"]).exists() and time.monotonic() < deadline:
        time.sleep(0.01)
if tool == "git":
    if "ls-files" in args:
        root = pathlib.Path(os.environ["PHPA_FAKE_SOURCE"])
        sys.stdout.write("\\0".join(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()) + "\\0")
    elif "rev-parse" in args:
        print("4" * 40)
    sys.exit(0)
spec = importlib.util.spec_from_file_location("command_fixtures", os.environ["PHPA_TEST_MODULE"])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
environment = module.CommandEnvironment()
state = pathlib.Path(os.environ["PHPA_FAKE_STATE"])
if state.exists():
    environment.cluster = json.loads(state.read_text())
result = environment(args)
state.write_text(json.dumps(environment.cluster))
sys.stdout.write(result.stdout)
sys.stderr.write(result.stderr)
sys.exit(result.returncode)
'''
            for name in ("git", "docker", "kind", "kubectl", "helm"):
                path = tools_dir / name
                path.write_text(tool_body)
                path.chmod(0o755)
            output = work / "evidence"
            child_pid, parent_pid = work / "child.pid", work / "parent.pid"
            environment = {**os.environ, "PATH": str(tools_dir) + os.pathsep + os.environ["PATH"],
                "PHPA_FAKE_SOURCE": str(source), "PHPA_FAKE_STATE": str(work / "cluster.json"),
                "PHPA_TEST_MODULE": str(Path(__file__).resolve()),
                "PHPA_CHILD_PID": str(child_pid), "PHPA_PARENT_PID": str(parent_pid)}
            finalization_started, finalization_release = work / "finalization-started", work / "finalization-release"
            if finalization_stage:
                environment.update(PHPA_FINALIZE_STAGE=finalization_stage,
                    PHPA_FINALIZE_STARTED=str(finalization_started), PHPA_FINALIZE_RELEASE=str(finalization_release))
            process = subprocess.Popen([sys.executable, str(source / "hack/run_metrics_safety.py"),
                "--cluster-name", "phpa-metrics-safety-test", "--output", str(output),
                "--timeout-seconds", "6" if mode == "timeout" else "30"],
                env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)

            def alive(pid):
                try:
                    state = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()[0]
                    return state != "Z"
                except FileNotFoundError:
                    return False

            finished, child_running = False, False
            stdout, stderr = "", ""
            try:
                if nested_verifier:
                    deadline = time.monotonic() + 5
                    while not child_pid.exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.025)
                    self.assertTrue(child_pid.exists(), "Fake verifier did not start before the run budget")
                if mode in ("sigterm", "delete-sigterm"):
                    process.send_signal(signal.SIGTERM)
                if finalization_stage:
                    deadline = time.monotonic() + 9
                    while not finalization_started.exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.025)
                    self.assertTrue(finalization_started.exists(), "Finalization did not reach the selected command")
                    process.send_signal(signal.SIGTERM)
                    time.sleep(0.025)
                    process.send_signal(signal.SIGINT)
                    finalization_release.write_text("finish the bounded command")
                try:
                    stdout, stderr = process.communicate(timeout=13)
                    finished = True
                except subprocess.TimeoutExpired:
                    pass
                child_running = alive(int(child_pid.read_text())) if child_pid.exists() else False
            finally:
                for path in (child_pid, parent_pid, finalization_started):
                    if path.exists():
                        try:
                            os.kill(int(path.read_text()), signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    stdout, stderr = process.communicate(timeout=5)
            summary_path = output / "summary.json"
            summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
            receipts = [json.loads(p.read_text()) for p in output.glob("*-acceptance.json")]
            receipt = receipts[0] if receipts else {}
            evidence = {"mode": mode, "returned_within_bound": finished, "child_running_after_return": child_running,
                "returncode": process.returncode, "summary": summary, "acceptance_receipt": receipt,
                "stdout": stdout, "stderr": stderr}
            saved = os.environ.get("FAULT_PROCESS_RECEIPTS")
            if saved:
                destination = Path(saved)
                destination.mkdir(parents=True, exist_ok=True)
                (destination / (mode + ".json")).write_text(json.dumps(evidence, indent=2) + "\n")
            self.assertTrue(finished, "Public CLI did not return within its bounded shutdown grace")
            self.assertFalse(child_running, "Nested verifier process survived CLI shutdown")
            self.assertTrue(summary, "Finalization signal skipped summary.json")
            self.assertTrue(summary["cluster_deleted"], summary)
            self.assertTrue((output / "artifact-manifest.json").is_file())
            if nested_verifier:
                self.assertNotEqual(0, process.returncode)
                self.assertIsNotNone(summary["run_error"])
                self.assertIn("nested stdout retained", receipt.get("stdout", ""))
                self.assertIn("nested stderr retained", receipt.get("stderr", ""))
                self.assertTrue(receipt["termination"]["forced_kill"])
                self.assertTrue(receipt["termination"]["group_stopped"])

    def test_timeout_stops_nested_verifier_and_preserves_receipts(self):
        self.run_interrupted_cli("timeout")

    def test_sigterm_stops_nested_verifier_and_preserves_receipts(self):
        self.run_interrupted_cli("sigterm")

    def test_signals_during_diagnostics_preserve_cleanup_and_summary(self):
        self.run_interrupted_cli("diagnostics-sigterm")

    def test_second_signals_during_delete_preserve_cleanup_and_summary(self):
        self.run_interrupted_cli("delete-sigterm")


if __name__ == "__main__":
    unittest.main()
