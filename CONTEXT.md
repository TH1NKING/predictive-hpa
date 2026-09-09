# PredictiveHPA

PredictiveHPA turns CPU observations for a Kubernetes workload into bounded
replica recommendations. These terms distinguish workload identity, observed
demand and the protection applied before changing capacity.

## Language

**Target identity**:
The particular Deployment controlled by a PredictiveHPA, including its UID.
A replacement with the same namespace and name is a different target.
_Avoid_: Deployment name as identity

**Verified CPU observation**:
A CPU utilization observation whose contributing containers belong to the target
and whose usage and CPU requests cover the same container set.
_Avoid_: Average of whichever metrics happened to return

**Observation history**:
The retained sequence of verified CPU observations used to predict demand.
A historical observation keeps its original membership even after a rollout.
_Avoid_: Current Pod list applied retrospectively

**Source sample time**:
The timestamp of a stored raw CPU counter sample. It differs from the time an
expression is evaluated and does not prove when an exporter refreshed its cache.
_Avoid_: Query time as source freshness

**Requested replicas**:
The latest replica count requested through the target's Scale subresource.
It can differ from the number of observed or Ready replicas.
_Avoid_: Current replicas without identifying which count

**Recommendation history**:
Recent replica recommendations before stabilization is applied. It is distinct
from the CPU observation history and from successful Scale writes.
_Avoid_: History without specifying which history

**Cold-start protection**:
A full stabilization window protecting the target's requested capacity when
the controller lacks trustworthy recommendation history.
_Avoid_: Restored history
