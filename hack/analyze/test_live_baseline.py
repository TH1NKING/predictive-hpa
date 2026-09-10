"""Hand-worked evidence checked through the public live baseline analysis CLI."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ANALYZER = Path(__file__).with_name("live_baseline.py")
ONSET = 1788998400.0


def stamp(offset: float) -> str:
    return datetime.fromtimestamp(ONSET + offset, timezone.utc).isoformat()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def write_rows(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(map(json.dumps, rows)) + "\n", encoding="utf-8")


def cycle(start: float, *, desired: int = 1, previous: int | None = None) -> dict:
    common = {"reconcileStartedAt": stamp(start)}
    row = {**common,
        "query": {**common, "msg": "Queried CPU utilization", "queryError": "", "samples": 6,
            "latestEvaluationAt": stamp(start), "sourceTimestamp": stamp(start - 5),
            "observationFinishedAt": stamp(start + 0.5)},
        "decision": {**common, "msg": "Evaluated PredictiveHPA scaling decision", "decisionMode": "Current",
            "decisionAt": stamp(start + 0.6), "stabilizationEvaluatedAt": stamp(start),
            "coldStartProtection": False, "coldStartProtectedUntil": stamp(-100),
            "currentReplicas": 1, "currentCPU%": 2, "finalDesired": desired, "samples": 6},
        "finish": {**common, "msg": "Finished PredictiveHPA reconciliation",
            "reconcileFinishedAt": stamp(start + 1), "reconcileError": ""}}
    if previous is not None:
        row["scale"] = {**common, "msg": "Scaled Deployment", "decisionMode": "Current",
            "deployment": "php-apache", "scaleWriteStartedAt": stamp(start + 0.7),
            "scaleWriteFinishedAt": stamp(start + 0.8), "previousDesiredReplicas": previous,
            "currentReplicas": 0, "finalDesired": desired, "scaled": True}
    return row


def state(offset: float, requested: int = 1, ready: int = 1, metrics: str = "True") -> dict:
    return {"kind": "state", "request_started_at": ONSET + offset - 0.1,
        "request_finished_at": ONSET + offset, "status": "success", "response": {
            "deployment": {"metadata": {"uid": "deployment-uid", "name": "php-apache", "namespace": "default"},
                "spec": {"replicas": requested}, "status": {"readyReplicas": ready}},
            "phpa": {"metadata": {"uid": "phpa-uid", "generation": 1},
                "spec": {"decisionMode": "Current", "scaleTargetRef": {"kind": "Deployment", "name": "php-apache"}},
                "status": {"conditions": [{"type": "MetricsReady", "status": metrics, "observedGeneration": 1}]}},
            "pods": {"items": []}}}


class LiveBaselineCLI(unittest.TestCase):
    def fixture(self, directory: Path) -> list[dict]:
        write_json(directory / "metadata.yaml", {"experiment_id": "hand-worked", "decision_mode": "Current",
            "live_baseline": True, "pattern": "step", "benchmark_source_sha256": "a" * 64,
            "result": {"status": "success", "failure_reason": ""}})
        write_json(directory / "live-baseline-plan.json", {"protocol_version": "live-baseline-v1",
            "startup_mode": "warm", "decision_mode": "Current", "pattern": "step", "rps": 25,
            "interval_seconds": 2, "quiet_seconds": 30, "requeue_seconds": 30,
            "offered_duration_seconds": 181, "post_load_tail_seconds": 360,
            "controller_started_at": stamp(-600), "target_uid": "deployment-uid", "phpa_uid": "phpa-uid",
            "source_sha256": "a" * 64, "controller_binary_sha256": "b" * 64})
        (directory / "controller-binary.sha256").write_text("b" * 64 + "  /tmp/controller\n", encoding="utf-8")
        anchor = cycle(-40)
        write_json(directory / "live-baseline-gate.json", {"target_uid": "deployment-uid", "phpa_uid": "phpa-uid",
            "anchor": anchor, "startup_mode": "warm", "released_at": stamp(-35)})
        write_json(directory / "live-baseline-status.json", {"status": "success", "errors": [],
            "owned_processes_stopped": True, "gate_released": True})
        write_json(directory / "live-runner-status.json", {"protocol_version": "live-baseline-v1", "status": "success",
            "exit_code": 0, "controller_process_stopped": True})
        schedule = {"scenario_start_unix": ONSET - 30, "onset_unix": ONSET,
            "offered_end_unix": ONSET + 181, "pattern": "step"}
        (directory / "k6-warnings.log").write_text("PHPA_BASELINE_SCHEDULE " + json.dumps(schedule), encoding="utf-8")
        points = []
        for index, (duration, status) in enumerate([(100, "200"), (200, "200"), (300, "200"), (1000, "500")]):
            for metric, value in (("http_reqs", 1), ("http_req_duration", duration),
                    ("http_req_failed", int(status != "200")), ("baseline_request_attempt", (ONSET - 30) * 1000)):
                points.append({"type": "Point", "metric": metric, "data": {"time": stamp(index + 1),
                    "value": value, "tags": {"status": status, "expected_response": str(status == "200").lower()}}})
        points.append({"type": "Point", "metric": "dropped_iterations", "data": {"time": stamp(4), "value": 2}})
        write_rows(directory / "k6.json", points)
        cycles = [anchor, cycle(9.2, desired=2, previous=1), cycle(29.2, desired=1, previous=2)]
        write_rows(directory / "controller.log", [part for item in cycles for part in item.values() if isinstance(part, dict)])
        rows = [state(offset, 2 if 10 <= offset < 30 else 1, 2 if 14 <= offset < 30 else 1)
            for offset in range(-2, 545, 2)]
        write_rows(directory / "live-observations.ndjson", rows)
        return rows

    def run_cli(self, directory: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(ANALYZER), str(directory), *args],
            capture_output=True, text=True, timeout=20)

    def report(self, directory: Path) -> dict:
        result = self.run_cli(directory)
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads((directory / "live-baseline.json").read_text(encoding="utf-8"))

    def test_current_baseline_reports_service_cost_and_distinct_scale_ready_clocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.fixture(directory)
            report = self.report(directory)
            self.assertEqual(75, report["k6"]["successful_rate_http_200_pct"])
            self.assertEqual(895, report["k6"]["duration_p95_ms"])
            self.assertEqual(2, report["k6"]["dropped_iterations"])
            self.assertEqual(561, report["replica_time"]["requested"]["pod_seconds"])
            self.assertEqual(557, report["replica_time"]["ready"]["pod_seconds"])
            self.assertEqual(360, report["replica_time"]["requested"]["post_load_pod_seconds"])
            self.assertEqual(541, report["replica_time"]["ready"]["covered_seconds"])
            self.assertEqual(10, report["timing"]["first_scale_increase_seconds"])
            self.assertEqual([11.9, 14], [round(x, 1) for x in report["timing"]["first_observed_ready_growth_interval_seconds"]])
            self.assertEqual(5.5, report["controller_observations"]["accepted"][0]["source_age_at_observation_finish_seconds"])
            self.assertEqual("warm", report["startup"]["mode"])

    def test_mismatched_identity_mode_or_unverified_warm_gate_is_rejected_without_output(self) -> None:
        for case in ("target_uid", "phpa_uid", "mode", "stale_generation", "missing_condition",
                     "unprotected_gate", "missing_anchor", "failed_status", "missing_source", "k6_truncated",
                     "plan_uid", "source_hash", "binary_hash", "runner_failed", "busy_warm_gate", "missing_mode", "missing_accepted_query",
                     "runner_cleanup_failed"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                rows = self.fixture(directory)
                if case in ("target_uid", "phpa_uid", "mode", "stale_generation", "missing_condition"):
                    obj = rows[0]["response"]
                    if case == "target_uid":
                        obj["deployment"]["metadata"]["uid"] = "replacement"
                    elif case == "phpa_uid":
                        obj["phpa"]["metadata"]["uid"] = "replacement"
                    elif case == "mode":
                        obj["phpa"]["spec"]["decisionMode"] = "Hybrid"
                    elif case == "stale_generation":
                        for row in rows:
                            row["response"]["phpa"]["metadata"]["generation"] = 2
                    else:
                        for row in rows:
                            row["response"]["phpa"]["status"]["conditions"] = []
                    write_rows(directory / "live-observations.ndjson", rows)
                elif case in ("unprotected_gate", "missing_anchor"):
                    gate = json.loads((directory / "live-baseline-gate.json").read_text())
                    if case == "missing_anchor":
                        gate["anchor"] = None
                    else:
                        gate["anchor"]["decision"]["coldStartProtectedUntil"] = stamp(100)
                    write_json(directory / "live-baseline-gate.json", gate)
                elif case == "failed_status":
                    write_json(directory / "live-baseline-status.json", {"status": "failed", "errors": ["observer died"],
                        "owned_processes_stopped": False, "gate_released": True})
                elif case == "missing_source":
                    content = (directory / "controller.log").read_text()
                    (directory / "controller.log").write_text(content.replace('"sourceTimestamp"', '"lostSource"'))
                elif case == "k6_truncated":
                    points = [json.loads(line) for line in (directory / "k6.json").read_text().splitlines()]
                    write_rows(directory / "k6.json", [point for point in points if point["metric"] != "http_req_failed"])
                elif case in ("plan_uid", "source_hash", "binary_hash"):
                    plan = json.loads((directory / "live-baseline-plan.json").read_text())
                    plan[{"plan_uid": "target_uid", "source_hash": "source_sha256", "binary_hash": "controller_binary_sha256"}[case]] = "c" * 64
                    write_json(directory / "live-baseline-plan.json", plan)
                elif case == "runner_failed":
                    metadata = json.loads((directory / "metadata.yaml").read_text())
                    metadata["result"].update(status="failed", failure_reason="runner cleanup failed")
                    write_json(directory / "metadata.yaml", metadata)
                elif case == "runner_cleanup_failed":
                    write_json(directory / "live-runner-status.json", {"protocol_version": "live-baseline-v1",
                        "status": "failed", "exit_code": 3, "controller_process_stopped": False})
                elif case == "busy_warm_gate":
                    gate = json.loads((directory / "live-baseline-gate.json").read_text())
                    gate["anchor"]["decision"]["currentCPU%"] = 60
                    write_json(directory / "live-baseline-gate.json", gate)
                    log = [json.loads(line) for line in (directory / "controller.log").read_text().splitlines()]
                    for item in log:
                        if item["reconcileStartedAt"] == stamp(-40) and "currentCPU%" in item:
                            item["currentCPU%"] = 60
                    write_rows(directory / "controller.log", log)
                elif case == "missing_accepted_query":
                    logs = [json.loads(line) for line in (directory / "controller.log").read_text().splitlines()]
                    write_rows(directory / "controller.log", [item for item in logs if not (
                        item["reconcileStartedAt"] == stamp(9.2) and item["msg"] == "Queried CPU utilization")])
                else:
                    for row in rows:
                        del row["response"]["phpa"]["spec"]["decisionMode"]
                    write_rows(directory / "live-observations.ndjson", rows)
                result = self.run_cli(directory)
                self.assertEqual(2, result.returncode, result.stderr)
                self.assertIn("Live baseline analysis failed", result.stderr)
                self.assertFalse((directory / "live-baseline.json").exists())

    def test_schedule_only_uses_actual_k6_start_and_never_overwrites_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.fixture(directory)
            for path in directory.iterdir():
                if path.name not in ("live-baseline-plan.json", "k6-warnings.log"):
                    path.unlink()
            marker = {"scenario_start_unix": ONSET - 30, "onset_unix": ONSET, "offered_end_unix": ONSET + 181, "pattern": "step"}
            (directory / "k6-warnings.log").write_text('level=info msg=' + json.dumps(
                'PHPA_BASELINE_SCHEDULE ' + json.dumps(marker)) + ' source=console', encoding="utf-8")
            result = self.run_cli(directory, "--schedule-only")
            self.assertEqual(0, result.returncode, result.stderr)
            output = directory / "live-baseline-schedule.json"
            original = output.read_bytes()
            self.assertEqual(ONSET + 541, json.loads(original)["observation_end_unix"])
            self.assertEqual(2, self.run_cli(directory, "--schedule-only").returncode)
            self.assertEqual(original, output.read_bytes())
            plan = directory / "live-baseline-plan.json"
            original = plan.read_bytes()
            self.assertEqual(2, self.run_cli(directory, "--schedule-only", "--output", str(plan)).returncode)
            self.assertEqual(original, plan.read_bytes())

    def test_replica_timing_uses_its_own_read_interval_in_nonatomic_state_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            rows = self.fixture(directory)
            for row in rows:
                finish = row["request_finished_at"]
                row["requests"] = {resource: {"request_started_at": finish + offset - 0.1,
                    "request_finished_at": finish + offset, "status": "success"}
                    for resource, offset in (("deployment", 0.25), ("phpa", 0.75))}
                row["request_finished_at"] = finish + 0.9
            write_rows(directory / "live-observations.ndjson", rows)
            report = self.report(directory)
            self.assertEqual([12.15, 14.25], [round(x, 2) for x in report["timing"]["first_observed_ready_growth_interval_seconds"]])
            self.assertEqual(561, report["replica_time"]["requested"]["pod_seconds"])

    def test_missing_samples_failed_reads_and_stale_status_remain_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.fixture(directory)
            rows = [state(offset, metrics="False" if 4 <= offset <= 14 else "True")
                for offset in range(-2, 545, 2) if offset not in (6, 8)]
            for row in rows:
                offset = row["request_finished_at"] - ONSET
                if offset == 16:
                    row.update(status="error", error="API timeout", response={})
                if offset == 22:
                    row["response"]["phpa"]["status"]["conditions"][0]["observedGeneration"] = 0
            write_rows(directory / "live-observations.ndjson", rows)
            report = self.report(directory)
            requested = report["replica_time"]["requested"]
            self.assertIsNone(requested["pod_seconds"])
            self.assertEqual(531, requested["covered_pod_seconds"])
            self.assertEqual(10, requested["unknown_seconds"])
            self.assertEqual([[4, 10], [14, 18]], requested["unknown_intervals_seconds"])
            metrics = report["metrics_readiness"]
            self.assertEqual(4, metrics["sampled_false_seconds"])
            self.assertEqual([[10, 14]], metrics["sampled_false_intervals_seconds"])
            self.assertEqual(14, metrics["unknown_seconds"])
            self.assertEqual([[4, 10], [14, 18], [20, 24]], metrics["unknown_intervals_seconds"])
            self.assertFalse(report["quality"]["complete_sampling_coverage"])

    def test_no_expansion_is_retained_as_a_valid_observed_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.fixture(directory)
            write_rows(directory / "controller.log", [part for item in (cycle(-40), cycle(10), cycle(539))
                for part in item.values() if isinstance(part, dict)])
            write_rows(directory / "live-observations.ndjson", [state(offset) for offset in range(-2, 545, 2)])
            report = self.report(directory)
            self.assertEqual("no_increase_observed", report["timing"]["scale_outcome"])
            self.assertIsNone(report["timing"]["first_scale_increase_seconds"])
            self.assertIsNone(report["timing"]["first_observed_ready_growth_interval_seconds"])
            self.assertEqual(541, report["replica_time"]["requested"]["pod_seconds"])
            self.assertTrue(report["quality"]["complete_sampling_coverage"])

    def test_cold_start_keeps_controller_age_and_history_readiness_separate_from_warm_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.fixture(directory)
            plan = json.loads((directory / "live-baseline-plan.json").read_text())
            plan.update(startup_mode="cold", controller_started_at=stamp(-36))
            write_json(directory / "live-baseline-plan.json", plan)
            gate = json.loads((directory / "live-baseline-gate.json").read_text())
            gate.update(startup_mode="cold", anchor=None)
            write_json(directory / "live-baseline-gate.json", gate)
            item = cycle(20)
            item["decision"].update(coldStartProtection=True, coldStartProtectedUntil=stamp(264))
            write_rows(directory / "controller.log", [part for part in item.values() if isinstance(part, dict)])
            write_rows(directory / "live-observations.ndjson", [state(offset, metrics="False" if offset < 22 else "True")
                for offset in range(-2, 545, 2)])
            report = self.report(directory)
            self.assertEqual("cold", report["startup"]["mode"])
            self.assertEqual(36, report["startup"]["controller_start_to_onset_seconds"])
            self.assertEqual(20.5, report["startup"]["first_accepted_controller_observation_seconds"])
            self.assertEqual(22, report["metrics_readiness"]["sampled_false_seconds"])
            self.assertEqual([19.9, 22], [round(x, 1) for x in report["startup"]["first_observed_metrics_ready_interval_seconds"]])

    def test_duplicate_samples_or_missing_tail_cannot_produce_a_baseline(self) -> None:
        for case in ("duplicate", "missing_tail"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                rows = self.fixture(directory)
                if case == "duplicate":
                    rows.insert(6, rows[6])
                else:
                    rows = rows[:20]
                write_rows(directory / "live-observations.ndjson", rows)
                result = self.run_cli(directory)
                self.assertEqual(2, result.returncode, result.stderr)
                self.assertFalse((directory / "live-baseline.json").exists())


if __name__ == "__main__":
    unittest.main()
