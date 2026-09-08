// Diagnostic timing around the original step workload. The metric's value is
// the scenario start epoch in milliseconds; each point's data.time records an
// attempted request. Neither is inferred from the container process launch.
import execution from 'k6/execution';
import { Trend } from 'k6/metrics';
import { options as stepOptions } from './step.js';
import { get } from './lib/common.js';

export const options = stepOptions;
const requestAttempt = new Trend('latency_request_attempt');

export default function () {
  requestAttempt.add(execution.scenario.startTime);
  get('step');
}
