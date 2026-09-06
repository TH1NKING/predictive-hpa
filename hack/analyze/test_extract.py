from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import yaml

ANALYZE_DIR = Path(__file__).resolve().parent
if str(ANALYZE_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYZE_DIR))

import extract as extractor  # noqa: E402


ONSET = 1767226200


def controlled_fixture(directory: Path, preparation_s: int = 600, drain_s: int = 5) -> tuple[dict, list]:
    runner_start = ONSET - 30
    metadata = {
        "experiment_id": "step-native-60-r1",
        "pattern": "step",
        "controller": "native_hpa_60",
        "repeat": 1,
        "campaign": "controlled-test",
        "protocol_version": extractor.CONTROLLED_PROTOCOL,
        "rps": 25,
        "pre_allocated_vus": 100,
        "max_vus": 200,
        "benchmark_source_sha256": "a" * 64,
        "benchmark_config_sha256": "b" * 64,
        "post_load_tail_seconds": 360,
        "load_start_time_unix": ONSET,
        "offered_load_end_time_unix": ONSET + 181,
        "observation_end_time_unix": ONSET + 541,
        "end_time_unix": ONSET + 560,
        "start_time_utc": datetime.fromtimestamp(ONSET - preparation_s, timezone.utc).isoformat(),
        "end_time_utc": datetime.fromtimestamp(ONSET + 560, timezone.utc).isoformat(),
        "scale_down_stabilization_seconds": 60,
        "prediction_variant": "none",
    }
    samples = []
    for offset in range(-preparation_s, 556, 15):
        replicas = 5 if 45 <= offset < 240 else (4 if -90 <= offset < -60 else 1)
        samples.append((ONSET + offset, replicas))
    (directory / "k6-start-time-unix").write_text(str(runner_start), encoding="utf-8")
    (directory / "k6-end-time-unix").write_text(str(runner_start + 211 + drain_s), encoding="utf-8")
    (directory / "metadata.yaml").write_text(yaml.safe_dump(metadata), encoding="utf-8")
    (directory / "prom.json").write_text(json.dumps({
        "replicas": {"data": {"result": [{"values": samples}]}}
    }), encoding="utf-8")
    (directory / "events.yaml").write_text("items: []\n", encoding="utf-8")
    return metadata, samples


class ControlledMeasurementTests(unittest.TestCase):
    def test_preparation_and_runner_drain_do_not_change_comparable_cost(self) -> None:
        results = []
        legacy_costs = []
        for preparation, drain in ((600, 5), (180, 70)):
            with tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                controlled_fixture(directory, preparation, drain)
                extracted = extractor.extract(directory)
                results.append(extracted["measurement"])
                legacy_costs.append(extracted["resource"]["pod_seconds_during_experiment"])
        for result in results:
            self.assertTrue(result["window_valid"])
            self.assertEqual(541, result["window_duration_s"])
            self.assertEqual(1321, result["pod_seconds_load_onset_to_tail_end"])
            self.assertEqual(596, result["pod_seconds_post_load"])
            self.assertEqual(236, result["excess_pod_seconds_post_load"])
            self.assertEqual(59, result["post_load_above_min_s"])
        self.assertNotEqual(*legacy_costs)

    def test_setup_reset_is_excluded_and_first_scale_is_relative_to_load_onset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            controlled_fixture(directory)
            # Earliest k6 Point is delayed from runner start. It must not define
            # the new observation window or include the initial quiet period.
            point = {"type": "Point", "metric": "vus", "data": {
                "time": datetime.fromtimestamp(ONSET - 27, timezone.utc).isoformat(), "value": 0}}
            (directory / "k6.json").write_text(json.dumps(point) + "\n", encoding="utf-8")
            result = extractor.extract(directory)["measurement"]
        self.assertEqual(ONSET - 30, result["runner_start_time_unix"])
        self.assertEqual(45, result["first_scaleup_after_load_onset_s"])
        self.assertEqual(59, result["first_scaledown_after_offered_load_end_s"])
        self.assertEqual(59, result["full_scaledown_after_offered_load_end_s"])
        self.assertEqual([45, 240], [e["after_load_onset_s"] for e in result["events"]])

    def test_missing_runner_timestamp_never_uses_an_estimate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            metadata, samples = controlled_fixture(directory)
            (directory / "k6-start-time-unix").unlink()
            result, warnings = extractor.compute_controlled_measurement(directory, metadata, samples)
        self.assertFalse(result["window_valid"])
        self.assertNotIn("pod_seconds_load_onset_to_tail_end", result)
        self.assertTrue(warnings)

    def test_large_replica_gap_and_missing_boundary_are_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            metadata, samples = controlled_fixture(directory)
            cases = (
                [s for s in samples if s[0] != ONSET + 150],
                [s for s in samples if s[0] <= ONSET + 510],
                [s for s in samples if s[0] > ONSET],
            )
            for incomplete in cases:
                with self.subTest(samples=len(incomplete)):
                    result, warnings = extractor.compute_controlled_measurement(directory, metadata, incomplete)
                    self.assertFalse(result["window_valid"])
                    self.assertNotIn("pod_seconds_load_onset_to_tail_end", result)
                    self.assertTrue(warnings)

    def test_fixed_boundary_is_checked_against_schedule_and_actual_collection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            metadata, samples = controlled_fixture(directory)
            for field, value in (("observation_end_time_unix", ONSET + 550),
                                 ("end_time_unix", ONSET + 530),
                                 ("load_start_time_unix", ONSET + 1)):
                with self.subTest(field=field):
                    result, warnings = extractor.compute_controlled_measurement(directory, {**metadata, field: value}, samples)
                    self.assertFalse(result["window_valid"])
                    self.assertTrue(warnings)

    def test_scale_down_remains_censored_when_last_sample_above_min(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            metadata, samples = controlled_fixture(directory)
            samples = [(ts, max(v, 2) if ts >= ONSET + 45 else v) for ts, v in samples]
            result, warnings = extractor.compute_controlled_measurement(directory, metadata, samples)
        self.assertTrue(result["window_valid"])
        self.assertFalse(result["scaledown_completed"])
        self.assertIsNone(result["full_scaledown_after_offered_load_end_s"])
        self.assertEqual(360, result["post_load_above_min_s"])
        self.assertTrue(warnings)

    def test_http_200_count_is_separate_from_k6_expected_response_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "k6.json"
            points = [{"type": "Point", "metric": "http_req_duration", "data": {
                "value": 10, "tags": {"status": status, "expected_response": expected}}}
                for status, expected in (("200", "true"), ("200", "false"), ("302", "true"), ("503", "false"))]
            path.write_text("\n".join(map(json.dumps, points)), encoding="utf-8")
            result, _ = extractor.load_k6(path)
        self.assertEqual(2, result["successful_requests_http_200"])
        self.assertEqual(50, result["successful_rate_http_200_pct"])


if __name__ == "__main__":
    unittest.main()
