package controller

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	autoscalingv1alpha1 "github.com/th1nking/predictive-hpa/api/v1alpha1"
	"github.com/th1nking/predictive-hpa/internal/metricsprovider"
	"github.com/th1nking/predictive-hpa/internal/predictor"
)

// ErrReplayInvalidInput identifies malformed or incomplete evidence, distinct
// from a valid recording whose actual-mode policy outcome does not reproduce.
var ErrReplayInvalidInput = errors.New("invalid replay input")

const (
	replayComputedHistory  = "computed-history"
	replayRecordedForecast = "recorded-forecast"
)

type replayInput struct {
	SchemaVersion             int           `json:"schemaVersion"`
	PolicyHistoryCompleteness string        `json:"policyHistoryCompleteness"`
	Config                    replayConfig  `json:"config"`
	Cycles                    []replayCycle `json:"cycles"`
}

type replayConfig struct {
	MinReplicas          *int32 `json:"minReplicas"`
	MaxReplicas          int32  `json:"maxReplicas"`
	TargetCPU            int32  `json:"targetCPU"`
	AlphaPercent         int32  `json:"alphaPercent"`
	WindowSeconds        int32  `json:"windowSeconds"`
	HorizonSeconds       int32  `json:"horizonSeconds"`
	StabilizationSeconds *int32 `json:"stabilizationSeconds"`
}

type replayCycle struct {
	At                time.Time                        `json:"at"`
	PredictionSource  string                           `json:"predictionSource"`
	ObservedReplicas  *int32                           `json:"observedReplicas"`
	RequestedReplicas *int32                           `json:"requestedReplicas"`
	CurrentCPU        *float64                         `json:"currentCPU"`
	RawPrediction     *float64                         `json:"rawPrediction,omitempty"`
	Samples           []replaySample                   `json:"samples,omitempty"`
	ActualMode        autoscalingv1alpha1.DecisionMode `json:"actualMode"`
	Expected          replayExpected                   `json:"expected"`
}

type replaySample struct {
	Timestamp time.Time `json:"timestamp"`
	Value     *float64  `json:"value"`
}

type replayExpected struct {
	DecisionCPU         *float64   `json:"decisionCPU"`
	BoundedPrediction   *float64   `json:"boundedPrediction"`
	DesiredReplicas     *int32     `json:"desiredReplicas"`
	FinalDesired        *int32     `json:"finalDesired"`
	SkipReason          *string    `json:"skipReason"`
	Stabilized          *bool      `json:"stabilized"`
	RawPrediction       *float64   `json:"rawPrediction,omitempty"`
	ColdStartProtection *bool      `json:"coldStartProtection,omitempty"`
	ProtectedUntil      *time.Time `json:"protectedUntil,omitempty"`
	HistoryEntries      *int       `json:"historyEntries,omitempty"`
	HistoryOldestAt     *time.Time `json:"historyOldestAt,omitempty"`
}

// ReplayModeResult records the same policy fields emitted by the controller.
type ReplayModeResult struct {
	DecisionCPU         float64   `json:"decisionCPU"`
	BoundedPrediction   float64   `json:"boundedPrediction"`
	FormulaReplicas     int32     `json:"formulaReplicas"`
	DirectionClamped    bool      `json:"directionClamped"`
	WithinTolerance     bool      `json:"withinTolerance"`
	DesiredReplicas     int32     `json:"desiredReplicas"`
	FinalDesired        int32     `json:"finalDesired"`
	SkipReason          string    `json:"skipReason"`
	Stabilized          bool      `json:"stabilized"`
	ColdStartProtection bool      `json:"coldStartProtection"`
	ProtectedUntil      time.Time `json:"protectedUntil"`
	HistoryEntries      int       `json:"historyEntries"`
	HistoryOldestAt     time.Time `json:"historyOldestAt"`
}

// ReplayCycleResult compares modes while retaining the supplied actual state.
type ReplayCycleResult struct {
	At               time.Time                                             `json:"at"`
	PredictionSource string                                                `json:"predictionSource"`
	ActualMode       autoscalingv1alpha1.DecisionMode                      `json:"actualMode"`
	RawPrediction    float64                                               `json:"rawPrediction"`
	Modes            map[autoscalingv1alpha1.DecisionMode]ReplayModeResult `json:"modes"`
}

// ReplayReport is an offline policy comparison, never a simulated workload or
// an estimate of service latency, success rate, or resource savings.
type ReplayReport struct {
	SchemaVersion int                 `json:"schemaVersion"`
	Verified      bool                `json:"verified"`
	ReplicaInput  string              `json:"replicaInput"`
	Cycles        []ReplayCycleResult `json:"cycles"`
}

// ReplayDecisions evaluates isolated mode histories through production policy.
// It never constructs a Kubernetes client or writes a Scale resource.
func ReplayDecisions(input io.Reader) (*ReplayReport, error) {
	var recording replayInput
	decoder := json.NewDecoder(input)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&recording); err != nil {
		return nil, fmt.Errorf("%w: %v", ErrReplayInvalidInput, err)
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return nil, fmt.Errorf("%w: expected exactly one JSON document", ErrReplayInvalidInput)
	}
	if err := validateReplayInput(recording); err != nil {
		return nil, fmt.Errorf("%w: %v", ErrReplayInvalidInput, err)
	}
	report := &ReplayReport{SchemaVersion: 1, Verified: true, ReplicaInput: "fixed-observed-and-requested"}
	reconciler := &PredictiveHPAReconciler{}
	for i, cycle := range recording.Cycles {
		raw, err := replayPrediction(recording.Config, cycle)
		if err != nil {
			return nil, fmt.Errorf("cycle %d: %w", i+1, err)
		}
		result := ReplayCycleResult{At: cycle.At, PredictionSource: cycle.PredictionSource,
			ActualMode: cycle.ActualMode, RawPrediction: raw,
			Modes: make(map[autoscalingv1alpha1.DecisionMode]ReplayModeResult)}
		for _, mode := range []autoscalingv1alpha1.DecisionMode{
			autoscalingv1alpha1.DecisionModeCurrent,
			autoscalingv1alpha1.DecisionModePredictive,
			autoscalingv1alpha1.DecisionModeHybrid,
		} {
			result.Modes[mode] = replayMode(reconciler, recording.Config, cycle, raw, mode)
		}
		if err := verifyReplayMode(result.Modes[cycle.ActualMode], cycle.Expected); err != nil {
			return nil, fmt.Errorf("cycle %d at %s actual mode %s: %w", i+1, cycle.At.Format(time.RFC3339Nano), cycle.ActualMode, err)
		}
		report.Cycles = append(report.Cycles, result)
	}
	return report, nil
}

func validateReplayInput(input replayInput) error {
	if input.SchemaVersion != 1 || input.PolicyHistoryCompleteness != "complete" || len(input.Cycles) == 0 {
		return errors.New("schemaVersion 1, complete policy history from its creation, and at least one cycle are required")
	}
	if err := validateReplayConfig(input.Config); err != nil {
		return err
	}
	for i, cycle := range input.Cycles {
		if cycle.At.IsZero() || (i > 0 && !cycle.At.After(input.Cycles[i-1].At)) {
			return fmt.Errorf("cycle %d: nonzero strictly increasing decision timestamps are required", i+1)
		}
		if cycle.ActualMode != input.Cycles[0].ActualMode {
			return fmt.Errorf("cycle %d: one recording must retain the same actualMode and policy history identity", i+1)
		}
		if err := validateReplayCycle(cycle); err != nil {
			return fmt.Errorf("cycle %d: %w", i+1, err)
		}
	}
	return nil
}

func validateReplayConfig(config replayConfig) error {
	if config.MinReplicas == nil || *config.MinReplicas < 0 || config.MaxReplicas < 1 ||
		*config.MinReplicas > config.MaxReplicas || config.TargetCPU < 1 || config.TargetCPU > 100 {
		return errors.New("explicit valid minReplicas, maxReplicas and targetCPU are required")
	}
	if config.AlphaPercent < 1 || config.AlphaPercent > 99 || config.WindowSeconds < 15 || config.WindowSeconds > 3600 ||
		config.HorizonSeconds <= 0 || config.StabilizationSeconds == nil || *config.StabilizationSeconds < 0 {
		return errors.New("invalid or missing prediction/stabilization configuration")
	}
	return nil
}

func validateReplayCycle(cycle replayCycle) error {
	if cycle.ObservedReplicas == nil || cycle.RequestedReplicas == nil || cycle.CurrentCPU == nil ||
		*cycle.ObservedReplicas < 0 || *cycle.RequestedReplicas < 0 || *cycle.CurrentCPU < 0 {
		return errors.New("explicit nonnegative observedReplicas, requestedReplicas and currentCPU are required")
	}
	switch cycle.ActualMode {
	case autoscalingv1alpha1.DecisionModeCurrent, autoscalingv1alpha1.DecisionModePredictive, autoscalingv1alpha1.DecisionModeHybrid:
	default:
		return errors.New("actualMode must be Current, Predictive or Hybrid")
	}
	if cycle.PredictionSource != replayComputedHistory && cycle.PredictionSource != replayRecordedForecast {
		return errors.New("predictionSource must be computed-history or recorded-forecast")
	}
	expected := cycle.Expected
	if expected.DecisionCPU == nil || expected.BoundedPrediction == nil || expected.DesiredReplicas == nil ||
		expected.FinalDesired == nil || expected.SkipReason == nil || expected.Stabilized == nil {
		return errors.New("expected requires decisionCPU, boundedPrediction, desiredReplicas, finalDesired, skipReason and stabilized")
	}
	return nil
}

func replayPrediction(config replayConfig, cycle replayCycle) (float64, error) {
	if cycle.PredictionSource == replayRecordedForecast {
		if cycle.RawPrediction == nil || cycle.Samples != nil {
			return 0, fmt.Errorf("%w: recorded-forecast requires rawPrediction and no samples", ErrReplayInvalidInput)
		}
		return *cycle.RawPrediction, replayFloatMismatch("expected.rawPrediction", *cycle.RawPrediction, cycle.Expected.RawPrediction)
	}
	if err := validateReplaySamples(config, cycle); err != nil {
		return 0, fmt.Errorf("%w: %v", ErrReplayInvalidInput, err)
	}
	samples := make([]predictor.Sample, len(cycle.Samples))
	for i, sample := range cycle.Samples {
		samples[i] = predictor.Sample{Timestamp: sample.Timestamp, Value: *sample.Value}
	}
	raw, err := predictor.Predict(samples, predictor.EWMAConfig{
		Alpha:   float64(config.AlphaPercent) / 100,
		Horizon: time.Duration(config.HorizonSeconds) * time.Second,
	})
	if err != nil {
		return 0, fmt.Errorf("%w: prediction: %v", ErrReplayInvalidInput, err)
	}
	if math.IsNaN(raw) || math.IsInf(raw, 0) {
		return 0, fmt.Errorf("%w: nonfinite computed forecast", ErrReplayInvalidInput)
	}
	if err := replayFloatMismatch("rawPrediction", raw, cycle.RawPrediction); err != nil {
		return 0, err
	}
	if err := replayFloatMismatch("expected.rawPrediction", raw, cycle.Expected.RawPrediction); err != nil {
		return 0, err
	}
	return raw, nil
}

// Samples are an asserted verified observation series, not raw Prometheus
// counters. The replay validates its shape; target identity and source freshness
// must already have been established by the recording/collection boundary.
func validateReplaySamples(config replayConfig, cycle replayCycle) error {
	if len(cycle.Samples) < 2 {
		return errors.New("computed-history requires at least two complete observations")
	}
	latest := cycle.Samples[len(cycle.Samples)-1]
	if latest.Timestamp.After(cycle.At) || cycle.At.Sub(latest.Timestamp) > metricsprovider.DefaultMaxSampleAge {
		return errors.New("latest observation must not be future or older than the controller observation age limit")
	}
	for i, sample := range cycle.Samples {
		if sample.Timestamp.IsZero() || sample.Value == nil || *sample.Value < 0 ||
			sample.Timestamp.Before(latest.Timestamp.Add(-time.Duration(config.WindowSeconds)*time.Second)) {
			return errors.New("each observation requires a timestamp and nonnegative value within the history window")
		}
		if i > 0 && sample.Timestamp.Sub(cycle.Samples[i-1].Timestamp) < 15*time.Second {
			return errors.New("observations must be ordered and at least 15 seconds apart")
		}
	}
	if !replayFloatEqual(*latest.Value, *cycle.CurrentCPU) {
		return errors.New("currentCPU must match the complete observation history tail")
	}
	return nil
}

func verifyReplayMode(actual ReplayModeResult, expected replayExpected) error {
	return errors.Join(
		replayFloatMismatch("decisionCPU", actual.DecisionCPU, expected.DecisionCPU),
		replayFloatMismatch("boundedPrediction", actual.BoundedPrediction, expected.BoundedPrediction),
		replayMismatch("desiredReplicas", actual.DesiredReplicas, expected.DesiredReplicas),
		replayMismatch("finalDesired", actual.FinalDesired, expected.FinalDesired),
		replayMismatch("skipReason", actual.SkipReason, expected.SkipReason),
		replayMismatch("stabilized", actual.Stabilized, expected.Stabilized),
		replayMismatch("coldStartProtection", actual.ColdStartProtection, expected.ColdStartProtection),
		replayMismatch("historyEntries", actual.HistoryEntries, expected.HistoryEntries),
		replayTimeMismatch("protectedUntil", actual.ProtectedUntil, expected.ProtectedUntil),
		replayTimeMismatch("historyOldestAt", actual.HistoryOldestAt, expected.HistoryOldestAt),
	)
}

func replayMismatch[T comparable](field string, got T, want *T) error {
	if want != nil && got != *want {
		return fmt.Errorf("mismatch %s: got %v, expected %v", field, got, *want)
	}
	return nil
}

func replayFloatMismatch(field string, got float64, want *float64) error {
	if want != nil && !replayFloatEqual(got, *want) {
		return fmt.Errorf("mismatch %s: got %v, expected %v", field, got, *want)
	}
	return nil
}

func replayTimeMismatch(field string, got time.Time, want *time.Time) error {
	if want != nil && !got.Equal(*want) {
		return fmt.Errorf("mismatch %s: got %v, expected %v", field, got, *want)
	}
	return nil
}

func replayFloatEqual(got, want float64) bool {
	return math.Abs(got-want) <= 1e-9*max(1, math.Abs(want))
}

func replayMode(r *PredictiveHPAReconciler, config replayConfig, cycle replayCycle, raw float64,
	mode autoscalingv1alpha1.DecisionMode,
) ReplayModeResult {
	bounded := capPrediction(raw, *cycle.CurrentCPU)
	_, signal := selectDecisionSignal(mode, *cycle.CurrentCPU, bounded, config.TargetCPU)
	desired := directedRecommendation(*cycle.ObservedReplicas, *cycle.RequestedReplicas, signal,
		config.TargetCPU, *config.MinReplicas, config.MaxReplicas)
	phpa := &autoscalingv1alpha1.PredictiveHPA{
		ObjectMeta: metav1.ObjectMeta{Name: string(mode), Namespace: "offline-replay"},
		Spec: autoscalingv1alpha1.PredictiveHPASpec{
			MaxReplicas: config.MaxReplicas, ScaleDownStabilizationWindowSeconds: config.StabilizationSeconds,
		},
	}
	deploy := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: "offline-target"}}
	stabilization := r.stabilizeRecommendation(phpa, deploy, cycle.At, desired, *cycle.RequestedReplicas, *config.MinReplicas)
	formula := computeDesiredReplicas(*cycle.ObservedReplicas, signal, config.TargetCPU, *config.MinReplicas, config.MaxReplicas)
	return ReplayModeResult{
		DecisionCPU: signal, BoundedPrediction: bounded, DesiredReplicas: desired,
		FormulaReplicas: formula, DirectionClamped: desired != formula, WithinTolerance: withinTolerance(signal, config.TargetCPU),
		FinalDesired: stabilization.finalDesired, Stabilized: stabilization.stabilized,
		SkipReason: scalingSkipReason(stabilization.finalDesired, *cycle.RequestedReplicas, signal,
			config.TargetCPU, *config.MinReplicas, config.MaxReplicas),
		ColdStartProtection: stabilization.coldStart, ProtectedUntil: stabilization.protectedUntil,
		HistoryEntries: stabilization.historyEntries, HistoryOldestAt: stabilization.historyOldest,
	}
}
