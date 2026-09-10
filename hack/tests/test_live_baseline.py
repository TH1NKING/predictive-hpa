"""Live baseline contracts through public observer and runner CLIs."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import shlex
import shutil
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]
OBSERVER = ROOT / "hack/observe_live_baseline.py"


class LiveReadinessCLI(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="live-baseline-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.now = datetime.now(timezone.utc)
        self.state = {"deployment": {"metadata": {"uid": "target-uid"},
                                   "spec": {"replicas": 1}, "status": {"replicas": 1, "readyReplicas": 1}},
                      "phpa": {"metadata": {"uid": "phpa-uid", "generation": 2},
                               "spec": {"decisionMode": "Current"},
                               "status": {"conditions": [{"type": "MetricsReady", "status": "True",
                                                            "observedGeneration": 2}]}},
                      "pods": {"items": []}}
        self.cycle = {"reconcileID": "cycle", "reconcileStartedAt": self.stamp(-2),
                      "query": {"queryError": "", "samples": 3, "sourceTimestamp": self.stamp(-5)},
                      "decision": {"decisionMode": "Current", "samples": 3, "currentCPU%": 1.5,
                                   "currentReplicas": 1, "finalDesired": 1, "coldStartProtection": False,
                                   "coldStartProtectedUntil": self.stamp(-30),
                                   "stabilizationEvaluatedAt": self.stamp(-1)},
                      "finish": {"reconcileFinishedAt": self.stamp(-0.5), "reconcileError": ""}}

    def stamp(self, offset: float) -> str:
        return (self.now + timedelta(seconds=offset)).isoformat()

    def run_gate(self) -> subprocess.CompletedProcess:
        (self.directory / "state.json").write_text(json.dumps(self.state), encoding="utf-8")
        messages = {"query": "Queried CPU utilization", "decision": "Evaluated PredictiveHPA scaling decision",
                    "finish": "Finished PredictiveHPA reconciliation"}
        rows = [{"msg": message, "reconcileID": self.cycle["reconcileID"],
                 "reconcileStartedAt": self.cycle["reconcileStartedAt"], **self.cycle[key]}
                for key, message in messages.items()]
        (self.directory / "controller.log").write_text("\n".join(map(json.dumps, rows)) + "\n", encoding="utf-8")
        return subprocess.run([sys.executable, str(OBSERVER), "check-ready", "--state", str(self.directory / "state.json"),
            "--controller-log", str(self.directory / "controller.log"), "--decision-mode", "Current",
            "--target-uid", "target-uid", "--phpa-uid", "phpa-uid", "--output", str(self.directory / "gate.json")],
            capture_output=True, text=True, timeout=15)

    def test_ready_receipt_requires_verified_history_and_completed_protection(self) -> None:
        result = self.run_gate()
        self.assertEqual(0, result.returncode, result.stderr)
        gate = json.loads((self.directory / "gate.json").read_text())
        self.assertEqual("target-uid", gate["target_uid"])
        self.assertEqual("phpa-uid", gate["phpa_uid"])
        self.assertEqual("cycle", gate["anchor"]["reconcileID"])

    def test_unready_or_replaced_target_never_releases_warm_load(self) -> None:
        mutations = [lambda: self.cycle["decision"].update(samples=1),
                     lambda: self.cycle["decision"].update(coldStartProtection=True),
                     lambda: self.cycle["decision"].update(coldStartProtectedUntil=self.stamp(30)),
                     lambda: self.cycle["finish"].update(reconcileError="conflict"),
                     lambda: self.state["phpa"]["status"]["conditions"][0].update(observedGeneration=1),
                     lambda: self.state["deployment"]["status"].update(readyReplicas=0),
                     lambda: self.state["deployment"]["metadata"].update(uid="replacement")]
        original_state, original_cycle = json.dumps(self.state), json.dumps(self.cycle)
        for mutation in mutations:
            with self.subTest(mutation=mutations.index(mutation)):
                self.state, self.cycle = json.loads(original_state), json.loads(original_cycle)
                mutation()
                self.assertNotEqual(0, self.run_gate().returncode)
                self.assertFalse((self.directory / "gate.json").exists())

    def test_readiness_receipt_is_never_overwritten(self) -> None:
        (self.directory / "gate.json").write_text("retained evidence", encoding="utf-8")
        self.assertNotEqual(0, self.run_gate().returncode)
        self.assertEqual("retained evidence", (self.directory / "gate.json").read_text())


class LiveSampleCLI(unittest.TestCase):
    def test_cold_observer_records_gate_and_stops_without_owned_processes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="live-cold-") as temporary:
            directory = Path(temporary)
            script = directory / "fake_kubectl.py"
            script.write_text("import json,sys\n"
                "resource=sys.argv[sys.argv.index('get')+1]\n"
                "print(json.dumps({'metadata':{'uid':'phpa-uid' if resource=='predictivehpa' else 'target-uid'},'items':[]}))\n")
            if os.name == "nt":
                (directory / "kubectl.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n')
            else:
                command = directory / "kubectl"
                command.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} \"$@\"\n")
                command.chmod(0o755)
            arguments = [sys.executable, str(OBSERVER), "observe", "--run-dir", str(directory),
                "--context", "kind-live-test", "--startup-mode", "cold", "--decision-mode", "Current",
                "--pattern", "step", "--rps", "25", "--controller-started-at", datetime.now(timezone.utc).isoformat(),
                "--target-uid", "target-uid", "--phpa-uid", "phpa-uid", "--source-sha256", "a" * 64,
                "--binary-sha256", "b" * 64]
            process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env={**os.environ, "PATH": str(directory) + os.pathsep + os.environ.get("PATH", "")})
            try:
                deadline = time.monotonic() + 8
                while not (directory / "live-baseline-gate.json").exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                (directory / "live-baseline-stop").write_text("stop\n")
                stdout, stderr = process.communicate(timeout=15)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
            self.assertEqual(0, process.returncode, stdout + stderr)
            gate = json.loads((directory / "live-baseline-gate.json").read_text())
            self.assertEqual("cold", gate["startup_mode"])
            self.assertIsNone(gate["anchor"])
            status = json.loads((directory / "live-baseline-status.json").read_text())
            self.assertEqual("success", status["status"])
            self.assertTrue(status["owned_processes_stopped"])
            self.assertTrue(status["gate_released"])

    def test_sample_retains_individual_read_intervals_and_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="live-sample-") as temporary:
            directory = Path(temporary)
            script = directory / "fake_kubectl.py"
            script.write_text("import json,sys\n"
                "resource=sys.argv[sys.argv.index('get')+1]\n"
                "if resource=='predictivehpa':\n print('unavailable',file=sys.stderr);sys.exit(1)\n"
                "print(json.dumps({'metadata':{'uid':'target'},'kind':resource}))\n", encoding="utf-8")
            if os.name == "nt":
                (directory / "kubectl.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n')
            else:
                command = directory / "kubectl"
                command.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} \"$@\"\n")
                command.chmod(0o755)
            output = directory / "observations.ndjson"
            result = subprocess.run([sys.executable, str(OBSERVER), "sample", "--context", "kind-live-test",
                "--output", str(output)], capture_output=True, text=True, timeout=20,
                env={**os.environ, "PATH": str(directory) + os.pathsep + os.environ.get("PATH", "")})
            self.assertEqual(3, result.returncode, result.stderr)
            rows = [json.loads(line) for line in output.read_text().splitlines()]
            self.assertEqual(1, len(rows))
            self.assertEqual("state", rows[0]["kind"])
            self.assertEqual("error", rows[0]["status"])
            self.assertEqual("target", rows[0]["response"]["deployment"]["metadata"]["uid"])
            self.assertEqual({"deployment", "phpa", "pods"}, set(rows[0]["requests"]))
            self.assertIn("unavailable", rows[0]["requests"]["phpa"]["error"])


class LiveBenchmarkCLI(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "Windows TerminateProcess cannot exercise POSIX signal handlers")
    def test_campaign_sigterm_preserves_status_and_stops_owned_command_descendants(self) -> None:
        with tempfile.TemporaryDirectory(prefix="live-signal-") as temporary:
            directory = Path(temporary)
            kubeconfig = directory / "private-kubeconfig"
            kubeconfig.write_text("isolated fixture")
            child = directory / "child.py"
            child.write_text("import os,pathlib,signal,time\n"
                "root=pathlib.Path(os.environ['LIVE_SIGNAL_DIR'])\n"
                "def stop(*_):\n time.sleep(0.2);(root/'child-stopped').write_text('terminated');raise SystemExit(0)\n"
                "signal.signal(signal.SIGTERM,stop)\n"
                "(root/'child-started').write_text(str(os.getpid()))\n"
                "while True: time.sleep(1)\n")
            command = directory / "kubectl"
            command.write_text(f"#!{sys.executable}\nimport os,pathlib,subprocess,sys,time\n"
                "root=pathlib.Path(os.environ['LIVE_SIGNAL_DIR'])\n"
                "(root/'command-pid').write_text(str(os.getpid()))\n"
                "subprocess.Popen([sys.executable,str(root/'child.py')])\n"
                "while True: time.sleep(1)\n")
            command.chmod(0o755)
            output = directory / "campaign"
            process = subprocess.Popen([sys.executable, str(ROOT / "hack/run_live_campaign.py"), "--pattern", "step",
                "--context", "kind-phpa-live-baseline-test", "--kubeconfig", str(kubeconfig), "--output", str(output)],
                env={**os.environ, "PATH": str(directory) + os.pathsep + os.environ.get("PATH", ""), "LIVE_SIGNAL_DIR": str(directory)},
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
            try:
                deadline = time.monotonic() + 5
                while not (directory / "child-started").exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue((directory / "child-started").exists(), "Fake command descendant did not start")
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=12)
                self.assertEqual(3, process.returncode, stdout + stderr)
                report = json.loads((output / "campaign-status.json").read_text())
                self.assertEqual("failed", report["status"])
                self.assertEqual(["not_run"] * 9, [slot["status"] for slot in report["slots"]])
                self.assertTrue((directory / "child-stopped").exists(), "Campaign returned before its descendant stopped")
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                if (directory / "command-pid").exists():
                    try:
                        os.killpg(int((directory / "command-pid").read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_campaign_plan_has_all_rotated_assignments_before_any_run(self) -> None:
        result = subprocess.run([sys.executable, str(ROOT / "hack/run_live_campaign.py"), "--plan", "--pattern", "step"],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(["Current", "Predictive", "Hybrid", "Predictive", "Hybrid", "Current", "Hybrid", "Current", "Predictive"],
                         [slot["decision_mode"] for slot in plan["slots"]])
        self.assertEqual(["not_run"] * 9, [slot["status"] for slot in plan["slots"]])
        self.assertEqual({"http_200_ratio_min": 0.99, "all_request_p95_ms_max": 500, "dropped_iterations_max": 0},
                         plan["service_criteria"])

    def test_campaign_retains_preflight_failure_and_refuses_output_reuse(self) -> None:
        with tempfile.TemporaryDirectory(prefix="live-campaign-") as temporary:
            directory = Path(temporary)
            kubeconfig = directory / "private-kubeconfig"
            kubeconfig.write_text("private fixture")
            script = directory / "reject_kubectl.py"
            script.write_text("import sys\nprint('preflight rejected',file=sys.stderr)\nsys.exit(7)\n")
            if os.name == "nt":
                (directory / "kubectl.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n')
            else:
                command = directory / "kubectl"
                command.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} \"$@\"\n")
                command.chmod(0o755)
            output = directory / "evidence"
            arguments = [sys.executable, str(ROOT / "hack/run_live_campaign.py"), "--pattern", "step",
                "--context", "kind-phpa-live-baseline-test", "--kubeconfig", str(kubeconfig), "--output", str(output)]
            environment = {**os.environ, "PATH": str(directory) + os.pathsep + os.environ.get("PATH", "")}
            result = subprocess.run(arguments, env=environment, capture_output=True, text=True, timeout=15)
            self.assertEqual(3, result.returncode, result.stderr)
            report = json.loads((output / "campaign-status.json").read_text())
            self.assertEqual("failed", report["status"])
            self.assertEqual(["not_run"] * 9, [slot["status"] for slot in report["slots"]])
            self.assertTrue((output / "campaign-plan.json").exists())
            original = (output / "campaign-status.json").read_bytes()
            second = subprocess.run(arguments, env=environment, capture_output=True, text=True, timeout=15)
            self.assertNotEqual(0, second.returncode)
            self.assertEqual(original, (output / "campaign-status.json").read_bytes())
            self.assertNotIn("private fixture", "".join(path.read_text(errors="replace") for path in output.rglob("*") if path.is_file()))

    def test_campaign_stops_later_slots_after_failed_runner_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="live-slot-failure-") as temporary:
            directory = Path(temporary)
            kubeconfig = directory / "private-kubeconfig"
            kubeconfig.write_text("isolated fixture")
            script = directory / "commands.py"
            script.write_text("""import json,os,pathlib,sys
name=sys.argv[1]; args=sys.argv[2:]
if name=='kubectl':
 if 'config' in args: print('kind-phpa-live-baseline-test');sys.exit(0)
 resource=args[args.index('get')+1]
 values={
  'namespace':{'metadata':{'uid':'cluster'}},
  'nodes':{'items':[{'metadata':{'uid':'node','name':'node'},'spec':{},'status':{'nodeInfo':{'bootID':'boot'},'capacity':{},'allocatable':{}}}]},
  'deployment':{'metadata':{'uid':'target'},'spec':{'replicas':1}},
  'service':{'metadata':{'uid':'service'},'spec':{'clusterIP':'10.0.0.1'}},
  'hpa':{'items':[]},'predictivehpas':{'items':[]},'configmaps':{'items':[]},
  'pods':{'items':[{'metadata':{'namespace':'default','labels':{'run':'php-apache'}},'status':{'phase':'Running','containerStatuses':[{'imageID':'sha256:workload'}]}}]}}
 print(json.dumps(values[resource]))
elif name=='kind': print('phpa-live-baseline-test')
elif name=='go': pathlib.Path(args[args.index('-o')+1]).write_bytes(b'frozen test binary')
elif name=='bash':
 output=pathlib.Path(os.environ['EXPERIMENTS_ROOT'])/'failed-slot'
 output.mkdir();(output/'partial-receipt.txt').write_text('owned runner cleanup failed')
 sys.exit(3)
else: raise RuntimeError(name)
""", encoding="utf-8")
            for name in ("kubectl", "kind", "go", "bash"):
                if os.name == "nt":
                    (directory / (name + ".cmd")).write_text(f'@"{sys.executable}" "{script}" {name} %*\r\n')
                else:
                    command = directory / name
                    command.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} {name} \"$@\"\n")
                    command.chmod(0o755)
            output = directory / "campaign"
            result = subprocess.run([sys.executable, str(ROOT / "hack/run_live_campaign.py"), "--pattern", "step",
                "--context", "kind-phpa-live-baseline-test", "--kubeconfig", str(kubeconfig), "--output", str(output)],
                env={**os.environ, "PATH": str(directory) + os.pathsep + os.environ.get("PATH", "")},
                capture_output=True, text=True, timeout=30)
            self.assertEqual(3, result.returncode, result.stderr)
            status = json.loads((output / "campaign-status.json").read_text())
            self.assertEqual(["failed"] + ["not_run"] * 8, [slot["status"] for slot in status["slots"]])
            self.assertEqual(["runs/failed-slot"], status["slots"][0]["partial_run_dirs"])
            self.assertTrue((output / "runs/failed-slot/partial-receipt.txt").is_file())
            self.assertEqual([], list((output / "analysis").iterdir()))

    @unittest.skipUnless(shutil.which("node"), "Node is required for offline workload checks")
    def test_workload_preserves_step_and_ramp_and_records_actual_scenario_clock(self) -> None:
        loader = r"""
const vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
const calls=[],logs=[],points=[];
const context=vm.createContext({__ENV:{RPS:'25',BASELINE_PATTERN:process.argv[1]},console:{log:v=>logs.push(v)}});
function synthetic(names, values){return new vm.SyntheticModule(names,function(){names.forEach(n=>this.setExport(n,values[n]));},{context});}
const stubs={
 'k6/http':synthetic(['default'],{default:{get:(...a)=>calls.push(a)}}),
 'k6/execution':synthetic(['default'],{default:{scenario:{startTime:1788998400123,iterationInTest:0}}}),
 'k6/metrics':synthetic(['Trend'],{Trend:class {constructor(name){this.name=name;} add(value){points.push([this.name,value]);}}})};
const modules=new Map();function load(file){if(!modules.has(file))modules.set(file,new vm.SourceTextModule(fs.readFileSync(file,'utf8'),{context,identifier:file}));return modules.get(file);}
(async()=>{const module=load(path.resolve('hack/k6/baseline.js'));await module.link((name,parent)=>stubs[name]||load(path.resolve(path.dirname(parent.identifier),name)));await module.evaluate();module.namespace.default();process.stdout.write(JSON.stringify({options:module.namespace.options,logs,points,calls}));})().catch(e=>{process.stderr.write(e.stack);process.exitCode=1;});
"""
        for pattern, duration, end in (("step", 211, 1788998611.123), ("ramp", 270, 1788998670.123)):
            with self.subTest(pattern=pattern):
                result = subprocess.run([shutil.which("node"), "--experimental-vm-modules", "-e", loader, pattern],
                    cwd=ROOT, capture_output=True, text=True, timeout=15)
                self.assertEqual(0, result.returncode, result.stderr)
                data = json.loads(result.stdout)
                scenario = next(iter(data["options"]["scenarios"].values()))
                self.assertEqual(duration, sum(int(item["duration"][:-1]) for item in scenario["stages"]))
                schedule = json.loads(data["logs"][0].removeprefix("PHPA_BASELINE_SCHEDULE "))
                self.assertEqual(1788998430.123, schedule["onset_unix"])
                self.assertEqual(end, schedule["offered_end_unix"])
                self.assertEqual([["baseline_request_attempt", 1788998400123]], data["points"])
                self.assertEqual("http://php-apache.default.svc:80/", data["calls"][0][0])

    def test_live_plan_rotates_three_modes_and_separates_startup_identity(self) -> None:
        if os.name == "nt":
            bash = next((str(path) for path in (Path("D:/Git/bin/bash.exe"), Path("C:/Program Files/Git/bin/bash.exe"))
                         if path.is_file()), None)
        else:
            bash = shutil.which("bash")
        if not bash:
            self.skipTest("Bash unavailable")
        with tempfile.TemporaryDirectory(prefix="live-plan-") as temporary:
            base = {**os.environ, "LIVE_BASELINE": "true", "EXPERIMENTS_ROOT": temporary,
                    "BENCHMARK_CONTEXT": "kind-live-test", "BENCHMARK_PYTHON": sys.executable}
            warm = subprocess.run([bash, "hack/run_matrix.sh", "--dry-run"], cwd=ROOT,
                env={**base, "LIVE_BASELINE_STARTUP_MODE": "warm"}, capture_output=True, text=True, timeout=30)
            cold = subprocess.run([bash, "hack/run_matrix.sh", "--dry-run"], cwd=ROOT,
                env={**base, "LIVE_BASELINE_STARTUP_MODE": "cold"}, capture_output=True, text=True, timeout=30)
            self.assertEqual(0, warm.returncode, warm.stderr)
            self.assertEqual(0, cold.returncode, cold.stderr)
            self.assertIn("18 experiments planned", warm.stdout)
            self.assertIn("phpa_hybrid", warm.stdout)
            self.assertNotIn("native_hpa_300", warm.stdout)
            self.assertNotEqual([line for line in warm.stdout.splitlines() if line.startswith("Configuration SHA256:")],
                                [line for line in cold.stdout.splitlines() if line.startswith("Configuration SHA256:")])



if __name__ == "__main__":
    unittest.main()
