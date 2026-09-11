"""Hand-worked controller evidence exercised through the public analysis CLI."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


CLI = Path(__file__).with_name("decision_replay.py")
ZERO = 1789084800


def stamp(seconds):
    return datetime.fromtimestamp(ZERO + seconds, timezone.utc).isoformat()


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def fixture(root):
    spec = {"decisionMode": "Predictive", "minReplicas": 1, "maxReplicas": 10,
            "targetCPUUtilizationPercentage": 50, "scaleDownStabilizationWindowSeconds": 60,
            "scaleTargetRef": {"kind": "Deployment", "name": "app", "apiVersion": "apps/v1"},
            "prediction": {"algorithm": "EWMA", "alphaPercent": 30, "window": "1m", "horizon": "30s"}}
    identity = {"target_uid": "target-uid", "phpa_uid": "phpa-uid"}
    write(root / "live-baseline-plan.json", {**identity, "protocol_version": "live-baseline-v1",
          "startup_mode": "warm", "decision_mode": "Predictive", "pattern": "step", "interval_seconds": 2,
          "requeue_seconds": 30, "quiet_seconds": 30, "offered_duration_seconds": 181,
          "post_load_tail_seconds": 360, "controller_started_at": stamp(-61),
          "source_sha256": "a" * 64, "controller_binary_sha256": "b" * 64})
    write(root / "live-baseline-gate.json", {**identity, "startup_mode": "warm", "released_at": stamp(-1)})
    write(root / "live-baseline-schedule.json", {"scenario_start_unix": ZERO - 30,
          "load_onset_unix": ZERO, "offered_load_end_unix": ZERO + 181, "observation_end_unix": ZERO + 541})
    marker = {"scenario_start_unix": ZERO - 30, "onset_unix": ZERO, "offered_end_unix": ZERO + 181, "pattern": "step"}
    (root / "k6-stdout.log").write_text("PHPA_BASELINE_SCHEDULE " + json.dumps(marker) + "\n", encoding="utf-8")
    (root / "k6-warnings.log").write_text("", encoding="utf-8")
    write(root / "phpa-after-apply.json", {"metadata": {"name": "phpa", "namespace": "default", "uid": "phpa-uid", "generation": 1}, "spec": spec})
    write(root / "deployment-before.json", {"metadata": {"name": "app", "namespace": "default", "uid": "target-uid"}, "spec": {"replicas": 1}})
    write(root / "metadata.yaml", {"experiment_id": "worked", "live_baseline": True,
          "decision_mode": "Predictive", "benchmark_source_sha256": "a" * 64,
          "result": {"status": "success", "failure_reason": ""}, "git": {"commit": "c" * 40, "dirty": False}})
    (root / "controller-binary.sha256").write_text("b" * 64 + "  controller\n", encoding="utf-8")
    rows = []
    for at, count, cpu, predicted in [(-60, 1, None, None), (-30, 2, 0, 0), (0, 3, 80, 44.4), (30, 3, 80, 55.08)]:
        common = {"reconcileID": str(at), "reconcileStartedAt": stamp(at), "namespace": "default", "name": "phpa"}
        rows.append({**common, "msg": "Queried CPU utilization", "queryError": "", "samples": count,
                     "queryInstantAt": stamp(at), "latestEvaluationAt": stamp(at), "sourceTimestamp": stamp(at - 5),
                     "observationStartedAt": stamp(at), "observationFinishedAt": stamp(at + .003),
                     "queryStartedAt": stamp(at + .001), "queryFinishedAt": stamp(at + .002),
                     "observationSpacingSeconds": 15, "cpuRateWindowSeconds": 60})
        if cpu is not None:
            desired = 2 if at == 30 else 1
            rows.append({**common, "msg": "Evaluated PredictiveHPA scaling decision", "decisionAt": stamp(at + .004),
                         "stabilizationEvaluatedAt": stamp(at + .004), "currentReplicas": 1, "decisionMode": "Predictive",
                         "currentCPU%": cpu, "rawPredictedCPU%": predicted, "predictedCPU%": predicted,
                         "decisionCPU%": predicted, "desiredReplicas": desired, "finalDesired": desired,
                         "skipReason": "" if at == 30 else "DesiredEqualsCurrent", "stabilized": False,
                         "samples": count, "latestEvaluationAt": stamp(at), "coldStartProtection": True,
                         "coldStartProtectedUntil": stamp(30.004), "stabilizationHistoryEntries": int((at + 60) / 30),
                         "stabilizationHistoryOldestAt": stamp(-29.996)})
        if at == 30:
            rows.append({**common, "msg": "Scaled Deployment", "deployment": "app", "decisionMode": "Predictive",
                         "scaleWriteStartedAt": stamp(at + .005), "scaleWriteFinishedAt": stamp(at + .006),
                         "previousDesiredReplicas": 1, "currentReplicas": 1, "finalDesired": 2, "scaled": True})
        rows.append({**common, "msg": "Finished PredictiveHPA reconciliation", "reconcileError": "",
                     "reconcileFinishedAt": stamp(at + .007), "requeueAfterSeconds": 30})
    write_log(root, rows)
    states = []
    for at in [-60, -1, 0, 29, 31, 33, 35]:
        states.append({"kind": "state", "status": "success", "request_started_at": stamp(at), "request_finished_at": stamp(at + .01),
                       "response": {"deployment": {"metadata": {"uid": "target-uid", "name": "app", "namespace": "default"},
                                                   "spec": {"replicas": 2 if at > 30 else 1},
                                                   "status": {"readyReplicas": 2 if at >= 33 else 1}},
                                    "phpa": {"metadata": {"uid": "phpa-uid", "generation": 1}, "spec": spec}}})
    (root / "live-observations.ndjson").write_text("\n".join(map(json.dumps, states)) + "\n", encoding="utf-8")
    return rows, states


def write_log(root, rows):
    (root / "controller.log").write_text('setup\tStarting manager\n' + "\n".join(map(json.dumps, rows)) + "\n", encoding="utf-8")


class DecisionReplayAnalysisCLI(unittest.TestCase):
    def run_cli(self, run, output):
        return subprocess.run([sys.executable, str(CLI), str(run), "--output", str(output)],
                              capture_output=True, text=True, timeout=30)

    def test_preserves_missing_first_cpu_and_reports_bounded_ready_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            run.mkdir()
            fixture(run)
            result = self.run_cli(run, root / "report")
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((root / "report" / "report.json").read_text())
            inputs = json.loads((root / "report" / "replay-input.json").read_text())
            self.assertEqual([row["predictionSource"] for row in inputs["cycles"]],
                             ["recorded-forecast", "recorded-forecast", "computed-history"])
            self.assertEqual([s["value"] for s in inputs["cycles"][-1]["samples"]], [0, 80, 80])
            self.assertEqual(report["history"], {"computed": 1, "recorded": 2, "unknown_cpu_observations": 1})
            self.assertAlmostEqual(report["timing"]["first_scale_seconds"], 30.006, places=5)
            self.assertAlmostEqual(report["timing"]["first_ready_interval_seconds"][0], 31, places=5)
            self.assertAlmostEqual(report["timing"]["first_ready_interval_seconds"][1], 33.01, places=5)
            self.assertEqual(report["verification"], "prepared-only")

    def test_rejects_a_replaced_target_instead_of_comparing_different_workloads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            run.mkdir()
            _, states = fixture(run)
            states[-1]["response"]["deployment"]["metadata"]["uid"] = "replacement-uid"
            (run / "live-observations.ndjson").write_text("\n".join(map(json.dumps, states)) + "\n", encoding="utf-8")
            result = self.run_cli(run, root / "report")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("identity", result.stderr.lower())
            self.assertFalse((root / "report").exists())

    def test_rejects_unlogged_replica_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            run.mkdir()
            _, states = fixture(run)
            states[2]["response"]["deployment"]["spec"]["replicas"] = 4
            (run / "live-observations.ndjson").write_text("\n".join(map(json.dumps, states)) + "\n", encoding="utf-8")
            result = self.run_cli(run, root / "report")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("write chain", result.stderr.lower())
            self.assertFalse((root / "report").exists())

    def test_rejects_a_missing_decision_in_the_policy_history_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            run.mkdir()
            rows, _ = fixture(run)
            rows = [row for row in rows if not (row["msg"] == "Evaluated PredictiveHPA scaling decision"
                                                and row["reconcileID"] == "-30")]
            write_log(run, rows)
            result = self.run_cli(run, root / "report")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("decision", result.stderr.lower())

    def test_rejects_reversed_source_query_timing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            run.mkdir()
            rows, _ = fixture(run)
            rows[0]["queryFinishedAt"] = stamp(-61)
            write_log(run, rows)
            result = self.run_cli(run, root / "report")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("time", result.stderr.lower())

    def test_preserves_history_through_a_pre_query_readiness_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            run.mkdir()
            rows, _ = fixture(run)
            common = {"reconcileID": "15", "reconcileStartedAt": stamp(15), "namespace": "default", "name": "phpa"}
            gap = [{**common, "msg": "Queried CPU utilization", "queryError": "metricsprovider: incomplete data: Pod is not Ready",
                    "samples": 0, "queryStartedAt": None, "queryFinishedAt": None, "latestEvaluationAt": None,
                    "sourceTimestamp": None, "observationStartedAt": stamp(15), "observationFinishedAt": stamp(15.003),
                    "observationSpacingSeconds": 15, "cpuRateWindowSeconds": 60},
                   {**common, "msg": "Finished PredictiveHPA reconciliation", "reconcileError": "",
                    "reconcileFinishedAt": stamp(15.004), "requeueAfterSeconds": 30}]
            write_log(run, rows[:-4] + gap + rows[-4:])
            result = self.run_cli(run, root / "report")
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((root / "report" / "report.json").read_text())
            rejected = next(row for row in report["cycles"] if row["reconcile_id"] == "15")
            self.assertEqual(rejected["reason"], "metrics_rejected_before_query")
            self.assertIsNone(rejected["query_started_seconds"])
            self.assertEqual(report["history"]["computed"], 1)

    def test_retains_the_anchor_when_a_frequent_event_replaces_only_the_returned_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            run.mkdir()
            rows, _ = fixture(run)
            extra = json.loads(json.dumps(rows[2:5]))
            for row in extra:
                row["reconcileID"] = "-25"
                for key, value in row.items():
                    if isinstance(value, str) and value.startswith(stamp(-30)[:19]):
                        row[key] = value.replace(stamp(-30)[:19], stamp(-25)[:19])
            write_log(run, rows[:5] + extra + rows[5:])
            result = self.run_cli(run, root / "report")
            self.assertEqual(result.returncode, 0, result.stderr)
            inputs = json.loads((root / "report" / "replay-input.json").read_text())
            self.assertEqual(inputs["cycles"][-1]["samples"][0]["timestamp"], stamp(-30))

    def test_preserves_existing_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            run.mkdir()
            fixture(run)
            output = root / "report"
            output.mkdir()
            (output / "keep.txt").write_text("previous evidence", encoding="utf-8")
            result = self.run_cli(run, output)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((output / "keep.txt").read_text(), "previous evidence")

    def test_rejects_a_shifted_schedule_that_disagrees_with_the_actual_load_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            run.mkdir()
            fixture(run)
            schedule = json.loads((run / "live-baseline-schedule.json").read_text())
            schedule["load_onset_unix"] += 10
            write(run / "live-baseline-schedule.json", schedule)
            result = self.run_cli(run, root / "report")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("schedule", result.stderr.lower())

    def test_rejects_impossible_source_times_on_a_successful_observation(self):
        for source_time in [stamp(1000), stamp(-1000), None, 0, True]:
            with self.subTest(source_time=source_time), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                run = root / "run"
                run.mkdir()
                rows, _ = fixture(run)
                rows[0]["sourceTimestamp"] = source_time
                write_log(run, rows)
                result = self.run_cli(run, root / "report")
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((root / "report").exists())


if __name__ == "__main__":
    unittest.main()
