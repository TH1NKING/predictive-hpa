package metricsprovider

import (
	"fmt"
	"math"
	"strings"
	"time"

	"github.com/prometheus/common/model"
)

func matchesInstance(metric model.Metric, expected expectedContainer) bool {
	id := string(metric["id"])
	parts := strings.Split(id, "/")
	if len(parts) < 2 {
		return false
	}
	podFound := false
	for _, part := range parts[:len(parts)-1] {
		if part == "pod"+string(expected.podUID) || strings.HasSuffix(part, "-pod"+strings.ReplaceAll(string(expected.podUID), "-", "_")+".slice") {
			podFound = true
		}
	}
	leaf := parts[len(parts)-1]
	instanceFound := leaf == expected.containerID || leaf == "cri-containerd-"+expected.containerID+".scope" || leaf == "crio-"+expected.containerID+".scope" || leaf == "docker-"+expected.containerID+".scope"
	return podFound && instanceFound
}

func (p *PrometheusProvider) utilization(roster targetRoster, rates, sources model.Vector, at time.Time) (float64, time.Time, error) {
	if len(rates) == 0 || len(sources) == 0 {
		return 0, time.Time{}, ErrNoData
	}
	var totalCPU, totalRequest float64
	var oldest time.Time
	for key, expected := range roster.containers {
		rate, err := uniqueSample(rates, key, expected)
		if err != nil {
			return 0, time.Time{}, err
		}
		source, err := uniqueSample(sources, key, expected)
		if err != nil {
			return 0, time.Time{}, err
		}
		if err := validatePair(rate, source, at, p.maxSampleAge()); err != nil {
			return 0, time.Time{}, err
		}
		totalCPU += float64(rate.Value)
		totalRequest += expected.request
		sourceAt := time.Unix(0, int64(float64(source.Value)*float64(time.Second)))
		if oldest.IsZero() || sourceAt.Before(oldest) {
			oldest = sourceAt
		}
	}
	value := 100 * totalCPU / totalRequest
	if !finite(value) {
		return 0, time.Time{}, fmt.Errorf("%w: CPU aggregate is not finite", ErrInvalidData)
	}
	return value, oldest, nil
}

func (p *PrometheusProvider) maxSampleAge() time.Duration {
	if p.MaxSampleAge == 0 {
		return DefaultMaxSampleAge
	}
	return p.MaxSampleAge
}

func finite(value float64) bool { return !math.IsNaN(value) && !math.IsInf(value, 0) }

func validatePair(rate, source *model.Sample, at time.Time, maxAge time.Duration) error {
	rateLabels, sourceLabels := rate.Metric.Clone(), source.Metric.Clone()
	delete(rateLabels, model.MetricNameLabel)
	delete(sourceLabels, model.MetricNameLabel)
	if !rateLabels.Equal(sourceLabels) {
		return fmt.Errorf("%w: CPU and source timestamp series differ", ErrIncompleteData)
	}
	if rate.Timestamp.Time().UnixMilli() != at.UnixMilli() || source.Timestamp.Time().UnixMilli() != at.UnixMilli() {
		return fmt.Errorf("%w: unexpected query evaluation timestamp", ErrInvalidData)
	}
	cpu, sourceSeconds := float64(rate.Value), float64(source.Value)
	if !finite(cpu) || cpu < 0 || !finite(sourceSeconds) || sourceSeconds < 0 || sourceSeconds > float64(at.UnixNano())/float64(time.Second) {
		return fmt.Errorf("%w: CPU or source timestamp is invalid", ErrInvalidData)
	}
	if maxAge <= 0 {
		return fmt.Errorf("%w: maximum sample age must be positive", ErrInvalidData)
	}
	if sourceSeconds < float64(at.Add(-maxAge).UnixNano())/float64(time.Second) {
		return ErrStaleData
	}
	return nil
}

func uniqueSample(vector model.Vector, key containerKey, expected expectedContainer) (*model.Sample, error) {
	var found *model.Sample
	for _, sample := range vector {
		if string(sample.Metric["namespace"]) != key.namespace || string(sample.Metric["pod"]) != key.pod || string(sample.Metric["container"]) != key.container || !matchesInstance(sample.Metric, expected) {
			continue
		}
		if found != nil {
			return nil, fmt.Errorf("%w: ambiguous CPU series for %s/%s", ErrIncompleteData, key.pod, key.container)
		}
		found = sample
	}
	if found == nil {
		return nil, fmt.Errorf("%w: CPU series unavailable for %s/%s", ErrIncompleteData, key.pod, key.container)
	}
	return found, nil
}
