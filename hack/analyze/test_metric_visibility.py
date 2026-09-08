"""Metric visibility behavior through the public, offline CLI."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


CLI = Path(__file__).with_name("metric_visibility.py")
ONSET = 1788739200


def observation(kind, start, finish, result, **extra):
    return {"kind": kind, "status": "success", "request_started_at": ONSET + start,
            "request_finished_at": ONSET + finish, "evaluation_time_unix": ONSET + start,
            "response": {"status": "success", "data": {"result": result}}, **extra}


def cpu(samples, pod="old"):
    return {"metric": {"pod": pod, "container": "app"},
            "values": [[ONSET + stamp, str(value)] for stamp, value in samples]}


class MetricVisibilityCLI(unittest.TestCase):

    def test_missing_expansion_does_not_hide_unreadable_or_corrupt_inputs(self):
        for broken_input in ("missing_plan", "invalid_observations"):
            with self.subTest(broken_input=broken_input), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary) / "input"
                directory.mkdir()
                self.fixture(directory, [])
                (directory / "controller.log").write_text("", encoding="utf-8")
                if broken_input == "missing_plan":
                    (directory / "latency-plan.json").unlink()
                else:
                    (directory / "latency-observations.ndjson").write_text("broken JSON", encoding="utf-8")
                output = directory.parent / "must-not-exist.json"
                result = self.invoke(directory, "--output", str(output))
                self.assertEqual(2, result.returncode)
                self.assertFalse(output.exists())

    def fixture(self, directory, rows):
        (directory / "k6.json").write_text(json.dumps({"type": "Point", "metric": "latency_request_attempt",
            "data": {"time": ONSET + 0.25, "value": (ONSET - 30) * 1000}}), encoding="utf-8")
        (directory / "latency-plan.json").write_text(json.dumps({"source_range_seconds": 90}), encoding="utf-8")
        (directory / "controller.log").write_text(json.dumps({"msg": "Scaled Deployment",
            "reconcileID": "first", "reconcileStartedAt": ONSET + 49,
            "previousDesiredReplicas": 1, "currentReplicas": 1, "finalDesired": 2,
            "scaleWriteStartedAt": ONSET + 50, "scaleWriteFinishedAt": ONSET + 50.2}), encoding="utf-8")
        (directory / "latency-observations.ndjson").write_text(
            "\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    def invoke(self, directory, *extra):
        return subprocess.run([sys.executable, str(CLI), "--batch", "fixture", str(directory), *extra],
                              capture_output=True, text=True, timeout=20)

    def report(self, directory):
        result = self.invoke(directory)
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)["runs"][0]

    def test_sample_visibility_and_recent_increment_remain_distinct_from_expression(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.fixture(directory, [
                observation("prom_cpu_raw", 2, 3, [cpu([(-10, 10)])]),
                observation("prom_cpu_raw", 4, 4.2, [cpu([(-10, 10), (1, 12.2)])]),
                observation("prom_cpu_evaluated", 4, 4.3, [{"metric": {}, "value": [ONSET + 4, "47"]}]),
                observation("prom_cpu_evaluated", 8, 8.2, [{"metric": {}, "value": [ONSET + 8, "80"]}]),
                observation("prom_requests_raw", 4, 4.1, [cpu([(1, 0.2)])]),
                observation("prom_cpu_raw", 49.9, 50.1, [cpu([(-10, 10), (1, 12.2), (49, 100)])]),
            ])
            report = self.report(directory)
            self.assertEqual(50, report["cutoff_seconds"])
            self.assertEqual(1, len(report["pairs"]))
            pair = report["pairs"][0]
            self.assertEqual(11, pair["sample_gap_seconds"])
            self.assertAlmostEqual(0.2, pair["slope_cores"])
            self.assertEqual([2, 4.2], [round(x, 1) for x in pair["right_visibility_interval_seconds"]])
            self.assertEqual([0.2], report["request_values_cores"])
            self.assertEqual(80, report["first_above_threshold"]["cpu_percent"])
            self.assertEqual(47, report["evaluations"][0]["cpu_percent"])

    def test_unknown_visibility_and_left_open_window_counts_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.fixture(directory, [observation("prom_cpu_raw", 2, 2.2,
                                                 [cpu([(-58, 0), (-28, 1), (1, 2)])])])
            report = self.report(directory)
            self.assertIsNone(report["pairs"][0]["right_visibility_interval_seconds"][0])
            self.assertEqual({"30": 1, "60": 2}, report["window_samples"][0]["counts"])

    def test_counter_reset_is_not_reported_as_a_load_slope(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.fixture(directory, [observation("prom_cpu_raw", 4, 4.2, [cpu([(-10, 10), (1, 2)])])])
            report = self.report(directory)
            self.assertIsNone(report["pairs"][0]["slope_cores"])
            self.assertEqual("counter_reset", report["pairs"][0]["status"])
            self.assertIn("counter_reset", report["quality_flags"])

    def test_conflicting_counter_values_invalidate_only_the_same_labelled_series(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.fixture(directory, [
                observation("prom_cpu_raw", 4, 4.2, [cpu([(-10, 10), (1, 12)]), cpu([(-10, 20), (1, 24)], "other")]),
                observation("prom_cpu_raw", 6, 6.2, [cpu([(-10, 10), (1, 13)])]),
            ])
            report = self.report(directory)
            old = next(pair for pair in report["pairs"] if pair["labels"]["pod"] == "old")
            other = next(pair for pair in report["pairs"] if pair["labels"]["pod"] == "other")
            self.assertIsNone(old["slope_cores"])
            self.assertEqual("conflicting_sample_values", old["status"])
            self.assertEqual("valid", other["status"])
            self.assertIn("conflicting_sample_values", report["quality_flags"])

    def test_unusable_expression_results_are_kept_distinct_from_zero_cpu(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            scalar = lambda at, value: {"metric": {}, "value": [ONSET + at, value]}
            self.fixture(directory, [
                observation("prom_cpu_evaluated", 1, 1.1, []),
                observation("prom_cpu_evaluated", 2, 2.1, [], status="error", error="timeout"),
                observation("prom_cpu_evaluated", 3, 3.1, [scalar(3, "NaN")]),
                observation("prom_cpu_evaluated", 4, 4.1, [scalar(4, "10"), scalar(4, "20")]),
                observation("prom_cpu_evaluated", 5, 5.1, [scalar(5, "0")]),
            ])
            report = self.report(directory)
            self.assertEqual(["empty", "error", "nonfinite", "multiple_series", "valid"],
                             [row["status"] for row in report["evaluations"]])
            self.assertEqual([None, None, None, None, 0], [row["cpu_percent"] for row in report["evaluations"]])
            self.assertIsNone(report["first_above_threshold"])

    def test_explicit_output_keeps_all_inputs_and_refuses_input_or_existing_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = root / "one", root / "two"
            first.mkdir()
            second.mkdir()
            self.fixture(first, [])
            self.fixture(second, [])
            output = root / "report.json"
            result = self.invoke(first, str(second), "--output", str(output))
            self.assertEqual(0, result.returncode, result.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual([("fixture", "one"), ("fixture", "two")],
                             [(run["batch"], run["run"]) for run in report["runs"]])
            self.assertNotEqual(0, self.invoke(first, "--output", str(first / "new.json")).returncode)
            self.assertFalse((first / "new.json").exists())
            self.assertNotEqual(0, self.invoke(first, "--output", str(output)).returncode)
            self.assertEqual(2, len(json.loads(output.read_text(encoding="utf-8"))["runs"]))

    def test_run_without_a_true_scale_increase_is_retained_without_invented_cutoff(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.fixture(directory, [observation("prom_cpu_raw", 4, 4.2, [cpu([(-10, 10), (1, 12)])])])
            log = json.loads((directory / "controller.log").read_text(encoding="utf-8"))
            log["previousDesiredReplicas"] = 3  # currentReplicas=1 is not the old Scale target.
            (directory / "controller.log").write_text(json.dumps(log), encoding="utf-8")
            report = self.report(directory)
            self.assertIsNone(report["cutoff_seconds"])
            self.assertEqual([], report["pairs"])
            self.assertIn("missing_successful_expansion", report["quality_flags"])

    def test_invalid_source_values_and_failed_queries_are_not_silently_converted_to_load(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.fixture(directory, [
                observation("prom_cpu_raw", 2, 2.2, [], status="error", error="timeout"),
                observation("prom_cpu_raw", 4, 4.2, [cpu([(-10, 10), (1, "NaN")])]),
                observation("prom_requests_raw", 4, 4.1, [cpu([(1, "+Inf")])]),
            ])
            report = self.report(directory)
            self.assertIsNone(report["pairs"][0]["slope_cores"])
            self.assertIsNone(report["pairs"][0]["right_visibility_interval_seconds"][0])
            self.assertEqual([], report["request_values_cores"])
            self.assertEqual(["nonfinite_counter", "nonfinite_request", "prom_cpu_raw_error"], report["quality_flags"])

    def test_controller_query_boundaries_are_separate_from_observer_and_scale(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.fixture(directory, [])
            query = {"msg": "Queried CPU utilization", "reconcileID": "first", "reconcileStartedAt": ONSET + 49,
                     "queryStartedAt": ONSET + 49.1, "queryFinishedAt": ONSET + 49.2, "queryError": ""}
            decision = {"msg": "Evaluated PredictiveHPA scaling decision", "reconcileID": "first",
                        "reconcileStartedAt": ONSET + 49, "currentCPU%": 90}
            with (directory / "controller.log").open("a", encoding="utf-8") as stream:
                stream.write("\n" + json.dumps(query) + "\n" + json.dumps(decision))
            report = self.report(directory)
            self.assertEqual([49.1, 49.2], [round(v, 1) for v in report["controller_queries"][0]["query_interval_seconds"]])
            self.assertEqual(90, report["controller_queries"][0]["cpu_percent"])
            self.assertEqual(50.2, round(report["scale_response_seconds"], 1))

    def test_conflicting_samples_inside_one_response_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.fixture(directory, [observation("prom_cpu_raw", 4, 4.2,
                                                 [cpu([(-10, 10), (1, 12), (1, 13)])])])
            report = self.report(directory)
            self.assertEqual("conflicting_sample_values", report["pairs"][0]["status"])
            self.assertIsNone(report["pairs"][0]["slope_cores"])

    def test_successful_empty_raw_queries_remain_explicit_missing_observations(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.fixture(directory, [
                observation("prom_cpu_raw", 4, 4.2, []),
                observation("prom_requests_raw", 4.1, 4.3, []),
            ])
            report = self.report(directory)
            self.assertEqual(["prom_cpu_raw_empty", "prom_requests_raw_empty"], report["quality_flags"])
            self.assertEqual(["prom_cpu_raw", "prom_requests_raw"],
                             [row["kind"] for row in report["empty_raw_observations"]])
            self.assertEqual([4, 4.2], [round(value, 1) for value in
                             report["empty_raw_observations"][0]["observation_interval_seconds"]])
            self.assertEqual([], report["window_samples"])


if __name__ == "__main__":
    unittest.main()
