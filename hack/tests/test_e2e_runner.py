"""E2E isolation checks with only the external command boundary simulated."""

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from hack import run_e2e
from hack.tests.test_metrics_safety_runner import CommandEnvironment, command_processes

CURL_IMAGE = (run_e2e.ROOT / "test/e2e/curl-image.txt").read_text().strip()


class E2EEnvironment(CommandEnvironment):
    def __init__(self, failure=None):
        super().__init__()
        self.failure = failure
        self.child_environment = None
        self.state = None
        self.other_cluster = None
        self.actual_commands = []
        self.curl_cached = True

    def __call__(self, argv, **kwargs):
        result = super().__call__(argv, **kwargs)
        if argv[:3] == ["docker", "image", "inspect"] and argv[-1] == CURL_IMAGE:
            if self.curl_cached:
                result.stdout = json.dumps([{"Id": self.image_id, "RepoDigests": [CURL_IMAGE]}])
            else:
                result.returncode, result.stderr = 1, "curl is not cached"
        elif argv[:2] == ["docker", "pull"]:
            if self.failure == "curl-pull":
                result.returncode, result.stderr = 1, "curl registry unavailable"
            else:
                self.curl_cached = True
        elif argv[:3] == ["docker", "image", "save"]:
            Path(argv[argv.index("--output") + 1]).write_bytes(b"curl archive fixture")
        elif argv[0] == "docker" and "images" in argv and "import" in argv:
            if kwargs.get("stdin") is None or kwargs["stdin"].read() != b"curl archive fixture":
                result.returncode, result.stderr = 1, "curl archive was not streamed"
        if argv == ["kind", "get", "clusters"] and not self.cluster and self.other_cluster:
            result.stdout = self.other_cluster + "\n"
        if argv[:2] == ["go", "test"]:
            self.state = json.loads(Path(self.child_environment["PHPA_E2E_STATE"]).read_text())
            if self.failure == "suite":
                result.returncode, result.stderr = 1, "intentional suite failure"
            elif self.failure == "replacement":
                self.cluster["Id"] = "unowned-replacement"
        elif argv[:3] == ["kind", "create", "cluster"] and self.failure == "partial-create":
            result.returncode, result.stderr = 1, "create failed after creating the owned node"
        return result


class E2ERunnerTests(unittest.TestCase):
    def execute(self, environment, executable_paths=None, cert_manager_skip=None):
        launch = command_processes(environment)
        executable_paths = executable_paths or {}

        def start(argv, **kwargs):
            environment.actual_commands.append((argv, kwargs.get("cwd")))
            if argv[0] != "git":
                self.assertTrue(Path(argv[0]).is_absolute(), argv)
            # Normalize only after checking the actual Popen boundary. Existing
            # fixtures describe tool behavior, independent of install location.
            normalized = [Path(argv[0]).name, *argv[1:]]
            if normalized[:2] == ["go", "test"]:
                environment.child_environment = kwargs["env"]
            return launch(normalized, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence"
            output.mkdir()
            user_config = Path(directory) / "user.kubeconfig"
            user_config.write_text("USER_DEFAULT_CONTEXT_CANARY")
            args = argparse.Namespace(cluster_name="phpa-metrics-safety-test", timeout_seconds=1800)
            with patch.object(subprocess, "Popen", side_effect=start), \
                    patch.object(run_e2e.shutil, "which", side_effect=lambda command: executable_paths.get(command, command)), \
                    patch.dict(os.environ, {"KUBECONFIG": str(user_config), "KIND": "kind", "KUBECTL": "kubectl",
                                            "MAKEFLAGS": "KUBECTL=unsafe-kubectl"}), \
                    contextlib.redirect_stdout(io.StringIO()):
                if cert_manager_skip is None:
                    os.environ.pop("CERT_MANAGER_INSTALL_SKIP", None)
                else:
                    os.environ["CERT_MANAGER_INSTALL_SKIP"] = cert_manager_skip
                runner = run_e2e.E2ERunner(args, output)
                code = runner.finish()
            self.assertEqual("USER_DEFAULT_CONTEXT_CANARY", user_config.read_text())
            summary = json.loads((output / "summary.json").read_text())
            receipts = "\n".join(path.read_text() for path in output.glob("*.json"))
            self.assertNotIn("PRIVATE_KUBECONFIG_CANARY", receipts)
            if summary.get("private_workspace"):
                # A real identity mismatch preserves the private workspace for
                # recovery; this test owns the simulated files and removes them.
                run_e2e.shutil.rmtree(summary["private_workspace"])
            return code, summary

    def test_explicit_certificate_dependency_is_preserved(self):
        environment = E2EEnvironment()
        code, summary = self.execute(environment, cert_manager_skip="false")
        self.assertEqual(0, code, summary)
        self.assertEqual("false", environment.child_environment["CERT_MANAGER_INSTALL_SKIP"])

    def test_private_context_reaches_go_and_nested_make_and_is_cleaned(self):
        environment = E2EEnvironment()
        code, summary = self.execute(environment)
        self.assertEqual(0, code, summary)
        self.assertTrue(environment.deleted)
        child = environment.child_environment
        self.assertEqual("true", child["CERT_MANAGER_INSTALL_SKIP"])
        private = Path(child["PHPA_E2E_STATE"]).parent
        self.assertEqual(str(private / "cluster.kubeconfig"), child["KUBECONFIG"])
        self.assertEqual(str(private / "bin" / "kubectl"), child["KUBECTL"])
        self.assertEqual(str(private / "bin" / "kind"), child["KIND"])
        self.assertTrue(child["PATH"].startswith(str(private / "bin") + os.pathsep))
        self.assertNotIn("MAKEFLAGS", child)
        self.assertEqual("docker", child["KIND_EXPERIMENTAL_PROVIDER"])
        self.assertFalse(private.exists())
        for argv in environment.commands:
            if argv[0] == "kubectl" or argv[:3] in (["kind", "create", "cluster"], ["kind", "delete", "cluster"]):
                self.assertEqual(child["KUBECONFIG"], argv[argv.index("--kubeconfig") + 1])

    def test_existing_cluster_is_never_reused_or_deleted(self):
        environment = E2EEnvironment()
        environment.cluster = {"Id": "unowned"}
        code, summary = self.execute(environment)
        self.assertEqual(1, code)
        self.assertIn("refusing reuse", summary["run_error"])
        self.assertEqual([["kind", "get", "clusters"]], environment.commands)
        self.assertFalse(environment.deleted)

    def test_relative_tool_paths_remain_absolute_in_the_private_source_and_guards(self):
        with tempfile.TemporaryDirectory(prefix="e2e-relative-tools-", dir=run_e2e.ROOT) as directory:
            tools = {}
            for name in ("kind", "kubectl", "docker", "go", "make"):
                tool = Path(directory) / name
                tool.write_text("mock command boundary fixture")
                tools[name] = os.path.relpath(tool, Path.cwd())
                self.assertFalse(Path(tools[name]).is_absolute())
            environment = E2EEnvironment()
            code, summary = self.execute(environment, tools)
            self.assertEqual(0, code, summary)
            for name, relative in tools.items():
                self.assertEqual(str(Path(relative).resolve()), environment.state[name])
            invocation, cwd = next((argv, cwd) for argv, cwd in environment.actual_commands if Path(argv[0]).name == "go")
            self.assertEqual(str(Path(tools["go"]).resolve()), invocation[0])
            self.assertNotEqual(run_e2e.ROOT, cwd)
            self.assertEqual(str(Path(tools["docker"]).resolve()), environment.child_environment["CONTAINER_TOOL"])

    def test_similar_existing_cluster_name_does_not_trigger_reuse(self):
        environment = E2EEnvironment()
        environment.other_cluster = "phpa-metrics-safety-test-someone-else"
        code, summary = self.execute(environment)
        self.assertEqual(0, code, summary)
        self.assertTrue(environment.deleted)

    def test_partial_create_and_suite_failure_both_preserve_receipts_and_clean_up(self):
        for failure in ("partial-create", "suite"):
            with self.subTest(failure=failure):
                environment = E2EEnvironment(failure)
                code, summary = self.execute(environment)
                self.assertEqual(1, code)
                self.assertIsNotNone(summary["run_error"])
                self.assertTrue(summary["cluster_deleted"])
                self.assertTrue(environment.deleted)

    def test_replaced_node_is_never_deleted(self):
        environment = E2EEnvironment("replacement")
        code, summary = self.execute(environment)
        self.assertEqual(1, code)
        self.assertIn("identity changed", summary["cleanup_error"])
        self.assertFalse(environment.deleted)

    def test_pinned_curl_is_prepared_before_create_and_imported_for_the_node_platform(self):
        for cached in (False, True):
            with self.subTest(cached=cached):
                environment = E2EEnvironment()
                environment.curl_cached = cached
                code, summary = self.execute(environment)
                self.assertEqual(0, code, summary)
                calls = environment.commands
                creation = next(i for i, argv in enumerate(calls) if argv[:3] == ["kind", "create", "cluster"])
                pulls = [i for i, argv in enumerate(calls) if argv[:2] == ["docker", "pull"]]
                self.assertEqual(0 if cached else 1, len(pulls))
                if pulls:
                    self.assertLess(pulls[0], creation)
                    self.assertEqual(CURL_IMAGE, calls[pulls[0]][-1])
                imported = next(i for i, argv in enumerate(calls) if "images" in argv and "import" in argv)
                suite = next(i for i, argv in enumerate(calls) if argv[:2] == ["go", "test"])
                self.assertLess(creation, imported)
                self.assertLess(imported, suite)
                self.assertIn("linux/amd64", calls[imported])
                self.assertNotIn("--all-platforms", calls[imported])

    def test_curl_pull_failure_does_not_create_a_cluster(self):
        environment = E2EEnvironment("curl-pull")
        environment.curl_cached = False
        code, summary = self.execute(environment)
        self.assertEqual(1, code)
        self.assertIn("curl-image-pull", summary["run_error"])
        self.assertFalse(environment.deleted)
        self.assertFalse(any(argv[:3] == ["kind", "create", "cluster"] for argv in environment.commands))


class CommandGuardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        private = Path(self.directory.name)
        config = private / "cluster.kubeconfig"
        config.write_text("private config")
        self.state = {"kubeconfig": str(config), "context": "kind-owned", "cluster_name": "owned",
                      "kubeconfig_sha256": run_e2e.hashlib.sha256(config.read_bytes()).hexdigest(),
                      "node_name": "owned-control-plane", "node_container_id": "owned-id",
                      "cluster_uid": "owned-uid", "owner": str(private / "owner"),
                      "docker": "docker", "kubectl": "kubectl", "kind": "kind"}
        self.state_path = private / "e2e-owner.json"
        self.state_path.write_text(json.dumps(self.state))
        self.node = {"Id": "owned-id", "Name": "/owned-control-plane",
                     "Config": {"Labels": {"io.x-k8s.kind.cluster": "owned"}},
                     "Mounts": [{"Source": self.state["owner"], "Destination": run_e2e.OWNER_MOUNT}]}
        self.uid = "owned-uid"

    def external(self, argv, **kwargs):
        self.assertEqual(20, kwargs["timeout"])
        value = [self.node] if argv[0] == "docker" else {"metadata": {"uid": self.uid}}
        return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")

    def test_mutation_is_pinned_after_successful_node_and_api_checks(self):
        with patch.object(subprocess, "run", side_effect=self.external):
            argv = run_e2e.guarded_command(self.state_path, "kubectl", ["apply", "-f", "fixture.yaml"])
        self.assertEqual(["kubectl", "--kubeconfig", self.state["kubeconfig"], "--context", "kind-owned",
                          "--request-timeout=20s", "apply", "-f", "fixture.yaml"], argv)

    def test_retargeted_kubeconfig_and_replaced_cluster_are_rejected(self):
        for replacement in ("kubeconfig", "node", "uid", "mount"):
            with self.subTest(replacement=replacement):
                self.setUp()
                if replacement == "kubeconfig":
                    Path(self.state["kubeconfig"]).write_text("user context")
                elif replacement == "node":
                    self.node["Id"] = "replacement"
                elif replacement == "uid":
                    self.uid = "replacement"
                else:
                    self.node["Mounts"] = []
                with patch.object(subprocess, "run", side_effect=self.external), self.assertRaises(RuntimeError):
                    run_e2e.guarded_command(self.state_path, "kubectl", ["delete", "namespace", "example"])

    def test_context_override_and_nonowned_kind_mutations_are_rejected_before_commands(self):
        for tool, argv in (("kubectl", ["--context=real", "delete", "namespace", "example"]),
                           ("kind", ["delete", "cluster", "--name", "owned"]),
                           ("kind", ["load", "docker-image", "test:tag", "--name", "real"])):
            with self.subTest(tool=tool, argv=argv), patch.object(subprocess, "run") as external, \
                    self.assertRaises(RuntimeError):
                run_e2e.guarded_command(self.state_path, tool, argv)
            external.assert_not_called()


if __name__ == "__main__":
    unittest.main()
