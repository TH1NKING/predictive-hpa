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

import "math"

// tolerance is the relative error allowed between predicted and target CPU
// utilization before a scaling action is taken. Matches the native HPA
// controller's --horizontal-pod-autoscaler-tolerance default of 0.1 (10%).
//
// Two-layer rationale: EWMA smoothing suppresses input-side metric noise;
// tolerance suppresses output-side ceil-rounding jitter (e.g., 10 -> 11
// replicas is itself a 10% step). The two layers target different sources
// of noise and do not duplicate each other.
const tolerance = 0.1

// computeDesiredReplicas returns the next replicas count for a Deployment
// given the predicted CPU utilization and target. The formula matches the
// native HPA controller (kubernetes/pkg/controller/podautoscaler/
// replica_calculator.go::GetResourceReplicas):
//
//	desired = ceil(currentReplicas * predicted / target)
//
// then clamped to [minReplicas, maxReplicas]. Negative predicted values
// (which can arise from the first-difference forecast on a sharply
// descending signal) are clamped to 0 before the formula. minReplicas < 1
// is treated as 1 — v1alpha1 does not support scale-to-zero (see Roadmap).
//
// This function is intentionally stateless; the stabilization window
// (max-over-window of recent desireds) is applied by the caller, not here.
func computeDesiredReplicas(
	currentReplicas int32,
	predictedCPU float64,
	targetCPU int32,
	minReplicas, maxReplicas int32,
) int32 {
	if predictedCPU < 0 {
		predictedCPU = 0
	}

	desiredRaw := int32(math.Ceil(
		float64(currentReplicas) * predictedCPU / float64(targetCPU),
	))

	if minReplicas < 1 {
		minReplicas = 1
	}

	desired := min(max(desiredRaw, minReplicas), maxReplicas)
	return desired
}

// withinTolerance reports whether the predicted CPU utilization is within
// the tolerance band (default ±10%) of the target — in which case the
// caller should skip scaling to avoid jitter on small fluctuations.
//
// The check is performed on the ratio predicted/target rather than on
// desiredReplicas, matching the native HPA semantics: the ratio is what
// the formula scales by, so it is the natural quantity for the dead-zone.
func withinTolerance(predictedCPU float64, targetCPU int32) bool {
	if predictedCPU < 0 {
		predictedCPU = 0
	}
	ratio := predictedCPU / float64(targetCPU)
	return math.Abs(ratio-1.0) < tolerance
}
