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

package v1alpha1

import (
	autoscalingv2 "k8s.io/api/autoscaling/v2"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// PredictionAlgorithm defines the supported time-series prediction algorithms.
// +kubebuilder:validation:Enum=EWMA
type PredictionAlgorithm string

const (
	// PredictionAlgorithmEWMA uses Exponential Weighted Moving Average to predict
	// future metric values from historical observations.
	PredictionAlgorithmEWMA PredictionAlgorithm = "EWMA"
)

// DecisionMode selects the CPU signal used by the scaling policy.
// +kubebuilder:validation:Enum=Predictive;Current;Hybrid
type DecisionMode string

const (
	// DecisionModePredictive uses the bounded forecast for both directions.
	DecisionModePredictive DecisionMode = "Predictive"
	// DecisionModeCurrent uses the latest CPU observation for both directions.
	DecisionModeCurrent DecisionMode = "Current"
	// DecisionModeHybrid expands on current CPU and lets the forecast retain
	// replicas on falling demand, without initiating expansion by itself.
	DecisionModeHybrid DecisionMode = "Hybrid"
)

// PredictionConfig configures the time-series prediction behavior.
type PredictionConfig struct {
	// algorithm selects the prediction algorithm. Currently only "EWMA" is supported.
	// +kubebuilder:default=EWMA
	// +required
	Algorithm PredictionAlgorithm `json:"algorithm"`

	// alphaPercent is the EWMA smoothing factor scaled by 100.
	// A higher value gives more weight to recent observations.
	// For example, 30 means alpha = 0.3.
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=99
	// +required
	AlphaPercent int32 `json:"alphaPercent"`

	// window is the lookback duration of historical samples used to compute the EWMA.
	// Samples older than this window are dropped.
	// Examples: "5m", "10m".
	// +required
	Window metav1.Duration `json:"window"`

	// horizon is how far into the future the controller predicts metric values
	// to drive the scaling decision.
	// Examples: "30s", "1m".
	// +required
	Horizon metav1.Duration `json:"horizon"`
}

// PredictiveHPASpec defines the desired state of PredictiveHPA.
type PredictiveHPASpec struct {
	// scaleTargetRef points to the target resource to scale (e.g. a Deployment).
	// The reference is resolved within the same namespace as the PredictiveHPA.
	// +required
	ScaleTargetRef autoscalingv2.CrossVersionObjectReference `json:"scaleTargetRef"`

	// minReplicas is the lower bound for the number of replicas.
	// Defaults to 1 when unset.
	// +kubebuilder:validation:Minimum=0
	// +optional
	MinReplicas *int32 `json:"minReplicas,omitempty"`

	// maxReplicas is the upper bound for the number of replicas.
	// Must be >= minReplicas.
	// +kubebuilder:validation:Minimum=1
	// +required
	MaxReplicas int32 `json:"maxReplicas"`

	// targetCPUUtilizationPercentage is the desired average CPU utilization
	// across all pods of the scale target, expressed as a percentage of the
	// per-pod CPU request.
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=100
	// +required
	TargetCPUUtilizationPercentage int32 `json:"targetCPUUtilizationPercentage"`

	// prediction configures the time-series prediction used to drive proactive scaling.
	// +required
	Prediction PredictionConfig `json:"prediction"`

	// decisionMode selects the CPU signal for replica decisions. Predictive uses
	// the bounded EWMA forecast; Current uses observed CPU. Hybrid expands on
	// current CPU and uses the more conservative signal for shrinking, with
	// forecast-only expansion disabled. All modes share prediction readiness,
	// replica bounds, tolerance and stabilization. Defaults to Predictive.
	// +kubebuilder:default=Predictive
	// +optional
	DecisionMode DecisionMode `json:"decisionMode,omitempty"`

	// scaleDownStabilizationWindowSeconds is how long the controller waits before
	// applying a scale-down decision, to prevent oscillation. Defaults to 60s,
	// which is more aggressive than the native HPA default (300s) since the
	// EWMA already smooths out short-term spikes.
	// +kubebuilder:validation:Minimum=0
	// +kubebuilder:default=60
	// +optional
	ScaleDownStabilizationWindowSeconds *int32 `json:"scaleDownStabilizationWindowSeconds,omitempty"`
}

// PredictiveHPAStatus defines the observed state of PredictiveHPA.
type PredictiveHPAStatus struct {
	// currentReplicas is the actual number of replicas observed on the scale target.
	// +optional
	CurrentReplicas int32 `json:"currentReplicas"`

	// desiredReplicas is the replica count last computed by the controller.
	// +optional
	DesiredReplicas int32 `json:"desiredReplicas"`

	// currentCPUUtilizationPercentage is the most recently observed average
	// CPU utilization across pods of the scale target.
	// +optional
	CurrentCPUUtilizationPercentage *int32 `json:"currentCPUUtilizationPercentage,omitempty"`

	// predictedCPUUtilizationPercentage is the bounded EWMA forecast at horizon.
	// It is observable in every decision mode, including Current.
	// +optional
	PredictedCPUUtilizationPercentage *int32 `json:"predictedCPUUtilizationPercentage,omitempty"`

	// lastScaleTime is the last time the controller adjusted the replica count.
	// +optional
	LastScaleTime *metav1.Time `json:"lastScaleTime,omitempty"`

	// conditions represent the current state of the PredictiveHPA resource.
	// +listType=map
	// +listMapKey=type
	// +optional
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:shortName=phpa
// +kubebuilder:printcolumn:name="Reference",type=string,JSONPath=`.spec.scaleTargetRef.name`
// +kubebuilder:printcolumn:name="MinPods",type=integer,JSONPath=`.spec.minReplicas`
// +kubebuilder:printcolumn:name="MaxPods",type=integer,JSONPath=`.spec.maxReplicas`
// +kubebuilder:printcolumn:name="Replicas",type=integer,JSONPath=`.status.currentReplicas`
// +kubebuilder:printcolumn:name="Current%",type=integer,JSONPath=`.status.currentCPUUtilizationPercentage`
// +kubebuilder:printcolumn:name="Predicted%",type=integer,JSONPath=`.status.predictedCPUUtilizationPercentage`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`

// PredictiveHPA is the Schema for the predictivehpas API.
type PredictiveHPA struct {
	metav1.TypeMeta `json:",inline"`

	// metadata is a standard object metadata
	// +optional
	metav1.ObjectMeta `json:"metadata,omitzero"`

	// spec defines the desired state of PredictiveHPA
	// +required
	Spec PredictiveHPASpec `json:"spec"`

	// status defines the observed state of PredictiveHPA
	// +optional
	Status PredictiveHPAStatus `json:"status,omitzero"`
}

// +kubebuilder:object:root=true

// PredictiveHPAList contains a list of PredictiveHPA.
type PredictiveHPAList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitzero"`
	Items           []PredictiveHPA `json:"items"`
}

func init() {
	SchemeBuilder.Register(&PredictiveHPA{}, &PredictiveHPAList{})
}
