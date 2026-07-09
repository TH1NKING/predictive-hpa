// Package predictor provides time-series prediction primitives for the
// PredictiveHPA controller. The current implementation supports EWMA
// smoothing combined with first-difference linear extrapolation.
package predictor

import (
	"errors"
	"math"
	"time"
)

// Sample is a single time-stamped observation drawn from a monitoring
// system (typically Prometheus). A slice of Sample passed to Predict is
// assumed to be sorted by Timestamp ascending and to have approximately
// uniform inter-sample spacing.
type Sample struct {
	Timestamp time.Time
	Value     float64
}

// EWMAConfig holds the prediction parameters for the EWMA-based predictor.
type EWMAConfig struct {
	// Alpha is the smoothing factor of the EWMA. Must satisfy 0 < Alpha < 1.
	// Lower values produce more aggressive smoothing (and more lag).
	Alpha float64

	// Horizon is how far ahead in time the prediction projects.
	Horizon time.Duration
}

var (
	// ErrInsufficientData is returned when there are not enough samples
	// to produce a meaningful result. Predict requires at least 2 samples;
	// Smooth requires at least 1.
	ErrInsufficientData = errors.New("predictor: insufficient data")

	// ErrInvalidAlpha is returned when Alpha is not strictly in (0, 1).
	ErrInvalidAlpha = errors.New("predictor: alpha must be in (0, 1)")

	// ErrInvalidInterval is returned when the inferred sample interval is
	// non-positive (e.g. all samples share identical timestamps).
	ErrInvalidInterval = errors.New("predictor: non-positive sample interval")
)

// trendDampingFactor (phi in Holt's damped-trend method) attenuates the
// first-difference trend when projecting over the horizon. With 0 < phi < 1
// the projected trend contribution is the geometric sum Σ phi^i rather than a
// linear slope*steps, so a slope observed during a ramp-up does not project
// unbounded past a plateau. This directly curbs the EWMA forecast's tendency
// to overshoot to ~2x the current value on load onset — the dominant source
// of PredictiveHPA's steady-state over-provisioning. phi -> 1 recovers the
// undamped linear projection; smaller phi damps harder. 0.85 is a moderate
// default: it noticeably reduces overshoot while preserving lead on genuinely
// sustained trends.
const trendDampingFactor = 0.85

// Smooth applies an Exponentially Weighted Moving Average to the input
// series and returns a smoothed series of the same length.
//
//	S_0 = samples[0].Value
//	S_i = alpha * Y_i + (1 - alpha) * S_{i-1}   for i > 0
//
// S_0 is initialized to the first observation (industry-standard cold
// start; see pandas ewm with adjust=False, Statsmodels SimpleExpSmoothing).
//
// Smooth is exposed separately from Predict so callers can inspect the
// smoothed series directly for diagnostics and visualisation.
func Smooth(samples []Sample, alpha float64) ([]Sample, error) {
	if alpha <= 0 || alpha >= 1 {
		return nil, ErrInvalidAlpha
	}
	if len(samples) == 0 {
		return nil, ErrInsufficientData
	}

	smoothed := make([]Sample, len(samples))
	smoothed[0] = samples[0] // cold start: S_0 = Y_0
	for i := 1; i < len(samples); i++ {
		smoothed[i] = Sample{
			Timestamp: samples[i].Timestamp,
			Value:     alpha*samples[i].Value + (1-alpha)*smoothed[i-1].Value,
		}
	}
	return smoothed, nil
}

// Predict applies EWMA smoothing and extrapolates forward by Horizon
// using the first-difference slope of the smoothed tail.
//
// Algorithm:
//
//  1. S = Smooth(samples, Alpha)
//  2. step = (S[N].Timestamp - S[0].Timestamp) / (N-1)          // average interval
//  3. k = max(1, Horizon/step), clamped to N-1
//  4. slopePerSample = (S[N] - S[N-k]) / k
//  5. stepsAhead = Horizon / step
//  6. dampedSteps = phi*(1 - phi^stepsAhead)/(1 - phi)          // Holt damping
//  7. predicted = S[N] + slopePerSample * dampedSteps
//
// Step 6 applies damped-trend extrapolation (phi = trendDampingFactor): a
// plain linear projection (slopePerSample * stepsAhead) keeps extending a
// ramp-up slope after the signal plateaus, overshooting to ~2x the current
// value on load onset. The damped geometric sum converges; phi -> 1 recovers
// the undamped projection.
//
// Known limitation: Simple EWMA introduces a (1-Alpha)/Alpha lag bias on
// trending signals. The trade-off between lag and noise sensitivity is
// controlled by Alpha and is explored empirically in Phase 3 experiments.
func Predict(samples []Sample, cfg EWMAConfig) (float64, error) {
	if len(samples) < 2 {
		return 0, ErrInsufficientData
	}

	smoothed, err := Smooth(samples, cfg.Alpha)
	if err != nil {
		return 0, err
	}

	n := len(smoothed)
	totalSpan := smoothed[n-1].Timestamp.Sub(smoothed[0].Timestamp)
	if totalSpan <= 0 {
		return 0, ErrInvalidInterval
	}
	step := totalSpan / time.Duration(n-1)

	k := min(max(int(cfg.Horizon/step), 1), n-1)

	last := smoothed[n-1].Value
	prev := smoothed[n-1-k].Value
	slopePerSample := (last - prev) / float64(k)
	stepsAhead := float64(cfg.Horizon) / float64(step)

	// Damped-trend projection: geometric sum Σ_{i=1..stepsAhead} phi^i
	// = phi*(1 - phi^stepsAhead)/(1 - phi). Converges as stepsAhead grows,
	// unlike the linear slopePerSample*stepsAhead which overshoots past a
	// plateau. See trendDampingFactor.
	dampedSteps := trendDampingFactor * (1 - math.Pow(trendDampingFactor, stepsAhead)) / (1 - trendDampingFactor)

	return last + slopePerSample*dampedSteps, nil
}
