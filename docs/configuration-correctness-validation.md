# Configuration correctness verification

Baseline: `59d0a3863338dcc44a4ec9c58f8067eb3c5a4fa9`.
Contract: [configuration-correctness-plan.md](configuration-correctness-plan.md).

## Reproduced failures

| Boundary | Before the fix |
|---|---|
| Kubernetes 1.35 admission | An unstructured PHPA with `window: not-a-duration` was accepted; the controller Watch logged a duration decode failure |
| Reconcile → Scale | A negative horizon with CPU 20% → 100% and stabilization disabled reduced requested replicas from 5 to 1 |
| Predictor | The same two observations, alpha 0.3 and horizon -1m produced approximately -8.24% without an error |
| Analysis CLI | Consistently backdated evaluation timestamps preceding observation/startup were accepted |
| Replay CLI | A horizon beyond the new operational one-hour bound was accepted |

The first row was reproduced against an isolated Linux envtest API server and
etcd. The other red checks used the public reconciliation, predictor and CLI
boundaries. Each was followed by the corresponding fix and focused green check.

## Final checks

Linux used Go 1.26.2 and Kubernetes 1.35.0 envtest binaries. The final frozen
snapshot passed all checks below; generation, formatting and lint left every
source file unchanged, and the recovered hashes matched the local checkout.

| Check | Result |
|---|---|
| `make manifests generate` and `bash hack/sync-chart-crd.sh` | Passed; base and chart CRDs agree |
| Admission create/update suite | Passed, including malformed strings, duration boundaries and typed reads |
| `make lint-fix` | Passed with 0 issues |
| `GOFLAGS=-race make test` | Passed, including generation, vet, unit tests, CLI tests and envtest |
| Schema upgrade regression | Passed against a second isolated API server: old invalid object, stricter schema, status hold and repair |
| Helm lint and rendering | Passed; 1.33 rendering accepted and 1.32 rejected as required |
| Python analysis suite | 94 passed on Linux and Windows |
| Linux experiment harness | 85 tests, 82 passed and 3 Node-dependent tests skipped |
| Windows workload supplement | 4 passed, including the 3 Node checks skipped on Linux |
| Standards review | 0 remaining findings, including generated-file synchronization and the upgrade test |
| Spec review | The Kubernetes-version/status compatibility finding was fixed by the 1.33 bound and real upgrade regression; no other findings |

The first Linux lint attempt reported a repeated test reason string and a
constant-only fixture argument. The final tests share the reason constant and
use the fixture across two namespaces. The initial failure and successful run
are preserved separately. Helm rendering checks do not claim an actual 1.33
cluster was exercised; the API server used here was 1.35.

## Recording compatibility

All 18 original raw recordings were reprocessed with the updated Python
analysis CLI and a newly built real Go replay binary:

- 906 reconciliation cycles and 607 matching decisions;
- 457 forecasts recomputed from complete history and 150 recorded forecasts;
- 6,375 API observation checks;
- every generated replay input/result is JSON-equal to its previous result;
- every original-file hash manifest is unchanged.

This demonstrates that the new evaluation lower bound did not reject these
recordings. It does not establish new service-performance results. Outputs are
retained separately under `benchmark-runs/correctness-20260913/replay-validation/`.

## Deployment and verification boundaries

Chart 0.3.0 requires Kubernetes 1.33 or newer for stable validation ratcheting,
so an unchanged invalid legacy spec can still receive an explanatory status
update. This does not repair malformed duration strings that typed clients
cannot decode. Correct those objects before starting the controller.

Linux verification uses a new source directory, an explicit nonexistent live
kubeconfig and `USE_EXISTING_CLUSTER=false`; envtest starts its own API server.
Generation and chart synchronization are performed by repository commands.
Returned generated/formatted files are checked against the frozen local source
hashes before recovery. Earlier failing logs and snapshots are retained under
`benchmark-runs/correctness-20260913/`.

No Kind workload or live performance pilot is included in this verification.
The next pilot is a prepared protocol in
[cadence-pilot-plan.md](benchmarks/cadence-pilot-plan.md).
