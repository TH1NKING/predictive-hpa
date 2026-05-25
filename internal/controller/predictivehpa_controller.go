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
	apierrors "k8s.io/apimachinery/pkg/api/errors"
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

// Reconcile is the observe-only loop for v1alpha1: it fetches CPU
// utilization, runs EWMA prediction, and writes the result back to status
// without taking any scaling action. Scaling logic is added in step 11.
func (r *PredictiveHPAReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := logf.FromContext(ctx)

	// 1. Fetch PredictiveHPA.
	var phpa autoscalingv1alpha1.PredictiveHPA
	if err := r.Get(ctx, req.NamespacedName, &phpa); err != nil {
		if apierrors.IsNotFound(err) {
			// Resource deleted; nothing to do.
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

	current := samples[len(samples)-1].Value

	// 6. Update status (observe-only: DesiredReplicas mirrors CurrentReplicas).
	currentInt := int32(math.Round(current))
	predictedInt := int32(math.Round(predicted))
	phpa.Status.CurrentReplicas = deploy.Status.Replicas
	phpa.Status.DesiredReplicas = deploy.Status.Replicas
	phpa.Status.CurrentCPUUtilizationPercentage = &currentInt
	phpa.Status.PredictedCPUUtilizationPercentage = &predictedInt

	if err := r.Status().Update(ctx, &phpa); err != nil {
		return ctrl.Result{}, fmt.Errorf("update status: %w", err)
	}

	log.Info("reconciled",
		"currentCPU%", fmt.Sprintf("%.2f", current),
		"predictedCPU%", fmt.Sprintf("%.2f", predicted),
		"replicas", deploy.Status.Replicas,
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
