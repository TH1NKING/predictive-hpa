import json
import tempfile
import unittest
from pathlib import Path

from calibration import summarize_probe, summarize_run, write_report


class CalibrationEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.probe = self.root / "replicas-2_rps-1_test"
        self.probe.mkdir()
        self.put("probe.json", {"token": "probe-1", "replicas": 2, "rps": 1,
                                "duration_seconds": 90, "status": "success"})
        pods = [{"metadata": {"name": name, "uid": name}, "status": {
            "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [{"restartCount": 0}]}} for name in ("pod-a", "pod-b")]
        endpoints = {"items": [{"endpoints": [{"targetRef": {"kind": "Pod", "uid": name},
                      "conditions": {"ready": True}} for name in ("pod-a", "pod-b")]}]}
        for phase in ("before", "after"):
            self.put(f"pods-{phase}.json", {"items": pods})
            self.put(f"endpoints-{phase}.json", endpoints)
        self.put("cpu-by-pod.json", {"status": "success", "data": {"result": [
            {"metric": {"pod": name}, "values": [[145, "0.01"], [160, "0.02"]]}
            for name in ("pod-a", "pod-b")]}})
        (self.probe / "k6-start-time-unix").write_text("100\n")
        (self.probe / "k6-end-time-unix").write_text("190\n")
        points = [{"type": "Point", "metric": metric, "data": {"value": value}}
                  for metric, value in (("http_reqs", 2), ("http_req_failed", 0),
                                        ("http_req_duration", 10), ("http_req_duration", 12))]
        (self.probe / "k6.json").write_text("\n".join(json.dumps(p) for p in points))
        for name in ("pod-a", "pod-b"):
            (self.probe / f"{name}.log").write_text(
                '10.0.0.1 - - [date] "GET / HTTP/1.1" 200 10 "-" "phpa-routing/probe-1"\n'
                '10.0.0.1 - - [date] "GET / HTTP/1.1" 200 10 "-" "phpa-routing/old-probe"\n')

    def put(self, name, data):
        (self.probe / name).write_text(json.dumps(data), encoding="utf-8")

    def test_distribution_requires_identified_requests_and_cpu_for_every_pod(self):
        result = summarize_probe(self.probe)
        self.assertTrue(result["routing_observed"])
        self.assertEqual([1, 1], [p["requests"] for p in result["pods"]])
        self.assertFalse(result["capacity_validated"])

    def test_single_pod_traffic_is_unverified_despite_cpu_on_all_pods(self):
        (self.probe / "pod-b.log").write_text("")
        result = summarize_probe(self.probe)
        self.assertFalse(result["routing_observed"])
        self.assertIn("No identified probe requests for pod-b", result["problems"])

    def test_endpoint_mismatch_and_pod_restart_invalidate_fixed_replica_probe(self):
        self.put("endpoints-after.json", {"items": []})
        data = json.loads((self.probe / "pods-after.json").read_text())
        data["items"][0]["status"]["containerStatuses"][0]["restartCount"] = 1
        self.put("pods-after.json", data)
        result = summarize_probe(self.probe)
        self.assertFalse(result["routing_observed"])
        self.assertEqual(2, len(result["problems"]))

    def test_missing_data_is_reported_and_never_promoted_to_success(self):
        (self.probe / "cpu-by-pod.json").unlink()
        summary = summarize_run(self.root)
        self.assertFalse(summary["probes"][0]["routing_observed"])
        write_report(self.root, summary)
        self.assertIn("unverified", (self.root / "report.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
