from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import test_benchmark_ablation as benchmark_tests


REPO_ROOT = benchmark_tests.REPO_ROOT
NDJSON = (
    '{"type":"Metric","metric":"http_reqs","data":{"type":"counter"}}\n'
    '{"type":"Point","metric":"http_reqs","data":{"value":1,"time":"2026-09-05T00:00:01Z"}}\n'
)

# Every cluster command is replaced with a local executable. Unexpected commands
# fail closed rather than accidentally falling through to a real kubectl.
KUBECTL_STUB = r'''
set -eu
printf '%s\t' "$@" >> "$K6_TEST_TRACE"
printf '\n' >> "$K6_TEST_TRACE"
while [[ "${1:-}" == --* ]]; do shift; done
case "${1:-}" in
  config)
    [ "${2:-}" = current-context ] || exit 98
    printf '%s\n' "${MOCK_CURRENT_CONTEXT:-kind-runner-test}"
    ;;
  cluster-info) exit 0 ;;
  create)
    [ "${2:-}" = -f ] || exit 98
    manifest="$3"
    resource=$(jq -r .kind "$manifest")
    resource="${resource,,}"
    if [ "${MOCK_CREATE_FAIL:-}" = "$resource" ]; then
      echo "AlreadyExists: resource belongs to a previous run" >&2
      exit 1
    fi
    cp "$manifest" "$K6_TEST_ROOT/created-$resource.json"
    ;;
  wait) exit "${MOCK_WAIT_EXIT_CODE:-0}" ;;
  exec)
    while [ "${1:-}" != -- ] && [ "$#" -gt 0 ]; do shift; done
    shift
    case "${1:-}" in
      sh)
        case "${3:-}" in
          *'if [ -f /results/k6-exit-code ]'*)
            printf '%s' "${MOCK_K6_MARKER-${MOCK_K6_EXIT_CODE:-0}}"
            ;;
          *'pkill -TERM -x k6'*|*'kill -TERM'*) exit 0 ;;
          *) echo "Unexpected exec shell: $*" >&2; exit 98 ;;
        esac
        ;;
      cat)
        file="${2##*/}"
        if [ "${MOCK_FAIL_ARTIFACT:-}" = "$file" ]; then
          printf 'partial artifact'
          echo 'simulated interrupted artifact transfer' >&2
          exit 9
        fi
        if [ "${MOCK_EMPTY_ARTIFACT:-}" = "$file" ]; then exit 0; fi
        case "$file" in
          k6.json) cat "$K6_TEST_ROOT/fixture.ndjson" ;;
          k6-summary.json)
            if [ "${MOCK_BAD_SUMMARY:-}" = 1 ]; then
              printf '{"metrics":'
            elif [ "${MOCK_NESTED_SUMMARY:-}" = 1 ]; then
              printf '{"metrics":{"http_reqs":{"values":{"count":1}}}}\n'
            else
              printf '{"metrics":{"http_reqs":{"count":1,"rate":0.5}}}\n'
            fi
            ;;
          k6-version.txt) printf '%s\n' "${MOCK_K6_VERSION:-k6 v1.3.0}" ;;
          k6-exit-code) printf '%s\n' "${MOCK_K6_EXIT_CODE:-0}" ;;
          k6-start-time-utc) printf '2026-09-05T00:00:00Z\n' ;;
          k6-end-time-utc) printf '2026-09-05T00:00:02Z\n' ;;
          k6-start-time-unix) printf '1788566400\n' ;;
          k6-end-time-unix) printf '1788566402\n' ;;
          k6-stdout.log) printf 'completed 1 iteration\n' ;;
          k6-warnings.log) : ;;
          *) echo "Unexpected artifact: $file" >&2; exit 98 ;;
        esac
        ;;
      *) echo "Unexpected exec: $*" >&2; exit 98 ;;
    esac
    ;;
  get)
    case "${2:-}" in
      pod)
        if [[ "$*" == *jsonpath* ]]; then
          printf '%s\n' "${MOCK_POD_PHASE:-Running}"
        else
          printf '{"kind":"Pod","status":{"phase":"Running"}}\n'
        fi
        ;;
      events) printf 'apiVersion: v1\nkind: List\nitems: []\n' ;;
      *) echo "Unexpected get: $*" >&2; exit 98 ;;
    esac
    ;;
  logs) printf 'runner container logs\n' ;;
  delete)
    [ "$#" -eq 5 ] || { echo "Unsafe delete: $*" >&2; exit 98; }
    [ "$4" = --wait=false ] && [ "$5" = --ignore-not-found=true ] || exit 98
    case "$2" in pod|configmap) ;; *) exit 98 ;; esac
    case "$3" in phpa-k6-*) ;; *) exit 98 ;; esac
    ;;
  *) echo "Unexpected kubectl call: $*" >&2; exit 98 ;;
esac
'''


@unittest.skipUnless(benchmark_tests.find_bash(), "bash is required for runner checks")
class K6RunnerTests(unittest.TestCase):
    bash = benchmark_tests.find_bash()
    run_bash = benchmark_tests.BenchmarkScriptTests.run_bash

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix=".k6-runner-test-", dir=REPO_ROOT)
        self.addCleanup(temporary.cleanup)
        self.test_root = Path(temporary.name)
        self.stub_dir = self.test_root / "bin"
        self.stub_dir.mkdir()
        self.trace = self.test_root / "kubectl-calls.txt"
        self.output_dir = self.test_root / "output"
        self.environment = {
            "K6_TEST_BIN": self.stub_dir.relative_to(REPO_ROOT).as_posix(),
            "K6_TEST_TRACE": self.trace.relative_to(REPO_ROOT).as_posix(),
            "K6_TEST_ROOT": self.test_root.relative_to(REPO_ROOT).as_posix(),
            "K6_TEST_OUTPUT": self.output_dir.relative_to(REPO_ROOT).as_posix(),
            "BENCHMARK_CONTEXT": "kind-runner-test",
            "K6_RUNNER_TIMEOUT_SECONDS": "5",
            "K6_RUNNER_STARTUP_TIMEOUT_SECONDS": "5",
            "K6_IMAGE": "grafana/k6:1.3.0",
        }
        (self.test_root / "fixture.ndjson").write_text(
            NDJSON, encoding="utf-8", newline="\n"
        )
        self.write_stub("kubectl", KUBECTL_STUB)
        self.write_stub(
            "kind",
            'printf "kind\\t%s\\t%s\\n" "$1" "$2" >> "$K6_TEST_TRACE"\n'
            '[ "$1 $2" = "get clusters" ] || exit 98\n'
            'printf "%s\\n" "${MOCK_KIND_CLUSTERS:-runner-test}"\n',
        )

    def write_stub(self, name: str, contents: str) -> None:
        path = self.stub_dir / name
        path.write_text("#!/usr/bin/env bash\n" + contents, encoding="utf-8", newline="\n")
        path.chmod(0o755)

    def invoke(self, command: str, **environment: str):
        return self.run_bash(
            "-c",
            'set -euo pipefail\n'
            'export PATH="$PWD/$K6_TEST_BIN:$PATH"\n'
            'source hack/lib/k6_runner.sh\n' + command,
            extra_env={**self.environment, **environment},
        )

    def calls(self) -> list[list[str]]:
        if not self.trace.exists():
            return []
        return [
            line.rstrip("\t").split("\t")
            for line in self.trace.read_text(encoding="utf-8").splitlines()
        ]

    def resource_calls(self, verb: str) -> list[list[str]]:
        return [
            [arg for arg in call if not arg.startswith("--")]
            for call in self.calls()
            if verb in call
        ]

    def read_json(self, filename: str) -> dict:
        return json.loads((self.output_dir / filename).read_text(encoding="utf-8"))

    def assert_own_resources_cleaned(self) -> None:
        metadata = self.read_json("k6-runner.json")
        self.assertEqual(
            [["delete", "pod", metadata["pod"]],
             ["delete", "configmap", metadata["configmap"]]],
            self.resource_calls("delete"),
        )

    def test_render_is_offline_and_uses_service_dns_with_script_imports(self) -> None:
        result = self.invoke(
            'k6_runner_render step.js "$K6_TEST_OUTPUT" RPS=7',
            K6_BASE_URL="http://localhost:8080",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], self.calls())
        pod = self.read_json("k6-pod.json")
        configmap = self.read_json("k6-configmap.json")
        metadata = self.read_json("k6-runner.json")
        container = pod["spec"]["containers"][0]
        environment = {entry["name"]: entry["value"] for entry in container["env"]}
        self.assertEqual("http://php-apache.default.svc:80", environment["BASE_URL"])
        self.assertEqual("true", environment["K6_NO_CONNECTION_REUSE"])
        self.assertEqual("7", environment["RPS"])
        self.assertEqual("grafana/k6:1.3.0", container["image"])
        self.assertEqual("Never", pod["spec"]["restartPolicy"])
        self.assertFalse(pod["spec"].get("hostNetwork", False))
        self.assertEqual("default", pod["metadata"]["namespace"])
        self.assertEqual("service-clusterip", metadata["traffic_path"])
        self.assertEqual("in-cluster-service", metadata["execution_mode"])
        self.assertFalse(metadata["connection_reuse"])
        self.assertEqual("planned", metadata["status"])
        script_volume = next(v for v in pod["spec"]["volumes"] if "configMap" in v)
        self.assertEqual(configmap["metadata"]["name"], script_volume["configMap"]["name"])
        paths = {item["path"]: item["key"] for item in script_volume["configMap"]["items"]}
        for path in ("step.js", "lib/common.js"):
            self.assertEqual(
                (REPO_ROOT / "hack" / "k6" / path).read_text(encoding="utf-8"),
                configmap["data"][paths[path]].replace("\r\n", "\n"),
            )
        self.assertIn("/scripts/step.js", container["args"])

    def test_render_generates_distinct_names_for_consecutive_runs(self) -> None:
        result = self.invoke(
            'unset K6_RUNNER_NAME\n'
            'k6_runner_render step.js "$K6_TEST_OUTPUT/one"\n'
            'k6_runner_render step.js "$K6_TEST_OUTPUT/two"'
        )
        self.assertEqual(0, result.returncode, result.stderr)
        names = []
        for run in ("one", "two"):
            metadata = self.read_json(f"{run}/k6-runner.json")
            self.assertRegex(metadata["pod"], r"^phpa-k6-[a-z0-9-]+$")
            self.assertLessEqual(len(metadata["pod"]), 63)
            self.assertEqual(metadata["pod"], metadata["configmap"])
            names.append(metadata["pod"])
        self.assertNotEqual(*names)

    def test_render_rejects_endpoint_override_and_parent_path(self) -> None:
        for arguments in (
            'step.js "$K6_TEST_OUTPUT" BASE_URL=http://localhost:8080',
            '../step.js "$K6_TEST_OUTPUT"',
        ):
            with self.subTest(arguments=arguments):
                result = self.invoke("k6_runner_render " + arguments)
                self.assertNotEqual(0, result.returncode)
                self.assertEqual([], self.calls())

    def test_context_rejections_happen_before_any_resource_mutation(self) -> None:
        for overrides in (
            {"BENCHMARK_CONTEXT": ""},
            {"BENCHMARK_CONTEXT": "production"},
            {"MOCK_CURRENT_CONTEXT": "production"},
            {"MOCK_KIND_CLUSTERS": "unrelated-cluster"},
        ):
            with self.subTest(overrides=overrides):
                result = self.invoke(
                    'k6_runner_run step.js "$K6_TEST_OUTPUT"', **overrides
                )
                self.assertEqual(1, result.returncode, result.stderr)
                self.assertEqual([], self.resource_calls("create"))
                self.assertEqual([], self.resource_calls("delete"))
                self.assertFalse(self.output_dir.exists())

    def test_success_collects_original_ndjson_and_deletes_only_owned_objects(self) -> None:
        result = self.invoke('k6_runner_run step.js "$K6_TEST_OUTPUT"')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(NDJSON, (self.output_dir / "k6.json").read_text(encoding="utf-8"))
        records = [json.loads(line) for line in NDJSON.splitlines()]
        self.assertEqual(["Metric", "Point"], [record["type"] for record in records])
        metadata = self.read_json("k6-runner.json")
        self.assertEqual("success", metadata["status"])
        self.assertEqual(0, metadata["k6_exit_code"])
        self.assertEqual("", metadata["failure_reason"])
        self.assertEqual("k6 v1.3.0\n", (self.output_dir / "k6-version.txt").read_text())
        for resource in ("pod", "configmap"):
            created = json.loads(
                (self.test_root / f"created-{resource}.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata[resource], created["metadata"]["name"])
        for call in self.calls():
            if call[0] not in ("config", "kind"):
                self.assertIn("--context=kind-runner-test", call)
        self.assert_own_resources_cleaned()

    def test_failed_workload_keeps_artifacts_and_returns_failure(self) -> None:
        result = self.invoke(
            'k6_runner_run step.js "$K6_TEST_OUTPUT"', MOCK_K6_EXIT_CODE="99"
        )
        self.assertEqual(2, result.returncode, result.stderr)
        metadata = self.read_json("k6-runner.json")
        self.assertEqual("failed", metadata["status"])
        self.assertEqual(99, metadata["k6_exit_code"])
        self.assertIn("99", metadata["failure_reason"])
        self.assertEqual(NDJSON, (self.output_dir / "k6.json").read_text(encoding="utf-8"))
        self.assert_own_resources_cleaned()

    def test_official_image_build_version_is_preserved_and_succeeds(self) -> None:
        version = "k6 v1.3.0+dirty (commit/5870e99ae8-dirty, go1.25.1, linux/amd64)"
        image = (
            "grafana/k6:1.3.0@sha256:"
            "a90b459a3768c46ad1013da53af24189f735d7112273c6ac3212ca8ed0e18656"
        )
        result = self.invoke(
            'k6_runner_run step.js "$K6_TEST_OUTPUT"',
            K6_IMAGE=image,
            MOCK_K6_VERSION=version,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("success", self.read_json("k6-runner.json")["status"])
        self.assertEqual(image, self.read_json("k6-runner.json")["image"])
        self.assertEqual(
            version + "\n", (self.output_dir / "k6-version.txt").read_text(encoding="utf-8")
        )
        self.assert_own_resources_cleaned()

    def test_version_accepts_build_metadata_but_rejects_other_release_suffixes(self) -> None:
        self.output_dir.mkdir()
        artifacts = {
            "k6.json": NDJSON,
            "k6-summary.json": '{"metrics":{"http_reqs":{"count":1}}}\n',
            "k6-start-time-utc": "2026-09-05T00:00:00Z\n",
            "k6-end-time-utc": "2026-09-05T00:00:02Z\n",
            "k6-start-time-unix": "1788566400\n",
            "k6-end-time-unix": "1788566402\n",
            "k6-exit-code": "0\n",
        }
        for filename, contents in artifacts.items():
            (self.output_dir / filename).write_text(contents, encoding="utf-8")
        accepted = (
            "k6 v1.3.0",
            "k6 v1.3.0 (commit/5870e99ae8, go1.25.1, linux/amd64)",
            "k6 v1.3.0+dirty",
            "k6 v1.3.0+build.001.g5870e99ae8-dirty (go1.25.1, linux/amd64)",
            "k6 v1.3.0+BUILD-7.0",
        )
        rejected = (
            "k6 v1.3.1+dirty",
            "k6 v1.30.0+dirty",
            "k6 v1.3.01+dirty",
            "k6 v1.3.0-rc.1",
            "k6 v1.3.0-rc.1+dirty",
            "k6 v1.3.0dirty",
            "k6 v1.3.0+",
            "k6 v1.3.0+.dirty",
            "k6 v1.3.0+dirty.",
            "k6 v1.3.0+build..dirty",
            "k6 v1.3.0+build_dirty",
            "k6 v1.3.0+dirty+again",
            "k6 v1.3.0+dirty/extra",
        )
        for version in (*accepted, *rejected):
            with self.subTest(version=version):
                (self.output_dir / "k6-version.txt").write_text(
                    version + "\n", encoding="utf-8"
                )
                result = self.invoke('k6_runner_validate_artifacts "$K6_TEST_OUTPUT"')
                self.assertEqual(0 if version in accepted else 1, result.returncode, result.stderr)
                if version in rejected:
                    self.assertIn("does not match the pinned image version", result.stderr)
                self.assertEqual(
                    version + "\n", (self.output_dir / "k6-version.txt").read_text(encoding="utf-8")
                )
        self.assertEqual([], self.calls())

    def test_nested_summary_counter_is_also_accepted(self) -> None:
        result = self.invoke(
            'k6_runner_run step.js "$K6_TEST_OUTPUT"', MOCK_NESTED_SUMMARY="1"
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("success", self.read_json("k6-runner.json")["status"])
        self.assert_own_resources_cleaned()

    def test_collection_failure_preserves_partial_data_and_runner_for_recovery(self) -> None:
        result = self.invoke(
            'k6_runner_run step.js "$K6_TEST_OUTPUT"', MOCK_FAIL_ARTIFACT="k6.json"
        )
        self.assertEqual(3, result.returncode, result.stderr)
        metadata = self.read_json("k6-runner.json")
        self.assertEqual("failed", metadata["status"])
        self.assertIn("collection", metadata["failure_reason"])
        self.assertEqual("partial artifact", (self.output_dir / "k6.json.partial").read_text())
        self.assertFalse((self.output_dir / "k6.json").exists())
        self.assertTrue((self.output_dir / "k6-version.txt").exists())
        self.assertEqual([], self.resource_calls("delete"))

    def test_invalid_ndjson_or_no_requests_retains_runner_for_recovery(self) -> None:
        fixtures = ("", '{"type":', '{}\n',
                    '{"type":"Point","metric":"http_reqs","data":{"value":0}}\n')
        for index, contents in enumerate(fixtures):
            with self.subTest(contents=contents):
                (self.test_root / "fixture.ndjson").write_text(contents, encoding="utf-8")
                self.output_dir = self.test_root / f"invalid-ndjson-{index}"
                result = self.invoke(
                    'k6_runner_run step.js "$K6_TEST_OUTPUT"',
                    K6_TEST_OUTPUT=self.output_dir.relative_to(REPO_ROOT).as_posix(),
                )
                self.assertEqual(3, result.returncode, result.stderr)
                self.assertEqual("failed", self.read_json("k6-runner.json")["status"])
                self.assertIn("validation", self.read_json("k6-runner.json")["failure_reason"])
                self.assertEqual(contents, (self.output_dir / "k6.json").read_text())
                self.assertEqual([], self.resource_calls("delete"))

    def test_empty_required_artifacts_or_invalid_summary_cannot_succeed(self) -> None:
        cases = [
            {"MOCK_EMPTY_ARTIFACT": filename}
            for filename in ("k6-summary.json", "k6-version.txt", "k6-start-time-utc",
                             "k6-start-time-unix", "k6-end-time-utc", "k6-end-time-unix")
        ] + [{"MOCK_BAD_SUMMARY": "1"}]
        for index, overrides in enumerate(cases):
            with self.subTest(overrides=overrides):
                self.output_dir = self.test_root / f"invalid-artifact-{index}"
                result = self.invoke(
                    'k6_runner_run step.js "$K6_TEST_OUTPUT"',
                    K6_TEST_OUTPUT=self.output_dir.relative_to(REPO_ROOT).as_posix(),
                    **overrides,
                )
                self.assertEqual(3, result.returncode, result.stderr)
                self.assertEqual("failed", self.read_json("k6-runner.json")["status"])
                self.assertEqual([], self.resource_calls("delete"))

    def test_reusing_output_directory_preserves_earlier_evidence(self) -> None:
        self.output_dir.mkdir()
        original = '{"status":"failed","reason":"retain original evidence"}\n'
        (self.output_dir / "k6-runner.json").write_text(original, encoding="utf-8")
        result = self.invoke('k6_runner_run step.js "$K6_TEST_OUTPUT"')
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertIn("fresh output directory", result.stderr)
        self.assertEqual(original, (self.output_dir / "k6-runner.json").read_text())
        self.assertEqual([], self.resource_calls("create"))
        self.assertEqual([], self.resource_calls("delete"))

    def test_configmap_create_collision_does_not_delete_existing_resource(self) -> None:
        result = self.invoke(
            'if k6_runner_run step.js "$K6_TEST_OUTPUT"; then exit 0; else exit "$?"; fi',
            MOCK_CREATE_FAIL="configmap",
        )
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("failed", self.read_json("k6-runner.json")["status"])
        self.assertEqual([], self.resource_calls("delete"))
        self.assertFalse((self.test_root / "created-pod.json").exists())

    def test_pod_create_collision_cleans_only_new_configmap(self) -> None:
        result = self.invoke(
            'if k6_runner_run step.js "$K6_TEST_OUTPUT"; then exit 0; else exit "$?"; fi',
            MOCK_CREATE_FAIL="pod",
        )
        self.assertNotEqual(0, result.returncode)
        metadata = self.read_json("k6-runner.json")
        self.assertEqual("failed", metadata["status"])
        self.assertEqual(
            [["delete", "configmap", metadata["configmap"]]],
            self.resource_calls("delete"),
        )

    def test_startup_failure_is_preserved_when_caller_checks_return_code(self) -> None:
        result = self.invoke(
            'if k6_runner_run step.js "$K6_TEST_OUTPUT"; then exit 0; else exit "$?"; fi',
            MOCK_WAIT_EXIT_CODE="1",
        )
        self.assertNotEqual(0, result.returncode)
        metadata = self.read_json("k6-runner.json")
        self.assertEqual("failed", metadata["status"])
        self.assertIn("ready", metadata["failure_reason"])
        self.assert_own_resources_cleaned()

    def test_unpinned_k6_version_is_rejected_before_cluster_access(self) -> None:
        result = self.invoke(
            'k6_runner_run step.js "$K6_TEST_OUTPUT"', K6_IMAGE="grafana/k6:latest"
        )
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertIn("1.3.0", result.stderr)
        self.assertEqual([], self.calls())

    def test_runner_pod_failure_without_exit_marker_is_not_success(self) -> None:
        result = self.invoke(
            'k6_runner_run step.js "$K6_TEST_OUTPUT"',
            MOCK_K6_MARKER="",
            MOCK_POD_PHASE="Failed",
        )
        self.assertEqual(2, result.returncode, result.stderr)
        metadata = self.read_json("k6-runner.json")
        self.assertEqual("failed", metadata["status"])
        self.assertIn("terminated", metadata["failure_reason"])
        self.assert_own_resources_cleaned()


if __name__ == "__main__":
    unittest.main()
