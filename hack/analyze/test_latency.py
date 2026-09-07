"""Timing evidence regressions through the standalone diagnostic analysis CLI."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone


ANALYZER = Path(__file__).with_name("latency.py")
ONSET = 1788739200.0


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def observation(kind: str, start: float, end: float, result: list) -> dict:
    return {"kind": kind, "request_started_at": start, "request_finished_at": end,
            "status": "success", "evaluation_time_unix": start,
            "response": {"status": "success", "data": {"result": result}}}


def stamp(offset: float) -> str:
    return datetime.fromtimestamp(ONSET + offset, timezone.utc).isoformat()


def log_record(message: str, cycle: float, **fields: object) -> str:
    return f"{stamp(cycle)}\tINFO\t{message}\t" + json.dumps({
        "reconcileID": f"cycle-{cycle}", "reconcileStartedAt": stamp(cycle), **fields})


class LatencyCLI(unittest.TestCase):
    def fixture(self, directory: Path) -> None:
        write_json(directory / "metadata.yaml", {"experiment_id": "phase-test", "decision_mode": "Current"})
        write_json(directory / "latency-plan.json", {"requested_offset_seconds": 10,
                   "requeue_seconds": 30, "phase_tolerance_seconds": 2})
        (directory / "controller.log").write_text("", encoding="utf-8")
        (directory / "k6.json").write_text(json.dumps({"type": "Point", "metric": "latency_request_attempt",
            "data": {"time": "2026-09-07T00:00:00.250Z", "value": (ONSET - 30) * 1000}}) + "\n", encoding="utf-8")

    def analyze(self, directory: Path) -> dict:
        result = subprocess.run([sys.executable, str(ANALYZER), str(directory)],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads((directory / "latency.json").read_text(encoding="utf-8"))

    def test_scrape_time_evaluation_time_and_visibility_interval_remain_distinct(self) -> None:
        # A scrape at +1 is absent in a query at +2, then first visible in the
        # query at +4..+4.2. A evaluated CPU result at +6 is a different clock.
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.fixture(directory)
            label = {"pod": "php-apache-old", "container": "php-apache"}
            records = [
                observation("prom_cpu_raw", ONSET + 2, ONSET + 2.2,
                            [{"metric": label, "values": [[ONSET - 14, "8"]]}]),
                observation("prom_cpu_raw", ONSET + 4, ONSET + 4.2,
                            [{"metric": label, "values": [[ONSET - 14, "8"], [ONSET + 1, "9"]]}]),
                observation("prom_cpu_evaluated", ONSET + 6, ONSET + 6.2,
                            [{"metric": {}, "value": [ONSET + 6, "63"]}]),
            ]
            (directory / "latency-observations.ndjson").write_text(
                "\n".join(json.dumps(row) for row in records), encoding="utf-8")
            report = self.analyze(directory)
            self.assertEqual(ONSET, report["timing"]["load_onset_unix"])
            self.assertEqual(0.25, report["timing"]["first_request_attempt_seconds"])
            first = report["source_visibility"]["first_post_onset_cpu_sample"]
            self.assertEqual(1, first["sample_seconds"])
            self.assertEqual([2, 4.2], [round(v, 1) for v in first["visibility_interval_seconds"]])
            signal = report["source_visibility"]["first_above_threshold_cpu_observation"]
            self.assertEqual(6, signal["evaluation_seconds"])
            self.assertEqual(63, signal["cpu_percent"])
            self.assertEqual(55, report["source_visibility"]["threshold_cpu_percent"])

    def test_successful_target_increase_is_distinct_from_observed_replica_lag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.fixture(directory)
            (directory / "latency-observations.ndjson").write_text("", encoding="utf-8")
            logs = [
                log_record("Evaluated PredictiveHPA scaling decision", -36, decisionAt=stamp(-35.5),
                           currentReplicas=1, finalDesired=1, **{"currentCPU%": 2}),
                log_record("Finished PredictiveHPA reconciliation", -36,
                           reconcileFinishedAt=stamp(-35), reconcileError="", requeueAfterSeconds=30),
                log_record("Scaled Deployment", 3, previousDesiredReplicas=3, currentReplicas=1,
                           finalDesired=2, scaleWriteStartedAt=stamp(3.1), scaleWriteFinishedAt=stamp(3.2)),
                log_record("Queried CPU utilization", 12, queryStartedAt=stamp(12.1),
                           queryFinishedAt=stamp(12.2), latestEvaluationAt=stamp(12.1), samples=21),
                log_record("Evaluated PredictiveHPA scaling decision", 12, decisionAt=stamp(13),
                           currentReplicas=2, finalDesired=3, skipReason="", **{"currentCPU%": 63}),
                log_record("Scaled Deployment", 12, previousDesiredReplicas=2, currentReplicas=1,
                           finalDesired=3, scaleWriteStartedAt=stamp(13.1), scaleWriteFinishedAt=stamp(14)),
                log_record("Finished PredictiveHPA reconciliation", 12,
                           reconcileFinishedAt=stamp(15), reconcileError="status update conflict", requeueAfterSeconds=0),
            ]
            (directory / "controller.log").write_text("\n".join(logs), encoding="utf-8")
            report = self.analyze(directory)
            self.assertEqual(14, report["timing"]["first_scale_write_seconds"])
            self.assertEqual(13, report["timing"]["first_expansion_decision_seconds"])
            self.assertAlmostEqual(12.2, report["timing"]["first_controller_above_threshold_query_seconds"], places=5)
            self.assertEqual(35, report["phase"]["actual_reconcile_gap_seconds"])
            self.assertFalse(report["phase"]["within_tolerance"])
            self.assertIn("reconcile_gap_exceeds_nominal_interval", report["quality"]["flags"])
            self.assertIsNone(report["timing"]["first_new_pod_ready_seconds"])
            self.assertIsNone(report["timing"]["first_new_pod_request_seconds"])
            self.assertIn("missing_new_pod_ready", report["quality"]["flags"])
            self.assertEqual("status update conflict", report["reconciliations"][-1]["finish"]["reconcileError"])

    def test_new_pod_ready_endpoint_eligibility_and_tagged_request_are_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.fixture(directory)
            def api(kind: str, start: float, items: list) -> dict:
                return {"kind": kind, "request_started_at": ONSET + start,
                        "request_finished_at": ONSET + start + 0.2, "status": "success", "response": {"items": items}}
            old = {"metadata": {"name": "php-apache-old", "uid": "old", "creationTimestamp": stamp(-100)},
                   "status": {"conditions": [{"type": "Ready", "status": "True", "lastTransitionTime": stamp(-90)}]}}
            new = {"metadata": {"name": "php-apache-new", "uid": "new", "creationTimestamp": stamp(3)},
                   "status": {"conditions": [{"type": "Ready", "status": "True", "lastTransitionTime": stamp(8)}]}}
            rows = [api("pods", -1, [old]), api("pods", 9, [old, new]), api("endpoints", 8, []),
                    api("endpoints", 10, [{"endpoints": [{"conditions": {"ready": True},
                        "targetRef": {"uid": "new", "name": "php-apache-new"}, "addresses": ["10.0.0.2"]}]}]),
                    {"kind": "prom_cpu_raw", "request_started_at": ONSET + 12,
                     "request_finished_at": ONSET + 13, "status": "error", "error": "read timed out"}]
            (directory / "latency-observations.ndjson").write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
            access = directory / "workload-access"
            access.mkdir()
            (access / "php-apache-new_new.log").write_text(
                f'{stamp(9.1)} 10.0.0.1 - - [07/Sep/2026:00:00:09 +0000] "GET / HTTP/1.1" 200 99 "-" "other-run"\n'
                f'{stamp(11.7)} 10.0.0.1 - - [07/Sep/2026:00:00:10 +0000] "GET / HTTP/1.1" 200 99 "-" "phpa-benchmark/phase-test"\n',
                encoding="utf-8")
            report = self.analyze(directory)
            pod = report["new_pods"][0]
            self.assertEqual(3, pod["created_seconds"])
            self.assertEqual(8, pod["ready_seconds"])
            self.assertEqual([8, 10.2], [round(v, 1) for v in pod["endpoint_ready_interval_seconds"]])
            self.assertEqual(10, pod["first_request_received_seconds"])
            self.assertAlmostEqual(11.7, pod["first_access_log_emitted_seconds"], places=5)
            self.assertEqual(8, report["timing"]["first_new_pod_ready_seconds"])
            self.assertEqual(10, report["timing"]["first_new_pod_request_seconds"])
            self.assertEqual(1, report["quality"]["observation_error_count"])
            self.assertIn("observer_request_errors", report["quality"]["flags"])

    def test_exact_scenario_window_occupancy_requires_full_replica_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.fixture(directory)
            (directory / "latency-observations.ndjson").write_text("", encoding="utf-8")
            write_json(directory / "metadata.yaml", {"experiment_id": "phase-test", "decision_mode": "Current",
                                                       "load_start_time_unix": ONSET - 1})
            values = [[ONSET + offset, "2" if 10 <= offset < 40 else "1"] for offset in range(-5, 552, 15)]
            write_json(directory / "prom.json", {"replicas": {"data": {"result": [{"values": values}]}}})
            complete = self.analyze(directory)["scenario_window_occupancy"]
            # One Pod for 541s plus a second Pod for [10,40) = 571 Pod-seconds.
            self.assertTrue(complete["window_valid"])
            self.assertEqual(541, complete["duration_seconds"])
            self.assertEqual(571, complete["pod_seconds"])
            self.assertEqual(360, complete["post_load_pod_seconds"])
            self.assertEqual(1, complete["onset_offset_from_legacy_window_seconds"])
            write_json(directory / "prom.json", {"replicas": {"data": {"result": [{"values": values[:2] + values[3:]}]}}})
            incomplete = self.analyze(directory)["scenario_window_occupancy"]
            self.assertFalse(incomplete["window_valid"])
            self.assertIsNone(incomplete["pod_seconds"])
            self.assertIn("Replica coverage gap", incomplete["error"])

    def test_gate_receipts_share_the_observation_stream_without_becoming_metric_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.fixture(directory)
            (directory / "latency-observations.ndjson").write_text(json.dumps({
                "kind": "gate_release", "request_started_at": ONSET - 31,
                "request_finished_at": ONSET - 30, "status": "success", "response": "released\n1788739170\n"}),
                encoding="utf-8")
            report = self.analyze(directory)
            self.assertIsNone(report["source_visibility"]["first_post_onset_cpu_sample"])

    def test_controller_freshness_uses_only_observer_responses_completed_before_its_query(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.fixture(directory)
            label = {"pod": "php-apache-old", "container": "php-apache"}
            rows = [observation("prom_cpu_raw", ONSET + 1, ONSET + 1.2,
                                [{"metric": label, "values": [[ONSET - 3, "8"]]}]),
                    observation("prom_cpu_raw", ONSET + 2, ONSET + 4,
                                [{"metric": label, "values": [[ONSET + 1, "9"]]}])]
            (directory / "latency-observations.ndjson").write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
            (directory / "controller.log").write_text(log_record("Queried CPU utilization", 3,
                queryStartedAt=stamp(3), queryFinishedAt=stamp(3.5), latestEvaluationAt=stamp(3)), encoding="utf-8")
            report = self.analyze(directory)
            evidence = report["reconciliations"][0]["source_before_query"]
            self.assertEqual(6, evidence["cpu"][0]["sample_age_at_query_seconds"])
            self.assertEqual(ONSET - 3, evidence["cpu"][0]["sample_unix"])
            self.assertEqual(1, evidence["overlapping_observer_query_count"])

    def test_schedule_only_keeps_full_tail_when_k6_initialization_takes_forty_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            # The process launched 40s before the scenario; no Prometheus, Pod,
            # controller or metadata artifacts exist yet at this public boundary.
            (directory / "k6-start-time-unix").write_text(str(int(ONSET - 70)), encoding="utf-8")
            (directory / "k6.json").write_text(json.dumps({"type": "Point", "metric": "latency_request_attempt",
                "data": {"time": stamp(0.25), "value": (ONSET - 30) * 1000}}) + "\n", encoding="utf-8")
            result = subprocess.run([sys.executable, str(ANALYZER), str(directory), "--schedule-only"],
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(0, result.returncode, result.stderr)
            schedule = json.loads((directory / "latency-schedule.json").read_text(encoding="utf-8"))
            self.assertEqual(ONSET, schedule["load_onset_unix"])
            self.assertEqual(ONSET + 181, schedule["offered_load_end_unix"])
            self.assertEqual(ONSET + 541, schedule["observation_end_unix"])

    def test_fifteen_second_cadence_reports_its_phase_without_wrapping_a_long_gap(self) -> None:
        for gap, matches in ((10.5, True), (25, False)):
            with self.subTest(gap=gap), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                self.fixture(directory)
                write_json(directory / "latency-plan.json", {"requested_offset_seconds": 10,
                           "requeue_seconds": 15, "phase_tolerance_seconds": 2})
                (directory / "latency-observations.ndjson").write_text("", encoding="utf-8")
                (directory / "controller.log").write_text("\n".join([
                    log_record("Evaluated PredictiveHPA scaling decision", -gap-1,
                               decisionAt=stamp(-gap-.5), currentReplicas=1, finalDesired=1, **{"currentCPU%": 2}),
                    log_record("Finished PredictiveHPA reconciliation", -gap-1,
                               reconcileFinishedAt=stamp(-gap), reconcileError="", requeueAfterSeconds=15),
                ]), encoding="utf-8")
                report = self.analyze(directory)
                self.assertEqual(15, report["phase"]["requeue_seconds"])
                self.assertEqual(gap, report["phase"]["actual_reconcile_gap_seconds"])
                self.assertEqual(matches, report["phase"]["within_tolerance"])
                if not matches:
                    self.assertIsNone(report["phase"]["phase_error_seconds"])
                    self.assertIn("reconcile_gap_exceeds_nominal_interval", report["quality"]["flags"])


if __name__ == "__main__":
    unittest.main()
