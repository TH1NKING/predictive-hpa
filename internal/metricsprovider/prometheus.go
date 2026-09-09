// Package metricsprovider collects verified live CPU observations for a Deployment.
package metricsprovider

import (
	"context"
	"errors"
	"fmt"
	"hash/fnv"
	"reflect"
	"regexp"
	"slices"
	"strings"
	"sync"
	"time"

	"github.com/prometheus/client_golang/api"
	promv1 "github.com/prometheus/client_golang/api/prometheus/v1"
	"github.com/prometheus/common/model"
	appsv1 "k8s.io/api/apps/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/clock"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	"github.com/th1nking/predictive-hpa/internal/predictor"
)

// CPUHistory contains only observations collected and verified by this process.
// SourceTimestamp is the oldest source timestamp across the latest observation's containers.
type CPUHistory struct {
	Samples         []predictor.Sample
	ObservedAt      time.Time
	SourceTimestamp time.Time
}

// Provider supplies CPU utilization observations for an exact Deployment incarnation.
type Provider interface {
	AverageCPUUtilizationPercentage(context.Context, *appsv1.Deployment, time.Duration) (CPUHistory, error)
}

var (
	// ErrNoData means there is no eligible workload or no metric response.
	ErrNoData = errors.New("metricsprovider: no data returned")
	// ErrStaleData means the newest usable raw counter is older than the allowed age.
	ErrStaleData = errors.New("metricsprovider: stale source data")
	// ErrInvalidData means a value or resource configuration cannot safely drive scaling.
	ErrInvalidData = errors.New("metricsprovider: invalid data")
	// ErrIncompleteData means the verified container set lacks unambiguous, ready input.
	ErrIncompleteData = errors.New("metricsprovider: incomplete data")
	// ErrTargetChanged means identity, membership or resource inputs changed during collection.
	ErrTargetChanged = errors.New("metricsprovider: target changed during observation")
)

const (
	// DefaultMaxSampleAge bounds the age of actual CPU counter samples, not query evaluation times.
	DefaultMaxSampleAge = 45 * time.Second
	observationSpacing  = 15 * time.Second
	maxHistoryWindow    = time.Hour
	maxTargets          = 256
	queryTimeout        = 10 * time.Second
)

type targetHistory struct {
	uid      types.UID
	samples  []predictor.Sample
	lastUsed time.Time
}

// PrometheusProvider joins live API identity and requests with Prometheus CPU samples.
// Configure Clock and MaxSampleAge before sharing the provider between goroutines.
type PrometheusProvider struct {
	api          promv1.API
	reader       client.Reader
	Clock        clock.PassiveClock
	MaxSampleAge time.Duration
	mu           sync.Mutex
	history      map[types.NamespacedName]*targetHistory
	// Fixed gates serialize a target's observations without holding the history lock.
	// Hash collisions only serialize unrelated targets; waits share the query deadline.
	gates [64]chan struct{}
}

// NewPrometheus requires an uncached API reader so both roster snapshots are authoritative.
func NewPrometheus(baseURL string, reader client.Reader) (*PrometheusProvider, error) {
	if reader == nil {
		return nil, fmt.Errorf("%w: API reader is required", ErrInvalidData)
	}
	promClient, err := api.NewClient(api.Config{Address: baseURL})
	if err != nil {
		return nil, fmt.Errorf("metricsprovider: build prometheus client: %w", err)
	}
	p := &PrometheusProvider{api: promv1.NewAPI(promClient), reader: reader, Clock: clock.RealClock{},
		MaxSampleAge: DefaultMaxSampleAge, history: make(map[types.NamespacedName]*targetHistory)}
	for i := range p.gates {
		p.gates[i] = make(chan struct{}, 1)
	}
	return p, nil
}

// AverageCPUUtilizationPercentage samples a live roster twice around the CPU queries.
// Invalid observations discard this target's history, requiring prediction warmup again.
func (p *PrometheusProvider) AverageCPUUtilizationPercentage(
	ctx context.Context, target *appsv1.Deployment, window time.Duration,
) (history CPUHistory, observationErr error) {
	started := time.Now()
	var evaluatedAt time.Time
	var queryStarted, queryFinished time.Time
	var gate chan struct{}
	gateHeld := false
	defer func() {
		if observationErr != nil && target != nil && gateHeld {
			p.discard(client.ObjectKeyFromObject(target))
		}
		p.logObservation(ctx, started, queryStarted, queryFinished, evaluatedAt, history, observationErr)
		if gateHeld {
			<-gate
		}
	}()
	if target == nil || target.UID == "" {
		return CPUHistory{}, fmt.Errorf("%w: target UID is required", ErrInvalidData)
	}
	ctx, cancel := context.WithTimeout(ctx, queryTimeout)
	defer cancel()
	if err := ctx.Err(); err != nil {
		return CPUHistory{}, err
	}
	gate = p.queryGate(client.ObjectKeyFromObject(target))
	select {
	case gate <- struct{}{}:
		gateHeld = true
	case <-ctx.Done():
		return CPUHistory{}, ctx.Err()
	}
	if window < observationSpacing || window > maxHistoryWindow {
		return CPUHistory{}, fmt.Errorf("%w: history window must be between 15s and 1h", ErrInvalidData)
	}
	roster, err := p.readRoster(ctx, target)
	if err != nil {
		return CPUHistory{}, err
	}
	evaluatedAt = p.now().Truncate(time.Millisecond)
	selector := cpuSelector(target.Namespace, roster)
	queryStarted = time.Now()
	rates, err := p.query(ctx, "rate("+selector+"[1m])", evaluatedAt)
	queryFinished = time.Now()
	if err != nil {
		return CPUHistory{}, err
	}
	sources, err := p.query(ctx, "timestamp("+selector+")", evaluatedAt)
	queryFinished = time.Now()
	if err != nil {
		return CPUHistory{}, err
	}
	value, sourceAt, err := p.utilization(roster, rates, sources, evaluatedAt)
	if err != nil {
		return CPUHistory{}, err
	}
	after, err := p.readRoster(ctx, target)
	if err != nil {
		return CPUHistory{}, fmt.Errorf("%w: post-query roster: %v", ErrTargetChanged, err)
	}
	if !reflect.DeepEqual(roster, after) {
		return CPUHistory{}, ErrTargetChanged
	}
	if p.now().Sub(sourceAt) > p.maxSampleAge() {
		return CPUHistory{}, ErrStaleData
	}
	return p.record(target, window, predictor.Sample{Timestamp: evaluatedAt, Value: value}, sourceAt), nil
}

func (p *PrometheusProvider) queryGate(key types.NamespacedName) chan struct{} {
	hash := fnv.New64a()
	_, _ = hash.Write([]byte(key.String()))
	return p.gates[hash.Sum64()%uint64(len(p.gates))]
}

func cpuSelector(namespace string, roster targetRoster) string {
	names := make(map[string]struct{})
	for key := range roster.containers {
		names[key.pod] = struct{}{}
	}
	patterns := make([]string, 0, len(names))
	for name := range names {
		patterns = append(patterns, regexp.QuoteMeta(name))
	}
	slices.Sort(patterns)
	return fmt.Sprintf(`container_cpu_usage_seconds_total{namespace=%q,pod=~%q,container!="",container!="POD"}`, namespace, strings.Join(patterns, "|"))
}

func (p *PrometheusProvider) now() time.Time {
	if p.Clock != nil {
		return p.Clock.Now()
	}
	return time.Now()
}

func (p *PrometheusProvider) query(ctx context.Context, expression string, at time.Time) (model.Vector, error) {
	result, warnings, err := p.api.Query(ctx, expression, at)
	if err != nil {
		return nil, fmt.Errorf("metricsprovider: instant query: %w", err)
	}
	if len(warnings) != 0 {
		return nil, fmt.Errorf("%w: Prometheus warnings: %v", ErrIncompleteData, warnings)
	}
	vector, ok := result.(model.Vector)
	if !ok {
		return nil, fmt.Errorf("%w: expected Prometheus vector, got %T", ErrInvalidData, result)
	}
	return vector, nil
}

func (p *PrometheusProvider) record(target *appsv1.Deployment, window time.Duration, sample predictor.Sample, sourceAt time.Time) CPUHistory {
	p.mu.Lock()
	defer p.mu.Unlock()
	key := client.ObjectKeyFromObject(target)
	stored := p.history[key]
	if stored == nil || stored.uid != target.UID {
		if stored == nil && len(p.history) >= maxTargets {
			p.evictOldest()
		}
		stored = &targetHistory{uid: target.UID}
		p.history[key] = stored
	}
	stored.lastUsed = sample.Timestamp
	cutoff := sample.Timestamp.Add(-window)
	retained := make([]predictor.Sample, 0, len(stored.samples)+1)
	for _, previous := range stored.samples {
		if !previous.Timestamp.Before(cutoff) && previous.Timestamp.Before(sample.Timestamp) {
			retained = append(retained, previous)
		}
	}
	stored.samples = retained
	if len(retained) == 0 || sample.Timestamp.Sub(retained[len(retained)-1].Timestamp) >= observationSpacing {
		stored.samples = append(stored.samples, sample)
		return CPUHistory{Samples: append([]predictor.Sample(nil), stored.samples...), ObservedAt: sample.Timestamp, SourceTimestamp: sourceAt}
	}
	// Keep the last anchor internally so frequent events cannot postpone warmup.
	// Return the live observation in its place, using its actual evaluation time.
	result := append([]predictor.Sample(nil), retained[:len(retained)-1]...)
	result = append(result, sample)
	return CPUHistory{Samples: result, ObservedAt: sample.Timestamp, SourceTimestamp: sourceAt}
}

func (p *PrometheusProvider) evictOldest() {
	var oldestKey types.NamespacedName
	var oldest time.Time
	for key, stored := range p.history {
		if oldest.IsZero() || stored.lastUsed.Before(oldest) {
			oldestKey, oldest = key, stored.lastUsed
		}
	}
	delete(p.history, oldestKey)
}

func (p *PrometheusProvider) discard(key types.NamespacedName) {
	p.mu.Lock()
	defer p.mu.Unlock()
	delete(p.history, key)
}

func (p *PrometheusProvider) logObservation(
	ctx context.Context, started, queryStarted, queryFinished, evaluatedAt time.Time, history CPUHistory, err error,
) {
	errorMessage := ""
	if err != nil {
		errorMessage = err.Error()
	}
	finished := time.Now()
	var queryDuration float64
	if !queryStarted.IsZero() && !queryFinished.IsZero() {
		queryDuration = queryFinished.Sub(queryStarted).Seconds()
	}
	logf.FromContext(ctx).Info("Queried CPU utilization",
		"observationStartedAt", started.UTC().Format(time.RFC3339Nano), "observationFinishedAt", finished.UTC().Format(time.RFC3339Nano),
		"queryStartedAt", optionalTimestamp(queryStarted), "queryFinishedAt", optionalTimestamp(queryFinished),
		"queryDurationSeconds", queryDuration, "queryInstantAt", optionalTimestamp(evaluatedAt),
		"latestEvaluationAt", optionalTimestamp(history.ObservedAt), "sourceTimestamp", optionalTimestamp(history.SourceTimestamp), "samples", len(history.Samples),
		"cpuRateWindowSeconds", 60, "observationSpacingSeconds", observationSpacing.Seconds(), "queryError", errorMessage)
}

func optionalTimestamp(value time.Time) any {
	if value.IsZero() {
		return nil
	}
	return value.UTC().Format(time.RFC3339Nano)
}
