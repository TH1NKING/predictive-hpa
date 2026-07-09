/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

	http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/
package controller

import (
	"math"
	"testing"
)

func TestComputeDesiredReplicas(t *testing.T) {
	tests := []struct {
		name         string
		curr         int32
		predictedCPU float64
		targetCPU    int32
		minR         int32
		maxR         int32
		want         int32
	}{
		{
			name: "ScaleUp",
			curr: 2, predictedCPU: 80, targetCPU: 50, minR: 1, maxR: 10,
			want: 4,
		},
		{
			name: "ScaleDown",
			curr: 5, predictedCPU: 20, targetCPU: 50, minR: 1, maxR: 10,
			want: 2,
		},
		{
			name: "PureFormula_NoToleranceCheck",
			curr: 5, predictedCPU: 53, targetCPU: 50, minR: 1, maxR: 10,
			want: 6,
		},
		{
			name: "ToleranceBoundary_RatioExactly1_1",
			curr: 5, predictedCPU: 55, targetCPU: 50, minR: 1, maxR: 10,
			want: 6,
		},
		{
			name: "NegativePredicted_ClampedToZero",
			curr: 5, predictedCPU: -10, targetCPU: 50, minR: 1, maxR: 10,
			want: 1,
		},
		{
			name: "ExceedsMax_ClampedToMax",
			curr: 8, predictedCPU: 200, targetCPU: 50, minR: 1, maxR: 10,
			want: 10,
		},
		{
			name: "BelowMin_ClampedToMin",
			curr: 5, predictedCPU: 1, targetCPU: 50, minR: 3, maxR: 10,
			want: 3,
		},
		{
			name: "MinReplicasZero_FallsBackToOne",
			curr: 5, predictedCPU: 1, targetCPU: 50, minR: 0, maxR: 10,
			want: 1,
		},
		{
			name: "ColdStartZeroes_ReturnsMin",
			curr: 0, predictedCPU: 0, targetCPU: 50, minR: 1, maxR: 10,
			want: 1,
		},
		{
			name: "CeilRoundsUp",
			curr: 3, predictedCPU: 70, targetCPU: 50, minR: 1, maxR: 10,
			want: 5,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := computeDesiredReplicas(tt.curr, tt.predictedCPU, tt.targetCPU, tt.minR, tt.maxR)
			if got != tt.want {
				t.Errorf("computeDesiredReplicas(curr=%d, pred=%.1f, target=%d, min=%d, max=%d) = %d, want %d",
					tt.curr, tt.predictedCPU, tt.targetCPU, tt.minR, tt.maxR, got, tt.want)
			}
		})
	}
}

func TestWithinTolerance(t *testing.T) {
	tests := []struct {
		name         string
		predictedCPU float64
		targetCPU    int32
		want         bool
	}{
		{name: "ExactMatch", predictedCPU: 50, targetCPU: 50, want: true},
		{name: "JustWithinPositive", predictedCPU: 54, targetCPU: 50, want: true},
		{name: "JustWithinNegative", predictedCPU: 46, targetCPU: 50, want: true},
		{name: "BoundaryExactly10Percent_Excluded", predictedCPU: 55, targetCPU: 50, want: false},
		{name: "OutsidePositive", predictedCPU: 80, targetCPU: 50, want: false},
		{name: "OutsideNegative", predictedCPU: 20, targetCPU: 50, want: false},
		{name: "NegativePredictedClampedToZero_OutOfTolerance", predictedCPU: -10, targetCPU: 50, want: false},
		{name: "ZeroPredicted_OutOfTolerance", predictedCPU: 0, targetCPU: 50, want: false},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := withinTolerance(tt.predictedCPU, tt.targetCPU)
			if got != tt.want {
				t.Errorf("withinTolerance(pred=%.1f, target=%d) = %v, want %v",
					tt.predictedCPU, tt.targetCPU, got, tt.want)
			}
		})
	}
}

func TestCapPrediction(t *testing.T) {
	// maxLeadFactor is 1.3, so the ceiling is currentCPU * 1.3.
	tests := []struct {
		name       string
		predicted  float64
		currentCPU float64
		want       float64
	}{
		{name: "WithinLead_Unchanged", predicted: 55, currentCPU: 50, want: 55},           // 55 <= 65
		{name: "ExceedsLead_CappedToCeiling", predicted: 200, currentCPU: 100, want: 130}, // 100*1.3
		{name: "ExactCeiling_Unchanged", predicted: 65, currentCPU: 50, want: 65},         // 50*1.3, not > ceiling
		{name: "BelowCurrent_Unchanged", predicted: 30, currentCPU: 50, want: 30},
		{name: "Negative_ClampedToZero", predicted: -10, currentCPU: 50, want: 0},
		{name: "ZeroCurrent_CeilingZero", predicted: 20, currentCPU: 0, want: 0},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := capPrediction(tt.predicted, tt.currentCPU)
			if math.Abs(got-tt.want) > 1e-9 {
				t.Errorf("capPrediction(pred=%.1f, current=%.1f) = %v, want %v",
					tt.predicted, tt.currentCPU, got, tt.want)
			}
		})
	}
}
