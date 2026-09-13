#!/usr/bin/env python3
"""Run the frozen six-slot Current cadence pilot in an owned dedicated Kind cluster."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import sys
from pathlib import Path

from run_live_campaign import Campaign, digest, write_json


def plan(rps: int) -> dict:
    assignments = ((1, 30, 2), (1, 15, 2), (2, 15, 7), (2, 30, 7), (3, 30, 12), (3, 15, 12))
    return {'protocol_version': 'cadence-pilot-v1', 'pattern': 'step', 'rps': rps,
            'startup_mode': 'warm', 'quiet_seconds': 0, 'pre_gate_idle_seconds_min': 30,
            'source_gate_timeout_seconds': 120, 'phase_tolerance_seconds': 1,
            'pair_source_age_tolerance_seconds': 1, 'observer_interval_seconds': 1,
            'service_criteria': {'http_200_ratio_min': 0.99, 'all_request_p95_ms_max': 500,
                                 'dropped_iterations_max': 0},
            'slots': [{'slot': index, 'pair': pair, 'repeat': index, 'requeue_seconds': cadence,
                       'offset_seconds': offset, 'decision_mode': 'Current', 'controller': 'phpa_current',
                       'status': 'not_run'} for index, (pair, cadence, offset) in enumerate(assignments, 1)]}


class CadenceCampaign(Campaign):
    def __init__(self, args: argparse.Namespace, output: Path) -> None:
        args.pattern = 'step'
        super().__init__(args, output)
        self.environment.update(CADENCE_PILOT='true', BENCHMARK_CONTROLLERS='phpa_current')

    def assignment_plan(self) -> dict:
        return plan(self.args.rps)

    def build_extra_binaries(self, private: Path) -> None:
        binary = private / ('observe-cpu.exe' if os.name == 'nt' else 'observe-cpu')
        self.command('build-cpu-observer', [shutil.which('go') or 'go', 'build', '-trimpath',
                     '-o', str(binary), './cmd/observe-cpu'], 600)
        self.environment.update(CADENCE_CPU_BINARY=str(binary), CADENCE_CPU_SHA256=digest(binary))
        write_json(self.output / 'cpu-observer-binary.json', {'sha256': digest(binary), 'build_flags': ['-trimpath']})

    def configure_slot(self, slot: dict) -> None:
        if digest(Path(self.environment['CADENCE_CPU_BINARY'])) != self.environment['CADENCE_CPU_SHA256']:
            raise ValueError('Frozen CPU observation binary changed between slots')
        self.environment.update(LIVE_BASELINE_REQUEUE_SECONDS=str(slot['requeue_seconds']),
            CADENCE_OFFSET_SECONDS=str(slot['offset_seconds']), CADENCE_SLOT=str(slot['slot']),
            CADENCE_PAIR=str(slot['pair']))

    def analyze_slot(self, slot: dict, run: Path, analysis: Path) -> None:
        self.command(f"slot-{slot['slot']:02d}-analysis", [sys.executable, 'hack/analyze/cadence.py',
                     '--run-dir', str(run), '--output', str(analysis)])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', action='store_true')
    parser.add_argument('--rps', type=int, choices=range(1, 1001), required=True,
                        help='RPS frozen after this campaign\'s fresh capacity calibration')
    parser.add_argument('--context')
    parser.add_argument('--kubeconfig', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--expected-phpa-receipt', type=Path)
    args = parser.parse_args()
    if args.plan:
        print(json.dumps(plan(args.rps), indent=2))
        return 0
    if not args.context or not re.fullmatch(r'kind-phpa-cadence-[a-z0-9][a-z0-9-]*', args.context):
        parser.error('Use an explicit kind-phpa-cadence-* dedicated cluster')
    if args.kubeconfig is None or not args.kubeconfig.is_file() or args.output is None:
        parser.error('An existing isolated --kubeconfig and fresh --output directory are required')
    output = args.output.resolve()
    if output.exists() or args.kubeconfig.resolve().is_relative_to(output):
        parser.error('Output must be new and must not contain the private kubeconfig')
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def interrupted(number: int, _frame: object) -> None:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt(f'Received signal {number}')

    signal.signal(signal.SIGTERM, interrupted)
    try:
        return CadenceCampaign(args, output).run()
    except OSError as error:
        print(f'Cadence campaign failed: {error}', file=sys.stderr)
        return 3
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == '__main__':
    raise SystemExit(main())
