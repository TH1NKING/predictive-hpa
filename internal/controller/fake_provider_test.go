package controller

import (
	"context"
	"sync"
	"time"

	"github.com/th1nking/predictive-hpa/internal/metricsprovider"
	"github.com/th1nking/predictive-hpa/internal/predictor"
)

// fakeMetricsProvider is an in-memory metricsprovider.Provider used by envtest
// specs. It returns a configurable series of CPU samples per (namespace,
// deployment) key, decoupling Reconcile tests from a real Prometheus.
//
// The fake is intentionally minimal:
//   - Per-key sample series, mutable from the test body (call SetSamples).
//   - Returns metricsprovider.ErrNoData when no series has been set, mirroring
//     the production provider's "no data yet" signal so the corresponding
//     Reconcile branch (RequeueAfter) is exercised.
//   - Concurrency-safe: Reconcile runs in a background goroutine while the
//     test body sets up samples.
type fakeMetricsProvider struct {
	mu      sync.Mutex
	samples map[string][]predictor.Sample
}

func newFakeMetricsProvider() *fakeMetricsProvider {
	return &fakeMetricsProvider{
		samples: make(map[string][]predictor.Sample),
	}
}

// AverageCPUUtilizationPercentage implements metricsprovider.Provider.
// The `window` argument is accepted for signature compatibility but ignored:
// the fake returns whatever series the test configured, verbatim.
func (f *fakeMetricsProvider) AverageCPUUtilizationPercentage(
	_ context.Context,
	namespace, deployment string,
	_ time.Duration,
) ([]predictor.Sample, error) {
	f.mu.Lock()
	defer f.mu.Unlock()

	key := namespace + "/" + deployment
	series, ok := f.samples[key]
	if !ok || len(series) == 0 {
		return nil, metricsprovider.ErrNoData
	}
	// Return a copy so the caller (Reconcile) can't mutate the test's state.
	out := make([]predictor.Sample, len(series))
	copy(out, series)
	return out, nil
}

// SetSamples installs a sample series for the given Deployment. Subsequent
// Reconcile calls for that Deployment will observe these samples until the
// test overwrites or clears them.
func (f *fakeMetricsProvider) SetSamples(namespace, deployment string, series []predictor.Sample) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.samples[namespace+"/"+deployment] = series
}

// SetConstantCPU is a convenience helper: install N samples of identical CPU%
// spaced `step` apart, ending at now. Useful when the test only cares about
// the latest value (currentCPU = samples[last]) and a flat EWMA prediction.
func (f *fakeMetricsProvider) SetConstantCPU(
	namespace, deployment string,
	cpuPercent float64,
	n int,
	step time.Duration,
) {
	now := time.Now()
	series := make([]predictor.Sample, n)
	for i := range n {
		series[i] = predictor.Sample{
			Timestamp: now.Add(-time.Duration(n-1-i) * step),
			Value:     cpuPercent,
		}
	}
	f.SetSamples(namespace, deployment, series)
}
