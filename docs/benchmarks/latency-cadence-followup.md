# Single-variable reconciliation-cadence follow-up

## Evidence and question

The completed six-run diagnostic used source
`075e0564c1af741a9eceb3d36ec84482800239d7`. Independent raw parsing matched all
138 comparisons. The first separately observed above-threshold CPU response
arrived after 26.283 seconds on average; the subsequent observed wait until the
expanding controller query averaged 12.923 seconds. These are observational
intervals, not a causal partition of total latency.

Both assigned 10-second runs expose a specific repeated penalty. Controller
queries around +19 seconds still saw CPU below the expansion threshold. The
observer then saw sufficient CPU at +23.829 and +29.657 seconds, while the next
controller queries began around +49.071 and +48.925 seconds: another 25.242 and
19.268 seconds later. Query start through successful Scale response took only
5.5–8.4 milliseconds across the six runs.

The user's authorized third step is to validate one variable selected from the
diagnosis. This follow-up changes only the configured normal requeue interval
from 30 to 15 seconds. It tests the repeated post-information wait. It does not
claim that coordination dominates total latency; source production, scrape
visibility and averaging still contribute a larger elapsed interval on average.

## Implementation contract

Expose a manager `--requeue-interval` duration setting with a 30-second default.
Existing reconciler construction without an explicit setting preserves 30
seconds. Reject explicit invalid/subsecond durations. Normal completion and
transient no-data/missing-target waits use the configured interval; the existing
60-second unsupported-configuration retry remains unchanged.

Keep the provider query, 15-second query grid, one-minute rate window, prediction,
Current decision, tolerance, stabilization and replica bounds unchanged. No CRD
field is added. Completion diagnostics report the actual returned interval.

The diagnostic runner accepts `LATENCY_REQUEUE_SECONDS=15|30`, defaulting to 30.
It passes the explicit duration to the manager and observer, includes it in the
configuration identity and records it in metadata. Each observer's plan describes
one run; the outer frozen campaign plan is the authority for assignment order.
Idle-anchor validation uses the assigned interval and still requires a successful
one-replica decision with current CPU below 5 percent.

## Frozen four-run protocol

Use one reviewed source and controller binary for both treatments in the same
dedicated `kind-hpa-dev` environment. Preserve the sealed six-run diagnostic.
Take new original-resource snapshots and use a new output root. Recheck the
applicable capacity calibration, source cleanliness, image digests, workload and
generator resources, monitoring configuration and exclusive experiment ownership.

All four runs use Current, requested onset offset 10 seconds, 25 RPS, min/max
1/10, target CPU 50%, stabilization 60 seconds, alpha 30%, history 5 minutes and
horizon 30 seconds. Keep the same 30-second quiet stage, 181-second step schedule,
360-second tail, two-second observer and one-second gate clock.

Assignments are fixed before load:

| Slot | Normal requeue interval | Requested offset |
|---|---:|---:|
| 1 | 30 seconds | 10 seconds |
| 2 | 15 seconds | 10 seconds |
| 3 | 15 seconds | 10 seconds |
| 4 | 30 seconds | 10 seconds |

This ABBA order balances early/late positions for a descriptive two-repeat pilot;
it is not randomization. The phase is selected because the initial diagnostic
reproduced a penalty there, so any conclusion is conditional on this selected
phase. Do not combine this batch with the earlier six as one randomized sample.

Schedule process launch at `ceil(idle finish + 30 + offset)` and retain the
existing quiet stage. The resulting additional 60 seconds is a multiple of both
intervals. Record actual scenario onset and actual prior-idle gap; preserve the
two-second phase tolerance and flag gaps beyond interval plus tolerance without
hiding them modulo the cadence. Misses retain their assigned slot.

## Outcomes and acceptance

Primary descriptive outcomes are first genuine successful Scale target increase
from actual workload onset and the signed interval between separately observed
sufficient CPU and the expanding controller query. Retain every decision and
query timestamp, including failures and event-triggered extra reconciliations.

Also report new-Pod Ready/request evidence with one-second precision, HTTP 200
fraction, all-request p95, dropped iterations, total and post-load Pod-seconds
over the actual 541-second window, reconciliation counts and query durations.
Faster polling implies more queries; query duration is not CPU consumption.

Completion requires all four assigned runs, one common binary, two configuration
identities distinguished by cadence, complete observer cleanup and actual-window
coverage, preserved failures, independent raw recomputation, and exact resource
restoration. Stop on infrastructure/collection failure; do not rerun or retune in
response to service outcomes.

Use the already confirmed public controller/log and analysis-CLI test seams,
focused red/green checks, required Go lint/unit/envtest and relevant offline
harness checks. Review standards and this contract against the confirmed original
baseline `6034dfdbac9927c03d84d03d1859fb3522d24ac5` before freezing the new source.

Retain the 30-second default. This focused pilot is intended to validate the
mechanism and expose service/resource tradeoffs; it does not establish a better
global default or production performance. Publish the observed result whether
positive, negative or inconclusive, and explain the method in the Chinese guide.

## Execution record

Completed on September 7 with source `32568c1cd6a2543bcc525b75abd41365bff5d6d2`.
All four assignments, complete scenario windows and restoration checks passed;
independent raw recomputation matched 112 comparisons. The default remains 30s.
See the [results and attribution limits](latency-cadence-followup-20260907.md).
