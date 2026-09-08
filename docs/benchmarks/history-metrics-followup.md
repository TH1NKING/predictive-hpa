# History retention and metrics diagnostic follow-up

The user authorized the following order on September 8, 2026: fix stabilization
history growth during prolonged non-downscaling operation, then investigate
source-sample visibility and the one-minute CPU averaging window, and deliver a
Chinese explanation of the changes, evidence, and alternatives.

The confirmed review baseline is
`18a3abc8f68ef21f1a91ca100fefd1189fe0325c`. Confirmed test seams are public
Reconcile behavior and diagnostic logs, and the raw-evidence analysis CLI.

## Controller acceptance

Expire recommendations outside the configured stabilization window on every
decision, including scale-up and unchanged recommendations. Retain observations
at the inclusive cutoff. Preserve replica decisions, existing defaults, and
the remaining in-window scale-down protection. Test sustained non-downscaling
behavior and expiry with FakeClock through the public diagnostic stream and
Deployment scale API. Distinguish the injected policy clock from wall-clock
diagnostic timing. No CRD or persisted history is required for this repair.

Run focused failing/passing regressions, then required Go lint and full
unit/envtest checks. Linux validation uses a new isolated source directory and
does not use the development cluster as an envtest substitute.

## Diagnostic acceptance

First reuse the six original Current diagnostics and four matched cadence runs,
retaining their separate batch identities. Their periodic raw snapshots permit
a retrospective investigation of visibility and recent counter increments.
The detailed input and interpretation contract is in
[metric-visibility-diagnostic.md](metric-visibility-diagnostic.md).

Retain all ten runs, query failures, absent values, and timestamp uncertainty.
Use only evidence before the first successful scale increase for the principal
comparison. Explain what the archived evidence distinguishes and which causal
contributions remain unidentified. Do not equate a counter difference with a
short-window Prometheus rate, or report a hypothetical window change as an
observed service improvement. A new cluster campaign is warranted only if the
archived evidence cannot meaningfully narrow this diagnostic question.

Commit the repair, reusable analysis, compact evidence and Chinese walkthrough
on the current branch. Review standards and these requirements independently.
