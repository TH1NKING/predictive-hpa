from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml


ANALYZE_DIR = Path(__file__).resolve().parent
if str(ANALYZE_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYZE_DIR))

import aggregate  # noqa: E402
import extract as extractor  # noqa: E402


def set_nested(obj: dict[str, Any], path: str, value: Any) -> None:
    """Set a dotted path on a nested dictionary."""
    current = obj
    parts = path.split(".")
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def synthetic_extract(controller: str, repeat: int, metric_value: float) -> dict:
    """Build a complete aggregate input with one value for every shared metric."""
    stabilization_seconds = 300 if controller == "native_hpa_300" else 60
    result: dict[str, Any] = {
        "experiment_id": f"step-{controller}-r{repeat}",
        "pattern": "step",
        "controller": controller,
        "repeat": repeat,
        "campaign": "stabilization-window-ablation-v2",
        "scale_down_stabilization_seconds": stabilization_seconds,
        "prediction_variant": "ewma_damped_cap" if controller == "phpa" else "none",
        "duration_s": 600,
        "git_commit": "abcdef0",
        "warnings": [],
        "phpa": None,
    }
    for _, path, _, _ in aggregate.METRIC_SPECS:
        set_nested(result, path, metric_value)
    if controller == "phpa":
        result["phpa"] = {
            "total_reconciles": 10,
            "scaled_true_count": 2,
            "stabilized_true_count": 1,
            "skip_reasons": {},
        }
    return result


class AggregateReportTests(unittest.TestCase):
    def test_cli_writes_censor_marker_as_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_successful_experiment(
                root,
                "censored-step-native-r1",
                experiment_id="censored-run",
                campaign="stabilization-window-ablation-v2",
                stabilization_seconds=300,
                prediction_variant="none",
            )
            extract_path = root / "censored-step-native-r1" / "extract.json"
            extract = json.loads(extract_path.read_text(encoding="utf-8"))
            extract["controller"] = "native_hpa_300"
            extract["scaling"] = {"scaledown_completed": False}
            extract["resource"] = {"waste_window_s": 100}
            extract["warnings"] = []
            extract_path.write_text(json.dumps(extract), encoding="utf-8")
            env = os.environ.copy()
            env["PYTHONUTF8"] = "0"
            env["PYTHONIOENCODING"] = "utf-8"

            result = subprocess.run(
                [sys.executable, str(ANALYZE_DIR / "aggregate.py"), str(root)],
                env=env,
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
                timeout=10,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            report = (root / "AGGREGATE_REPORT.md").read_text(encoding="utf-8")
            self.assertIn("†", report)

    def test_cli_stdout_emits_utf8_under_non_utf8_locale(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_successful_experiment(
                root,
                "censored-step-native-r1",
                experiment_id="censored-run",
                campaign="stabilization-window-ablation-v2",
                stabilization_seconds=300,
                prediction_variant="none",
            )
            extract_path = root / "censored-step-native-r1" / "extract.json"
            extract = json.loads(extract_path.read_text(encoding="utf-8"))
            extract["controller"] = "native_hpa_300"
            extract["scaling"] = {"scaledown_completed": False}
            extract["resource"] = {"waste_window_s": 100}
            extract["warnings"] = []
            extract_path.write_text(json.dumps(extract), encoding="utf-8")
            env = os.environ.copy()
            env["PYTHONUTF8"] = "0"
            env.pop("PYTHONIOENCODING", None)

            result = subprocess.run(
                [
                    sys.executable,
                    str(ANALYZE_DIR / "aggregate.py"),
                    str(root),
                    "--stdout",
                ],
                env=env,
                capture_output=True,
                check=False,
                timeout=10,
            )

            self.assertEqual(0, result.returncode, result.stderr.decode(errors="replace"))
            self.assertIn("†".encode(), result.stdout)

    def test_report_marks_censored_scaledown_groups_and_effects(self) -> None:
        values = {
            "native_hpa_300": 100.0,
            "native_hpa_60": 60.0,
            "phpa": 40.0,
        }
        extracts = [
            synthetic_extract(controller, 1, values[controller])
            for controller, _ in aggregate.CONTROLLER_COLUMNS
        ]
        for item in extracts:
            item["scaling"]["scaledown_completed"] = True
        extracts[0]["scaling"]["scaledown_completed"] = False

        report = aggregate.render_report(extracts, [])
        waste_line = next(
            line
            for line in report.splitlines()
            if line.startswith("| step | Waste window after k6 stop |")
        )
        first_scaledown_line = next(
            line
            for line in report.splitlines()
            if line.startswith("| First scale-down relative to k6 stop |")
        )

        self.assertIn("100 s (n=1) †", waste_line)
        self.assertIn("-40.0 (-40%) †", waste_line)
        self.assertNotIn("†", first_scaledown_line)
        self.assertIn("**† Censored group**", report)
        self.assertIn("waste-window values are lower bounds", report)

    def test_report_uses_precise_peak_and_scaledown_metric_names(self) -> None:
        extracts = [
            synthetic_extract(controller, 1, 10.0)
            for controller, _ in aggregate.CONTROLLER_COLUMNS
        ]

        report = aggregate.render_report(extracts, [])

        self.assertIn("| Peak replicas |", report)
        self.assertIn("| First scale-down relative to k6 stop |", report)
        self.assertNotIn("Peak / steady-state replicas", report)
        self.assertNotIn("First scale-down (after k6 stop)", report)

    def test_ramp_extract_uses_270_second_schedule_for_post_load_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            experiment_dir = Path(temp_dir)
            metadata = {
                "experiment_id": "ramp-native-60-r1",
                "pattern": "ramp",
                "controller": "native_hpa_60",
                "repeat": 1,
                "start_time_utc": "2026-01-01T00:00:00Z",
                "end_time_utc": "2026-01-01T00:08:20Z",
                "git": {"commit": "abcdef0"},
            }
            prometheus = {
                "replicas": {
                    "data": {
                        "result": [
                            {
                                "values": [
                                    [1767225600, "1"],
                                    [1767225800, "2"],
                                    [1767226000, "1"],
                                    [1767226100, "1"],
                                ]
                            }
                        ]
                    }
                },
                "cpu_pct": {"data": {"result": []}},
                "rps_pkt_rate": {"data": {"result": []}},
            }
            (experiment_dir / "metadata.yaml").write_text(
                yaml.safe_dump(metadata), encoding="utf-8"
            )
            (experiment_dir / "prom.json").write_text(
                json.dumps(prometheus), encoding="utf-8"
            )

            result = extractor.extract(experiment_dir)

            self.assertEqual(
                result["scaling"]["full_scaledown_rel_s_after_k6_stop"], 100
            )
            self.assertEqual(result["resource"]["waste_window_s"], 100)

    def test_extract_prefers_observed_k6_start_over_metadata_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            experiment_dir = Path(temp_dir)
            metadata = {
                "experiment_id": "ramp-native-60-r1",
                "pattern": "ramp",
                "controller": "native_hpa_60",
                "repeat": 1,
                "start_time_utc": "2026-01-01T00:00:00Z",
                "end_time_utc": "2026-01-01T00:08:20Z",
                "git": {"commit": "abcdef0"},
            }
            prometheus = {
                "replicas": {
                    "data": {
                        "result": [
                            {
                                "values": [
                                    [1767225600, "1"],
                                    [1767225800, "2"],
                                    [1767226000, "1"],
                                    [1767226100, "1"],
                                ]
                            }
                        ]
                    }
                },
                "cpu_pct": {"data": {"result": []}},
                "rps_pkt_rate": {"data": {"result": []}},
            }
            k6_point = {
                "type": "Point",
                "metric": "vus",
                "data": {
                    "time": "2026-01-01T00:00:40Z",
                    "value": 0,
                    "tags": {},
                },
            }
            (experiment_dir / "metadata.yaml").write_text(
                yaml.safe_dump(metadata), encoding="utf-8"
            )
            (experiment_dir / "prom.json").write_text(
                json.dumps(prometheus), encoding="utf-8"
            )
            (experiment_dir / "k6.json").write_text(
                json.dumps(k6_point) + "\n", encoding="utf-8"
            )

            result = extractor.extract(experiment_dir)

            self.assertEqual(result["k6"]["observed_start_time_utc"], "2026-01-01T00:00:40Z")
            self.assertEqual(
                result["scaling"]["full_scaledown_rel_s_after_k6_stop"], 90
            )
            self.assertEqual(result["resource"]["waste_window_s"], 90)

    def test_extract_requires_final_sample_at_min_to_complete_scaledown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            experiment_dir = Path(temp_dir)
            metadata = {
                "experiment_id": "ramp-native-300-r2",
                "pattern": "ramp",
                "controller": "native_hpa_300",
                "repeat": 2,
                "start_time_utc": "2026-01-01T00:00:00Z",
                "end_time_utc": "2026-01-01T00:08:20Z",
                "git": {"commit": "abcdef0"},
            }
            prometheus = {
                "replicas": {
                    "data": {
                        "result": [
                            {
                                "values": [
                                    [1767225600, "1"],
                                    [1767225800, "5"],
                                    [1767226000, "4"],
                                    [1767226100, "4"],
                                ]
                            }
                        ]
                    }
                },
                "cpu_pct": {"data": {"result": []}},
                "rps_pkt_rate": {"data": {"result": []}},
            }
            (experiment_dir / "metadata.yaml").write_text(
                yaml.safe_dump(metadata), encoding="utf-8"
            )
            (experiment_dir / "prom.json").write_text(
                json.dumps(prometheus), encoding="utf-8"
            )

            result = extractor.extract(experiment_dir)

            self.assertFalse(result["scaling"]["scaledown_completed"])
            self.assertIsNone(
                result["scaling"]["full_scaledown_rel_s_after_k6_stop"]
            )
            self.assertTrue(
                any(
                    warning.startswith("scaledown_completed=false")
                    for warning in result["warnings"]
                )
            )

    def test_spike_extract_uses_241_second_schedule_for_post_load_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            experiment_dir = Path(temp_dir)
            metadata = {
                "experiment_id": "spike-native-60-r1",
                "pattern": "spike",
                "controller": "native_hpa_60",
                "repeat": 1,
                "start_time_utc": "2026-01-01T00:00:00Z",
                "end_time_utc": "2026-01-01T00:08:20Z",
                "git": {"commit": "abcdef0"},
            }
            prometheus = {
                "replicas": {
                    "data": {
                        "result": [
                            {
                                "values": [
                                    [1767225600, "1"],
                                    [1767225800, "2"],
                                    [1767226000, "1"],
                                    [1767226100, "1"],
                                ]
                            }
                        ]
                    }
                },
                "cpu_pct": {"data": {"result": []}},
                "rps_pkt_rate": {"data": {"result": []}},
            }
            (experiment_dir / "metadata.yaml").write_text(
                yaml.safe_dump(metadata), encoding="utf-8"
            )
            (experiment_dir / "prom.json").write_text(
                json.dumps(prometheus), encoding="utf-8"
            )

            result = extractor.extract(experiment_dir)

            self.assertEqual(
                result["scaling"]["full_scaledown_rel_s_after_k6_stop"], 129
            )
            self.assertEqual(result["resource"]["waste_window_s"], 129)

    def test_three_controller_order_and_both_effects_cover_every_metric(self) -> None:
        values = {
            "native_hpa_300": 100.0,
            "native_hpa_60": 60.0,
            "phpa": 40.0,
        }
        extracts = [
            synthetic_extract(controller, repeat, values[controller])
            for repeat in range(1, 4)
            # Deliberately not report order: the output must use CONTROLLER_COLUMNS.
            for controller in ("phpa", "native_hpa_300", "native_hpa_60")
        ]

        report = aggregate.render_report(extracts, [])

        executive_header = (
            "| Pattern | Metric | Native-300 | Native-60 | PHPA-60 | "
            "Window effect (Native-60 - Native-300) | "
            "Prediction effect (PHPA-60 - Native-60) |"
        )
        per_pattern_header = executive_header.replace("Pattern | ", "")
        self.assertIn(executive_header, report)
        self.assertIn(per_pattern_header, report)
        self.assertIn(
            "*Native-300 runs: 3 | Native-60 runs: 3 | PHPA-60 runs: 3*",
            report,
        )

        per_pattern_section = report.split("## 3. Per-Pattern Comparison", 1)[1]
        per_pattern_section = per_pattern_section.split(
            "**PHPA-specific decision counters:**", 1
        )[0]
        per_pattern_lines = per_pattern_section.splitlines()
        for metric_name, _, _, _ in aggregate.METRIC_SPECS:
            matching_lines = [
                line
                for line in per_pattern_lines
                if line.startswith(f"| {metric_name} |")
            ]
            self.assertEqual(
                len(matching_lines),
                1,
                f"expected one per-pattern row for {metric_name}",
            )
            self.assertTrue(
                matching_lines[0].endswith(
                    "| -40.0 (-40%) | -20.0 (-33%) |"
                ),
                matching_lines[0],
            )

        for stale_phrase in (
            "design intent",
            "smoothing tax",
            "core selling point",
            "Surprise:",
            "scale-down advantage",
            "native_hpa (1:1 design)",
        ):
            self.assertNotIn(stale_phrase, report)

    def test_effects_handle_missing_and_zero_baselines(self) -> None:
        self.assertEqual(aggregate.compute_delta([1.0], [None]), "n/a")
        self.assertEqual(
            aggregate.compute_delta([5.0], [0.0]),
            "+5.0 (baseline ~0)",
        )
        self.assertEqual(
            aggregate.compute_delta([5.0], [-5.0]),
            "+10.0 (negative baseline)",
        )

    def test_load_extracts_uses_only_supplied_root_and_enriches_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            current_root = base / "ablation-v2"
            historical_root = base / "experiments"
            current_root.mkdir()
            historical_root.mkdir()

            self._write_successful_experiment(
                current_root,
                "new-step-phpa-r1",
                experiment_id="new-run",
                campaign="stabilization-window-ablation-v2",
                stabilization_seconds=60,
                prediction_variant="ewma_damped_cap",
            )
            self._write_successful_experiment(
                historical_root,
                "old-step-phpa-r1",
                experiment_id="old-run",
                campaign="historical",
                stabilization_seconds=60,
                prediction_variant="legacy",
            )

            loaded, skipped = aggregate.load_extracts(current_root)

            self.assertEqual(skipped, [])
            self.assertEqual([item["experiment_id"] for item in loaded], ["new-run"])
            self.assertEqual(
                loaded[0]["campaign"], "stabilization-window-ablation-v2"
            )
            self.assertEqual(loaded[0]["scale_down_stabilization_seconds"], 60)
            self.assertEqual(loaded[0]["prediction_variant"], "ewma_damped_cap")

    def test_extract_passes_ablation_metadata_through(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            experiment_dir = Path(temp_dir)
            metadata = {
                "experiment_id": "step-native-60-r1",
                "pattern": "step",
                "controller": "native_hpa_60",
                "repeat": 1,
                "campaign": "stabilization-window-ablation-v2",
                "scale_down_stabilization_seconds": 60,
                "prediction_variant": "none",
                "start_time_utc": "2026-08-26T00:00:00Z",
                "end_time_utc": "2026-08-26T00:10:00Z",
                "git": {"commit": "abcdef0"},
            }
            (experiment_dir / "metadata.yaml").write_text(
                yaml.safe_dump(metadata),
                encoding="utf-8",
            )

            result = extractor.extract(experiment_dir)

            self.assertEqual(
                result["campaign"], "stabilization-window-ablation-v2"
            )
            self.assertEqual(result["scale_down_stabilization_seconds"], 60)
            self.assertEqual(result["prediction_variant"], "none")

    def _write_successful_experiment(
        self,
        root: Path,
        directory_name: str,
        *,
        experiment_id: str,
        campaign: str,
        stabilization_seconds: int,
        prediction_variant: str,
    ) -> None:
        experiment_dir = root / directory_name
        experiment_dir.mkdir()
        metadata = {
            "campaign": campaign,
            "scale_down_stabilization_seconds": stabilization_seconds,
            "prediction_variant": prediction_variant,
            "result": {"status": "success"},
        }
        extract = {
            "experiment_id": experiment_id,
            "pattern": "step",
            "controller": "phpa",
            "repeat": 1,
        }
        (experiment_dir / "metadata.yaml").write_text(
            yaml.safe_dump(metadata),
            encoding="utf-8",
        )
        (experiment_dir / "extract.json").write_text(
            json.dumps(extract),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
