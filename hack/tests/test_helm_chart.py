"""Render the real chart to verify manager health probes with either metrics mode."""

import os
from pathlib import Path
import shutil
import subprocess
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
HELM = os.environ.get("HELM") or shutil.which("helm")


@unittest.skipUnless(HELM, "Helm is required to render the chart (set HELM to its executable)")
class HelmHealthProbeTests(unittest.TestCase):
    def test_health_probes_remain_available_with_metrics_enabled_or_disabled(self):
        for enabled in (False, True):
            with self.subTest(metrics=enabled):
                result = subprocess.run(
                    [HELM, "template", "interview", str(ROOT / "deploy/charts/predictive-hpa"),
                     "--namespace", "interview-manager", "--set", "replicaCount=2",
                     "--set", "metricsService.enabled=" + str(enabled).lower()],
                    capture_output=True, text=True, encoding="utf-8", timeout=30,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                documents = list(yaml.safe_load_all(result.stdout))
                deployment = next(x for x in documents if x and x["kind"] == "Deployment")
                manager = deployment["spec"]["template"]["spec"]["containers"][0]
                self.assertEqual(2, deployment["spec"]["replicas"])
                self.assertIn("--health-probe-bind-address=:8081", manager["args"])
                ports = {p["name"]: p["containerPort"] for p in manager["ports"]}
                self.assertEqual(8081, ports["health"])
                self.assertEqual(enabled, "metrics" in ports)
                for probe, path in (("livenessProbe", "/healthz"),
                                    ("readinessProbe", "/readyz")):
                    self.assertEqual({"path": path, "port": "health"}, manager[probe]["httpGet"])
                    self.assertGreater(manager[probe]["periodSeconds"], 0)


if __name__ == "__main__":
    unittest.main()
