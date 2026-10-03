package controller

import (
	"errors"
	"fmt"
	"time"

	"k8s.io/apimachinery/pkg/util/validation"

	autoscalingv1alpha1 "github.com/th1nking/predictive-hpa/api/v1alpha1"
)

var errInvalidConfiguration = errors.New("invalid PredictiveHPA configuration")

// Admission does not revalidate stored objects when the schema is upgraded.
// Check decodable legacy configuration before querying metrics or writing Scale.
func validateConfiguration(spec autoscalingv1alpha1.PredictiveHPASpec) error {
	if spec.ScaleTargetRef.APIVersion != "apps/v1" || spec.ScaleTargetRef.Kind != "Deployment" {
		return fmt.Errorf("%w: scaleTargetRef must identify an apps/v1 Deployment", errInvalidConfiguration)
	}
	if problems := validation.IsDNS1123Subdomain(spec.ScaleTargetRef.Name); len(problems) != 0 {
		return fmt.Errorf("%w: scaleTargetRef.name must be a valid Deployment name: %v", errInvalidConfiguration, problems)
	}
	if spec.TargetCPUUtilizationPercentage < 1 || spec.TargetCPUUtilizationPercentage > 100 {
		return fmt.Errorf("%w: targetCPUUtilizationPercentage must be between 1 and 100", errInvalidConfiguration)
	}
	if spec.Prediction.AlphaPercent < 1 || spec.Prediction.AlphaPercent > 99 {
		return fmt.Errorf("%w: prediction.alphaPercent must be between 1 and 99", errInvalidConfiguration)
	}
	// Preserve API defaults when they are absent on a decodable legacy object.
	// Both the empty algorithm and mode already execute the default EWMA policy.
	if spec.Prediction.Algorithm != "" && spec.Prediction.Algorithm != autoscalingv1alpha1.PredictionAlgorithmEWMA {
		return fmt.Errorf("%w: prediction.algorithm must be EWMA", errInvalidConfiguration)
	}
	switch spec.DecisionMode {
	case "", autoscalingv1alpha1.DecisionModePredictive, autoscalingv1alpha1.DecisionModeCurrent, autoscalingv1alpha1.DecisionModeHybrid:
	default:
		return fmt.Errorf("%w: decisionMode must be Predictive, Current or Hybrid", errInvalidConfiguration)
	}
	if spec.ScaleDownStabilizationWindowSeconds != nil && *spec.ScaleDownStabilizationWindowSeconds < 0 {
		return fmt.Errorf("%w: scaleDownStabilizationWindowSeconds must not be negative", errInvalidConfiguration)
	}
	if window := spec.Prediction.Window.Duration; window < 15*time.Second || window > time.Hour {
		return fmt.Errorf("%w: prediction.window must be between 15s and 1h", errInvalidConfiguration)
	}
	if horizon := spec.Prediction.Horizon.Duration; horizon <= 0 || horizon > time.Hour {
		return fmt.Errorf("%w: prediction.horizon must be greater than 0s and at most 1h", errInvalidConfiguration)
	}
	if spec.MinReplicas != nil && (*spec.MinReplicas < 0 || *spec.MinReplicas > spec.MaxReplicas) || spec.MaxReplicas < 1 {
		return fmt.Errorf("%w: require 0 <= minReplicas <= maxReplicas and maxReplicas >= 1", errInvalidConfiguration)
	}
	return nil
}
