# Build prediction history from verified live CPU observations

The controller verifies the live Pod -> ReplicaSet -> Deployment UID chain
through the Kubernetes API, pairs CPU usage with requests from the same Pod
specification, and retains accepted utilization observations. It does not infer
historical ownership from workload name prefixes or from kube-state-metrics
ReplicaSet owner names: those metrics do not include the owner UID needed to
distinguish object incarnations. The upstream [ReplicaSet metric labels](https://github.com/kubernetes/kube-state-metrics/blob/main/docs/metrics/workload/replicaset-metrics.md)
and [Pod metric labels](https://github.com/kubernetes/kube-state-metrics/blob/main/docs/metrics/workload/pod-metrics.md)
document this difference.

The history preserves a recorded observation after its contributing Pods have
been removed. It is bounded and scoped to a target UID. A new process rebuilds
history from live observations; at least two distinct accepted observations are
needed by every decision mode. Current object ownership cannot establish the
ownership of arbitrary metrics from before the process started.

CPU requests come from the verified live Pod specifications. This removes a
second, asynchronously scraped denominator and allows missing requests to be
reported instead of silently omitting containers. This version deliberately
rejects unsupported Pod-level resource and restartable-init-sidecar semantics.

## Consequences

The sampling cadence and cold-start behavior differ from the previous 15-second
Prometheus range-query pipeline. Previous benchmark results remain historical;
they must not be reused as performance evidence for this implementation.
The CPU rate window remains 60 seconds. Raw counter timestamps establish the
age of stored Prometheus samples, not the time cAdvisor refreshed an internal
cache. This change establishes input correctness; it does not promise an
earlier scale-up or improved service quality.

Persisting an ownership ledger or exporting historical owner UIDs could support
restart recovery of a longer prediction history. That adds a separate durable
data lifecycle and is deferred until there is a demonstrated need.
