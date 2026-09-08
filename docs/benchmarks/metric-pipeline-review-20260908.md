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
The complete analysis suite passed 69 tests. The original reviewer independently
replayed all three original fixtures and the three added regressions: all pass,
with no further findings. The Standards reviewer also checked the corrective
diff (`e1cb2ea..e41e52e`) and found no new violations or reportable smells.
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

The later kernel31 preparation accepts only the exact recorded kernel/reboot/
memory transition and retains the first rejected preflight unchanged. Six CLI
checks, nineteen guard checks and an independent read-only review passed before
launching that new batch. The same source `e41e52e` was used for calibration and
all three Current runs.

## Scientific delivery

Three runs and full restoration completed successfully. Local archive verification
matched 572 files and six complete seals. A separate raw ledger matched 6513
comparable fields, with eighteen coverage gaps retained explicitly; independent
chain parsing additionally checked source-node identity, match counts, scrape
history and the three line-indexed examples. Independent raw service and resource
state checks passed 38/38. No test or successful orchestration is presented as
proof of service improvement. Final results and caveats are in
[the report](metric-pipeline-20260908.md).

Implementation findings: Standards 0 (no outstanding severity); Spec 0
(three initial P2 findings resolved and independently replayed).

The final report review confirmed the values, scope and delivery. It found one
P3 figure-label issue: the axis said all scrape reports while the figure displayed
only times at or after load onset. The label now says post-onset scrape reports,
and both caption and text state that range; the negative-time reports remain in
the evidence. The corrected figure was rendered and visually checked, and its
input hashes and separate analysis-support archive were refreshed. No numeric
result or sealed experimental input changed.
