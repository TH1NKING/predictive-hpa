# Metrics correctness and restart protection

This is the implementation and review spec for the user's accepted sequence:
metric attribution and aggregation, unavailable/stale input protection, then
restart-safe scale-down protection. The review baseline is
`9156078f326446bf9e5413bc0c810776a78564ae`.

## 1. Metric attribution and aggregation

- Resolve a target by Deployment UID. A label selector narrows the candidate
  Pods; the Pod controller UID, ReplicaSet UID and Deployment controller UID
  establish membership. Another Deployment with a similar name must not affect
  the target's input.
- Read the authoritative CPU requests for the same regular containers whose
  live CPU usage is measured. Compute `100 * sum(cpu cores) / sum(request cores)`.
  Missing/nonpositive requests, unsupported resource semantics and ambiguous
  container instances are unavailable inputs, not silently filtered values.
- Bind cAdvisor series to the Pod UID and current runtime container instance.
  Reject incomplete or duplicate inputs. Recheck the target and contributing
  roster after the query, so a concurrent rollout cannot combine different
  membership or request snapshots.
- Retain verified live observations by target identity; keep past observations
  across normal rollouts and Pod deletion, including Pending/unready transitions
  and temporarily absent new-container metrics. Reject the current incomplete
  observation without erasing prior verified facts or filling the gap with zero.
  Invalid/stale samples and query failures reset CPU history. Rebuild history on
  process restart rather than assigning past samples using only the current Pod list.
- Keep the 60-second CPU rate window. Bound observation storage and report the
  supported history window and observation cadence explicitly.

## 2. Unavailable and stale inputs

- Inspect raw CPU sample timestamps separately from Prometheus evaluation time.
  Reject stale/future timestamps and nonfinite or negative usage. Default maximum
  source sample age is 45 seconds; queries have a bounded deadline.
- Every contributing container must have complete, fresh input. This version
  holds Scale when inputs are unavailable; it does not replace missing data with
  zero or infer a safe direction from an incomplete aggregate.
- Set `MetricsReady` with a reason and observed generation. Clear obsolete CPU
  display values on unavailability while preserving the last successful scale
  time. A later sequence of valid observations resumes reconciliation.
- Validate the Provider result at the controller boundary before running the
  predictor; an invalid forecast must not reach replica arithmetic.

## 3. Restart and identity safety

- Missing recommendation history starts one full stabilization window protecting
  the live requested capacity; scale-up remains possible. A new leader receives
  the same protection. This is conservative rebuilding, not persisted history
  recovery.
- Isolate history on PHPA UID, target UID and target reference changes. Any
  change to a positive window rebuilds protection for one full new window,
  including a shorter window; clock rollback also rebuilds protection. Zero
  disables stabilization immediately.
- Retain raw recommendations in bounded conservative time buckets. Historical
  maxima cannot initiate an expansion or reverse an already-requested downscale.
  Current replica bounds remain authoritative.
- Recheck PHPA/target identity before writes, including successful status
  publication when no Scale update is needed, use optimistic concurrency, and
  preserve the distinction between requested and observed replicas.
- Enable Helm leader election with the needed namespace-scoped Lease RBAC;
  keep the default manager replica count at one.

## Public verification boundaries

The accepted regression and cluster verification are exercised at these seams:

1. Metrics Provider result, with Kubernetes API and Prometheus HTTP as external
   system boundaries: ownership, hand-calculated weighted CPU, rolling updates,
   missing/ambiguous/stale data and recovery.
2. Reconcile -> Kubernetes Scale/status, with controlled time: input rejection,
   conditions, cold starts, UID replacement, concurrency and window changes.
3. An isolated Kind cluster with a dedicated kubeconfig: real Prometheus/cAdvisor
   collection, same-prefix workloads, actual Scale changes and controller
   restart/leader transition. Windows Go tests are not Linux cluster evidence.

Run focused tests during implementation; regenerate manifests when RBAC/type
markers change, run lint-fix and the full Go suite at the end, then perform
independent Standards and Spec reviews and address actionable findings.
Record exact commands, identities and failed attempts with the verification
results. Historical benchmark outcomes are not acceptance results for this work.
