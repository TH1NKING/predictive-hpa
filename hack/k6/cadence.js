// Start the unchanged busy step shape only after the external source-sample gate.
import execution from 'k6/execution';
import { Trend } from 'k6/metrics';
import { TARGET_RPS, PRE_ALLOCATED_VUS, MAX_VUS, get } from './lib/common.js';

export const options = {
  scenarios: {
    step_load: {
      executor: 'ramping-arrival-rate',
      startRate: 0,
      timeUnit: '1s',
      preAllocatedVUs: PRE_ALLOCATED_VUS,
      maxVUs: MAX_VUS,
      stages: [
        { duration: '1s', target: TARGET_RPS },
        { duration: '179s', target: TARGET_RPS },
        { duration: '1s', target: 0 },
      ],
    },
  },
};
const requestAttempt = new Trend('baseline_request_attempt');

export default function () {
  const start = execution.scenario.startTime / 1000;
  if (execution.scenario.iterationInTest === 0) {
    console.log('PHPA_BASELINE_SCHEDULE ' + JSON.stringify({
      pattern: 'step', scenario_start_unix: start, onset_unix: start, offered_end_unix: start + 181,
    }));
  }
  requestAttempt.add(execution.scenario.startTime);
  get('step');
}
