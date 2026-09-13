package controller

import (
	"errors"
	"fmt"
	"time"

	autoscalingv1alpha1 "github.com/th1nking/predictive-hpa/api/v1alpha1"
)

var errInvalidConfiguration = errors.New("invalid PredictiveHPA configuration")

// Admission does not revalidate stored objects when the schema is upgraded.
// Check decodable legacy configuration before querying metrics or writing Scale.
func validateConfiguration(spec autoscalingv1alpha1.PredictiveHPASpec) error {
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
