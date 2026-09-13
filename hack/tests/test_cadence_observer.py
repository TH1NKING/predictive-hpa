"""Cadence launch contracts through the public offline and HTTP CLIs."""
from datetime import datetime, timedelta, timezone
import json
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import os
import signal
import shlex
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "hack/observe_cadence.py"


class CadenceAnchorCLI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.base = datetime.now(timezone.utc) - timedelta(seconds=5)

    def stamp(self, seconds):
        return (self.base + timedelta(seconds=seconds)).isoformat()

    def row(self, sequence, at, sources):
        return {"protocol_version": "cadence-cpu-v1", "kind": "cpu_observation", "sequence": sequence,
                "observation_started_at": self.stamp(at), "observation_finished_at": self.stamp(at + .1),
                "evaluated_at": self.stamp(at), "target_uid": "target", "status": "success", "error": "",
                "source_timestamp": self.stamp(min(sources)), "utilization_percent": 1,
                "containers": [{"pod": "web", "pod_uid": "pod", "container": name,
                                "runtime_id": name + "-runtime", "source_timestamp": self.stamp(source)}
                               for name, source in zip(("app", "sidecar"), sources)], "queries": []}

    def invoke(self, rows):
        path = self.directory / "cpu.ndjson"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return subprocess.run([sys.executable, str(CLI), "check-anchor", "--observations", str(path),
            "--target-uid", "target", "--after", self.stamp(0), "--offset-seconds", "2",
            "--output", str(self.directory / "gate.json")], text=True, capture_output=True, timeout=10)

    def test_only_complete_forward_progress_releases_an_anchor(self):
        rows = [self.row(1, 1, (-10, -9)), self.row(2, 2, (-8, -9)), self.row(3, 3, (-7, -6))]
        result = self.invoke(rows)
        self.assertEqual(0, result.returncode, result.stderr)
        gate = json.loads((self.directory / "gate.json").read_text())
        self.assertEqual(3, gate["anchor"]["sequence"])
        self.assertEqual(2, gate["previous_observation"]["sequence"])
        self.assertAlmostEqual((self.base + timedelta(seconds=5.1)).timestamp(), gate["planned_onset_unix"])

    def test_replaced_partial_or_rejected_coverage_does_not_release(self):
        for mutation in ("partial", "runtime", "rejected"):
            with self.subTest(mutation=mutation):
                rows = [self.row(1, 1, (-10, -9)), self.row(2, 2, (-8, -7))]
                if mutation == "partial":
                    rows[1]["containers"].pop()
                elif mutation == "runtime":
                    rows[1]["containers"][0]["runtime_id"] = "replacement"
                else:
                    rows[1]["status"] = "rejected"
                self.assertEqual(4, self.invoke(rows).returncode)
                self.assertFalse((self.directory / "gate.json").exists())

    def test_bad_source_evidence_fails_without_publishing_a_gate(self):
        for mutation in ("future", "stale", "identity", "duplicate", "sequence", "error", "missing-time"):
            with self.subTest(mutation=mutation):
                rows = [self.row(1, 1, (-10, -9)), self.row(2, 2, (-8, -7))]
                if mutation in ("future", "stale"):
                    rows[1]["containers"][0]["source_timestamp"] = self.stamp(5 if mutation == "future" else -60)
                elif mutation == "identity":
                    rows[1]["target_uid"] = "other"
                elif mutation == "duplicate":
                    rows[1]["containers"].append(rows[1]["containers"][0])
                elif mutation == "sequence":
                    rows[1]["sequence"] = 3
                elif mutation == "error":
                    rows[1]["error"] = "query failed"
                else:
                    rows[1]["evaluated_at"] = None
                self.assertEqual(3, self.invoke(rows).returncode)
                self.assertFalse((self.directory / "gate.json").exists())

    def test_http_gate_releases_a_preinitialized_runner_once(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.reply({"paused": True, "running": False, "stopped": False, "status": 4})

            def do_PATCH(self):
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.reply({"paused": False, "running": True, "stopped": False, "status": 7})

            def reply(self, attributes):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"data": {"type": "status", "id": "default", "attributes": attributes}}).encode())

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.base = datetime.now(timezone.utc) - timedelta(seconds=3)
        path = self.directory / "cpu.ndjson"
        path.write_text("".join(json.dumps(row) + "\n" for row in
            [self.row(1, 1, (-10, -9)), self.row(2, 2, (-8, -7))]), encoding="utf-8")
        result = subprocess.run([sys.executable, str(CLI), "release", "--observations", str(path),
            "--target-uid", "target", "--after", self.stamp(0), "--offset-seconds", "2",
            "--k6-url", f"http://127.0.0.1:{server.server_port}", "--timeout-seconds", "3",
            "--output", str(self.directory / "gate.json")], capture_output=True, text=True, timeout=8)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([{"data": {"type": "status", "id": "default", "attributes": {"paused": False}}}], requests)
        gate = json.loads((self.directory / "gate.json").read_text())
        self.assertEqual("released", gate["status"])
        self.assertEqual(2, gate["anchor"]["sequence"])

    def test_live_preflight_retains_binary_identity_failure(self):
        result = subprocess.run([sys.executable, str(CLI), "observe", "--run-dir", str(self.directory),
            "--context", "kind-phpa-cadence-test", "--cpu-binary", sys.executable,
            "--cpu-sha256", "0" * 64, "--prometheus-address", "http://127.0.0.1:9090",
            "--target-uid", "target", "--phpa-uid", "phpa", "--requeue-seconds", "15",
            "--offset-seconds", "2", "--pair", "1", "--slot", "2"],
            text=True, capture_output=True, timeout=10)
        self.assertEqual(3, result.returncode, result.stderr)
        status = json.loads((self.directory / "cadence-status.json").read_text())
        self.assertEqual("failed", status["status"])
        self.assertIn("binary", status["errors"][0])
        self.assertTrue(status["owned_processes_stopped"])
        self.assertFalse(status["gate_released"])

    def test_source_wait_times_out_without_releasing_and_retains_receipt(self):
        patches = []
        connected = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"data":{"attributes":{"paused":true,"running":false,"stopped":false,"status":4}}}')
                connected.set()

            def do_PATCH(self):
                patches.append(True)
                self.send_error(500)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        path = self.directory / "cpu.ndjson"
        path.write_text(json.dumps(self.row(1, 1, (-10, -9))) + "\n", encoding="utf-8")
        arguments = [sys.executable, str(CLI), "release", "--observations", str(path),
            "--target-uid", "target", "--after", self.stamp(0), "--offset-seconds", "2",
            "--k6-url", f"http://127.0.0.1:{server.server_port}", "--timeout-seconds", ".2",
            "--output", str(self.directory / "gate.json")]
        started = time.monotonic()
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=5)
        self.assertEqual(3, result.returncode, result.stderr)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual([], patches)
        receipt = json.loads((self.directory / "gate.json").read_text())
        self.assertEqual("failed", receipt["status"])
        self.assertIn("deadline", receipt["error"])
        # A failed attempt remains exclusive: a second invocation cannot replace it.
        self.assertEqual(3, subprocess.run(arguments, capture_output=True, timeout=5).returncode)
        self.assertEqual(receipt, json.loads((self.directory / "gate.json").read_text()))
        if os.name != "nt":
            arguments[-1] = str(self.directory / "signal.json")
            arguments[arguments.index("--timeout-seconds") + 1] = "4"
            connected.clear()
            process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertTrue(connected.wait(3))
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=3)
                self.assertEqual(3, process.returncode, stdout + stderr)
                self.assertIn("interrupted", json.loads((self.directory / "signal.json").read_text())["error"])
                self.assertEqual([], patches)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()


class CadenceLiveWarmCLI(unittest.TestCase):
    def run_live_observer(self, failure=None):
        """Drive the real CLI through its process, HTTP, and retained-input boundaries."""
        with tempfile.TemporaryDirectory(prefix="cadence-inflight-") as temporary:
            directory = Path(temporary)
            def stamp(offset=0):
                return (datetime.now(timezone.utc) + timedelta(seconds=offset)).isoformat()

            state = {"deployment": {"metadata": {"uid": "target"}, "spec": {"replicas": 1},
                        "status": {"replicas": 1, "readyReplicas": 1}},
                "phpa": {"metadata": {"uid": "phpa", "generation": 2}, "spec": {"decisionMode": "Current"},
                    "status": {"conditions": [{"type": "MetricsReady", "status": "True", "observedGeneration": 2}]}}}
            query = {"queryError": "", "samples": 3, "sourceTimestamp": stamp(-5), "currentCPU%": 1.5}
            decision = {"decisionMode": "Current", "samples": 3, "currentCPU%": 1.5,
                "currentReplicas": 1, "finalDesired": 1, "coldStartProtection": False,
                "coldStartProtectedUntil": stamp(-90), "stabilizationEvaluatedAt": stamp(-1)}
            messages = {"query": "Queried CPU utilization", "decision": "Evaluated PredictiveHPA scaling decision",
                "finish": "Finished PredictiveHPA reconciliation"}
            def event(kind, started, values):
                return {"msg": messages[kind], "reconcileStartedAt": started, **values}
            initial = stamp(-2)
            logs = [event("query", initial, query), event("decision", initial, decision),
                event("finish", initial, {"reconcileFinishedAt": stamp(-1), "requeueAfterSeconds": 15})]
            log = directory / "controller.log"
            log.write_text("".join(json.dumps(row) + "\n" for row in logs), encoding="utf-8")
            for name, value in {
                "live-baseline-plan.json": {"protocol_version": "cadence-pilot-v1", "requeue_seconds": 15,
                    "target_uid": "target", "phpa_uid": "phpa", "controller_binary_sha256": "b" * 64},
                "controller-command.json": {"requeue_seconds": 15, "args": ["--requeue-interval=15s"], "binary_sha256": "b" * 64},
                "live-baseline-gate.json": {"released_at": stamp(-32)},
                "k6-runner.json": {"pod": "phpa-k6-test", "script": "cadence.js"},
            }.items():
                (directory / name).write_text(json.dumps(value), encoding="utf-8")

            # These stand-alone executables implement the public CPU producer and
            # kubectl port-forward boundaries. No application module is replaced.
            script = directory / "process_fixture.py"
            script.write_text('''import json,os,pathlib,sys,time
from datetime import datetime,timezone
root=pathlib.Path(os.environ['CADENCE_FIXTURE_DIR'])
stop=root/'cadence-cpu-stop'
stamp=lambda value: datetime.fromtimestamp(value,timezone.utc).isoformat()
if sys.argv[1]=='forward':
 print('Forwarding from 127.0.0.1:'+os.environ['CADENCE_FIXTURE_PORT']+' -> 6565',flush=True)
 while not stop.exists(): time.sleep(.02)
else:
 sequence=0
 while not stop.exists():
  now=time.time(); sequence+=1
  print(json.dumps({'kind':'cpu_observation','protocol_version':'cadence-cpu-v1','sequence':sequence,
   'target_uid':'target','status':'success','error':'','observation_started_at':stamp(now),
   'observation_finished_at':stamp(now),'evaluated_at':stamp(now),'source_timestamp':stamp(now-5),
   'utilization_percent':90 if (root/'cpu-hot').exists() else 1.5,'containers':[{'pod':'web','pod_uid':'pod','container':name,
   'runtime_id':name+'-runtime','source_timestamp':stamp(now-5)} for name in ['app','sidecar']],'queries':[]}),flush=True)
  deadline=time.monotonic()+1
  while not stop.exists() and time.monotonic()<deadline: time.sleep(.02)
 print(json.dumps({'kind':'cpu_observer_summary','status':'completed'}),flush=True)
''', encoding="utf-8")
            binaries = {}
            for name, mode in (("cpu-producer", "cpu"), ("kubectl", "forward")):
                executable = directory / (name + ".cmd" if os.name == "nt" else name)
                executable.write_text(f'@"{sys.executable}" "{script}" {mode} %*\r\n' if os.name == "nt" else
                    f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} {mode} "$@"\n', encoding="utf-8")
                executable.chmod(0o700)
                binaries[name] = executable
            pending, stopping = threading.Event(), threading.Event()
            patches, fixture_errors, pending_starts = [], [], []
            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    if not pending.is_set():
                        pending_starts.append(stamp())
                        with log.open("a", encoding="utf-8") as stream:
                            stream.write(json.dumps(event("query", pending_starts[0], query)) + "\n")
                        pending.set()
                    self.reply({"paused": True, "running": False, "stopped": False, "status": 4})

                def do_PATCH(self):
                    patches.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                    self.reply({"paused": False, "running": True, "stopped": False, "status": 7})

                def reply(self, attributes):
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(json.dumps({"data": {"attributes": attributes}}).encode())

                def log_message(self, *_):
                    pass

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            def update_state():
                with (directory / "live-observations.ndjson").open("w", encoding="utf-8") as stream:
                    while not stopping.is_set():
                        at = stamp()
                        stream.write(json.dumps({"kind": "state", "status": "success", "request_started_at": at,
                            "request_finished_at": at, "response": state}) + "\n")
                        stream.flush()
                        if (directory / "cadence-gate.json").exists():
                            (directory / "cadence-stop").write_text("stop", encoding="utf-8")
                        stopping.wait(.05)

            writer = threading.Thread(target=update_state, daemon=True)
            writer.start()
            environment = {**os.environ, "PATH": str(directory) + os.pathsep + os.environ.get("PATH", ""),
                "CADENCE_FIXTURE_DIR": str(directory), "CADENCE_FIXTURE_PORT": str(server.server_port)}
            arguments = [sys.executable, str(CLI), "observe", "--run-dir", str(directory),
                "--context", "kind-phpa-cadence-test", "--cpu-binary", str(binaries["cpu-producer"]),
                "--cpu-sha256", hashlib.sha256(binaries["cpu-producer"].read_bytes()).hexdigest(),
                "--prometheus-address", "http://127.0.0.1:9090", "--target-uid", "target", "--phpa-uid", "phpa",
                "--requeue-seconds", "15", "--offset-seconds", "2", "--pair", "1", "--slot", "2"]
            process = subprocess.Popen(arguments, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            def inject_failure():
                if failure is None:
                    return
                if not pending.wait(4):
                    fixture_errors.append("The HTTP gate did not reach the healthy unfinished cycle")
                    return
                time.sleep(.3)
                if process.poll() is not None:
                    fixture_errors.append("The observer exited during the healthy unfinished cycle")
                    return
                with log.open("a", encoding="utf-8") as stream:
                    if failure in ("query_error", "query_hot"):
                        stream.write(json.dumps(event("decision", pending_starts[0], decision)) + "\n")
                        stream.write(json.dumps(event("finish", pending_starts[0],
                            {"reconcileFinishedAt": stamp(), "requeueAfterSeconds": 15})) + "\n")
                        changed = {"queryError": "Prometheus unavailable"} if failure == "query_error" else {"currentCPU%": 90}
                        stream.write(json.dumps(event("query", stamp(), {**query, **changed})) + "\n")
                    elif failure == "decision_hot":
                        stream.write(json.dumps(event("decision", pending_starts[0],
                            {**decision, "currentCPU%": 90, "finalDesired": 2})) + "\n")
                    elif failure in ("partial_tail", "malformed_line"):
                        stream.write('{"msg":"Queried CPU utilization",' + ("\n" if failure == "malformed_line" else ""))
                    elif failure == "cpu_hot":
                        (directory / "cpu-hot").touch()
                    elif failure == "generation":
                        state["phpa"]["status"]["conditions"][0]["observedGeneration"] = 1
                    elif failure == "replicas":
                        state["deployment"]["status"]["readyReplicas"] = 0
                    elif failure == "identity":
                        state["deployment"]["metadata"]["uid"] = "replaced"

            injector = threading.Thread(target=inject_failure, daemon=True)
            injector.start()
            try:
                stdout, stderr = process.communicate(timeout=12)
                status = json.loads((directory / "cadence-status.json").read_text())
                gate = json.loads((directory / "cadence-gate.json").read_text()) if (directory / "cadence-gate.json").exists() else None
                return process.returncode, stdout + stderr, status, gate, patches, fixture_errors
            finally:
                stopping.set()
                (directory / "cadence-cpu-stop").touch(exist_ok=True)
                writer.join(timeout=2)
                injector.join(timeout=2)
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=3)
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=2)

    def test_healthy_unfinished_reconciliation_does_not_cancel_source_gate(self):
        code, output, status, gate, patches, errors = self.run_live_observer()
        self.assertEqual(0, code, output + json.dumps(status))
        self.assertEqual([], errors)
        self.assertEqual("success", status["status"])
        self.assertTrue(status["owned_processes_stopped"])
        self.assertEqual("released", gate["status"])
        self.assertEqual(1, len(patches))

    def test_new_query_error_during_inflight_reconciliation_still_cancels_gate(self):
        code, output, status, gate, patches, errors = self.run_live_observer("query_error")
        self.assertEqual([], errors)
        self.assertEqual(3, code, output + json.dumps(status))
        self.assertEqual("failed", status["status"])
        self.assertIsNone(gate)
        self.assertEqual([], patches)

    def test_unfinished_final_log_line_does_not_cancel_an_eligible_source_anchor(self):
        code, output, status, gate, patches, errors = self.run_live_observer("partial_tail")
        self.assertEqual([], errors)
        self.assertEqual(0, code, output + json.dumps(status))
        self.assertEqual("released", gate["status"])
        self.assertEqual(1, len(patches))

    def test_actual_warm_changes_and_complete_bad_log_lines_still_cancel_gate(self):
        for failure in ("query_hot", "decision_hot", "cpu_hot", "generation", "replicas", "identity", "malformed_line"):
            with self.subTest(failure=failure):
                code, output, status, gate, patches, errors = self.run_live_observer(failure)
                self.assertEqual([], errors)
                self.assertEqual(3, code, output + json.dumps(status))
                self.assertEqual("failed", status["status"])
                self.assertIsNone(gate)
                self.assertEqual([], patches)


if __name__ == "__main__":
    unittest.main()
