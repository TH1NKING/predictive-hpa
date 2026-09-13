# Configuration and replay evidence correctness

Review baseline: `59d0a3863338dcc44a4ec9c58f8067eb3c5a4fa9`.

## Behavior

- Reject malformed prediction durations at CRD admission, before an object can
  break typed client decoding. Keep Go duration strings, including composite
  and fractional values. Accept a history window from 15 seconds through one
  hour and a prediction horizon greater than zero and at most one hour.
  Operational forecasts require a future horizon; the generic predictor can
  still return its smoothed tail for a zero horizon.
- Reject a replica minimum greater than its maximum. Apply the same duration
  and replica bounds at the reconciliation boundary for decodable objects
  stored before the schema upgrade. Invalid configuration holds Scale, clears
  obsolete CPU display values, and reports `MetricsReady=False` with reason
  `InvalidConfiguration`. Correcting the configuration resumes normal work.
- Reject negative horizons at the predictor's public boundary. Keep business
  limits in the controller; the generic predictor does not impose a one-hour
  upper bound.
- Require Kubernetes 1.33 or newer in the chart, including stable validation
  ratcheting so status can report an unchanged invalid legacy specification.
  Verify the old-schema create, schema-upgrade, status-write and repair path
  against the actual API server rather than only a fake client.
- Reject replay evidence whose evaluation predates the current observation.
  Compare against observation start truncated to milliseconds, matching the
  production query instant precision. Retain source freshness and ordering
  checks and verify existing recordings without rewriting historical assets.

## Verification

Exercise admission create/update through the Kubernetes API, including invalid
literal strings sent as unstructured objects and successful typed listing after
rejection. Exercise runtime protection through Reconcile and Scale/status,
including repair and recovery. Exercise replay rejection and the valid
millisecond boundary through the public analysis CLI. Run generated manifests,
lint, Go unit/envtest checks and the relevant Python suites, then independently
review standards and this contract against the fixed baseline.

## Limits and follow-up

Admission changes do not repair already stored malformed strings; deployments
must inspect and correct those objects with an unstructured client before
starting a typed controller. A valid history window must still contain two
actual observations; a 15-second schema minimum does not guarantee warmup with
a 30-second reconciliation interval.

After correctness validation, the next experiment remains the phase-paired
Current-mode 30-second versus 15-second pilot described in the decision replay
report. Keep the 60-second CPU rate window and existing safety checks, preserve
historical data, and use a fresh isolated campaign with service routing and
capacity calibration before interpreting service outcomes.
