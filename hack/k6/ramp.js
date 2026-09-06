// hack/k6/ramp.js
// Pattern 2: Ramp load — linear ramp up, hold at peak, linear ramp down.
//
// Purpose: Showcase EWMA's predictive lead. On a linearly-rising signal,
// PHPA's predicted = S[N] + slope * horizon yields a predicted value
// that consistently leads the current observed value, allowing the
// controller to scale up before utilization exceeds target.
//
// Native HPA on the same signal will lag by ~30s (metrics scrape
// interval + reconciliation cycle).
//
// Timeline (k6-side, ~4.5 min):
//   0-30s    quiet (baseline)
//   30-90s   ramp up from 0 to TARGET_RPS (60s linear)
//   90-210s  hold at TARGET_RPS (120s)
//   210-270s ramp down from TARGET_RPS to 0 (60s linear)
//
// Tail observation (scale-down period) is handled by run_benchmark.sh.

import {
  TARGET_RPS,
  PRE_LOAD_QUIET_SECONDS,
  PRE_ALLOCATED_VUS,
  MAX_VUS,
  get,
} from './lib/common.js';

const RAMP_UP_SECONDS = 60;
const HOLD_SECONDS = 120;
const RAMP_DOWN_SECONDS = 60;

export const options = {
  scenarios: {
    ramp_load: {
      executor: 'ramping-arrival-rate',
      startRate: 0,
      timeUnit: '1s',
      preAllocatedVUs: PRE_ALLOCATED_VUS,
      maxVUs: MAX_VUS,
      stages: [
        { duration: `${PRE_LOAD_QUIET_SECONDS}s`, target: 0 },
        { duration: `${RAMP_UP_SECONDS}s`, target: TARGET_RPS },
        { duration: `${HOLD_SECONDS}s`, target: TARGET_RPS },
        { duration: `${RAMP_DOWN_SECONDS}s`, target: 0 },
      ],
    },
  },
};

export default function () {
  get('ramp');
}
