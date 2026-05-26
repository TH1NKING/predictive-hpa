// hack/k6/step.js
// Pattern 1: Step load — sudden ramp to target RPS, hold, sudden release.
//
// Purpose: Comparable to Phase 0 baseline load pattern. Measures:
//   - First scale-up latency   (load on -> first replicas change)
//   - Convergence time         (load on -> replicas reach steady state)
//   - Scale-down window effect (load off -> first scale-down)
//   - Total scale-down duration (load off -> replicas back to min)
//
// Timeline (total ~7.5 min):
//   0-30s    quiet (baseline)
//   30-31s   step-up   (1s ramp, approximates instant)
//   31-210s  hold at TARGET_RPS  (179s)
//   210-211s step-down (1s ramp, approximates instant)
//   211-450s tail observation (239s)

import {
  TARGET_RPS,
  PRE_LOAD_QUIET_SECONDS,
  POST_LOAD_TAIL_SECONDS,
  get,
} from './lib/common.js';

const HOLD_SECONDS = 180;

export const options = {
  scenarios: {
    step_load: {
      executor: 'ramping-arrival-rate',
      startRate: 0,
      timeUnit: '1s',
      preAllocatedVUs: 100,
      maxVUs: 200,
      stages: [
        { duration: `${PRE_LOAD_QUIET_SECONDS}s`, target: 0 },
        { duration: '1s', target: TARGET_RPS },
        { duration: `${HOLD_SECONDS - 1}s`, target: TARGET_RPS },
        { duration: '1s', target: 0 },
        { duration: `${POST_LOAD_TAIL_SECONDS - 1}s`, target: 0 },
      ],
    },
  },
};

export default function () {
  get('step');
}
