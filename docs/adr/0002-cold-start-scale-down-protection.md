# Rebuild scale-down protection conservatively after restart

When trustworthy recommendation history is unavailable, protect the live
requested replica count for one full stabilization window. Scope history to the
PredictiveHPA UID and target Deployment UID, and allow higher current demand to
scale up during protection. This preserves a defined safety boundary on restart
and leader transition without claiming to restore the lost recommendations.

Persisting every recommendation in status or a ConfigMap would require a
durability protocol around separate Scale and history writes, conflict handling,
bounded serialization and schema evolution. Rebuilding a conservative window
instead may retain replicas for an extra window after restart. That is the
chosen cost for this implementation.

Recommendation history stores raw recommendations, never its own stabilized
output. Its maximum may retain capacity but may not initiate expansion. Bounded
time buckets preserve the maximum and latest observation in each bucket, so a
high recommendation cannot expire early; conservative extra retention is
bounded by one bucket width.

Changing a positive stabilization window rebuilds the buckets and protects live
requested capacity for one complete new window, including when shortening it.
The original within-bucket peak times cannot be recovered exactly after
aggregation. Rebuilding avoids pretending that the old bucket geometry obeys
the new retention bound; setting the window to zero disables protection
immediately.
