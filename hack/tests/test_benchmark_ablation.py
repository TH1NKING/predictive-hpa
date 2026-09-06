from __future__ import annotations

import os
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_BENCHMARK = REPO_ROOT / "hack" / "run_benchmark.sh"
RUN_MATRIX = REPO_ROOT / "hack" / "run_matrix.sh"


def find_bash() -> str | None:
    """Prefer Git Bash on Windows; the system bash.exe is the WSL launcher."""
    if os.name == "nt":
        for candidate in (
            Path(r"D:\Git\bin\bash.exe"),
            Path(r"C:\Program Files\Git\bin\bash.exe"),
        ):
            if candidate.is_file():
                return str(candidate)
        return None
    return shutil.which("bash")


class NativeHPAManifestTests(unittest.TestCase):
    def test_native_manifests_only_differ_by_stabilization_window(self) -> None:
        with (REPO_ROOT / "config" / "benchmark" / "native-hpa.yaml").open(
            encoding="utf-8"
        ) as f:
            native_300 = yaml.safe_load(f)
        with (REPO_ROOT / "config" / "benchmark" / "native-hpa-60.yaml").open(
            encoding="utf-8"
        ) as f:
            native_60 = yaml.safe_load(f)

        window_300 = native_300["spec"]["behavior"]["scaleDown"].pop(
            "stabilizationWindowSeconds"
        )
        window_60 = native_60["spec"]["behavior"]["scaleDown"].pop(
            "stabilizationWindowSeconds"
        )

        self.assertEqual(300, window_300)
        self.assertEqual(60, window_60)
        self.assertEqual(native_300, native_60)

        with (
            REPO_ROOT
            / "config"
            / "samples"
            / "autoscaling_v1alpha1_predictivehpa.yaml"
        ).open(encoding="utf-8") as f:
            phpa = yaml.safe_load(f)
        self.assertEqual(60, phpa["spec"]["scaleDownStabilizationWindowSeconds"])


@unittest.skipUnless(find_bash(), "bash is required for benchmark script checks")
class BenchmarkScriptTests(unittest.TestCase):
    bash = find_bash()

    def run_bash(
        self,
        *args: str,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["BENCHMARK_PYTHON"] = sys.executable
        if os.name == "nt":
            git_root = Path(self.bash).resolve().parents[1]
            git_paths = [REPO_ROOT / "bin", git_root / "usr" / "bin", git_root / "bin"]
            env["PATH"] = os.pathsep.join(
                [*(str(path) for path in git_paths), env.get("PATH", "")]
            )
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [self.bash, *args],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
            timeout=10,
        )

    def test_scripts_have_valid_bash_syntax(self) -> None:
        for script in ("hack/run_benchmark.sh", "hack/run_matrix.sh", "hack/lib/benchmark_config.sh"):
            with self.subTest(script=script):
                result = self.run_bash("-n", script)
                self.assertEqual(0, result.returncode, result.stderr)

    def test_controller_variants_are_accepted_before_runtime_checks(self) -> None:
        for controller in ("native_hpa_300", "native_hpa_60", "phpa"):
            with self.subTest(controller=controller):
                result = self.run_bash(
                    "hack/run_benchmark.sh", "step", controller, "0"
                )
                self.assertEqual(1, result.returncode)
                self.assertIn("repeat_idx must be positive integer", result.stderr)
                self.assertNotIn("controller must be", result.stderr)

        legacy = self.run_bash(
            "hack/run_benchmark.sh", "step", "native_hpa", "0"
        )
        self.assertEqual(1, legacy.returncode)
        self.assertIn("controller must be", legacy.stderr)

    def test_metadata_fields_and_controller_manifests_are_explicit(self) -> None:
        script = RUN_BENCHMARK.read_text(encoding="utf-8")
        matrix_script = RUN_MATRIX.read_text(encoding="utf-8")
        default_root = (
            'EXPERIMENTS_ROOT="${EXPERIMENTS_ROOT:-experiments/service-routing-v1}"'
        )
        self.assertIn(default_root, script)
        self.assertIn(default_root, matrix_script)
        self.assertIn(
            'CAMPAIGN="${CAMPAIGN:-service-routing-v1}"', script
        )
        self.assertIn('PREDICTION_VARIANT="ewma_damped_cap"', script)
        for field in (
            "campaign:",
            "scale_down_stabilization_seconds:",
            "prediction_variant:",
            "traffic_path:",
            "load_generator:",
            "endpoint:",
            "k6_image:",
            "connection_reuse:",
        ):
            self.assertIn(field, script)
        self.assertIn(
            'NATIVE_HPA_YAML="config/benchmark/native-hpa.yaml"', script
        )
        self.assertIn(
            'NATIVE_HPA_YAML="config/benchmark/native-hpa-60.yaml"', script
        )

        controller_case = script.split('case "$CONTROLLER" in', 1)[1].split(
            "esac", 1
        )[0]
        expected_variants = {
            "native_hpa_300": (
                'NATIVE_HPA_YAML="config/benchmark/native-hpa.yaml"',
                "SCALE_DOWN_STABILIZATION_SECONDS=300",
                'PREDICTION_VARIANT="none"',
            ),
            "native_hpa_60": (
                'NATIVE_HPA_YAML="config/benchmark/native-hpa-60.yaml"',
                "SCALE_DOWN_STABILIZATION_SECONDS=60",
                'PREDICTION_VARIANT="none"',
            ),
            "phpa": (
                'NATIVE_HPA_YAML=""',
                "SCALE_DOWN_STABILIZATION_SECONDS=60",
                'PREDICTION_VARIANT="ewma_damped_cap"',
            ),
        }
        branch_pattern = (
            r"  {controller}\)\n(?P<body>.*?)(?=\n  "
            r"(?:native_hpa_300|native_hpa_60|phpa|\*)\))"
        )
        for controller, expected_lines in expected_variants.items():
            with self.subTest(controller_mapping=controller):
                match = re.search(
                    branch_pattern.format(controller=re.escape(controller)),
                    controller_case,
                    re.DOTALL,
                )
                self.assertIsNotNone(match)
                for expected_line in expected_lines:
                    self.assertIn(expected_line, match.group("body"))

    def test_matrix_rotates_all_controllers_and_isolates_experiment_root(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix=".ablation-test-", dir=REPO_ROOT
        ) as temp_dir:
            temp_root = Path(temp_dir)
            old_root = temp_root / "old"
            current_root = temp_root / "current"
            old_root.mkdir()
            current_root.mkdir()

            old_run = old_root / "20260826_000000_step_phpa_r1"
            old_run.mkdir()
            (old_run / "metadata.yaml").write_text(
                "result:\n  status: success\n", encoding="utf-8"
            )
            # A copied historical success must not satisfy the corrected
            # traffic-path campaign merely because the directory name matches.
            legacy_current_run = current_root / "20260708_000000_step_phpa_r1"
            legacy_current_run.mkdir()
            (legacy_current_run / "metadata.yaml").write_text(
                "result:\n  status: success\n", encoding="utf-8"
            )

            relative_root = current_root.relative_to(REPO_ROOT).as_posix()
            result = self.run_bash(
                "hack/run_matrix.sh",
                "--dry-run",
                extra_env={"EXPERIMENTS_ROOT": relative_root},
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertNotIn("command not found", result.stderr)
            self.assertIn("Summary: 0 already done, 27 pending.", result.stdout)

            plan_pattern = re.compile(
                r"^\[\s*\d+/27\]\s+(step|ramp|spike)\s+"
                r"(native_hpa_300|native_hpa_60|phpa)\s+r([123])\s+PENDING$",
                re.MULTILINE,
            )
            plan = plan_pattern.findall(result.stdout)
            self.assertEqual(27, len(plan))

            base_order = ["native_hpa_300", "native_hpa_60", "phpa"]
            for pattern in ("step", "ramp", "spike"):
                for repeat in (1, 2, 3):
                    actual = [
                        controller
                        for found_pattern, controller, found_repeat in plan
                        if found_pattern == pattern and int(found_repeat) == repeat
                    ]
                    rotation = repeat - 1
                    expected = base_order[rotation:] + base_order[:rotation]
                    self.assertEqual(expected, actual)

            current_run = current_root / "20260826_000001_step_phpa_r1"
            current_run.mkdir()
            (current_run / "metadata.yaml").write_text(
                'campaign: "service-routing-v1"\n'
                'traffic_path: "service-clusterip"\n'
                'load_generator: "in-cluster-service"\n'
                'endpoint: "http://php-apache.default.svc:80"\n'
                'k6_image: "grafana/k6:1.3.0"\n'
                'connection_reuse: false\n'
                'result:\n  status: success\n',
                encoding="utf-8",
            )
            resumed = self.run_bash(
                "hack/run_matrix.sh",
                "--dry-run",
                extra_env={"EXPERIMENTS_ROOT": relative_root},
            )
            self.assertEqual(0, resumed.returncode, resumed.stderr)
            self.assertNotIn("command not found", resumed.stderr)
            self.assertIn("Summary: 0 already done, 27 pending.", resumed.stdout)

            # An old Service-path success still lacks the current load/VU and
            # measurement identity. Only an exact current identity may resume.
            (current_run / "metadata.yaml").write_text(
                self.success_metadata(result.stdout), encoding="utf-8"
            )
            self.write_valid_extract(current_run)
            resumed = self.run_bash(
                "hack/run_matrix.sh", "--dry-run",
                extra_env={"EXPERIMENTS_ROOT": relative_root},
            )
            self.assertEqual(0, resumed.returncode, resumed.stderr)
            self.assertIn("Summary: 1 already done, 26 pending.", resumed.stdout)

    @staticmethod
    def success_metadata(plan: str) -> str:
        """Materialize the identity displayed by the public offline plan."""
        fields = dict(line.split(": ", 1) for line in plan.splitlines() if ": " in line)
        vus = re.fullmatch(r"(\d+) preallocated, (\d+) maximum", fields["Virtual users"])
        return (
            'campaign: "service-routing-v1"\n'
            'traffic_path: "service-clusterip"\n'
            'load_generator: "in-cluster-service"\n'
            'endpoint: "http://php-apache.default.svc:80"\n'
            'k6_image: "grafana/k6:1.3.0"\n'
            'connection_reuse: false\n'
            f'protocol_version: "{fields["Protocol"]}"\n'
            f'rps: {fields["Offered RPS"]}\n'
            f'benchmark_source_sha256: "{fields["Source SHA256"]}"\n'
            f'benchmark_config_sha256: "{fields["Configuration SHA256"]}"\n'
            f'pre_allocated_vus: {vus[1]}\n'
            f'max_vus: {vus[2]}\n'
            'post_load_tail_seconds: 360\n'
            'experiment_id: step-phpa-r1\n'
            'pattern: step\ncontroller: phpa\nrepeat: 1\n'
            'scale_down_stabilization_seconds: 60\n'
            'prediction_variant: ewma_damped_cap\n'
            'git:\n  commit: abcdef0123456789\n'
            'load_start_time_unix: 1700000030\n'
            'offered_load_end_time_unix: 1700000211\n'
            'observation_end_time_unix: 1700000571\n'
            'result:\n  status: success\n'
        )

    @staticmethod
    def write_valid_extract(directory: Path) -> None:
        metadata = yaml.safe_load((directory / "metadata.yaml").read_text(encoding="utf-8"))
        extracted = {**metadata, "git_commit": metadata["git"]["commit"], "measurement": {
            "window_valid": True,
            "load_onset_time_unix": 1700000030,
            "offered_load_end_time_unix": 1700000211,
            "tail_end_time_unix": 1700000571,
        }}
        (directory / "extract.json").write_text(json.dumps(extracted), encoding="utf-8")

    def test_selected_pilot_rotates_two_controllers_and_supports_more_repeats(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".pilot-plan-", dir=REPO_ROOT) as root:
            result = self.run_bash(
                "hack/run_matrix.sh", "--dry-run", extra_env={
                    "EXPERIMENTS_ROOT": Path(root).relative_to(REPO_ROOT).as_posix(),
                    "BENCHMARK_PATTERNS": "step",
                    "BENCHMARK_CONTROLLERS": "native_hpa_60 phpa",
                    "BENCHMARK_REPEATS": "5", "RPS": "40",
                },
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("10 experiments planned", result.stdout)
            self.assertIn("Offered RPS: 40", result.stdout)
            self.assertIn("400 preallocated, 480 maximum", result.stdout)
            plan = re.findall(
                r"^\[\s*\d+/10\]\s+(\w+)\s+(\w+)\s+r(\d+)\s+PENDING$",
                result.stdout, re.MULTILINE,
            )
            self.assertEqual([
                ("step", controller, str(repeat))
                for repeat in range(1, 6)
                for controller in (
                    ("native_hpa_60", "phpa") if repeat % 2 else ("phpa", "native_hpa_60")
                )
            ], plan)

    def test_invalid_configuration_is_rejected_before_runtime_checks(self) -> None:
        invalid = {
            "RPS": ("", "0", "01", "1001", "-1", "1.5", "25\n", "25;echo bad"),
            "BENCHMARK_REPEATS": ("", "0", "01", "1001", "1\n2"),
            "BENCHMARK_PATTERNS": ("", "  ", "step step", "unknown", "step\nramp", "step\rramp"),
            "BENCHMARK_CONTROLLERS": ("", "phpa phpa", "native_hpa", "phpa\nnative_hpa_60"),
        }
        for name, values in invalid.items():
            for value in values:
                with self.subTest(name=name, value=value):
                    result = self.run_bash(
                        "hack/run_matrix.sh", "--dry-run", extra_env={name: value},
                    )
                    self.assertEqual(1, result.returncode, result.stderr)
                    self.assertIn("ERROR:", result.stderr)
                    self.assertNotIn("Pre-flight", result.stdout)
                    self.assertNotIn("experiments planned", result.stdout)

    def test_resume_requires_exact_configuration_but_allows_repeat_expansion(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".pilot-resume-", dir=REPO_ROOT) as root:
            exp_root = Path(root)
            env = {
                "EXPERIMENTS_ROOT": exp_root.relative_to(REPO_ROOT).as_posix(),
                "BENCHMARK_PATTERNS": "step", "BENCHMARK_CONTROLLERS": "native_hpa_60 phpa",
                "BENCHMARK_REPEATS": "1", "RPS": "25",
            }
            plan = self.run_bash("hack/run_matrix.sh", "--dry-run", extra_env=env)
            self.assertEqual(0, plan.returncode, plan.stderr)
            run = exp_root / "20260906_000000_step_phpa_r1"
            run.mkdir()
            metadata = self.success_metadata(plan.stdout)
            (run / "metadata.yaml").write_text(metadata, encoding="utf-8")
            self.write_valid_extract(run)

            expanded = self.run_bash(
                "hack/run_matrix.sh", "--dry-run", extra_env={**env, "BENCHMARK_REPEATS": "2"},
            )
            self.assertEqual(0, expanded.returncode, expanded.stderr)
            self.assertIn("1 already done, 3 pending", expanded.stdout)
            changed_rps = self.run_bash(
                "hack/run_matrix.sh", "--dry-run", extra_env={**env, "RPS": "26"},
            )
            self.assertEqual(0, changed_rps.returncode, changed_rps.stderr)
            self.assertIn("0 already done, 2 pending", changed_rps.stdout)
            changed_context = self.run_bash(
                "hack/run_matrix.sh", "--dry-run",
                extra_env={**env, "BENCHMARK_CONTEXT": "kind-other-pilot"},
            )
            self.assertEqual(0, changed_context.returncode, changed_context.stderr)
            self.assertIn("0 already done, 2 pending", changed_context.stdout)

            valid_extract = (run / "extract.json").read_text(encoding="utf-8")
            invalid_extract = json.loads(valid_extract)
            invalid_extract["measurement"]["window_valid"] = False
            (run / "extract.json").write_text(json.dumps(invalid_extract), encoding="utf-8")
            invalid = self.run_bash("hack/run_matrix.sh", "--dry-run", extra_env=env)
            self.assertEqual(0, invalid.returncode, invalid.stderr)
            self.assertIn("0 already done, 2 pending", invalid.stdout)
            self.assertIn("invalid controlled measurement", invalid.stderr)
            (run / "extract.json").unlink()
            missing = self.run_bash("hack/run_matrix.sh", "--dry-run", extra_env=env)
            self.assertEqual(0, missing.returncode, missing.stderr)
            self.assertIn("0 already done, 2 pending", missing.stdout)
            (run / "extract.json").write_text(valid_extract, encoding="utf-8")

            for original, replacement in (
                ('protocol_version: "controlled-pilot-v1"', 'protocol_version: "old"'),
                ('benchmark_source_sha256: "', 'benchmark_source_sha256: "different-'),
                ('benchmark_config_sha256: "', 'benchmark_config_sha256: "different-'),
                ('connection_reuse: false', 'connection_reuse: true'),
                ('pre_allocated_vus: 250', 'pre_allocated_vus: 100'),
                ('max_vus: 300', 'max_vus: 200'),
            ):
                with self.subTest(replacement=replacement):
                    self.assertIn(original, metadata)
                    (run / "metadata.yaml").write_text(
                        metadata.replace(original, replacement), encoding="utf-8"
                    )
                    changed = self.run_bash("hack/run_matrix.sh", "--dry-run", extra_env=env)
                    self.assertEqual(0, changed.returncode, changed.stderr)
                    self.assertIn("0 already done, 2 pending", changed.stdout)

    def test_matrix_dry_run_never_contacts_a_cluster(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix=".matrix-offline-test-", dir=REPO_ROOT
        ) as temp_dir:
            temp_root = Path(temp_dir)
            stub_dir = temp_root / "bin"
            stub_dir.mkdir()
            for command in ("kubectl", "kind", "curl", "k6"):
                stub = stub_dir / command
                stub.write_text(
                    '#!/usr/bin/env bash\n'
                    'printf "%s\\n" "$0 $*" >> "$K6_TEST_TRACE"\n'
                    'exit 99\n',
                    encoding="utf-8",
                    newline="\n",
                )
                stub.chmod(0o755)
            trace = temp_root / "cluster-calls.txt"
            result = self.run_bash(
                "-c",
                'export PATH="$PWD/$K6_TEST_BIN:$PATH"\n'
                'bash hack/run_matrix.sh --dry-run',
                extra_env={
                    "K6_TEST_BIN": stub_dir.relative_to(REPO_ROOT).as_posix(),
                    "K6_TEST_TRACE": trace.relative_to(REPO_ROOT).as_posix(),
                    "EXPERIMENTS_ROOT": (
                        temp_root / "new-campaign"
                    ).relative_to(REPO_ROOT).as_posix(),
                },
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("27 pending", result.stdout)
            self.assertFalse(trace.exists(), "Dry-run contacted a cluster tool")

    def test_invalid_extraction_stops_matrix_and_preserves_failed_attempt(self) -> None:
        # A fake collector replaces cluster work. The real matrix and extractor
        # must reject its incomplete evidence through their command interfaces.
        with tempfile.TemporaryDirectory(prefix=".matrix-failure-", dir=REPO_ROOT) as temporary:
            root = Path(temporary)
            for name in ("hack", "api", "cmd", "internal", "config"):
                shutil.copytree(REPO_ROOT / name, root / name,
                                ignore=shutil.ignore_patterns(".venv", "__pycache__", "*.pyc"))
            for name in ("go.mod", "go.sum"):
                shutil.copyfile(REPO_ROOT / name, root / name)
            prerequisite = root / "hack" / "prerequisites_check.sh"
            prerequisite.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8", newline="\n")
            prerequisite.chmod(0o755)
            collector = root / "hack" / "run_benchmark.sh"
            collector.write_text('''#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib/k6_runner.sh"
source "$(dirname "$0")/lib/benchmark_config.sh"
benchmark_config_init
benchmark_config_fingerprint
out="$EXPERIMENTS_ROOT/20260906_000000_${1}_${2}_r${3}"
mkdir -p "$out"
cat > "$out/metadata.yaml" <<META
campaign: "$CAMPAIGN"
traffic_path: "$K6_TRAFFIC_PATH"
load_generator: "$K6_EXECUTION_MODE"
endpoint: "$K6_BASE_URL"
k6_image: "$K6_IMAGE"
connection_reuse: false
protocol_version: "$BENCHMARK_PROTOCOL_VERSION"
rps: $RPS
benchmark_source_sha256: "$BENCHMARK_SOURCE_SHA256"
benchmark_config_sha256: "$BENCHMARK_CONFIG_SHA256"
pre_allocated_vus: $BENCHMARK_PRE_ALLOCATED_VUS
max_vus: $BENCHMARK_MAX_VUS
pattern: $1
controller: $2
repeat: $3
start_time_utc: "2026-09-06T00:00:00Z"
end_time_utc: "2026-09-06T00:10:00Z"
result:
  status: success
  failure_reason: ""
META
printf 'retained raw evidence\\n' > "$out/raw-marker.txt"
''', encoding="utf-8", newline="\n")
            collector.chmod(0o755)
            relative_root = root.relative_to(REPO_ROOT).as_posix()
            result = self.run_bash(f"{relative_root}/hack/run_matrix.sh", extra_env={
                "EXPERIMENTS_ROOT": "runs", "BENCHMARK_PATTERNS": "step",
                "BENCHMARK_CONTROLLERS": "native_hpa_60 phpa", "BENCHMARK_REPEATS": "1",
            })
            self.assertEqual(2, result.returncode, result.stderr + result.stdout)
            self.assertIn("Completed this session: 0", result.stdout)
            attempts = list((root / "runs").iterdir())
            self.assertEqual(1, len(attempts), "Matrix continued after an invalid measurement")
            metadata = yaml.safe_load((attempts[0] / "metadata.yaml").read_text())
            self.assertEqual("failed", metadata["result"]["status"])
            self.assertIn("controlled extraction failed", metadata["result"]["failure_reason"])
            self.assertTrue((attempts[0] / "raw-marker.txt").exists())
            self.assertFalse(json.loads((attempts[0] / "extract.json").read_text())["measurement"]["window_valid"])


@unittest.skipUnless(shutil.which("node"), "node is required for offline k6 module checks")
class BenchmarkWorkloadTests(unittest.TestCase):
    def load_workloads(self, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        # Evaluate the actual ES modules with only k6/http replaced by a stub;
        # no rewriting of the workload source and no network requests are needed.
        loader = r"""
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const context = vm.createContext({__ENV: JSON.parse(process.argv[1])});
const modules = new Map();
const http = new vm.SyntheticModule(['default'], function () {
  this.setExport('default', {get() { throw new Error('Unexpected HTTP request'); }});
}, {context});
function load(filename) {
  if (!modules.has(filename)) modules.set(filename,
    new vm.SourceTextModule(fs.readFileSync(filename, 'utf8'), {context, identifier: filename}));
  return modules.get(filename);
}
(async () => {
  const result = {};
  for (const pattern of ['step', 'ramp', 'spike']) {
    const module = load(path.resolve('hack/k6', pattern + '.js'));
    await module.link((name, parent) => name === 'k6/http' ? http
      : load(path.resolve(path.dirname(parent.identifier), name)));
    await module.evaluate();
    result[pattern] = module.namespace.options;
  }
  process.stdout.write(JSON.stringify(result));
})().catch(error => {process.stderr.write(error.message); process.exitCode = 1;});
"""
        return subprocess.run(
            [shutil.which("node"), "--experimental-vm-modules", "-e", loader, json.dumps(env)],
            cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", timeout=10,
        )

    def test_workloads_use_requested_rps_and_timeout_sized_concurrency(self) -> None:
        for env, rps in (({}, 25), ({"RPS": "1"}, 1), ({"RPS": "1000"}, 1000)):
            with self.subTest(env=env):
                loaded = self.load_workloads(env)
                self.assertEqual(0, loaded.returncode, loaded.stderr)
                for pattern, options in json.loads(loaded.stdout).items():
                    scenario = next(iter(options["scenarios"].values()))
                    self.assertEqual(max(20, rps * 10), scenario["preAllocatedVUs"])
                    self.assertEqual(max(40, rps * 12), scenario["maxVUs"])
                    self.assertEqual(rps, max(stage["target"] for stage in scenario["stages"]))
                    self.assertEqual(
                        {"step": 211, "ramp": 270, "spike": 241}[pattern],
                        sum(int(stage["duration"][:-1]) for stage in scenario["stages"]),
                    )
                    self.assertEqual({"duration": "30s", "target": 0}, scenario["stages"][0])

    def test_workloads_reject_invalid_rps_before_execution(self) -> None:
        for rps in ("", "0", "01", "1.5", "1001", "Infinity", "25\n", " 25", "25;bad"):
            with self.subTest(rps=rps):
                loaded = self.load_workloads({"RPS": rps})
                self.assertEqual(1, loaded.returncode, loaded.stderr)
                self.assertIn("RPS must be an integer from 1 to 1000", loaded.stderr)


if __name__ == "__main__":
    unittest.main()
