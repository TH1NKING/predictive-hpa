// Package metricsprovider abstracts fetching CPU utilization time-series
// for a Kubernetes Deployment. Implementations may pull from Prometheus,
// metrics-server, or other monitoring backends.
package metricsprovider

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/prometheus/client_golang/api"
	promv1 "github.com/prometheus/client_golang/api/prometheus/v1"
	"github.com/prometheus/common/model"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	"github.com/th1nking/predictive-hpa/internal/predictor"
)

// Provider abstracts fetching of metric time-series for a Deployment.
type Provider interface {
	// AverageCPUUtilizationPercentage returns the average CPU utilization
	// (in percent of requested CPU) across all pods of the named Deployment,
	// sampled over the most recent `window` of time.
	AverageCPUUtilizationPercentage(
		ctx context.Context,
		namespace, deployment string,
		window time.Duration,
	) ([]predictor.Sample, error)
}

// ErrNoData is returned when Prometheus succeeds but yields no series
// (e.g. the Deployment has no Pods yet, or kube-state-metrics hasn't
// caught up). Callers should treat this as a transient condition.
var ErrNoData = errors.New("metricsprovider: no data returned")

// PrometheusProvider implements Provider against the Prometheus HTTP API.
type PrometheusProvider struct {
	api  promv1.API
	step time.Duration
}

// NewPrometheus constructs a PrometheusProvider pointed at the given
// Prometheus base URL (e.g. "http://localhost:9090").
func NewPrometheus(baseURL string) (*PrometheusProvider, error) {
	client, err := api.NewClient(api.Config{Address: baseURL})
	if err != nil {
		return nil, fmt.Errorf("metricsprovider: build prometheus client: %w", err)
	}
	return &PrometheusProvider{
		api:  promv1.NewAPI(client),
		step: 15 * time.Second,
	}, nil
}

// cpuUtilQueryTemplate computes per-Deployment average CPU utilization in
// percent. The `container!=""` filter excludes the pod-level pseudo-
// aggregate series emitted by cAdvisor (verified in Phase 0/8.5a).
const cpuUtilQueryTemplate = `(avg(rate(container_cpu_usage_seconds_total{namespace="%s",pod=~"%s-.*",container!=""}[1m]))/avg(kube_pod_container_resource_requests{namespace="%s",pod=~"%s-.*",resource="cpu"}))*100`

// AverageCPUUtilizationPercentage queries Prometheus for the given
// Deployment's CPU utilization series and returns it as a []predictor.Sample
// ordered by ascending timestamp.
func (p *PrometheusProvider) AverageCPUUtilizationPercentage(
	ctx context.Context,
	namespace, deployment string,
	window time.Duration,
) (samples []predictor.Sample, queryErr error) {
	query := fmt.Sprintf(cpuUtilQueryTemplate, namespace, deployment, namespace, deployment)

	end := time.Now()
	start := end.Add(-window)

	queryStarted := time.Now()
	result, _, err := p.api.QueryRange(ctx, query, promv1.Range{
		Start: start,
		End:   end,
		Step:  p.step,
	})
	queryFinished := time.Now()
	defer func() {
		var latestEvaluationAt any
		if len(samples) > 0 {
			latestEvaluationAt = samples[len(samples)-1].Timestamp.UTC().Format(time.RFC3339Nano)
		}
		errorMessage := ""
		if queryErr != nil {
			errorMessage = queryErr.Error()
		}
		// QueryRange timestamps identify evaluation points, not source scrapes.
		// Raw-series visibility is collected separately by the experiment observer.
		logf.FromContext(ctx).Info("Queried CPU utilization",
			"queryStartedAt", queryStarted.UTC().Format(time.RFC3339Nano),
			"queryFinishedAt", queryFinished.UTC().Format(time.RFC3339Nano),
			"queryDurationSeconds", queryFinished.Sub(queryStarted).Seconds(),
			"rangeStartAt", start.UTC().Format(time.RFC3339Nano),
			"rangeEndAt", end.UTC().Format(time.RFC3339Nano),
			"queryStepSeconds", p.step.Seconds(), "cpuRateWindowSeconds", 60,
			"latestEvaluationAt", latestEvaluationAt, "samples", len(samples),
			"queryError", errorMessage)
	}()
	if err != nil {
		return nil, fmt.Errorf("metricsprovider: query_range: %w", err)
	}

	matrix, ok := result.(model.Matrix)
	if !ok {
		return nil, fmt.Errorf("metricsprovider: unexpected result type %T (want Matrix)", result)
	}
	if len(matrix) == 0 {
		return nil, ErrNoData
	}
	// avg() drops all labels, so the result should be a single series.
	if len(matrix) > 1 {
		return nil, fmt.Errorf("metricsprovider: got %d series, want 1 (query bug)", len(matrix))
	}

	series := matrix[0]
	samples = make([]predictor.Sample, len(series.Values))
	for i, v := range series.Values {
		samples[i] = predictor.Sample{
			Timestamp: v.Timestamp.Time(),
			Value:     float64(v.Value),
		}
	}
	return samples, nil
}
