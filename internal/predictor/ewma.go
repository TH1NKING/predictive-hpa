// Package predictor provides time-series prediction primitives for the
// PredictiveHPA controller. The current implementation supports EWMA
// smoothing combined with first-difference linear extrapolation.
package predictor

import (
	"errors"
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
//  2. step = (S[N].Timestamp - S[0].Timestamp) / (N)            // average interval
//  3. k = max(1, Horizon/step), clamped to N
//  4. slopePerSample = (S[N] - S[N-k]) / k
//  5. stepsAhead = Horizon / step
//  6. predicted = S[N] + slopePerSample * stepsAhead
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

	k := int(cfg.Horizon / step)
	if k < 1 {
		k = 1
	}
	if k > n-1 {
		k = n - 1
	}

	last := smoothed[n-1].Value
	prev := smoothed[n-1-k].Value
	slopePerSample := (last - prev) / float64(k)
	stepsAhead := float64(cfg.Horizon) / float64(step)

	return last + slopePerSample*stepsAhead, nil
}
