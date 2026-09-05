from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

import test_benchmark_ablation as benchmark_tests


REPO_ROOT = benchmark_tests.REPO_ROOT


@unittest.skipUnless(benchmark_tests.find_bash(), "bash is required for calibration checks")
class CalibrationScriptTests(unittest.TestCase):
    bash = benchmark_tests.find_bash()
    run_bash = benchmark_tests.BenchmarkScriptTests.run_bash

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix=".calibration-test-", dir=REPO_ROOT)
        self.addCleanup(temporary.cleanup)
        self.test_root = Path(temporary.name)
        self.stub_dir = self.test_root / "bin"
        self.stub_dir.mkdir()
        self.trace = self.test_root / "external-calls.txt"
        self.output_root = self.test_root / "calibration-output"
        for name in ("kubectl", "kind", "curl", "k6", "docker", "make"):
            stub = self.stub_dir / name
            stub.write_text(
                '#!/usr/bin/env bash\n'
                'printf "%s\\n" "$0 $*" >> "$K6_TEST_TRACE"\n'
                'echo "Unexpected external command during offline test" >&2\n'
                'exit 99\n',
                encoding="utf-8",
                newline="\n",
            )
            stub.chmod(0o755)

    def invoke(self, *arguments: str, clear_defaults: bool = False, **environment: str):
        command = (
            'set -euo pipefail\n'
            'export PATH="$PWD/$K6_TEST_BIN:$PATH"\n'
        )
        if clear_defaults:
            command += "unset PROBE_REPLICAS PROBE_RPS_LIST PROBE_DURATION_SECONDS\n"
        command += 'bash hack/run_calibration.sh "$@"'
        return self.run_bash(
            "-c", command, "calibration-test", *arguments,
            extra_env={
                "K6_TEST_BIN": self.stub_dir.relative_to(REPO_ROOT).as_posix(),
                "K6_TEST_TRACE": self.trace.relative_to(REPO_ROOT).as_posix(),
                "CALIBRATION_ROOT": self.output_root.relative_to(REPO_ROOT).as_posix(),
                "BENCHMARK_CONTEXT": "kind-offline-calibration-test",
                **environment,
            },
        )

    def assert_offline(self) -> None:
        self.assertFalse(
            self.trace.exists(),
            "Calibration contacted an external command: "
            + (self.trace.read_text(encoding="utf-8") if self.trace.exists() else ""),
        )
        self.assertFalse(self.output_root.exists(), "Offline validation created an output directory")

    def test_default_dry_run_lists_nine_probes_without_cluster_or_output(self) -> None:
        result = self.invoke("--dry-run", clear_defaults=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("http://php-apache.default.svc:80", result.stdout)
        plan = re.findall(r"^replicas=(\d+) rps=(\d+) duration=(\d+)s$", result.stdout, re.MULTILINE)
        self.assertEqual(
            [(str(replicas), str(rps), "90") for replicas in (1, 5, 10) for rps in (1, 3, 5)],
            plan,
        )
        self.assert_offline()

    def test_custom_dry_run_preserves_requested_order_and_bounds(self) -> None:
        result = self.invoke(
            "--dry-run", PROBE_REPLICAS="10 1", PROBE_RPS_LIST="1000 2",
            PROBE_DURATION_SECONDS="600",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        plan = re.findall(r"^replicas=(\d+) rps=(\d+) duration=(\d+)s$", result.stdout, re.MULTILINE)
        self.assertEqual(
            [("10", "1000", "600"), ("10", "2", "600"),
             ("1", "1000", "600"), ("1", "2", "600")],
            plan,
        )
        self.assert_offline()

    def test_invalid_flags_are_rejected_before_live_path(self) -> None:
        for arguments in (("--live",), ("--dryrun",), ("--dry-run", "extra")):
            with self.subTest(arguments=arguments):
                result = self.invoke(*arguments)
                self.assertEqual(1, result.returncode, result.stderr)
                self.assertIn("Usage:", result.stderr)
                self.assert_offline()

    def test_invalid_durations_are_rejected_before_live_path(self) -> None:
        for duration in ("0", "59", "601", "90.5", "-90", "090", "abc", "18446744073709551676"):
            with self.subTest(duration=duration):
                result = self.invoke(PROBE_DURATION_SECONDS=duration)
                self.assertEqual(1, result.returncode, result.stderr)
                self.assertIn("Probe duration", result.stderr)
                self.assert_offline()

    def test_invalid_replica_lists_are_rejected_before_live_path(self) -> None:
        for replicas in (" ", "0", "11", "1,5,10", "1 five", "1 -2", "1\n5", "1\r5"):
            with self.subTest(replicas=replicas):
                result = self.invoke(PROBE_REPLICAS=replicas)
                self.assertEqual(1, result.returncode, result.stderr)
                self.assertIn("ERROR:", result.stderr)
                self.assert_offline()

    def test_invalid_rps_lists_are_rejected_before_live_path(self) -> None:
        for rps in (
            " ", "0", "1001", "1,3,5", "1 five", "1 -2", "1 1.5", "1\n3", "1\r3",
            "18446744073709551617",
        ):
            with self.subTest(rps=rps):
                result = self.invoke(PROBE_RPS_LIST=rps)
                self.assertEqual(1, result.returncode, result.stderr)
                self.assertIn("ERROR:", result.stderr)
                self.assert_offline()


if __name__ == "__main__":
    unittest.main()
