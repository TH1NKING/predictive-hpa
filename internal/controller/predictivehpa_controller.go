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
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/clock"
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

	// conditionScaleDownStabilized indicates whether the scale-down
	// stabilization window is currently capping the computed desired.
	conditionScaleDownStabilized = "ScaleDownStabilized"
)

// PredictiveHPAReconciler reconciles a PredictiveHPA object.
type PredictiveHPAReconciler struct {
	client.Client
	Scheme          *runtime.Scheme
	MetricsProvider metricsprovider.Provider

	// Clock is an injectable time source. Production code leaves it nil
	// and SetupWithManager defaults it to clock.RealClock{}; envtest specs
	// inject a clock. FakeClock to drive the scale-down stabilization window
	// deterministically without real waits. The PassiveClock interface is
	// sufficient because the controller only needs Now(); ticker-based APIs
	// are not used.
	Clock clock.PassiveClock

	// history tracks recent desiredReplicas per PHPA for the scale-down
	// stabilization window. Lazy-initialized in SetupWithManager; entries
	// are deleted in Reconcile when the corresponding PHPA is no longer
	// found (controller-runtime invokes Reconcile on delete events).
	// Access to both the map and the contained scaleHistory entries is
	// guarded by mu.
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
// prediction, computes the desired replicas (formula + clamp + tolerance),
// applies the scale-down stabilization window (max over recent desireds),
// and writes the result to the Deployment scale subresource.
//
// The stabilization window prevents rapid downscaling on transient CPU
// drops: when scaling down, finalDesired is capped to the max desired
// observed within the past spec.scaleDownStabilizationWindowSeconds.
// History is kept in-memory and lost on controller restart; restart-safe
// persistence is on the v1beta1 roadmap.
func (r *PredictiveHPAReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := logf.FromContext(ctx)

	// 1. Fetch PredictiveHPA. On NotFound, drop the in-memory history for
	//    this key to prevent map leaks (controller-runtime invokes Reconcile
	//    on delete events).
	var phpa autoscalingv1alpha1.PredictiveHPA
	if err := r.Get(ctx, req.NamespacedName, &phpa); err != nil {
		if apierrors.IsNotFound(err) {
			r.mu.Lock()
			delete(r.history, req.NamespacedName)
			r.mu.Unlock()
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, fmt.Errorf("get PredictiveHPA: %w", err)
	}

	// 2. Validate scaleTargetRef.
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

	// 6. Clamp negative prediction.
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

	// 8. Apply scale-down stabilization window.
	//
	// Record every computed desired (regardless of direction) so the
	// max-over-window calculation is complete. When scaling down, cap
	// finalDesired to the max desired observed within the past window
	// — preventing rapid downscaling on transient CPU drops.
	//
	// cold-start: if after record + prune the history holds only the
	// just-recorded entry, either this is the first reconcile of the PHPA
	// or the controller restarted. In both cases scale-down has no
	// historical safety net for this round.
	now := r.Clock.Now()
	stabilizationWindowSec := int32(60)
	if phpa.Spec.ScaleDownStabilizationWindowSeconds != nil {
		stabilizationWindowSec = *phpa.Spec.ScaleDownStabilizationWindowSeconds
	}
	stabilizationWindow := time.Duration(stabilizationWindowSec) * time.Second

	r.mu.Lock()
	hist, ok := r.history[req.NamespacedName]
	if !ok {
		hist = &scaleHistory{}
		r.history[req.NamespacedName] = hist
	}
	hist.record(now, desiredReplicas)

	finalDesired := desiredReplicas
	stabilized := false
	coldStart := false
	if desiredReplicas < currentReplicas {
		finalDesired = hist.maxInWindow(now, stabilizationWindow)
		if finalDesired > desiredReplicas {
			stabilized = true
		}
		if hist.len() == 1 {
			coldStart = true
		}
	}
	r.mu.Unlock()

	if coldStart {
		log.Info("Stabilization window cold-start: no prior history, applying scale-down without window protection",
			"desired", desiredReplicas, "current", currentReplicas)
	}

	// 9. Decide whether to actually scale.
	scaled := false
	skipReason := ""
	switch {
	case finalDesired == currentReplicas:
		skipReason = "DesiredEqualsCurrent"
	case withinTolerance(predicted, phpa.Spec.TargetCPUUtilizationPercentage):
		skipReason = "WithinToleranceBand"
	default:
		scale := &autoscalingv1.Scale{}
		if err := r.SubResource("scale").Get(ctx, &deploy, scale); err != nil {
			return ctrl.Result{}, fmt.Errorf("get Deployment scale: %w", err)
		}
		scale.Spec.Replicas = finalDesired
		if err := r.SubResource("scale").Update(
			ctx, &deploy, client.WithSubResourceBody(scale),
		); err != nil {
			return ctrl.Result{}, fmt.Errorf("update Deployment scale: %w", err)
		}
		scaled = true
	}

	// 10. Update status.
	currentInt := int32(math.Round(currentCPU))
	predictedInt := int32(math.Round(predicted))
	phpa.Status.CurrentReplicas = currentReplicas
	phpa.Status.DesiredReplicas = finalDesired
	phpa.Status.CurrentCPUUtilizationPercentage = &currentInt
	phpa.Status.PredictedCPUUtilizationPercentage = &predictedInt
	if scaled {
		nowMeta := metav1.Now()
		phpa.Status.LastScaleTime = &nowMeta
	}

	var stabCondition metav1.Condition
	if stabilized {
		stabCondition = metav1.Condition{
			Type:   conditionScaleDownStabilized,
			Status: metav1.ConditionTrue,
			Reason: "WithinStabilizationWindow",
			Message: fmt.Sprintf("computed %d, stabilized to %d (window=%ds)",
				desiredReplicas, finalDesired, stabilizationWindowSec),
			ObservedGeneration: phpa.Generation,
		}
	} else {
		stabCondition = metav1.Condition{
			Type:               conditionScaleDownStabilized,
			Status:             metav1.ConditionFalse,
			Reason:             "NoStabilizationNeeded",
			Message:            "Scaling up, unchanged, or no historical max to apply",
			ObservedGeneration: phpa.Generation,
		}
	}
	meta.SetStatusCondition(&phpa.Status.Conditions, stabCondition)

	if err := r.Status().Update(ctx, &phpa); err != nil {
		return ctrl.Result{}, fmt.Errorf("update status: %w", err)
	}

	log.Info("reconciled",
		"currentCPU%", fmt.Sprintf("%.2f", currentCPU),
		"predictedCPU%", fmt.Sprintf("%.2f", predicted),
		"currentReplicas", currentReplicas,
		"desiredReplicas", desiredReplicas,
		"finalDesired", finalDesired,
		"stabilized", stabilized,
		"scaled", scaled,
		"skipReason", skipReason,
		"samples", len(samples),
	)

	return ctrl.Result{RequeueAfter: requeueDefault}, nil
}

// SetupWithManager sets up the controller with the Manager.
func (r *PredictiveHPAReconciler) SetupWithManager(mgr ctrl.Manager) error {
	r.history = make(map[types.NamespacedName]*scaleHistory)
	if r.Clock == nil {
		r.Clock = clock.RealClock{}
	}
	return ctrl.NewControllerManagedBy(mgr).
		For(&autoscalingv1alpha1.PredictiveHPA{}).
		Named("predictivehpa").
		Complete(r)
}
