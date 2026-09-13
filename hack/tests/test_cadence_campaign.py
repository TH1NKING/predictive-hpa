"""Cadence experiment contracts through public campaign and shell CLIs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

import test_benchmark_ablation as shell_tests
import test_k6_runner as runner_tests

ROOT = Path(__file__).resolve().parents[2]


class CadenceCampaignCLI(unittest.TestCase):
    def campaign(self, *arguments: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(ROOT / 'hack/run_cadence_campaign.py'), *arguments],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=10)

    def test_plan_freezes_six_current_slots_and_requires_calibrated_rps(self) -> None:
        result = self.campaign('--plan', '--rps', '17')
        self.assertEqual(0, result.returncode, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual('cadence-pilot-v1', plan['protocol_version'])
        self.assertEqual(17, plan['rps'])
        self.assertEqual([(1, 30, 2), (1, 15, 2), (2, 15, 7), (2, 30, 7), (3, 30, 12), (3, 15, 12)],
                         [(slot['pair'], slot['requeue_seconds'], slot['offset_seconds']) for slot in plan['slots']])
        self.assertEqual(['Current'] * 6, [slot['decision_mode'] for slot in plan['slots']])
        self.assertEqual(['not_run'] * 6, [slot['status'] for slot in plan['slots']])
        self.assertNotEqual(0, self.campaign('--plan').returncode)

    def test_cluster_failure_retains_every_assignment_without_starting_a_slot(self) -> None:
        with tempfile.TemporaryDirectory(prefix='cadence-cli-') as directory:
            root = Path(directory)
            kubeconfig = root / 'private.kubeconfig'
            kubeconfig.write_text('unused fixture', encoding='utf-8')
            stub = root / ('kubectl.cmd' if os.name == 'nt' else 'kubectl')
            stub.write_text('@echo off\necho fixture-cluster-unreachable 1>&2\nexit /b 19\n' if os.name == 'nt'
                            else '#!/bin/sh\necho fixture-cluster-unreachable >&2\nexit 19\n', encoding='utf-8')
            stub.chmod(0o700)
            output = root / 'campaign'
            result = self.campaign('--rps', '17', '--context', 'kind-phpa-cadence-test',
                                   '--kubeconfig', str(kubeconfig), '--output', str(output),
                                   env={**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH']})
            self.assertEqual(3, result.returncode, result.stderr)
            receipt = json.loads((output / 'campaign-status.json').read_text())
            self.assertEqual('failed', receipt['status'])
            self.assertEqual(['not_run'] * 6, [slot['status'] for slot in receipt['slots']])
            self.assertEqual([], list((output / 'runs').iterdir()))
            self.assertIn('fixture-cluster-unreachable', next((output / 'commands').glob('*.stderr')).read_text())

    def test_failed_runner_keeps_its_slot_and_prevents_all_later_slots(self) -> None:
        with tempfile.TemporaryDirectory(prefix='cadence-failed-slot-') as temporary:
            directory = Path(temporary)
            kubeconfig = directory / 'private.kubeconfig'
            kubeconfig.write_text('isolated fixture', encoding='utf-8')
            script = directory / 'external_commands.py'
            script.write_text('''import json,os,pathlib,sys
name=sys.argv[1]; args=sys.argv[2:]
if name=='kubectl':
 if 'config' in args: print('kind-phpa-cadence-test');sys.exit(0)
 resource=args[args.index('get')+1]
 values={
  'namespace':{'metadata':{'uid':'cluster'}},
  'nodes':{'items':[{'metadata':{'uid':'node','name':'phpa-cadence-test-control-plane'},'spec':{},'status':{'nodeInfo':{'bootID':'boot'},'capacity':{},'allocatable':{}}}]},
  'deployment':{'metadata':{'uid':'target'},'spec':{'replicas':1}},
  'service':{'metadata':{'uid':'service'},'spec':{'clusterIP':'10.0.0.1'}},
  'hpa':{'items':[]},'predictivehpas':{'items':[]},'configmaps':{'items':[]},
  'pods':{'items':[{'metadata':{'namespace':'default','labels':{'run':'php-apache'}},'status':{'phase':'Running','containerStatuses':[{'imageID':'sha256:workload'}]}}]}}
 print(json.dumps(values[resource]))
elif name=='kind':
 if args==['get','clusters']: print('phpa-cadence-test')
 elif args==['get','nodes','--name','phpa-cadence-test']: print('phpa-cadence-test-control-plane')
 else: raise RuntimeError(args)
elif name=='go': pathlib.Path(args[args.index('-o')+1]).write_bytes(b'frozen fixture binary')
elif name=='bash':
 output=pathlib.Path(os.environ['EXPERIMENTS_ROOT'])/'failed-slot'
 output.mkdir();(output/'partial-receipt.json').write_text(json.dumps({key:os.environ[key] for key in
 ['CADENCE_SLOT','CADENCE_PAIR','CADENCE_OFFSET_SECONDS','LIVE_BASELINE_REQUEUE_SECONDS','CADENCE_CPU_SHA256']}))
 sys.exit(3)
else: raise RuntimeError(name)
''', encoding='utf-8')
            for name in ('kubectl', 'kind', 'go', 'bash'):
                if os.name == 'nt':
                    (directory / (name + '.cmd')).write_text(f'@"{sys.executable}" "{script}" {name} %*\r\n')
                else:
                    command = directory / name
                    command.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} {name} "$@"\n')
                    command.chmod(0o700)
            output = directory / 'campaign'
            result = self.campaign('--rps', '17', '--context', 'kind-phpa-cadence-test',
                '--kubeconfig', str(kubeconfig), '--output', str(output),
                env={**os.environ, 'PATH': str(directory) + os.pathsep + os.environ['PATH']})
            self.assertEqual(3, result.returncode, result.stderr)
            receipt = json.loads((output / 'campaign-status.json').read_text())
            self.assertEqual(['failed'] + ['not_run'] * 5, [slot['status'] for slot in receipt['slots']])
            self.assertEqual(['runs/failed-slot'], receipt['slots'][0]['partial_run_dirs'])
            partial = json.loads((output / 'runs/failed-slot/partial-receipt.json').read_text())
            self.assertEqual(['1', '1', '2', '30'], [partial[key] for key in
                ('CADENCE_SLOT', 'CADENCE_PAIR', 'CADENCE_OFFSET_SECONDS', 'LIVE_BASELINE_REQUEUE_SECONDS')])
            binary = json.loads((output / 'cpu-observer-binary.json').read_text())
            self.assertEqual(binary['sha256'], partial['CADENCE_CPU_SHA256'])
            self.assertEqual([], list((output / 'analysis').iterdir()))

    @unittest.skipUnless(shutil.which('node'), 'Node is required for offline workload checks')
    def test_busy_step_shape_and_request_evidence_use_the_actual_resumed_scenario_clock(self) -> None:
        loader = r'''
const vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
const calls=[],logs=[],points=[];
const context=vm.createContext({__ENV:{RPS:'17'},console:{log:v=>logs.push(v)}});
function synthetic(names,values){return new vm.SyntheticModule(names,function(){names.forEach(n=>this.setExport(n,values[n]));},{context});}
const stubs={
 'k6/http':synthetic(['default'],{default:{get:(...a)=>calls.push(a)}}),
 'k6/execution':synthetic(['default'],{default:{scenario:{startTime:1788998400123,iterationInTest:0}}}),
 'k6/metrics':synthetic(['Trend'],{Trend:class {constructor(name){this.name=name;} add(value){points.push([this.name,value]);}}})};
const modules=new Map();function load(file){if(!modules.has(file))modules.set(file,new vm.SourceTextModule(fs.readFileSync(file,'utf8'),{context,identifier:file}));return modules.get(file);}
(async()=>{const module=load(path.resolve('hack/k6/cadence.js'));await module.link((name,parent)=>stubs[name]||load(path.resolve(path.dirname(parent.identifier),name)));await module.evaluate();module.namespace.default();process.stdout.write(JSON.stringify({options:module.namespace.options,logs,points,calls}));})().catch(e=>{process.stderr.write(e.stack);process.exitCode=1;});
'''
        result = subprocess.run([shutil.which('node'), '--experimental-vm-modules', '-e', loader],
                                cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
        result = json.loads(result.stdout)
        scenario = result['options']['scenarios']['step_load']
        self.assertEqual([{'duration': '1s', 'target': 17}, {'duration': '179s', 'target': 17},
                          {'duration': '1s', 'target': 0}], scenario['stages'])
        schedule = json.loads(result['logs'][0].removeprefix('PHPA_BASELINE_SCHEDULE '))
        self.assertEqual({'pattern': 'step', 'scenario_start_unix': 1788998400.123,
                          'onset_unix': 1788998400.123, 'offered_end_unix': 1788998581.123}, schedule)
        self.assertEqual([['baseline_request_attempt', 1788998400123]], result['points'])
        self.assertEqual('http://php-apache.default.svc:80/', result['calls'][0][0])
        self.assertEqual('10s', result['calls'][0][1]['timeout'])


@unittest.skipUnless(shell_tests.find_bash(), 'bash is required')
class CadenceShellCLI(unittest.TestCase):
    bash = shell_tests.find_bash()
    run_bash = shell_tests.BenchmarkScriptTests.run_bash
    environment = {'CADENCE_PILOT': 'true', 'LIVE_BASELINE': 'true', 'LIVE_BASELINE_STARTUP_MODE': 'warm',
                   'LIVE_BASELINE_REQUEUE_SECONDS': '15', 'CADENCE_PAIR': '1', 'CADENCE_SLOT': '2',
                   'CADENCE_OFFSET_SECONDS': '2', 'RPS': '17', 'BENCHMARK_PATTERNS': 'step',
                   'BENCHMARK_CONTROLLERS': 'phpa_current', 'LATENCY_DIAGNOSTIC': 'false'}

    def test_invalid_assignment_or_cadence_is_rejected_before_cluster_access(self) -> None:
        for overrides in ({'LIVE_BASELINE_REQUEUE_SECONDS': '10'}, {'CADENCE_SLOT': '1'},
                          {'CADENCE_OFFSET_SECONDS': '7'}, {'LIVE_BASELINE': 'false'},
                          {'LIVE_BASELINE_STARTUP_MODE': 'cold'}):
            with self.subTest(overrides=overrides):
                result = self.run_bash('-c', 'source hack/lib/k6_runner.sh; source hack/lib/benchmark_config.sh; benchmark_config_init',
                                       extra_env={**self.environment, **overrides})
                self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)

    def test_cadence_runner_is_preinitialized_and_paused_with_an_owned_rest_gate(self) -> None:
        with tempfile.TemporaryDirectory(prefix='.cadence-render-', dir=ROOT) as temporary:
            output = Path(temporary) / 'rendered'
            result = self.run_bash('-c',
                'set -e; source hack/lib/k6_runner.sh; source hack/lib/benchmark_config.sh; '
                'benchmark_config_init; k6_runner_render cadence.js "$CADENCE_TEST_OUTPUT" RPS=17',
                extra_env={**self.environment, 'CADENCE_TEST_OUTPUT': output.relative_to(ROOT).as_posix()})
            self.assertEqual(0, result.returncode, result.stderr)
            pod = json.loads((output / 'k6-pod.json').read_text())
            command = pod['spec']['containers'][0]['args'][0]
            self.assertIn('--paused', command)
            self.assertIn('--address 0.0.0.0:6565', command)
            receipt = json.loads((output / 'k6-runner.json').read_text())
            self.assertEqual({'protocol_version': 'cadence-pilot-v1', 'paused': True,
                              'rest_port': 6565, 'source_timeout_seconds': 120}, receipt['cadence_gate'])
            self.assertEqual('http://php-apache.default.svc:80', receipt['endpoint'])

    def test_cadence_benchmark_rejects_a_different_controller_or_workload_before_preflight(self) -> None:
        result = self.run_bash('hack/run_benchmark.sh', 'ramp', 'phpa_current', '2', extra_env=self.environment)
        self.assertNotEqual(0, result.returncode)
        self.assertIn('CADENCE_PILOT requires step phpa_current and the assigned slot repeat', result.stderr)


@unittest.skipUnless(shell_tests.find_bash(), 'bash is required')
class CadenceRunnerLifecycle(unittest.TestCase):
    bash = shell_tests.find_bash()
    run_bash = shell_tests.BenchmarkScriptTests.run_bash
    setUp = runner_tests.K6RunnerTests.setUp
    write_stub = runner_tests.K6RunnerTests.write_stub
    invoke = runner_tests.K6RunnerTests.invoke
    calls = runner_tests.K6RunnerTests.calls

    def test_gate_failure_stops_the_assigned_load_before_resource_cleanup(self) -> None:
        result = self.invoke('mkdir -p "$K6_TEST_OUTPUT"\n'
            'printf \'{"status":"failed"}\\n\' > "$K6_TEST_OUTPUT/cadence-status.json"\n'
            'CADENCE_OBSERVER_PID=$$\n'
            'k6_runner_run cadence.js "$K6_TEST_OUTPUT" RPS=17',
            CADENCE_PILOT='true', LIVE_BASELINE='true')
        self.assertNotEqual(0, result.returncode)
        receipt = json.loads((self.output_dir / 'k6-runner.json').read_text())
        self.assertEqual('failed', receipt['status'])
        self.assertIn('cadence observer failed', receipt['failure_reason'])
        calls = self.calls()
        stopped = next(index for index, call in enumerate(calls) if 'exec' in call and any('kill -TERM' in arg for arg in call))
        deleted = next(index for index, call in enumerate(calls) if 'delete' in call and 'pod' in call)
        self.assertLess(stopped, deleted)


if __name__ == '__main__':
    unittest.main()
