"""Retained metric pipeline evidence through the public, offline analysis CLI."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


CLI = Path(__file__).with_name("metric_pipeline.py")
ONSET = 1788739200


def observation(kind, cycle, evaluation, result=None, **extra):
    return {"kind": kind, "cycle_id": cycle, "evaluation_time_unix": ONSET + evaluation,
            "request_started_at": ONSET + evaluation, "request_finished_at": ONSET + evaluation + 0.2,
            "duration_seconds": 0.2, "status": "success",
            "response": {"status": "success", "data": {"result": result or []}}, **extra}


def scalar(evaluation, value):
    return {"metric": {}, "value": [ONSET + evaluation, str(value)]}


def cpu(samples, **labels):
    return {"metric": {"pod": "old", "container": "app", **labels},
            "values": [[ONSET + stamp, str(value)] for stamp, value in samples]}


class MetricPipelineCLI(unittest.TestCase):
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

    def test_pairs_actual_prometheus_results_at_shared_time_and_excludes_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.fixture(directory, [
                observation("prom_cpu_evaluated_30s", 0, -1, [scalar(-1, 90)]),
                observation("prom_cpu_evaluated", 0, -1, [scalar(-1, 90)]),
                observation("prom_cpu_evaluated_30s", 1, 4, [scalar(4, 55)]),
                observation("prom_cpu_evaluated", 1, 4, [scalar(4, 40)]),
                observation("prom_cpu_evaluated_30s", 2, 8, [scalar(8, 70)]),
                observation("prom_cpu_evaluated", 2, 8, [scalar(8, 50)]),
                observation("prom_cpu_evaluated_30s", 3, 12, [scalar(12, 85)]),
                observation("prom_cpu_evaluated", 3, 12, [scalar(12, 60)]),
                observation("prom_cpu_evaluated", 4, 49.8, [scalar(49.8, 100)]),
            ])
            report = self.report(directory)
            self.assertEqual(50, report["cutoff_seconds"])
            self.assertEqual([1, 2, 3], [row["cycle_id"] for row in report["cycles"]])
            self.assertEqual("matched", report["cycles"][0]["pair_status"])
            self.assertEqual(55, report["cycles"][0]["evaluations"]["30"]["cpu_percent"])
            self.assertEqual(8, report["first_above_threshold"]["30"]["evaluation_seconds"])
            self.assertEqual(12, report["first_above_threshold"]["60"]["evaluation_seconds"])
            self.assertEqual(4, report["first_crossing_60_minus_30_seconds"])
            self.assertIn("not a causal estimate", report["crossing_interpretation"])

    def test_unusable_or_unpaired_evaluations_remain_distinct_from_zero_cpu(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            rows = [observation("prom_cpu_evaluated", cycle, cycle, [scalar(cycle, 0)]) for cycle in range(1, 9)]
            rows += [
                observation("prom_cpu_evaluated_30s", 1, 1),
                observation("prom_cpu_evaluated_30s", 2, 2, status="error", error="timeout"),
                observation("prom_cpu_evaluated_30s", 3, 3, [scalar(3, "NaN")]),
                observation("prom_cpu_evaluated_30s", 4, 4, [scalar(4, 10), scalar(4, 20)]),
                observation("prom_cpu_evaluated_30s", 5, 5, [scalar(5, 0)]),
                observation("prom_cpu_evaluated_30s", 7, 7.5, [scalar(7.5, 95)]),
                observation("prom_cpu_evaluated_30s", 8, 8, [scalar(8, 95)]),
                observation("prom_cpu_evaluated_30s", 8, 8, [scalar(8, 96)]),
            ]
            self.fixture(directory, rows)
            report = self.report(directory)
            self.assertEqual(["empty", "error", "nonfinite", "multiple", "valid", "missing", "valid", "duplicate"],
                             [row["evaluations"]["30"]["status"] for row in report["cycles"]])
            self.assertEqual([None, None, None, None, 0, None, 95, None],
                             [row["evaluations"]["30"]["cpu_percent"] for row in report["cycles"]])
            self.assertEqual(["matched"] * 5 + ["missing", "mismatched", "duplicate"],
                             [row["pair_status"] for row in report["cycles"]])
            self.assertIsNone(report["first_above_threshold"]["30"])
            self.assertIn("evaluation_time_mismatch", report["quality_flags"])
            self.assertEqual(2, len(report["cycles"][-1]["evaluations"]["30"]["observations"]))

    def test_counts_left_open_windows_and_bounds_visibility_from_online_snapshots(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.fixture(directory, [
                observation("prom_cpu_raw", 1, 2, [cpu([(-58, 0), (-28, 1), (1, 2)])]),
                observation("prom_cpu_raw", 2, 4, [cpu([(-28, 1), (1, 2), (1.5, 3)]),
                                                       cpu([(1, "NaN")], pod="other")]),
                observation("prom_requests_raw", 2, 4, [cpu([(1, 0.2)])]),
                observation("prom_cpu_raw", 3, 6, [], status="error", error="timeout"),
                observation("prom_cpu_raw", 4, 8, []),
            ])
            report = self.report(directory)
            self.assertEqual({"30": 1, "60": 2}, report["cycles"][0]["window_samples"][0]["counts"])
            self.assertEqual({"30": 2, "60": 3}, report["cycles"][1]["window_samples"][0]["counts"])
            sample = next(row for row in report["raw_samples"] if row["sample_seconds"] == 1.5)
            self.assertEqual([2, 4.2], [round(v, 1) for v in sample["visibility_interval_seconds"]])
            first = next(row for row in report["raw_samples"] if row["sample_seconds"] == -58)
            self.assertIsNone(first["visibility_interval_seconds"][0])
            self.assertEqual([0.2], report["request_values_cores"])
            self.assertIn("nonfinite_counter", report["quality_flags"])
            self.assertIn("prom_cpu_raw_error", report["quality_flags"])
            self.assertIn("prom_cpu_raw_empty", report["quality_flags"])
            self.assertNotIn("slope_cores", sample)

    def test_source_timestamps_match_raw_samples_without_treating_last_seen_as_cpu_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.fixture(directory, [
                observation("source_cadvisor", 1, 4, response={"lines": [
                    f'container_cpu_usage_seconds_total{{pod="old",container="app"}} 2 {(ONSET + 1) * 1000}',
                    f'container_last_seen{{pod="old",container="app"}} {ONSET + 3} {(ONSET + 3) * 1000}',
                    'container_cpu_usage_seconds_total{pod="old",container="app"} 2',
                    'container_cpu_usage_seconds_total{pod="unknown",container="app"} 9',
                ]}, source_node="node-a"),
                observation("prom_cpu_raw", 1, 4, [cpu([(1, 2), (2, 2)], instance="node-a")]),
            ])
            report = self.report(directory)
            self.assertEqual(["exact_sample_match", "ambiguous_counter_value", "no_matching_raw_sample"],
                             [row["status"] for row in report["source_raw_matches"]])
            source = report["source_samples"]
            self.assertEqual(1, source[0]["explicit_sample_seconds"])
            self.assertEqual(3, source[1]["last_seen_seconds"])
            self.assertIsNone(source[2]["explicit_sample_seconds"])
            self.assertIn("not CPU update time", source[1]["timestamp_interpretation"])
            self.assertEqual(1, report["source_raw_matches"][0]["candidates"][0]["sample_seconds"])
            self.assertIsNone(report["source_raw_matches"][0]["ingestion_time_seconds"])

    def test_deduplicates_reported_scrapes_and_measures_full_observer_overhead(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target = {"labels": {"instance": "node-a"}, "discoveredLabels": {"__meta_kubernetes_node_name": "node-a"},
                      "scrapeUrl": "https://node-a/metrics/cadvisor", "health": "up", "lastScrape": ONSET + 1,
                      "lastScrapeDuration": 0.4, "lastError": ""}
            rows = [{"kind": "gate", "status": "success"}]
            for cycle, evaluation in ((1, 2), (2, 4)):
                rows += [observation("prom_scrape_targets", cycle, evaluation,
                                     response={"status": "success", "data": {"activeTargets": [target]}}),
                         observation("prom_cpu_evaluated", cycle, evaluation, [scalar(evaluation, 0)]),
                         observation("prom_cpu_evaluated_30s", cycle, evaluation, [scalar(evaluation, 0)]),
                         observation("observer_cycle", cycle, evaluation, duration_seconds=2.5,
                                     request_finished_at=ONSET + evaluation + 2.5,
                                     observation_interval_seconds=2, overrun_seconds=0.5)]
            rows.append(observation("prom_scrape_targets", 3, 8,
                        response={"status": "success", "data": {"activeTargets": [
                            {**target, "lastScrape": ONSET + 7, "health": "down", "lastError": "timeout"}]}}))
            self.fixture(directory, rows)
            report = self.report(directory)
            self.assertEqual(2, len(report["scrapes"]))
            self.assertEqual(2, report["scrapes"][0]["observation_count"])
            self.assertEqual(1, report["scrapes"][0]["reported_scrape_seconds"])
            self.assertEqual(0.4, report["scrapes"][0]["reports"][0]["reported_duration_seconds"])
            self.assertEqual("timeout", report["scrapes"][1]["reports"][0]["last_error"])
            self.assertIn("not an ingestion timestamp", report["scrape_interpretation"])
            self.assertEqual(4, report["overhead"]["prometheus_query_count"])
            self.assertEqual(7, report["overhead"]["metric_request_count"])
            self.assertEqual(2, report["overhead"]["overrun_cycle_count"])
            self.assertEqual(5, report["overhead"]["total_cycle_duration_seconds"])
            self.assertIn("scrape_reported_error", report["quality_flags"])

    def test_no_expansion_is_retained_but_never_hides_corrupt_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.fixture(directory, [])
            (directory / "controller.log").write_text("", encoding="utf-8")
            report = self.report(directory)
            self.assertIsNone(report["cutoff_seconds"])
            self.assertEqual([], report["cycles"])
            self.assertIn("missing_successful_expansion", report["quality_flags"])
            (directory / "latency-observations.ndjson").write_text(json.dumps(
                observation("prom_cpu_raw", 1, 2, [cpu([(1, "not-a-number")])])), encoding="utf-8")
            output = directory.parent / (directory.name + "-must-not-exist.json")
            result = self.invoke(directory, "--output", str(output))
            self.assertEqual(2, result.returncode)
            self.assertFalse(output.exists())
            (directory / "latency-observations.ndjson").write_text("broken JSON", encoding="utf-8")
            self.assertEqual(2, self.invoke(directory).returncode)
            (directory / "latency-observations.ndjson").write_text("", encoding="utf-8")
            (directory / "latency-plan.json").unlink()
            self.assertEqual(2, self.invoke(directory).returncode)

    def test_retains_scrape_failures_from_raw_samples_between_target_snapshots(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            up = {"metric": {"__name__": "up", "instance": "node-a", "job": "cadvisor"},
                  "values": [[ONSET + 1, "1"], [ONSET + 3, "0"]]}
            self.fixture(directory, [observation("prom_scrape_raw", 1, 4, [up]),
                                     observation("prom_scrape_raw", 2, 6, [up]),
                                     observation("prom_scrape_raw", 3, 8),
                                     observation("prom_scrape_raw", 4, 10, status="error", error="timeout")])
            report = self.report(directory)
            self.assertEqual(2, len(report["scrape_metric_samples"]))
            self.assertEqual(0, report["scrape_metric_samples"][1]["value"])
            self.assertEqual(["success", "success", "empty", "error"],
                             [row["status"] for row in report["scrape_metric_observations"]])
            self.assertIn("scrape_up_zero", report["quality_flags"])
            self.assertEqual(4, report["overhead"]["prometheus_query_count"])

    def test_new_output_outside_all_inputs_preserves_existing_and_multi_run_reports(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = root / "one", root / "two"
            first.mkdir()
            second.mkdir()
            self.fixture(first, [])
            self.fixture(second, [])
            inside = first / "must-not-exist.json"
            self.assertEqual(2, self.invoke(first, "--output", str(inside)).returncode)
            self.assertFalse(inside.exists())
            output = root / "report.json"
            result = self.invoke(first, str(second), "--output", str(output))
            self.assertEqual(0, result.returncode, result.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(["one", "two"], [row["run"] for row in report["runs"]])
            self.assertEqual(2, self.invoke(first, "--output", str(output)).returncode)
            self.assertEqual(report, json.loads(output.read_text(encoding="utf-8")))

    def test_resets_conflicts_and_nonfinite_samples_cannot_be_hidden_in_source_matches(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.fixture(directory, [
                observation("prom_cpu_raw", 1, 4, [cpu([(-5, 10), (1, 2), (2, 4), (3, "NaN")])]),
                observation("prom_cpu_raw", 2, 6, [cpu([(2, 5)])]),
                observation("source_cadvisor", 2, 6, response={"lines": [
                    f'container_cpu_usage_seconds_total{{pod="old",container="app"}} 2 {(ONSET + 1) * 1000}',
                    f'container_cpu_usage_seconds_total{{pod="old",container="app"}} 4 {(ONSET + 2) * 1000}',
                    f'container_cpu_usage_seconds_total{{pod="old",container="app"}} NaN {(ONSET + 3) * 1000}',
                ]}, source_node="node-a"),
            ])
            report = self.report(directory)
            self.assertIn("counter_reset", report["quality_flags"])
            self.assertIn("conflicting_sample_values", report["quality_flags"])
            self.assertIn("nonfinite_counter", report["quality_flags"])
            self.assertTrue(report["source_raw_matches"][0]["candidates"][0]["counter_reset_from_previous"])
            self.assertEqual([], report["source_raw_matches"][1]["candidates"])
            self.assertEqual([], report["source_raw_matches"][2]["candidates"])


if __name__ == "__main__":
    unittest.main()
