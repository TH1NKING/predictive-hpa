// Retain the established load shapes while recording the actual k6 scenario clock.
import execution from 'k6/execution';
import { Trend } from 'k6/metrics';
import { options as stepOptions } from './step.js';
import { options as rampOptions } from './ramp.js';
import { get } from './lib/common.js';

const pattern = __ENV.BASELINE_PATTERN;
if (pattern !== 'step' && pattern !== 'ramp') {
  throw new Error('BASELINE_PATTERN must be step or ramp');
}
export const options = pattern === 'step' ? stepOptions : rampOptions;
const requestAttempt = new Trend('baseline_request_attempt');

export default function () {
  const start = execution.scenario.startTime / 1000;
  if (execution.scenario.iterationInTest === 0) {
    console.log('PHPA_BASELINE_SCHEDULE ' + JSON.stringify({
      pattern: pattern,
      scenario_start_unix: start,
      onset_unix: start + 30,
      offered_end_unix: start + (pattern === 'step' ? 211 : 270),
    }));
  }
  requestAttempt.add(execution.scenario.startTime);
  get(pattern);
}
