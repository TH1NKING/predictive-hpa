# History and metric diagnostic implementation review

The user confirmed baseline `18a3abc8f68ef21f1a91ca100fefd1189fe0325c` and the
public Reconcile/diagnostic-log and analysis-CLI test seams. The baseline resolved
and the committed diff was nonempty before independent reviews started.

Initial review command: `git diff 18a3abc8f68ef21f1a91ca100fefd1189fe0325c...HEAD`.
Initial reviewed commits were `48d1db6` (history retention) and `77e1899` (metric
analysis, evidence and teaching). Both reviewers then checked the corrective
diff against `77e1899`; the Spec reviewer independently replayed the failing
CLI fixtures. Requirements are in [the task specification](history-metrics-followup.md)
and [the diagnostic protocol](metric-visibility-diagnostic.md).

## Standards

0 documented-standard violations and 0 sufficiently concrete baseline smells.
The history cleanup and diagnostic snapshot stay inside the existing lock;
valid windows include the newly appended recommendation. Tests observe public
logs and the API. The analysis, reports and figure distinguish source times,
visibility bounds and independent queries. Repository instructions and lint
configuration were checked; SVG path data was not reviewed line by line.

The corrective diff also introduced no violations or reportable smells. It
moves input parsing before the no-expansion return, and preserves empty raw
queries through explicit observations and flags. The new tests use the CLI.

## Spec

Initial review found two P2 defects, both resolved:

1. The protocol requires an error for unreadable or unparsable inputs. With no
   expansion, the CLI returned success before reading the plan or observations.
   It now reads all required files before reporting a valid no-expansion result.
   Missing-plan and corrupt-observation fixtures exit 2 and create no output.
2. The protocol requires explicit missing-data flags. Successful empty CPU and
   request raw queries previously disappeared into empty collections. They now
   retain their kind, evaluation time, request interval and missing-data flags;
   absent series do not become invented zero-CPU values or per-series counts.

Independent replay confirmed both fixes. Valid input without an expansion still
retains the run with a null cutoff and `missing_successful_expansion`. No scope
creep or further errors in the controller repair or result interpretation were
identified. Final unresolved Spec findings: 0.

The original history failure, both new CLI failures and their passing runs are
retained under `benchmark-runs/history-metrics-20260908/`. Required Go lint and
full unit/envtest checks passed; the final analysis suite passed 57 tests. The
Linux harness passed 36 tests with two Node-dependent skips, both passed
separately on Windows. The final diagnostic output was independently compared
again after the CLI corrections; exact counts and hashes are in the
[evidence JSON](assets/metric-visibility-inputs-20260908.json).

Final findings: Standards 0 (no outstanding severity); Spec 0 (two initial P2s resolved).
