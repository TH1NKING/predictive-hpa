# Same-controller decision-mode ablation

## Question and scope

The September 6 pilot observed an early PHPA reconciliation with current CPU
98.65%, forecast about 52.87% and a 50% target. The forecast fell inside the
10% tolerance band, suppressing expansion. This follow-up tests whether using
current CPU for expansion improves this behavior and the dynamic service
outcome. It does not assume that prediction explains the complete Native/PHPA
performance difference.

The user-approved work is: reproduce the delayed expansion through the public
controller interface, implement three selectable decisions within PHPA, run a
matched 25 RPS step pilot, and explain the results and method tradeoffs. Review
changes against baseline `e72b607c92c016cfd9bce14191250bdd0eca6164`.

## Decision contract

`spec.decisionMode` is optional and defaults to `Predictive`, preserving existing
resources' behavior. It selects the decision signal, not the forecasting
algorithm (`prediction.algorithm` remains EWMA).

| Mode | Signal supplied to the common replica and tolerance rules |
|---|---|
| `Predictive` | Bounded EWMA forecast, matching the existing controller |
| `Current` | Latest CPU observation from the same provider and history |
| `Hybrid` | `max(currentCPU, min(boundedForecast, targetCPU))` |

Hybrid expands using current demand. On falling demand, the forecast may retain
more replicas than Current; it cannot initiate expansion by itself, or request
fewer replicas than current demand requires. A current observation within the
tolerance band therefore retains its protection even when the forecast is low.
The common min/max bounds, stabilization history and tolerance remain in place.
All modes still calculate and expose the forecast and share its sample-readiness
requirements, so the ablation changes decision selection alone.

Regression coverage uses Kubernetes resource creation/read/scale and PHPA status
through envtest, with controlled CPU samples at the external metrics boundary.
It must cover the early high-current/low-forecast case, unchanged default
behavior, current-only behavior, Hybrid's falling-load floor and forecast-only
expansion guard, and the shared stabilization and replica bounds. Admission must
default the omitted mode and reject unknown values.

Decision logs retain current and forecast values and add the effective mode and
decision signal. A logged successful scale write is distinct from a later
sampled Deployment replica increase; analysis must label these separately.

## Frozen small-pilot design

Use the isolated Kind environment and snapshot/restore procedure from
[the controlled pilot](controlled-pilot.md), with a separate source checkout and
output directory. Before load, record the source commit, effective configuration,
image digests, CPU/memory allocations, monitoring settings and cluster identity.
Verify the existing 25 RPS passing/failing calibration remains applicable to the
same workload and environment; record any fresh diagnostic separately.

Compare only `phpa` (Predictive), `phpa_current` (Current), and `phpa_hybrid`
(Hybrid), at 25 RPS, min/max 1/10, target CPU 50%, stabilization 60s, alpha 30%,
window 5m, horizon 30s. Keep the same controller binary, provider, reconciliation
schedule, application and load generator across all nine runs.

Three repeat blocks rotate execution positions:

1. Predictive, Current, Hybrid.
2. Current, Hybrid, Predictive.
3. Hybrid, Predictive, Current.

This balances positions for a descriptive pilot; it does not remove carryover,
host contention or justify statistical significance. Check recovery between
runs and preserve failed attempts, dropped iterations and exclusion reasons.
Do not select a mode or change parameters in response to intermediate outcomes.

Use the existing 30s quiet period, 181s step schedule, and fixed +360s post-load
tail (541s comparable window from onset). Report every run and descriptive group
summaries for HTTP 200 fraction, all-request and success-only p95, dropped
iterations, first logged expansion, first sampled expansion, peak replicas,
total Pod-seconds and post-load Pod-seconds. Count failed requests and timeouts.
Any efficiency claim requires considering service quality alongside occupancy.

## Completion and interpretation

Run focused regression tests during implementation, regenerate CRD/DeepCopy and
the chart's CRD copy using project commands, then run lint, the full unit/envtest
suite and offline benchmark/analysis checks. Review both documented standards
and this contract before freezing the experimental source.

Archive raw evidence and checksums outside Git. Publish a compact results report
and a Chinese walkthrough covering what changed, why each method was chosen,
alternative methods' costs, and limits of the conclusion. Restore benchmark
resources and remove any task-specific access after evidence retrieval.
