# Verified-observation baseline and reproducible acceptance

The user authorized this sequence on September 10, 2026: rebuild performance
evidence for the metrics-safety implementation, make fault acceptance reproducible
through the normal image and Helm deployment path, and consider optimization only
when the new evidence identifies a useful single-variable experiment. Deliver a
Chinese explanation of implementation, evidence, alternatives and limitations.

The confirmed review baseline is `b70ed69e3719a337dcf9adfae0130a6a3c907dd1`.
The confirmed test seams are the public experiment/acceptance CLIs (exit codes,
output artifacts and cleanup), the analysis CLI with hand-worked examples, and
real-cluster Scale/status/service behavior. Use red/green checks at these seams,
review Standards and Spec independently, and commit on the current branch.

## Performance baseline

Use a newly created, dedicated Kind cluster and an isolated kubeconfig. Freeze
source, controller binary/image, cluster/node identities, workload/generator
images, CPU resources, scrape interval, rate window, reconciliation interval,
controller parameters and observer settings. Revalidate Service request
distribution and fixed-replica capacity before dynamic runs. Historical 25 RPS,
one/five-replica results are a calibration candidate, not a present guarantee.

First exercise a complete Current run to establish collection and cleanup. Then
run a frozen step block with Current, Predictive and Hybrid, three repetitions
each and rotated order. Once this pipeline is reliable, apply the same comparison
to a gradual ramp. Keep exploratory runs separate from assigned comparison slots.
Record every assigned slot, including failure, missing data and no expansion;
stop later slots on unsafe state, collection or restoration failure. Never replace
unfavorable runs selectively. A small pilot supports descriptive conclusions.

Use the controller's verified live observations and decision logs for its input
timeline. Old name-prefix Prometheus expressions are not accepted controller
observations. Record source sample age, accepted-history readiness,
`MetricsReady=False` intervals, successful Scale writes and subsequently sampled
Ready replicas. Distinguish exact write responses from bounded sampling times.
Warm-start runs require verified history and a completed initial protection
window before load. Observe cold start separately with an explicitly recorded
controller start and load schedule; do not mix it into warm-start aggregates.

Report HTTP success, all-request p95, dropped iterations, requested/Ready replica
time integrals, first Scale increase and Ready-capacity timing together. Replica
time is not CPU consumption or a bill. Freeze service criteria before comparison.
The diagnostic service criterion is at least 99% HTTP 200, all-request p95 no
greater than 500 ms and zero dropped iterations, as in the existing controlled
pilot. It is a preselected diagnostic criterion, not a production SLO guarantee.
Use 15-second Prometheus scrapes for the performance baseline, a 60-second CPU
rate window and the default 30-second requeue. The fault workflow separately
retains its previously verified 5-second scrape configuration; do not combine
these batches. Step offers load for 181 seconds and ramp for 240 seconds after
the actual scenario's 30-second quiet stage. Both retain a complete 360-second
post-load observation tail.
Raw evidence stays in ignored run directories; publish compact inputs, integrity
manifests and independently recomputable results without credentials.

## Reproducible fault acceptance

Add a documented public entry that builds the current Dockerfile, creates its own
Kind cluster, installs dedicated monitoring and a two-replica Helm manager, then
runs the existing metrics-safety acceptance. Reuse the established 11 functional
checks. Add a GitHub workflow that runs this entry and uploads evidence on success
and failure. This does not publish a controller image or performance claims.

Reject existing output directories, cluster names and conflicting resources.
Record cluster and node container identity; check ownership before cleanup. Keep
kubeconfig outside the evidence tree. Every command is bounded, failed attempts
are retained, and run/diagnostic/cleanup failures remain distinguishable and cause
a nonzero exit. Attempt cleanup after partial creation and interruption when
ownership is established; never delete resources whose identity has changed.

## Evidence-conditioned decision

Only change controller timing, sampling or policy after the fresh baseline gives
a concrete hypothesis. Otherwise preserve defaults and document why no
optimization was selected. More complex prediction, additional metric types and
history persistence are outside this work. Summarize what was actually executed,
what remains unverified, and the practical costs of the chosen safety policy.
