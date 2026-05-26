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
	"context"
	"errors"
	"fmt"
	"math"
	"sync"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	autoscalingv1 "k8s.io/api/autoscaling/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	autoscalingv1alpha1 "github.com/th1nking/predictive-hpa/api/v1alpha1"
	"github.com/th1nking/predictive-hpa/internal/metricsprovider"
	"github.com/th1nking/predictive-hpa/internal/predictor"
)

const (
	// requeueDefault is the interval between reconciliations under normal
	// conditions. The native HPA controller uses 15s; we use 30s because
	// the EWMA history window (typically 5m) is much larger than the
	// polling interval — more frequent reconciliation yields no signal.
	requeueDefault = 30 * time.Second

	// requeueOnConfigError is used when the user has configured something
	// unsupported (e.g. scaleTargetRef.Kind != Deployment). Slow polling
	// because the user has to intervene anyway.
	requeueOnConfigError = 60 * time.Second
)

// PredictiveHPAReconciler reconciles a PredictiveHPA object.
type PredictiveHPAReconciler struct {
	client.Client
	Scheme          *runtime.Scheme
	MetricsProvider metricsprovider.Provider

	// history tracks recent desiredReplicas per PHPA for the scale-down
	// stabilization window. Lazy-initialized in SetupWithManager. Access
	// to both the map and the contained scaleHistory entries is guarded
	// by mu.
	mu      sync.Mutex
	history map[types.NamespacedName]*scaleHistory
}

// +kubebuilder:rbac:groups=autoscaling.brian.io,resources=predictivehpas,verbs=get;list;watch
// +kubebuilder:rbac:groups=autoscaling.brian.io,resources=predictivehpas/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=autoscaling.brian.io,resources=predictivehpas/finalizers,verbs=update
// +kubebuilder:rbac:groups=apps,resources=deployments,verbs=get;list;watch
// +kubebuilder:rbac:groups=apps,resources=deployments/scale,verbs=get;update;patch
// +kubebuilder:rbac:groups="",resources=events,verbs=create;patch
// +kubebuilder:rbac:groups=events.k8s.io,resources=events,verbs=create;patch

// Reconcile fetches CPU utilization for the target Deployment, runs EWMA
// prediction to project utilization horizon seconds ahead, applies the
// scaling formula (with negative-prediction clamp, min/max clamp, and a
// tolerance band), and writes the result to the Deployment scale subresource.
//
// The scale-down stabilization window is not yet applied at this commit —
// scale-down therefore happens immediately. The window is wired in the next
// commit.
func (r *PredictiveHPAReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := logf.FromContext(ctx)

	// 1. Fetch PredictiveHPA.
	var phpa autoscalingv1alpha1.PredictiveHPA
	if err := r.Get(ctx, req.NamespacedName, &phpa); err != nil {
		if apierrors.IsNotFound(err) {
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, fmt.Errorf("get PredictiveHPA: %w", err)
	}

	// 2. Validate scaleTargetRef. v1alpha1 supports Deployment only.
	if phpa.Spec.ScaleTargetRef.Kind != "Deployment" {
		log.Info("unsupported scaleTargetRef kind (v1alpha1 supports Deployment only)",
			"kind", phpa.Spec.ScaleTargetRef.Kind)
		return ctrl.Result{RequeueAfter: requeueOnConfigError}, nil
	}

	// 3. Fetch the target Deployment.
	var deploy appsv1.Deployment
	deployKey := types.NamespacedName{
		Namespace: phpa.Namespace,
		Name:      phpa.Spec.ScaleTargetRef.Name,
	}
	if err := r.Get(ctx, deployKey, &deploy); err != nil {
		if apierrors.IsNotFound(err) {
			log.Info("target Deployment not found", "deployment", deployKey)
			return ctrl.Result{RequeueAfter: requeueDefault}, nil
		}
		return ctrl.Result{}, fmt.Errorf("get Deployment: %w", err)
	}

	// 4. Fetch CPU utilization series.
	window := phpa.Spec.Prediction.Window.Duration
	samples, err := r.MetricsProvider.AverageCPUUtilizationPercentage(
		ctx, phpa.Namespace, deploy.Name, window,
	)
	if err != nil {
		if errors.Is(err, metricsprovider.ErrNoData) {
			log.Info("metrics not yet available, will retry",
				"deployment", deploy.Name)
			return ctrl.Result{RequeueAfter: requeueDefault}, nil
		}
		return ctrl.Result{}, fmt.Errorf("fetch metrics: %w", err)
	}

	// 5. Run EWMA prediction.
	alpha := float64(phpa.Spec.Prediction.AlphaPercent) / 100.0
	horizon := phpa.Spec.Prediction.Horizon.Duration
	predicted, err := predictor.Predict(samples, predictor.EWMAConfig{
		Alpha:   alpha,
		Horizon: horizon,
	})
	if err != nil {
		if errors.Is(err, predictor.ErrInsufficientData) {
			log.Info("insufficient samples for prediction, will retry",
				"samples", len(samples))
			return ctrl.Result{RequeueAfter: requeueDefault}, nil
		}
		return ctrl.Result{}, fmt.Errorf("predict: %w", err)
	}

	currentCPU := samples[len(samples)-1].Value

	// 6. Clamp negative prediction. The first-difference forecast can output
	// negative values on sharply descending signals (e.g., when load testing
	// ends); the algorithm is mathematically correct, but the business layer
	// caps it before deriving replicas. Log at V(1) to preserve the raw value
	// for debugging without spamming default logs.
	if predicted < 0 {
		log.V(1).Info("Prediction clamped to zero", "raw", predicted)
		predicted = 0
	}

	// 7. Compute desired replicas (formula + min/max clamp).
	currentReplicas := deploy.Status.Replicas

	minReplicas := int32(1)
	if phpa.Spec.MinReplicas != nil {
		minReplicas = *phpa.Spec.MinReplicas
		if minReplicas < 1 {
			// v1alpha1 does not support scale-to-zero; the pure function
			// will fall back to 1. Logged at V(1) to avoid spamming on
			// every 30s reconcile.
			log.V(1).Info("minReplicas=0 not supported in v1alpha1; treating as 1")
		}
	}

	desiredReplicas := computeDesiredReplicas(
		currentReplicas,
		predicted,
		phpa.Spec.TargetCPUUtilizationPercentage,
		minReplicas,
		phpa.Spec.MaxReplicas,
	)

	// 8. Decide whether to actually scale.
	//
	// Two early-exit paths:
	//   (a) desired == current: idempotency optimization (avoid etcd write).
	//   (b) within tolerance band: predicted is close enough to target that
	//       the adjustment is not worth Pod churn.
	//
	// The scale-down stabilization window is not yet applied here — scale-
	// down therefore happens immediately when the formula calls for it.
	// Stabilization is added in the next commit.
	scaled := false
	skipReason := ""
	switch {
	case desiredReplicas == currentReplicas:
		skipReason = "DesiredEqualsCurrent"
	case withinTolerance(predicted, phpa.Spec.TargetCPUUtilizationPercentage):
		skipReason = "WithinToleranceBand"
	default:
		// Update via Deployment/scale subresource (matches the native HPA
		// controller and the principle of least privilege: RBAC grants
		// deployments/scale=get;update;patch but deployments=get;list;watch
		// only — this controller cannot accidentally edit Deployment fields
		// other than spec.replicas).
		scale := &autoscalingv1.Scale{}
		if err := r.SubResource("scale").Get(ctx, &deploy, scale); err != nil {
			return ctrl.Result{}, fmt.Errorf("get Deployment scale: %w", err)
		}
		scale.Spec.Replicas = desiredReplicas
		if err := r.SubResource("scale").Update(
			ctx, &deploy, client.WithSubResourceBody(scale),
		); err != nil {
			return ctrl.Result{}, fmt.Errorf("update Deployment scale: %w", err)
		}
		scaled = true
	}

	// 9. Update status.
	currentInt := int32(math.Round(currentCPU))
	predictedInt := int32(math.Round(predicted))
	phpa.Status.CurrentReplicas = currentReplicas
	phpa.Status.DesiredReplicas = desiredReplicas
	phpa.Status.CurrentCPUUtilizationPercentage = &currentInt
	phpa.Status.PredictedCPUUtilizationPercentage = &predictedInt
	if scaled {
		now := metav1.Now()
		phpa.Status.LastScaleTime = &now
	}

	if err := r.Status().Update(ctx, &phpa); err != nil {
		return ctrl.Result{}, fmt.Errorf("update status: %w", err)
	}

	log.Info("reconciled",
		"currentCPU%", fmt.Sprintf("%.2f", currentCPU),
		"predictedCPU%", fmt.Sprintf("%.2f", predicted),
		"currentReplicas", currentReplicas,
		"desiredReplicas", desiredReplicas,
		"scaled", scaled,
		"skipReason", skipReason,
		"samples", len(samples),
	)

	return ctrl.Result{RequeueAfter: requeueDefault}, nil
}

// SetupWithManager sets up the controller with the Manager.
func (r *PredictiveHPAReconciler) SetupWithManager(mgr ctrl.Manager) error {
	r.history = make(map[types.NamespacedName]*scaleHistory)
	return ctrl.NewControllerManagedBy(mgr).
		For(&autoscalingv1alpha1.PredictiveHPA{}).
		Named("predictivehpa").
		Complete(r)
}
