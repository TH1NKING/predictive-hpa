# Metrics safety: independent review

Baseline: `9156078f326446bf9e5413bc0c810776a78564ae`.
Initial implementation reviewed: `fd5fb8dbf7d786ed64d2555dac5d5473d92061f1`.
The Standards and Spec axes were reviewed independently against `AGENTS.md`,
`CONTEXT.md`, the ADRs and `docs/metrics-safety-plan.md`.

## Standards

The initial review reported zero documented-standard breaches and zero
actionable baseline code smells. In particular, generated RBAC remained tied
to markers, target watches were added, status used conditions and optimistic
patching, and the two kinds of history agreed with the domain glossary.

## Spec

The initial review found two P2 issues after the unit, envtest and race checks
had passed:

1. A normal rollout's Pending/unready Pod caused the provider to erase previously
   verified CPU history. The original regression replaced a Ready Pod directly
   and missed the intermediate state. The required behavior is to reject the
   incomplete current observation and preserve previously verified facts.
2. The no-Scale-write path rechecked PHPA identity but did not recheck the target
   before publishing successful status. A same-name target replacement could
   therefore receive old-target CPU in a new `MetricsReady=True` publication.

These findings distinguish input/identity correctness from passing fixtures;
they require public-boundary regressions and a follow-up review of the fixes.

## Follow-up

Both issues were fixed in `9b1a677` and independently re-reviewed:

- `TestProviderRetainsAcceptedHistoryWhileRolloutMetricsWarmUp` covers Pending
  -> Ready with no CPU yet -> fresh CPU, retaining the original timestamps and
  values of previously verified observations.
- `TestReconcileSafetyNoScaleDecisionCannotPublishMetricsForReplacedTarget`
  replaces the target after the initial Scale read on a no-write decision. The
  success-status path now rechecks UID and clears stale CPU with
  `MetricsReady=False/TargetChanged`.

Standards follow-up: 0 residual findings. Spec follow-up: both original P2s
resolved, 0 residual findings. Linux lint-fix, full make test and the
controller/provider race checks passed after the fixes. Final cluster evidence
is recorded separately in [validation](metrics-safety-validation.md); the earlier
image's run is not substituted for the corrected image.
