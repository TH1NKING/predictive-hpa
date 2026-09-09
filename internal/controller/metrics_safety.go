package controller

import (
	"context"
	"errors"
	"fmt"
	"math"
	"reflect"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	autoscalingv1alpha1 "github.com/th1nking/predictive-hpa/api/v1alpha1"
	"github.com/th1nking/predictive-hpa/internal/metricsprovider"
	"github.com/th1nking/predictive-hpa/internal/predictor"
)

const conditionMetricsReady = "MetricsReady"

// Validate again at the consumer boundary: an alternative Provider must not
// feed NaN, duplicate timestamps, or stale source data into replica arithmetic.
func validateCPUHistory(history metricsprovider.CPUHistory, now time.Time, window time.Duration) error {
	if len(history.Samples) == 0 {
		return metricsprovider.ErrNoData
	}
	if !history.Samples[len(history.Samples)-1].Timestamp.Equal(history.ObservedAt) {
		return fmt.Errorf("%w: observation metadata does not identify the latest CPU sample", metricsprovider.ErrInvalidData)
	}
	if history.ObservedAt.IsZero() || history.SourceTimestamp.IsZero() ||
		history.ObservedAt.After(now) || history.SourceTimestamp.After(history.ObservedAt) {
		return fmt.Errorf("%w: missing or future observation/source timestamp", metricsprovider.ErrInvalidData)
	}
	if now.Sub(history.SourceTimestamp) > metricsprovider.DefaultMaxSampleAge || now.Sub(history.ObservedAt) > metricsprovider.DefaultMaxSampleAge {
		return metricsprovider.ErrStaleData
	}
	for i, sample := range history.Samples {
		if math.IsNaN(sample.Value) || math.IsInf(sample.Value, 0) || sample.Value < 0 || sample.Timestamp.IsZero() ||
			sample.Timestamp.After(history.ObservedAt) || sample.Timestamp.Before(history.ObservedAt.Add(-window)) {
			return fmt.Errorf("%w: unusable CPU sample", metricsprovider.ErrInvalidData)
		}
		if i > 0 && sample.Timestamp.Sub(history.Samples[i-1].Timestamp) < 15*time.Second {
			return fmt.Errorf("%w: observations must be ordered and at least 15s apart", metricsprovider.ErrInvalidData)
		}
	}
	if now.Sub(history.Samples[len(history.Samples)-1].Timestamp) > metricsprovider.DefaultMaxSampleAge {
		return metricsprovider.ErrStaleData
	}
	if len(history.Samples) < 2 {
		return predictor.ErrInsufficientData
	}
	return nil
}

func metricsFailureReason(err error) (string, bool) {
	switch {
	case errors.Is(err, metricsprovider.ErrNoData):
		return "NoData", true
	case errors.Is(err, metricsprovider.ErrStaleData):
		return "StaleData", true
	case errors.Is(err, metricsprovider.ErrInvalidData):
		return "InvalidData", true
	case errors.Is(err, metricsprovider.ErrIncompleteData):
		return "IncompleteData", true
	case errors.Is(err, metricsprovider.ErrTargetChanged):
		return "TargetChanged", true
	case errors.Is(err, predictor.ErrInsufficientData):
		return "InsufficientSamples", true
	default:
		return "QueryFailed", false
	}
}

func (r *PredictiveHPAReconciler) metricsUnavailable(ctx context.Context, phpa *autoscalingv1alpha1.PredictiveHPA,
	err error, interval time.Duration) (ctrl.Result, error) {
	reason, transient := metricsFailureReason(err)
	statusErr := r.patchStatus(ctx, phpa, func(status *autoscalingv1alpha1.PredictiveHPAStatus) {
		status.CurrentCPUUtilizationPercentage = nil
		status.PredictedCPUUtilizationPercentage = nil
		meta.SetStatusCondition(&status.Conditions, metav1.Condition{Type: conditionMetricsReady,
			Status: metav1.ConditionFalse, Reason: reason, Message: err.Error(), ObservedGeneration: phpa.Generation})
		meta.SetStatusCondition(&status.Conditions, metav1.Condition{Type: conditionScaleDownStabilized,
			Status: metav1.ConditionUnknown, Reason: "MetricsUnavailable", Message: "No scaling recommendation evaluated while CPU metrics are unavailable",
			ObservedGeneration: phpa.Generation})
	})
	if statusErr != nil {
		return ctrl.Result{}, errors.Join(err, statusErr)
	}
	if transient {
		return ctrl.Result{RequeueAfter: interval}, nil
	}
	return ctrl.Result{}, err
}

func (r *PredictiveHPAReconciler) now() time.Time {
	if r.Clock != nil {
		return r.Clock.Now()
	}
	return time.Now()
}

// Re-read without the informer cache before side effects. A resourceVersion
// conflict remains retryable; UID/generation mismatches abandon stale policy.
func (r *PredictiveHPAReconciler) freshPHPA(ctx context.Context, expected *autoscalingv1alpha1.PredictiveHPA) (*autoscalingv1alpha1.PredictiveHPA, error) {
	reader := r.APIReader
	if reader == nil {
		reader = r.Client
	}
	var latest autoscalingv1alpha1.PredictiveHPA
	if err := reader.Get(ctx, client.ObjectKeyFromObject(expected), &latest); err != nil {
		return nil, err
	}
	if latest.UID != expected.UID || latest.Generation != expected.Generation || !latest.DeletionTimestamp.IsZero() {
		return nil, apierrors.NewConflict(schema.GroupResource{Group: "autoscaling.brian.io", Resource: "predictivehpas"}, expected.Name,
			fmt.Errorf("PredictiveHPA identity or policy changed during reconciliation"))
	}
	return &latest, nil
}

// A hold/tolerance decision also publishes target-specific observations. Verify
// that incarnation before publishing success, even when no Scale write occurs.
func (r *PredictiveHPAReconciler) validateStatusTarget(ctx context.Context, expected *appsv1.Deployment) error {
	reader := r.APIReader
	if reader == nil {
		reader = r.Client
	}
	var latest appsv1.Deployment
	if err := reader.Get(ctx, client.ObjectKeyFromObject(expected), &latest); err != nil {
		return fmt.Errorf("%w: could not verify target before status publication: %v", metricsprovider.ErrTargetChanged, err)
	}
	if latest.UID != expected.UID || !latest.DeletionTimestamp.IsZero() {
		return fmt.Errorf("%w: target incarnation changed before status publication", metricsprovider.ErrTargetChanged)
	}
	return nil
}

func (r *PredictiveHPAReconciler) patchStatus(ctx context.Context, expected *autoscalingv1alpha1.PredictiveHPA,
	mutate func(*autoscalingv1alpha1.PredictiveHPAStatus)) error {
	latest, err := r.freshPHPA(ctx, expected)
	if err != nil {
		return err
	}
	before := latest.DeepCopy()
	mutate(&latest.Status)
	if reflect.DeepEqual(before.Status, latest.Status) {
		return nil
	}
	if err := r.Status().Patch(ctx, latest, client.MergeFromWithOptions(before, client.MergeFromWithOptimisticLock{})); err != nil {
		return fmt.Errorf("patch PredictiveHPA status: %w", err)
	}
	return nil
}

// Status is an int32 display field. Saturation preserves the direction of an
// extreme finite value while the floating-point decision remains clamped.
func cpuStatusValue(value float64) int32 {
	return int32(min(math.Round(value), float64(math.MaxInt32)))
}

type decisionStatusUpdate struct {
	currentCPU      float64
	predictedCPU    float64
	currentReplicas int32
	desiredReplicas int32
	stabilization   stabilizationResult
	scaled          bool
}

// publishDecisionStatus translates a completed policy outcome into owned API
// fields. patchStatus supplies the fresh version and preserves other writers'
// conditions; an unsuccessful Scale write never reaches this publication step.
func (r *PredictiveHPAReconciler) publishDecisionStatus(ctx context.Context, phpa *autoscalingv1alpha1.PredictiveHPA, target *appsv1.Deployment, update decisionStatusUpdate) error {
	if err := r.validateStatusTarget(ctx, target); err != nil {
		return err
	}
	currentInt, predictedInt := cpuStatusValue(update.currentCPU), cpuStatusValue(update.predictedCPU)
	stabilization := update.stabilization
	condition := metav1.Condition{
		Type: conditionScaleDownStabilized, Status: metav1.ConditionFalse,
		Reason: "NoStabilizationNeeded", Message: "Scaling up, unchanged, or no historical max to apply",
		ObservedGeneration: phpa.Generation,
	}
	if stabilization.stabilized {
		condition.Status = metav1.ConditionTrue
		condition.Reason = "WithinStabilizationWindow"
		condition.Message = fmt.Sprintf("computed %d, stabilized to %d (window=%ds)",
			update.desiredReplicas, stabilization.finalDesired, stabilization.windowSeconds)
		if stabilization.coldStart {
			condition.Reason = "ColdStartProtection"
			condition.Message = fmt.Sprintf("Computed %d, retaining %d while rebuilding history until %s",
				update.desiredReplicas, stabilization.finalDesired, stabilization.protectedUntil.UTC().Format(time.RFC3339Nano))
		}
	}
	return r.patchStatus(ctx, phpa, func(status *autoscalingv1alpha1.PredictiveHPAStatus) {
		status.CurrentReplicas, status.DesiredReplicas = update.currentReplicas, stabilization.finalDesired
		status.CurrentCPUUtilizationPercentage, status.PredictedCPUUtilizationPercentage = &currentInt, &predictedInt
		if update.scaled {
			now := metav1.Now()
			status.LastScaleTime = &now
		}
		meta.SetStatusCondition(&status.Conditions, condition)
		meta.SetStatusCondition(&status.Conditions, metav1.Condition{
			Type: conditionMetricsReady, Status: metav1.ConditionTrue, Reason: "ValidMetrics",
			Message: "CPU observations are complete, fresh and ready for prediction", ObservedGeneration: phpa.Generation,
		})
	})
}
