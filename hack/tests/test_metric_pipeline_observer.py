"""Exercise the observer CLI against HTTP and kubectl process boundaries."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[2]
OBSERVER = ROOT / "hack/run_latency_diagnostic.py"


class MetricPipelineObserverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="metric-observer-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "observations.ndjson"
        self.requests = []
        self.query_responses = {}
        self.targets = {"status": "success", "data": {"activeTargets": []}}
        self.target_delay = 0
        case = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                url = urlsplit(self.path)
                params = parse_qs(url.query)
                case.requests.append((url.path, params))
                if url.path == "/api/v1/targets":
                    time.sleep(case.target_delay)
                    body = case.targets
                else:
                    query = params["query"][0]
                    body = case.query_responses.get(query, {
                        "status": "success", "data": {"resultType": "vector", "result": []}})
                status, body = body if isinstance(body, tuple) else (200, body)
                encoded = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *_arguments) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.source = self.root / "source.txt"
        self.source.write_text("", encoding="utf-8")
        self.trace = self.root / "kubectl.json"
        script = self.root / "fake_kubectl.py"
        script.write_text(
            "import json, os, pathlib, sys\n"
            "pathlib.Path(os.environ['TEST_KUBE_TRACE']).write_text(json.dumps(sys.argv[1:]))\n"
            "sys.stdout.write(pathlib.Path(os.environ['TEST_CADVISOR']).read_text())\n",
            encoding="utf-8")
        if os.name == "nt":
            (self.root / "kubectl.cmd").write_text(
                f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        else:
            stub = self.root / "kubectl"
            stub.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} \"$@\"\n")
            stub.chmod(0o755)

    def sample(self, source_node: str = "observer-test-control-plane") -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(OBSERVER), "sample", "--output", str(self.output),
            "--context", "kind-observer-test", "--source-node", source_node,
            "--prometheus-url", self.url], capture_output=True, text=True, timeout=20,
            env={**os.environ, "PATH": str(self.root) + os.pathsep + os.environ.get("PATH", ""),
                 "TEST_CADVISOR": str(self.source), "TEST_KUBE_TRACE": str(self.trace)})

    def rows(self) -> dict:
        return {row["kind"]: row for row in map(json.loads, self.output.read_text().splitlines())}

    def test_sample_compares_raw_and_both_windows_at_one_evaluation_time(self) -> None:
        result = self.sample()
        self.assertEqual(0, result.returncode, result.stderr)
        rows = self.rows()
        kinds = ("prom_cpu_raw", "prom_requests_raw", "prom_cpu_evaluated", "prom_cpu_evaluated_30s")
        self.assertEqual({1}, {rows[kind]["cycle_id"] for kind in kinds})
        self.assertEqual(1, len({rows[kind]["evaluation_time_unix"] for kind in kinds}))
        self.assertEqual(1, len({params["time"][0] for path, params in self.requests if path == "/api/v1/query"}))
        self.assertIn("[1m]", rows["prom_cpu_evaluated"]["query"])
        self.assertIn("[30s]", rows["prom_cpu_evaluated_30s"]["query"])
        self.assertEqual([], rows["prom_cpu_evaluated_30s"]["response"]["data"]["result"])

    def test_source_evidence_keeps_original_counter_and_last_seen_timestamps(self) -> None:
        retained = [
            'container_cpu_usage_seconds_total{container="php-apache",namespace="default",pod="php-apache-abc",cpu="total"} 1.250 1700000000123',
            'container_last_seen{namespace="default",pod="php-apache-abc",container="php-apache"} 1700000000.125 1700000000123',
        ]
        excluded = [
            '# HELP container_cpu_usage_seconds_total CPU counter',
            'container_cpu_usage_seconds_total{namespace="other",pod="php-apache-abc"} 12 1700000000123',
            'container_last_seen{namespace="default",pod="unrelated"} 1700000000.125',
            'container_memory_usage_bytes{namespace="default",pod="php-apache-abc"} 99',
            'container_cpu_usage_seconds_total{namespace="default",pod="php-apache-abc",container="",id="parent-cgroup"} 1.5',
        ]
        self.source.write_text("\n".join([*retained, *excluded]) + "\n", encoding="utf-8")
        result = self.sample()
        self.assertEqual(0, result.returncode, result.stderr)
        row = self.rows()["source_cadvisor"]
        self.assertEqual({"lines": retained}, row["response"])
        self.assertEqual("observer-test-control-plane", row["source_node"])
        self.assertEqual(["--context", "kind-observer-test", "--namespace", "default", "--request-timeout=10s",
            "get", "--raw", "/api/v1/nodes/observer-test-control-plane/proxy/metrics/cadvisor"],
            json.loads(self.trace.read_text()))

    def test_targets_preserve_failed_scrape_evidence_without_auth_configuration(self) -> None:
        target = {"labels": {"job": "kubernetes-nodes-cadvisor", "instance": "observer-test-control-plane"},
            "discoveredLabels": {"__meta_kubernetes_node_name": "observer-test-control-plane", "secret": "private"},
            "health": "down", "lastScrape": "2023-11-14T22:13:20.125Z", "lastScrapeDuration": 0.4,
            "lastError": "server returned HTTP status 503", "scrapeUrl": "https://api/api/v1/nodes/observer-test-control-plane/proxy/metrics/cadvisor",
            "scrapeInterval": "15s", "scrapeTimeout": "10s", "authorization": {"credentials": "private"}}
        unrelated = {**target, "labels": {"job": "kubernetes-nodes-cadvisor", "instance": "another-node"},
            "discoveredLabels": {"__meta_kubernetes_node_name": "another-node"}}
        self.targets["data"]["activeTargets"] = [target, unrelated,
            {**target, "labels": {"job": "kubernetes-nodes", "instance": "observer-test-control-plane"}}]
        result = self.sample()
        self.assertEqual(0, result.returncode, result.stderr)
        observed = self.rows()["prom_scrape_targets"]["response"]["data"]["activeTargets"]
        self.assertEqual([{key: value for key, value in target.items() if key not in ("authorization", "discoveredLabels")}
            | {"discoveredLabels": {"__meta_kubernetes_node_name": "observer-test-control-plane"}}], observed)
        self.assertNotIn("private", self.output.read_text())
        self.assertIn(("/api/v1/targets", {"state": ["active"]}), self.requests)

    def test_api_errors_remain_error_evidence_while_empty_vectors_remain_success(self) -> None:
        query = '(avg(rate(container_cpu_usage_seconds_total{namespace="default",pod=~"php-apache-.*",container!=""}[30s]))/avg(kube_pod_container_resource_requests{namespace="default",pod=~"php-apache-.*",resource="cpu"}))*100'
        response = {"status": "error", "errorType": "execution", "error": "query timed out",
                    "data": {"result": []}}
        for http_status in (200, 422):
            with self.subTest(http_status=http_status):
                self.output = self.root / f"error-{http_status}.ndjson"
                self.query_responses[query] = (http_status, response)
                self.targets = (http_status, response)
                result = self.sample()
                self.assertEqual(3, result.returncode, result.stderr)
                rows = self.rows()
                for kind in ("prom_cpu_evaluated_30s", "prom_scrape_targets"):
                    self.assertEqual("error", rows[kind]["status"])
                    self.assertEqual(response, rows[kind]["response"])
                self.assertEqual("success", rows["prom_cpu_evaluated"]["status"])
                self.assertEqual([], rows["prom_cpu_evaluated"]["response"]["data"]["result"])

    def test_cycle_exposes_observation_cost_and_enabled_mode(self) -> None:
        self.target_delay = 0.2
        result = self.sample()
        self.assertEqual(0, result.returncode, result.stderr)
        rows = self.rows()
        cycle = rows.pop("observer_cycle")
        self.assertEqual(5, cycle["query_count"])
        self.assertTrue(cycle["metric_pipeline_diagnostic"])
        self.assertEqual("observer-test-control-plane", cycle["metric_pipeline_source_node"])
        self.assertEqual("metric-pipeline-v1", cycle["metric_pipeline_protocol_version"])
        self.assertGreaterEqual(cycle["duration_seconds"], 0.2)
        self.assertGreaterEqual(cycle["overrun_seconds"], 0)
        self.assertEqual(2, cycle["observation_interval_seconds"])
        for row in rows.values():
            self.assertLessEqual(cycle["request_started_at"], row["request_started_at"])
            self.assertGreaterEqual(cycle["request_finished_at"], row["request_finished_at"])

    def test_scrape_history_keeps_a_failed_scrape_between_target_polls(self) -> None:
        query = '{__name__=~"up|scrape_duration_seconds|scrape_samples_scraped|scrape_samples_post_metric_relabeling",job="kubernetes-nodes-cadvisor",instance="observer-test-control-plane"}[90s]'
        response = {"status": "success", "data": {"resultType": "matrix", "result": [{
            "metric": {"__name__": "up", "job": "kubernetes-nodes-cadvisor", "instance": "observer-test-control-plane"},
            "values": [[1700000000, "1"], [1700000015, "0"], [1700000030, "1"]]}]}}
        self.query_responses[query] = response
        result = self.sample()
        self.assertEqual(0, result.returncode, result.stderr)
        rows = self.rows()
        self.assertEqual(response, rows["prom_scrape_raw"]["response"])
        self.assertEqual(rows["prom_cpu_raw"]["evaluation_time_unix"], rows["prom_scrape_raw"]["evaluation_time_unix"])

    def test_sample_refuses_to_overwrite_evidence_before_contacting_services(self) -> None:
        original = "existing evidence\n"
        self.output.write_text(original)
        result = self.sample()
        self.assertEqual(3, result.returncode)
        self.assertIn("refuses to overwrite", result.stderr)
        self.assertEqual(original, self.output.read_text())
        self.assertEqual([], self.requests)
        self.assertFalse(self.trace.exists())

    def test_source_node_is_validated_before_a_preflight_request(self) -> None:
        result = self.sample("node/../../other")
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertIn("source-node", result.stderr)
        self.assertFalse(self.output.exists())
        self.assertEqual([], self.requests)

    def test_observe_accepts_explicit_pipeline_mode_before_starting_the_run(self) -> None:
        result = subprocess.run([sys.executable, str(OBSERVER), "observe", "--run-dir", str(self.root / "missing"),
            "--context", "kind-observer-test", "--offset-seconds", "10", "--metric-pipeline-diagnostic",
            "--source-node", "observer-test-control-plane"], capture_output=True, text=True, timeout=10)
        self.assertEqual(3, result.returncode, result.stderr)
        self.assertIn("existing benchmark run directory", result.stderr)


if __name__ == "__main__":
    unittest.main()
