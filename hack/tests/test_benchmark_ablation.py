from __future__ import annotations

import os
import re
import shutil
import subprocess
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
        for script in ("hack/run_benchmark.sh", "hack/run_matrix.sh"):
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
            self.assertIn("Summary: 1 already done, 26 pending.", resumed.stdout)

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


if __name__ == "__main__":
    unittest.main()
