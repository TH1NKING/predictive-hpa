// hack/k6/spike.js
// Pattern 3: Spike load — narrow pulses with quiet gaps in between.
//
// Purpose: Stress-test the scale-down stabilization window.
//
// Without the window (Phase 2 commit 726929a), this pattern produced
// ~5min oscillation cycles with ~18 Pod flips per cycle and service
// disruption (wget connect refused when scale-down hit 1 replica
// mid-pulse).
//
// With the 60s window (Phase 2 commit 51bf289), oscillation should be
// eliminated; expected Pod flips: ~3 (one scale-up per spike).
//
// Timeline (k6-side, ~4 min):
//   0-30s     quiet (baseline)
//   30-61s    spike 1 (1s up + 29s hold + 1s down)
//   61-120s   gap 1 (59s @ 0)
//   120-151s  spike 2
//   151-210s  gap 2
//   210-241s  spike 3
//   (no trailing tail stage — handled by run_benchmark.sh)

import {
  TARGET_RPS,
  PRE_LOAD_QUIET_SECONDS,
  PRE_ALLOCATED_VUS,
  MAX_VUS,
  get,
} from './lib/common.js';

const SPIKE_COUNT = 3;
const SPIKE_DURATION_SECONDS = 30;
const SPIKE_GAP_SECONDS = 60;

function buildStages() {
  const stages = [];
  stages.push({ duration: `${PRE_LOAD_QUIET_SECONDS}s`, target: 0 });
  for (let i = 0; i < SPIKE_COUNT; i++) {
    // Step-up (1s ≈ instant)
    stages.push({ duration: '1s', target: TARGET_RPS });
    // Hold during spike
    stages.push({ duration: `${SPIKE_DURATION_SECONDS - 1}s`, target: TARGET_RPS });
    // Step-down (1s ≈ instant)
    stages.push({ duration: '1s', target: 0 });
    // Gap before next spike — omitted after the last (tail handled by orchestrator)
    if (i < SPIKE_COUNT - 1) {
      stages.push({ duration: `${SPIKE_GAP_SECONDS - 1}s`, target: 0 });
    }
  }
  return stages;
}

export const options = {
  scenarios: {
    spike_load: {
      executor: 'ramping-arrival-rate',
      startRate: 0,
      timeUnit: '1s',
      preAllocatedVUs: PRE_ALLOCATED_VUS,
      maxVUs: MAX_VUS,
      stages: buildStages(),
    },
  },
};

export default function () {
  get('spike');
}
