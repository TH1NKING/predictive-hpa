package controller

import (
	"context"
	"sync"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	"k8s.io/utils/clock"

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
	mu        sync.Mutex
	samples   map[string][]predictor.Sample
	Clock     clock.PassiveClock
	histories map[string]metricsprovider.CPUHistory
	errors    map[string]error
}

func newFakeMetricsProvider() *fakeMetricsProvider {
	return &fakeMetricsProvider{
		samples:   make(map[string][]predictor.Sample),
		histories: make(map[string]metricsprovider.CPUHistory),
		errors:    make(map[string]error),
	}
}

// AverageCPUUtilizationPercentage implements metricsprovider.Provider.
// The `window` argument is accepted for signature compatibility but ignored:
// the fake returns whatever series the test configured, verbatim.
func (f *fakeMetricsProvider) AverageCPUUtilizationPercentage(
	_ context.Context,
	deployment *appsv1.Deployment,
	_ time.Duration,
) (metricsprovider.CPUHistory, error) {
	f.mu.Lock()
	defer f.mu.Unlock()

	key := deployment.Namespace + "/" + deployment.Name
	if err := f.errors[key]; err != nil {
		return metricsprovider.CPUHistory{}, err
	}
	if h, ok := f.histories[key]; ok {
		h.Samples = append([]predictor.Sample(nil), h.Samples...)
		return h, nil
	}
	series, ok := f.samples[key]
	if !ok || len(series) == 0 {
		return metricsprovider.CPUHistory{}, metricsprovider.ErrNoData
	}
	// Return a copy so the caller (Reconcile) can't mutate the test's state.
	out := make([]predictor.Sample, len(series))
	copy(out, series)
	now := time.Now()
	if f.Clock != nil {
		now = f.Clock.Now()
	}
	// Legacy envtest fixtures express relative CPU shapes; keep those shapes
	// current as FakeClock advances policy time. Explicit histories below test
	// stale and malformed source timestamps without this translation.
	shift := now.Sub(out[len(out)-1].Timestamp)
	for i := range out {
		out[i].Timestamp = out[i].Timestamp.Add(shift)
	}
	return metricsprovider.CPUHistory{Samples: out, ObservedAt: now, SourceTimestamp: now}, nil
}

// SetSamples installs a sample series for the given Deployment. Subsequent
// Reconcile calls for that Deployment will observe these samples until the
// test overwrites or clears them.
func (f *fakeMetricsProvider) SetSamples(namespace, deployment string, series []predictor.Sample) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.samples[namespace+"/"+deployment] = series
	delete(f.histories, namespace+"/"+deployment)
	delete(f.errors, namespace+"/"+deployment)
}

func (f *fakeMetricsProvider) SetHistory(namespace, deployment string, history metricsprovider.CPUHistory, err error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.histories[namespace+"/"+deployment] = history
	f.errors[namespace+"/"+deployment] = err
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
