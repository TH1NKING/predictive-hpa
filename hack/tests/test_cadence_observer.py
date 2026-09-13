"""Cadence launch contracts through the public offline and HTTP CLIs."""
from datetime import datetime, timedelta, timezone
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import os
import signal
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


if __name__ == "__main__":
    unittest.main()
