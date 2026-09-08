# Metric pipeline implementation review

The user confirmed baseline `24a13f468b81a770d1a3ed99d1d1fa3c9e1396c0` and
the public observer CLI/output and analysis CLI seams. Initial implementation
commit: `e1cb2eaad9fef0ca1362b3e74f06c0e83b40d9c1`.

The baseline resolved and `git diff 24a13f468b81a770d1a3ed99d1d1fa3c9e1396c0...HEAD`
was nonempty before two independent reviewers started. An intervening external
merge, `0fba159`, has the same tree as the confirmed baseline. Requirements are
in [the implementation protocol](metric-pipeline-followup.md).

## Standards

Initial review found zero documented-standard violations and zero concrete
baseline smells requiring changes. It checked all nine changed files against
AGENTS.md, the repository's agent documents and the existing Python/shell/CI
conventions. Generated assets and scaffold markers were not modified. This was
a read-only review; it did not rerun already-passing tests.

## Spec

The independent reviewer reproduced three P2 findings through the public CLI:

1. A retained HTTP 502 HTML response caused an `AttributeError` in error
   extraction rather than producing the run's query-error observation.
2. A source-node observation could be marked as an exact match to a sample
   explicitly associated with another node because target identity was ignored.
3. A cycle containing only the two evaluated CPU expressions could have no
   quality flags despite missing the rest of the pipeline observations.

All three now have failing/passing public CLI regressions and fixes. Error
responses retain their original body; node mapping can be verified, unknown or
contradictory; completeness checks cover every retained cycle and empty streams.
The complete analysis suite passed 69 tests. Independent rechecks are in progress.
The original fixtures and results are retained under
`benchmark-runs/review-spec-20260908/`.
Pending live results and the final teaching/report were explicitly outside this
pre-pilot implementation review, rather than silently treated as delivered.

## Experiment support

A separate read-only review of the new ignored campaign support found three
operational evidence issues inherited from or exposed by script reuse: a seal
failure could report success; the initial image patch checked only part of the
Deployment spec; and the finalizer merely archived support/tested-source proofs.

All three were corrected and independently rechecked. Calibration sealing now
starts with a failed receipt and only changes it after a completed seal. Both
image writes compare a fresh full spec and use UID/resourceVersion/spec patch
preconditions. Support and tested-source manifests are verified against actual
files and the selected commit before and after packaging. Twelve public CLI
checks and nineteen guard checks passed. Log existence alone is explicitly not
a claim that tests passed; actual execution outcomes remain separate evidence.

Final implementation and scientific-delivery review results will be recorded
after the remaining corrections and real pilot verification.
