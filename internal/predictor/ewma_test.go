package predictor

import (
	"errors"
	"math"
	"testing"
	"time"
)

// makeSeries is a test helper that builds a uniform-interval series.
//
//nolint:unparam // step is fixed at 15s in current tests; kept in signature for series-construction clarity
func makeSeries(t0 time.Time, step time.Duration, values []float64) []Sample {
	out := make([]Sample, len(values))
	for i, v := range values {
		out[i] = Sample{Timestamp: t0.Add(time.Duration(i) * step), Value: v}
	}
	return out
}

// --- Smooth ---

func TestSmooth_ErrorsOnInvalidAlpha(t *testing.T) {
	samples := makeSeries(time.Now(), 15*time.Second, []float64{1, 2, 3})
	for _, a := range []float64{-0.1, 0, 1, 1.5} {
		if _, err := Smooth(samples, a); !errors.Is(err, ErrInvalidAlpha) {
			t.Errorf("alpha=%v: expected ErrInvalidAlpha, got %v", a, err)
		}
	}
}

func TestSmooth_ErrorsOnEmptyInput(t *testing.T) {
	if _, err := Smooth(nil, 0.5); !errors.Is(err, ErrInsufficientData) {
		t.Errorf("expected ErrInsufficientData, got %v", err)
	}
}

func TestSmooth_ConstantSignalProducesConstantOutput(t *testing.T) {
	samples := makeSeries(time.Now(), 15*time.Second, []float64{5, 5, 5, 5, 5})
	got, err := Smooth(samples, 0.3)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	for i, s := range got {
		if math.Abs(s.Value-5) > 1e-9 {
			t.Errorf("index %d: expected 5, got %v", i, s.Value)
		}
	}
}

func TestSmooth_FormulaWithAlpha05(t *testing.T) {
	// With alpha = 0.5 and inputs 2, 4, 6:
	//   S_0 = 2 (cold start)
	//   S_1 = 0.5 * 4 + 0.5 * 2 = 3
	//   S_2 = 0.5 * 6 + 0.5 * 3 = 4.5
	samples := makeSeries(time.Now(), 15*time.Second, []float64{2, 4, 6})
	got, err := Smooth(samples, 0.5)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	expected := []float64{2, 3, 4.5}
	for i, want := range expected {
		if math.Abs(got[i].Value-want) > 1e-9 {
			t.Errorf("index %d: expected %v, got %v", i, want, got[i].Value)
		}
	}
}

// --- Predict ---

func TestPredict_ErrorsOnInsufficientData(t *testing.T) {
	samples := makeSeries(time.Now(), 15*time.Second, []float64{1})
	_, err := Predict(samples, EWMAConfig{Alpha: 0.5, Horizon: 30 * time.Second})
	if !errors.Is(err, ErrInsufficientData) {
		t.Errorf("expected ErrInsufficientData, got %v", err)
	}
}

func TestPredict_ConstantSignalPredictsSameConstant(t *testing.T) {
	samples := makeSeries(time.Now(), 15*time.Second, []float64{7, 7, 7, 7, 7})
	got, err := Predict(samples, EWMAConfig{Alpha: 0.3, Horizon: 30 * time.Second})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if math.Abs(got-7) > 1e-9 {
		t.Errorf("expected 7, got %v", got)
	}
}

func TestPredict_LinearRampPredictsForward(t *testing.T) {
	// Inputs: 0, 1, 2, 3, 4, 5 at 15s intervals; alpha = 0.5; horizon = 30s.
	// Smoothed (cold start S_0 = 0):
	//   S = [0, 0.5, 1.25, 2.125, 3.0625, 4.03125]
	// step = 15s, k = horizon/step = 2
	// slopePerSample = (S_5 - S_3) / 2 = (4.03125 - 2.125) / 2 = 0.953125
	// stepsAhead = 30s / 15s = 2
	// predicted = 4.03125 + 0.953125 * 2 = 5.9375
	samples := makeSeries(time.Now(), 15*time.Second, []float64{0, 1, 2, 3, 4, 5})
	got, err := Predict(samples, EWMAConfig{Alpha: 0.5, Horizon: 30 * time.Second})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	want := 5.9375
	if math.Abs(got-want) > 1e-9 {
		t.Errorf("expected %v, got %v", want, got)
	}
	// Sanity: prediction must be above the last observed value (signal trending up).
	if got <= 5 {
		t.Errorf("expected prediction > 5 (last observed), got %v", got)
	}
}

func TestPredict_DecreasingSignalPredictsDownward(t *testing.T) {
	samples := makeSeries(time.Now(), 15*time.Second, []float64{10, 8, 6, 4, 2})
	got, err := Predict(samples, EWMAConfig{Alpha: 0.5, Horizon: 30 * time.Second})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got >= 2 {
		t.Errorf("expected prediction below 2 (last observed), got %v", got)
	}
}

func TestPredict_IdenticalTimestampsReturnsError(t *testing.T) {
	now := time.Now()
	samples := []Sample{
		{Timestamp: now, Value: 1},
		{Timestamp: now, Value: 2},
	}
	_, err := Predict(samples, EWMAConfig{Alpha: 0.5, Horizon: 30 * time.Second})
	if !errors.Is(err, ErrInvalidInterval) {
		t.Errorf("expected ErrInvalidInterval, got %v", err)
	}
}
