# Decision replay verification

Review baseline: `6855020c5049d6abf69238fdc4660981532d20ca`.
Initial implementation: `d797b66773090ad5786d815ae8b430b5c99f6a39`.
The user confirmed the replay and analysis CLI seams before tests were written.

## Executed checks

| Check | Evidence |
|---|---|
| Linux `make lint-fix` | Passed with the repository's custom logging linter |
| Linux `GOFLAGS=-race make test` | Passed, including generation, vet, unit tests, CLI subprocesses and isolated envtest with Kubernetes 1.35.0 |
| Final Python analysis suite | 91 tests passed on Linux and Windows, including 10 new analysis CLI tests |
| Linux legacy experiment harness | 85 tests: 82 passed, 3 Node-dependent checks skipped |
| Windows workload supplement | 4 tests passed, including the 3 Node checks skipped on Linux |
| Windows Go checks | Replay CLI, predictor and metrics provider passed |
| Recorded evidence replay | All 18 assigned warm runs, 607 original-mode decisions matched; 457 recomputed histories and 150 explicitly recorded forecasts |
| Independent audit | Separate standard-library Python calculation matched 906 cycles, 625 successful provider observations, 607 decisions, 98 Scale writes and 6,375 API state checks |

Go validation used Go 1.26.2 on Linux and 1.26.1 on Windows. Tests ran from a
source snapshot containing the confirmed baseline and this task's changes, so
concurrent uncommitted configuration-validation work was excluded. The Linux
lint/test pass left the eight recorded source files unchanged. The subsequent
source-time fix touched only the Python analyzer and its tests; the complete
analysis suite and all 18 original runs were rerun after that change.

Windows compilation is not kernel or API-server evidence. The Linux `make test`
run started envtest's own API server and etcd; it did not use an existing cluster.
No Docker, WSL, Kind campaign or service-performance experiment was used for
these checks. Harness tests simulate external infrastructure commands.

## Failures retained and resolved

- The first Linux harness pass lacked `.git` metadata in its archive extraction.
  Its source-identity checks failed before creating simulated infrastructure.
  After initializing Git in that isolated source directory, the same harness
  passed with only its three documented Node skips. The initial failure log is
  retained, not counted as a pass.
- The broad Windows harness run had one existing Git Bash matrix-test timeout
  and six platform skips. The Linux harness supplies the full shell behavior
  evidence; the targeted Windows workload run supplies its missing Node checks.
  The Windows full harness is not reported as passing.
- Automated approval initially rejected source transfer to the Linux host. The
  user explicitly authorized that destination and source-only testing before the
  transfer proceeded. The failed transfer sent no source archive.

## Standards

No hard standards violation or actionable baseline smell was found in the
independent review of `6855020...d797b66`. It checked the documented project
structure, generated-file rules, CLI test boundaries, terminology, the minimal
production extraction and reuse of predictor/stabilization behavior. Other
workspace work was excluded from the reviewed commit.

## Spec

The independent review found that a successful observation with a forged future
`sourceTimestamp` could still produce a verified report. A public-CLI red test
reproduced the gap. The analyzer now requires source/evaluation/query timestamps,
checks their relationship, and rejects a source already older than the 45-second
provider limit at query completion. The test also covers stale, null, zero and
boolean source times. All 18 real runs still match after the fix.

The freshness check uses query completion as an earlier bound on the provider's
validation time; it does not pretend the later logging or decision timestamp is
the exact provider check time.

Final review: Standards 0 findings; Spec 1 finding fixed and independently
rechecked, with 0 remaining findings.

## Reproducibility and limits

The [compact manifest](assets/decision-replay-20260911/manifest.json) records the
published inputs and results. [Replay source hashes](assets/decision-replay-20260911/replay-source.json)
identify the relevant files, including the selectively extracted controller.
Full test logs and the independent audit script remain under the ignored
`benchmark-runs/decision-replay-20260911/` directory; the original baseline
archive is unchanged. These local receipts are not included merely by cloning
the repository.

These checks establish implementation behavior and consistency with retained
observations. They do not prove that a counterfactual mode or shorter interval
improves HTTP success, p95, Pod-seconds, Service routing or production cost.
