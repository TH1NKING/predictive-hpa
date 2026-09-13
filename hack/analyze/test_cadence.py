"""Frozen, hand-worked receipts exercised through the cadence analysis CLI."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import test_live_baseline as baseline_fixture


ANALYZER = Path(__file__).with_name("cadence.py")
ONSET = baseline_fixture.ONSET
stamp = baseline_fixture.stamp
write_json = baseline_fixture.write_json
write_rows = baseline_fixture.write_rows


def request(start: float, kind: str = "rate", *, failed: bool = False) -> dict:
    return {"query_kind": kind, "method": "GET", "started_at": stamp(start),
        "finished_at": stamp(start + 0.125), "duration_seconds": 0.125,
        "http_status": 503 if failed else 200, "error": "unavailable" if failed else ""}


def observation(sequence: int, finish: float, source: float, *, rejected: bool = False) -> dict:
    row = {"kind": "cpu_observation", "protocol_version": "cadence-cpu-v1", "sequence": sequence,
        "observation_started_at": stamp(finish - 0.5), "observation_finished_at": stamp(finish),
        "target_uid": "deployment-uid", "status": "rejected" if rejected else "success",
        "error": "unavailable" if rejected else "", "evaluated_at": stamp(finish - 0.5),
        "source_timestamp": stamp(source), "utilization_percent": 2,
        "containers": [{"pod": "php-apache-a", "pod_uid": "pod-a", "container": "php-apache",
            "runtime_id": "containerd://abc", "source_timestamp": stamp(source)},
            {"pod": "php-apache-a", "pod_uid": "pod-a", "container": "sidecar",
            "runtime_id": "containerd://def", "source_timestamp": stamp(source + 0.25)}],
        "queries": [request(finish - 0.5, failed=rejected)]}
    if not rejected:
        row["queries"].append(request(finish - 0.25, "timestamp"))
    return row


def retain_gate(directory: Path, gate: dict) -> None:
    write_json(directory / "cadence-gate.json", gate)
    write_json(directory / "cadence-gate-attempt.json", {key: gate[key] for key in ("protocol_version",
        "previous_observation", "anchor", "planned_onset_unix", "release_request_started_at")})


def fixture(directory: Path, *, cadence: int = 30, pair: int = 1, slot: int = 1, offset: int = 2,
            complete_cpu: bool = False) -> None:
    baseline_fixture.LiveBaselineCLI().fixture(directory)
    metadata = json.loads((directory / "metadata.yaml").read_text())
    metadata.update(cadence_pilot=True, requeue_seconds=cadence)
    write_json(directory / "metadata.yaml", metadata)
    plan = json.loads((directory / "live-baseline-plan.json").read_text())
    plan.update(protocol_version="cadence-pilot-v1", quiet_seconds=0, requeue_seconds=cadence)
    write_json(directory / "live-baseline-plan.json", plan)
    write_json(directory / "live-runner-status.json", {"protocol_version": "live-baseline-v1", "status": "success",
        "exit_code": 0, "controller_process_stopped": True})
    marker = {"scenario_start_unix": ONSET, "onset_unix": ONSET, "offered_end_unix": ONSET + 181, "pattern": "step"}
    (directory / "k6-warnings.log").write_text("PHPA_BASELINE_SCHEDULE " + json.dumps(marker), encoding="utf-8")
    points = [json.loads(line) for line in (directory / "k6.json").read_text().splitlines()]
    for point in points:
        if point["metric"] == "baseline_request_attempt":
            point["data"]["value"] = ONSET * 1000
    write_rows(directory / "k6.json", points)
    rows = [observation(1, -offset - 1, -offset - 8), observation(2, -offset, -offset - 7),
        observation(3, 1, -7), observation(4, 2, -7), observation(5, 3, -6),
        observation(6, 4, -6, rejected=True), observation(7, 542, 537)]
    if complete_cpu:
        rows.append(observation(0, 0, -9))
        # A live actor can reject Kubernetes identity/availability checks before
        # issuing HTTP. These full receipt rows keep sampling coverage explicit
        # without adding fictional Prometheus requests to the hand-worked count.
        for finish in range(5, 542):
            row = observation(0, finish, finish - 5, rejected=True)
            row["queries"] = []
            rows.append(row)
        rows.sort(key=lambda row: row["observation_finished_at"])
        for sequence, row in enumerate(rows, 1):
            row["sequence"] = sequence
    write_rows(directory / "cadence-cpu.ndjson", rows + [{"kind": "cpu_observer_summary", "status": "completed",
        "protocol_version": "cadence-cpu-v1", "target_uid": "deployment-uid", "observations": 545 if complete_cpu else 7,
        "successful_observations": 7 if complete_cpu else 6, "rejected_observations": 538 if complete_cpu else 1,
        "finished_at": stamp(543), "error": ""}])
    write_json(directory / "cadence-plan.json", {"protocol_version": "cadence-pilot-v1", "pair": pair,
        "slot": slot, "requeue_seconds": cadence, "offset_seconds": offset, "target_uid": "deployment-uid",
        "phpa_uid": "phpa-uid", "interval_seconds": 1, "gate_timeout_seconds": 120,
        "phase_tolerance_seconds": 1, "cpu_binary_sha256": "c" * 64})
    retain_gate(directory, {"protocol_version": "cadence-pilot-v1",
        "warm_ready_at": stamp(-35), "runner_ready_at": stamp(-20),
        "previous_observation": rows[0], "anchor": rows[1], "planned_onset_unix": ONSET,
        "release_request_started_at": stamp(0), "release_request_finished_at": stamp(0.125),
        "release_response": {"data": {"attributes": {"paused": False}}}})
    write_json(directory / "cadence-status.json", {"status": "success", "owned_processes_stopped": True,
        "gate_released": True, "errors": []})
    write_json(directory / "controller-command.json", {"args": ["/tmp/controller", f"--requeue-interval={cadence}s"],
        "requeue_seconds": cadence, "binary_sha256": "b" * 64})
    logs = [json.loads(line) for line in (directory / "controller.log").read_text().splitlines()]
    for row in logs:
        if row["msg"] == "Finished PredictiveHPA reconciliation":
            row["requeueAfterSeconds"] = cadence
        if row["msg"] == "Queried CPU utilization":
            start = baseline_fixture.datetime.fromisoformat(row["reconcileStartedAt"]).timestamp() - ONSET
            cpu = observation(1, start + 0.5, start - 5)
            row.update(queries=cpu["queries"], containerSources=cpu["containers"], targetUID="deployment-uid",
                observationStartedAt=stamp(start), **{"currentCPU%": 2})
    write_rows(directory / "controller.log", logs)
    # The original warm receipt embeds the exact captured cycle.
    gate = json.loads((directory / "live-baseline-gate.json").read_text())
    for name in ("query", "finish"):
        gate["anchor"][name] = next(row for row in logs if row["msg"] == gate["anchor"][name]["msg"])
    write_json(directory / "live-baseline-gate.json", gate)


class CadenceCLI(unittest.TestCase):
    def cli(self, directory: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(ANALYZER), "--run-dir", str(directory),
            "--output", str(directory / "cadence-analysis.json"), *args], capture_output=True, text=True, timeout=20)

    def test_complete_run_keeps_service_failure_and_counts_real_http_requests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fixture(directory)
            result = self.cli(directory)
            self.assertEqual(0, result.returncode, result.stderr)
            report = json.loads((directory / "cadence-analysis.json").read_text())
            self.assertFalse(report["comparison_eligible"])
            self.assertFalse(report["observer_sampling"]["coverage_complete"])
            self.assertEqual([[0, 0.5], [4, 541]], report["observer_sampling"]["unknown_intervals_seconds"])
            self.assertEqual(537.5, report["observer_sampling"]["unknown_seconds"])
            self.assertFalse(report["service_criteria"]["passed"])
            self.assertEqual(4, report["queries"]["controller"]["scenario_window"]["http_requests"])
            self.assertEqual(6, report["queries"]["controller"]["entire_capture"]["http_requests"])
            self.assertEqual(7, report["queries"]["observer"]["scenario_window"]["http_requests"])
            self.assertEqual(13, report["queries"]["observer"]["entire_capture"]["http_requests"])
            self.assertEqual(1, report["queries"]["observer"]["scenario_window"]["errors"])
            self.assertEqual(0.875, report["queries"]["observer"]["scenario_window"]["duration_seconds"])
            self.assertEqual(2, report["signals"]["observer"]["scenario_window"]["strictly_advanced_observations"])
            self.assertEqual(1, report["signals"]["observer"]["scenario_window"]["repeated_or_partial_observations"])
            self.assertEqual(39, report["phase"]["onset_after_last_completed_reconcile_seconds"])
            self.assertEqual(7, report["phase"]["oldest_source_age_at_anchor_seconds"])
            self.assertEqual(9, report["phase"]["oldest_source_age_at_onset_seconds"])
            self.assertEqual(561, report["baseline"]["replica_time"]["requested"]["pod_seconds"])

    def test_truncated_cpu_stream_cannot_pass_its_independent_completion_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fixture(directory)
            rows = [json.loads(line) for line in (directory / "cadence-cpu.ndjson").read_text().splitlines()]
            del rows[2]
            for sequence, row in enumerate(rows[:-1], 1):
                row["sequence"] = sequence
            write_rows(directory / "cadence-cpu.ndjson", rows)
            result = self.cli(directory)
            self.assertEqual(2, result.returncode, result.stderr)
            self.assertFalse((directory / "cadence-analysis.json").exists())

    def test_campaign_reports_original_pair_values_and_preserves_failed_unrun_slots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            slots = []
            for index, (pair, cadence, offset) in enumerate(((1, 30, 2), (1, 15, 2), (2, 15, 7),
                    (2, 30, 7), (3, 30, 12), (3, 15, 12)), 1):
                slot = {"slot": index, "pair": pair, "requeue_seconds": cadence, "offset_seconds": offset,
                    "status": "success" if index <= 2 else "failed" if index == 3 else "not_run"}
                if index <= 2:
                    run = directory / f"run-{index}"
                    run.mkdir()
                    fixture(run, cadence=cadence, slot=index, complete_cpu=True)
                    result = self.cli(run)
                    self.assertEqual(0, result.returncode, result.stderr)
                    slot.update(run_dir=f"run-{index}", analysis=f"run-{index}/cadence-analysis.json")
                if index == 3:
                    slot.update(error="source gate timeout", partial_run_dirs=["run-3-partial"])
                slots.append(slot)
            write_json(directory / "campaign-status.json", {"protocol_version": "cadence-pilot-v1",
                "rps": 25, "status": "failed", "error": "source gate timeout", "cleanup_error": "", "slots": slots})
            result = subprocess.run([sys.executable, str(ANALYZER), "--campaign", str(directory),
                "--output", str(directory / "campaign-analysis.json")], capture_output=True, text=True, timeout=20)
            self.assertEqual(0, result.returncode, result.stderr)
            report = json.loads((directory / "campaign-analysis.json").read_text())
            self.assertEqual(["success", "success", "failed", "not_run", "not_run", "not_run"],
                [slot["status"] for slot in report["slots"]])
            self.assertEqual([True, False, False], [pair["comparison_eligible"] for pair in report["pairs"]])
            self.assertEqual(0, report["pairs"][0]["delta_15_minus_30"]["controller_http_requests"])
            self.assertEqual(895, report["pairs"][0]["values_30_seconds"]["all_request_p95_ms"])
            self.assertFalse(report["pairs"][0]["service_passed_both"])
            self.assertEqual(["run-3-partial"], report["slots"][2]["partial_run_dirs"])
            # Keep the original scalar outcomes, but expose loss of CPU capture
            # as a reason that no paired treatment delta can be interpreted.
            fixture(directory / "run-1")
            (directory / "run-1" / "cadence-analysis.json").unlink()
            result = self.cli(directory / "run-1")
            self.assertEqual(0, result.returncode, result.stderr)
            result = subprocess.run([sys.executable, str(ANALYZER), "--campaign", str(directory),
                "--output", str(directory / "campaign-gaps.json")], capture_output=True, text=True, timeout=20)
            self.assertEqual(0, result.returncode, result.stderr)
            gap_pair = json.loads((directory / "campaign-gaps.json").read_text())["pairs"][0]
            self.assertFalse(gap_pair["comparison_eligible"])
            self.assertIsNone(gap_pair["delta_15_minus_30"])
            self.assertEqual(895, gap_pair["values_30_seconds"]["all_request_p95_ms"])

    def test_gate_cannot_skip_the_first_eligible_source_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fixture(directory)
            rows = [json.loads(line) for line in (directory / "cadence-cpu.ndjson").read_text().splitlines()]
            rows.insert(0, observation(1, -4, -11))
            for sequence, row in enumerate(rows[:-1], 1):
                row["sequence"] = sequence
            rows[-1].update(observations=8, successful_observations=7)
            write_rows(directory / "cadence-cpu.ndjson", rows)
            gate = json.loads((directory / "cadence-gate.json").read_text())
            gate.update(previous_observation=rows[1], anchor=rows[2])
            retain_gate(directory, gate)
            result = self.cli(directory)
            self.assertEqual(2, result.returncode, result.stderr)
            self.assertFalse((directory / "cadence-analysis.json").exists())

    def test_successful_observation_counts_post_fallback_attempts_and_their_http_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fixture(directory)
            rows = [json.loads(line) for line in (directory / "cadence-cpu.ndjson").read_text().splitlines()]
            queries = [request(0.5, "rate"), request(0.625, "rate"),
                request(0.75, "timestamp"), request(0.875, "timestamp")]
            for index in (0, 2):
                queries[index].update(method="POST", http_status=405, error="HTTP 405")
            rows[2]["queries"] = queries
            write_rows(directory / "cadence-cpu.ndjson", rows)
            result = self.cli(directory)
            self.assertEqual(0, result.returncode, result.stderr)
            report = json.loads((directory / "cadence-analysis.json").read_text())
            self.assertEqual(9, report["queries"]["observer"]["scenario_window"]["http_requests"])
            self.assertEqual(15, report["queries"]["observer"]["entire_capture"]["http_requests"])
            self.assertEqual(3, report["queries"]["observer"]["scenario_window"]["errors"])
            self.assertEqual(3, report["signals"]["observer"]["scenario_window"]["accepted_observations"])

    def test_invalid_identity_sources_queries_configuration_or_cleanup_never_publish_success(self) -> None:
        for case in ("rejected_target", "partial_gate", "duplicate_identity", "future", "stale", "missing_queries",
                     "query_duration", "missing_summary", "failed_summary", "command", "metadata", "log", "cleanup",
                     "missing_cpu", "inconsistent_cpu"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                fixture(directory)
                rows = [json.loads(line) for line in (directory / "cadence-cpu.ndjson").read_text().splitlines()]
                if case == "rejected_target":
                    rows[5]["target_uid"] = "replacement-deployment"
                elif case == "partial_gate":
                    # The minimum advances from -10 to -9.75, while one
                    # container remains unchanged. The gate must reject it.
                    rows[1]["containers"][1]["source_timestamp"] = stamp(-9.75)
                    rows[1]["source_timestamp"] = stamp(-9.75)
                    gate = json.loads((directory / "cadence-gate.json").read_text())
                    gate["anchor"] = rows[1]
                    retain_gate(directory, gate)
                elif case == "duplicate_identity":
                    rows[2]["containers"][1] = copy.deepcopy(rows[2]["containers"][0])
                elif case in ("future", "stale"):
                    rows[2]["containers"][0]["source_timestamp"] = stamp(1 if case == "future" else -45)
                    rows[2]["source_timestamp"] = rows[2]["containers"][0]["source_timestamp"]
                elif case == "missing_queries":
                    del rows[5]["queries"]
                elif case == "query_duration":
                    rows[2]["queries"][0]["duration_seconds"] = 3
                elif case == "missing_summary":
                    rows.pop()
                elif case == "failed_summary":
                    rows[-1]["status"] = "interrupted"
                elif case == "command":
                    command = json.loads((directory / "controller-command.json").read_text())
                    command["args"] = ["/tmp/controller", "--requeue-interval=15s"]
                    write_json(directory / "controller-command.json", command)
                elif case == "metadata":
                    metadata = json.loads((directory / "metadata.yaml").read_text())
                    metadata["requeue_seconds"] = 15
                    write_json(directory / "metadata.yaml", metadata)
                elif case in ("log", "missing_cpu", "inconsistent_cpu"):
                    logs = [json.loads(line) for line in (directory / "controller.log").read_text().splitlines()]
                    for row in logs:
                        if case == "log" and row["msg"] == "Finished PredictiveHPA reconciliation":
                            row["requeueAfterSeconds"] = 15
                        elif case != "log" and row["msg"] == "Queried CPU utilization" and row["reconcileStartedAt"] == stamp(9.2):
                            if case == "missing_cpu":
                                del row["currentCPU%"]
                            else:
                                row["currentCPU%"] = 80
                    write_rows(directory / "controller.log", logs)
                else:
                    write_json(directory / "cadence-status.json", {"status": "success", "owned_processes_stopped": False,
                        "gate_released": True, "errors": []})
                write_rows(directory / "cadence-cpu.ndjson", rows)
                result = self.cli(directory)
                self.assertEqual(2, result.returncode, result.stderr)
                self.assertFalse((directory / "cadence-analysis.json").exists())

    def test_gate_acknowledgement_must_match_the_retained_release_attempt(self) -> None:
        for missing in (False, True):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                fixture(directory)
                path = directory / "cadence-gate-attempt.json"
                if missing:
                    path.unlink()
                else:
                    attempt = json.loads(path.read_text())
                    attempt["release_request_started_at"] = stamp(1)
                    write_json(path, attempt)
                result = self.cli(directory)
                self.assertEqual(2, result.returncode, result.stderr)
                self.assertFalse((directory / "cadence-analysis.json").exists())

    def test_complete_phase_failure_is_retained_and_existing_outputs_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fixture(directory)
            rows = [json.loads(line) for line in (directory / "cadence-cpu.ndjson").read_text().splitlines()]
            for row in rows[:2]:
                for name in ("observation_started_at", "observation_finished_at", "evaluated_at"):
                    row[name] = stamp(baseline_fixture.datetime.fromisoformat(row[name]).timestamp() - ONSET - 2)
                for query in row["queries"]:
                    for name in ("started_at", "finished_at"):
                        query[name] = stamp(baseline_fixture.datetime.fromisoformat(query[name]).timestamp() - ONSET - 2)
            write_rows(directory / "cadence-cpu.ndjson", rows)
            gate = json.loads((directory / "cadence-gate.json").read_text())
            gate.update(previous_observation=rows[0], anchor=rows[1], planned_onset_unix=ONSET - 2)
            retain_gate(directory, gate)
            result = self.cli(directory)
            self.assertEqual(0, result.returncode, result.stderr)
            output = directory / "cadence-analysis.json"
            original = output.read_bytes()
            report = json.loads(original)
            self.assertTrue(report["evidence_valid"])
            self.assertFalse(report["phase_valid"])
            self.assertFalse(report["comparison_eligible"])
            self.assertEqual(4, report["phase"]["actual_offset_seconds"])
            self.assertEqual(2, self.cli(directory).returncode)
            self.assertEqual(original, output.read_bytes())
            original_plan = (directory / "cadence-plan.json").read_bytes()
            self.assertEqual(2, self.cli(directory, "--output", str(directory / "cadence-plan.json")).returncode)
            self.assertEqual(original_plan, (directory / "cadence-plan.json").read_bytes())

    def test_partial_container_progress_does_not_count_as_new_complete_information(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fixture(directory)
            rows = [json.loads(line) for line in (directory / "cadence-cpu.ndjson").read_text().splitlines()]
            rows[4]["containers"][1]["source_timestamp"] = stamp(-6.75)
            rows[4]["source_timestamp"] = stamp(-6.75)
            write_rows(directory / "cadence-cpu.ndjson", rows)
            result = self.cli(directory)
            self.assertEqual(0, result.returncode, result.stderr)
            report = json.loads((directory / "cadence-analysis.json").read_text())
            self.assertEqual(1, report["signals"]["observer"]["scenario_window"]["strictly_advanced_observations"])
            self.assertEqual(2, report["signals"]["observer"]["scenario_window"]["repeated_or_partial_observations"])

    def test_pair_with_different_source_ages_keeps_original_values_but_withholds_deltas(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            slots = []
            for index, (pair, cadence, offset) in enumerate(((1, 30, 2), (1, 15, 2), (2, 15, 7),
                    (2, 30, 7), (3, 30, 12), (3, 15, 12)), 1):
                slot = {"slot": index, "pair": pair, "requeue_seconds": cadence, "offset_seconds": offset,
                    "status": "success" if index <= 2 else "not_run"}
                if index <= 2:
                    run = directory / f"run-{index}"
                    run.mkdir()
                    fixture(run, cadence=cadence, slot=index, complete_cpu=True)
                    if index == 2:
                        rows = [json.loads(line) for line in (run / "cadence-cpu.ndjson").read_text().splitlines()]
                        for row in rows[:2]:
                            row["source_timestamp"] = stamp(baseline_fixture.datetime.fromisoformat(row["source_timestamp"]).timestamp() - ONSET - 2)
                            for source in row["containers"]:
                                source["source_timestamp"] = stamp(baseline_fixture.datetime.fromisoformat(source["source_timestamp"]).timestamp() - ONSET - 2)
                        write_rows(run / "cadence-cpu.ndjson", rows)
                        gate = json.loads((run / "cadence-gate.json").read_text())
                        gate.update(previous_observation=rows[0], anchor=rows[1])
                        retain_gate(run, gate)
                    result = self.cli(run)
                    self.assertEqual(0, result.returncode, result.stderr)
                    slot.update(run_dir=f"run-{index}", analysis=f"run-{index}/cadence-analysis.json")
                slots.append(slot)
            write_json(directory / "campaign-status.json", {"protocol_version": "cadence-pilot-v1",
                "rps": 25, "status": "in_progress", "slots": slots})
            result = subprocess.run([sys.executable, str(ANALYZER), "--campaign", str(directory),
                "--output", str(directory / "campaign-analysis.json")], capture_output=True, text=True, timeout=20)
            self.assertEqual(0, result.returncode, result.stderr)
            pair = json.loads((directory / "campaign-analysis.json").read_text())["pairs"][0]
            self.assertFalse(pair["comparison_eligible"])
            self.assertIsNone(pair["delta_15_minus_30"])
            self.assertEqual(9, pair["values_30_seconds"]["oldest_source_age_at_onset_seconds"])
            self.assertEqual(11, pair["values_15_seconds"]["oldest_source_age_at_onset_seconds"])

    def test_signal_loss_after_anchor_before_release_cannot_produce_valid_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fixture(directory)
            rows = [json.loads(line) for line in (directory / "cadence-cpu.ndjson").read_text().splitlines()]
            rows[2] = observation(3, -1, -7, rejected=True)
            rows[-1].update(successful_observations=5, rejected_observations=2)
            write_rows(directory / "cadence-cpu.ndjson", rows)
            result = self.cli(directory)
            self.assertEqual(2, result.returncode, result.stderr)
            self.assertFalse((directory / "cadence-analysis.json").exists())


if __name__ == "__main__":
    unittest.main()
