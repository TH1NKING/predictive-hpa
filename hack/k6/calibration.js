// Fixed-replica routing/capacity probe; no autoscaling decisions are tested here.
import http from 'k6/http';
import { BASE_URL, REQUEST_TIMEOUT } from './lib/common.js';

const rate = Number(__ENV.PROBE_RPS);
const duration = Number(__ENV.PROBE_DURATION_SECONDS);
const token = __ENV.PROBE_TOKEN;
if (!Number.isInteger(rate) || rate < 1 || !Number.isInteger(duration) || duration < 60 || !token) {
  throw new Error('Calibration requires positive PROBE_RPS, duration >= 60s, and PROBE_TOKEN');
}

export const options = {
  // Service balancing happens per connection. Fresh connections make this a
  // routing diagnostic; record this policy when comparing subsequent loads.
  noConnectionReuse: true,
  scenarios: {
    probe: {
      executor: 'constant-arrival-rate',
      rate,
      timeUnit: '1s',
      duration: `${duration}s`,
      preAllocatedVUs: Math.max(20, rate * 10),
      maxVUs: Math.max(40, rate * 12),
      gracefulStop: '15s',
    },
  },
};

export default function () {
  http.get(`${BASE_URL}/`, {
    timeout: REQUEST_TIMEOUT,
    headers: { 'User-Agent': `phpa-routing/${token}` },
    tags: { phase: 'calibration', probe: token },
  });
}
