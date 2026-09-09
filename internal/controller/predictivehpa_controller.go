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
	"fmt"
	"math"
	"sync"
	"time"

	"github.com/go-logr/logr"
	appsv1 "k8s.io/api/apps/v1"
	autoscalingv1 "k8s.io/api/autoscaling/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/clock"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/predicate"

	autoscalingv1alpha1 "github.com/th1nking/predictive-hpa/api/v1alpha1"
	"github.com/th1nking/predictive-hpa/internal/metricsprovider"
	"github.com/th1nking/predictive-hpa/internal/predictor"
)

const (
	// DefaultRequeueInterval is the normal polling interval when no override
	// is supplied. Every decision mode retains the same 30-second default.
	DefaultRequeueInterval = 30 * time.Second

	// requeueOnConfigError is used when the user has configured something
	// unsupported (e.g. scaleTargetRef.Kind != Deployment). Slow polling
	// because the user has to intervene anyway.
	requeueOnConfigError = 60 * time.Second

	// conditionScaleDownStabilized indicates whether the scale-down
	// stabilization window is currently capping the computed desired.
	conditionScaleDownStabilized = "ScaleDownStabilized"
	scaleTargetIndex             = "spec.scaleTargetRef.name"
)

// PredictiveHPAReconciler reconciles a PredictiveHPA object.
type PredictiveHPAReconciler struct {
	client.Client
	APIReader       client.Reader
	Scheme          *runtime.Scheme
	MetricsProvider metricsprovider.Provider

	// RequeueInterval controls normal and transient-data polling. Zero uses
	// the default 30-second interval; explicit intervals must be at least 1s.
	RequeueInterval time.Duration

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
// +kubebuilder:rbac:groups=apps,resources=replicasets,verbs=get;list;watch
// +kubebuilder:rbac:groups="",resources=pods,verbs=get;list;watch
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
// History is process-local. A restart conservatively protects the live Scale
// request for one complete window while rebuilding verified recommendations.
func (r *PredictiveHPAReconciler) Reconcile(ctx context.Context, req ctrl.Request) (result ctrl.Result, reconcileErr error) {
	// Diagnostic wall time is independent of the injectable policy clock. Carry
	// this boundary into the metrics provider so its query shares the same trace.
	reconcileStarted := time.Now()
	log := logf.FromContext(ctx).WithValues("reconcileStartedAt", reconcileStarted.UTC().Format(time.RFC3339Nano))
	ctx = logf.IntoContext(ctx, log)
	defer func() {
		logReconciliationCompletion(log, reconcileStarted, result, reconcileErr)
	}()
	requeueInterval, err := r.requeueInterval()
	if err != nil {
		return ctrl.Result{}, err
	}
	return r.reconcileTarget(ctx, req, requeueInterval)
}

// Request-level validation and timing stay at the public boundary, including
// early returns; target reconciliation retains the common scaling policy.
func (r *PredictiveHPAReconciler) reconcileTarget(
	ctx context.Context, req ctrl.Request, requeueInterval time.Duration,
) (ctrl.Result, error) {
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
		log.Info("Unsupported scaleTargetRef kind (v1alpha1 supports Deployment only)",
			"kind", phpa.Spec.ScaleTargetRef.Kind)
		return ctrl.Result{RequeueAfter: requeueOnConfigError}, nil
	}
	if !phpa.DeletionTimestamp.IsZero() {
		return ctrl.Result{}, nil
	}

	// 3. Fetch the target Deployment.
	var deploy appsv1.Deployment
	deployKey := types.NamespacedName{
		Namespace: phpa.Namespace,
		Name:      phpa.Spec.ScaleTargetRef.Name,
	}
	if err := r.Get(ctx, deployKey, &deploy); err != nil {
		if apierrors.IsNotFound(err) {
			log.Info("Target Deployment not found", "deployment", deployKey)
			return r.metricsUnavailable(ctx, &phpa, fmt.Errorf("%w: target Deployment not found", metricsprovider.ErrNoData), requeueInterval)
		}
		return ctrl.Result{}, fmt.Errorf("get Deployment: %w", err)
	}

	// 4. Fetch CPU utilization series.
	window := phpa.Spec.Prediction.Window.Duration
	cpuHistory, err := r.MetricsProvider.AverageCPUUtilizationPercentage(
		ctx, &deploy, window,
	)
	if err != nil {
		return r.metricsUnavailable(ctx, &phpa, err, requeueInterval)
	}
	if err := validateCPUHistory(cpuHistory, r.now(), window); err != nil {
		return r.metricsUnavailable(ctx, &phpa, err, requeueInterval)
	}
	samples := cpuHistory.Samples

	// 5. Run EWMA prediction.
	alpha := float64(phpa.Spec.Prediction.AlphaPercent) / 100.0
	horizon := phpa.Spec.Prediction.Horizon.Duration
	predicted, err := predictor.Predict(samples, predictor.EWMAConfig{
		Alpha:   alpha,
		Horizon: horizon,
	})
	if err != nil {
		return r.metricsUnavailable(ctx, &phpa, fmt.Errorf("%w: prediction: %v", metricsprovider.ErrInvalidData, err), requeueInterval)
	}
	if math.IsNaN(predicted) || math.IsInf(predicted, 0) {
		return r.metricsUnavailable(ctx, &phpa, fmt.Errorf("%w: nonfinite forecast", metricsprovider.ErrInvalidData), requeueInterval)
	}

	currentCPU := samples[len(samples)-1].Value

	// 6. Bound the prediction (business-layer policy; the predictor stays
	//    semantically honest). capPrediction clamps negatives to 0 and caps
	//    the upward lead to currentCPU * maxLeadFactor, preventing the EWMA
	//    forecast from driving ~2x over-provisioning on load onset.
	rawPredicted := predicted
	predicted = capPrediction(predicted, currentCPU)
	if predicted != rawPredicted {
		log.V(1).Info("Prediction bounded",
			"raw", rawPredicted, "bounded", predicted, "currentCPU", currentCPU)
	}

	mode, decisionCPU := selectDecisionSignal(phpa.Spec.DecisionMode, currentCPU, predicted, phpa.Spec.TargetCPUUtilizationPercentage)

	// 7. Compute desired replicas (formula + min/max clamp).
	scale := &autoscalingv1.Scale{}
	targetUID := deploy.UID
	if err := r.SubResource("scale").Get(ctx, &deploy, scale); err != nil {
		return ctrl.Result{}, fmt.Errorf("get Deployment scale: %w", err)
	}
	if scale.UID != targetUID {
		return r.metricsUnavailable(ctx, &phpa, metricsprovider.ErrTargetChanged, requeueInterval)
	}
	currentReplicas := scale.Status.Replicas
	requestedReplicas := scale.Spec.Replicas

	// v1alpha1 has no scale-to-zero path; default or explicit zero means one.
	minReplicas := max(int32(1), ptr.Deref(phpa.Spec.MinReplicas, int32(1)))

	desiredReplicas := computeDesiredReplicas(
		currentReplicas,
		decisionCPU,
		phpa.Spec.TargetCPUUtilizationPercentage,
		minReplicas,
		phpa.Spec.MaxReplicas,
	)
	// Actual Pod count can lag an already-issued Scale request. Keep the
	// selected CPU signal's direction relative to that live request: low CPU
	// cannot reverse a pending reduction, nor high CPU a pending expansion.
	if decisionCPU < float64(phpa.Spec.TargetCPUUtilizationPercentage) {
		desiredReplicas = min(desiredReplicas, requestedReplicas)
	} else if decisionCPU > float64(phpa.Spec.TargetCPUUtilizationPercentage) {
		desiredReplicas = max(desiredReplicas, requestedReplicas)
	}
	desiredReplicas = min(max(desiredReplicas, max(minReplicas, 1)), phpa.Spec.MaxReplicas)

	// 8. Apply the bounded history and identity/configuration cold-start guard.
	now := r.now()
	stabilization := r.stabilizeRecommendation(&phpa, &deploy, now, desiredReplicas, requestedReplicas, minReplicas)
	finalDesired, stabilized := stabilization.finalDesired, stabilization.stabilized

	// 9. Decide whether to actually scale.
	scaled := false
	skipReason := ""
	switch {
	case finalDesired == requestedReplicas:
		skipReason = "DesiredEqualsCurrent"
	case requestedReplicas >= max(minReplicas, 1) && requestedReplicas <= phpa.Spec.MaxReplicas &&
		withinTolerance(decisionCPU, phpa.Spec.TargetCPUUtilizationPercentage):
		skipReason = "WithinToleranceBand"
	}
	// Persist the policy outcome before Scale/status writes: either can fail,
	// and a later status conflict must not erase evidence of an earlier decision.
	log.Info("Evaluated PredictiveHPA scaling decision",
		"decisionAt", time.Now().UTC().Format(time.RFC3339Nano),
		"stabilizationEvaluatedAt", now.UTC().Format(time.RFC3339Nano),
		"stabilizationHistoryEntries", stabilization.historyEntries,
		"stabilizationHistoryOldestAt", stabilization.historyOldest.UTC().Format(time.RFC3339Nano),
		"coldStartProtection", stabilization.coldStart, "coldStartProtectedUntil", stabilization.protectedUntil.UTC().Format(time.RFC3339Nano),
		"decisionMode", mode, "decisionCPU%", decisionCPU,
		"rawPredictedCPU%", rawPredicted, "currentCPU%", currentCPU, "predictedCPU%", predicted,
		"currentReplicas", currentReplicas, "desiredReplicas", desiredReplicas, "finalDesired", finalDesired,
		"stabilized", stabilized, "skipReason", skipReason, "samples", len(samples),
		"latestEvaluationAt", samples[len(samples)-1].Timestamp.UTC().Format(time.RFC3339Nano))
	if skipReason == "" {
		if _, err := r.freshPHPA(ctx, &phpa); err != nil {
			return ctrl.Result{}, err
		}
		// Scale reads bypass the informer cache. A concurrent rollout or another
		// writer invalidates this decision rather than silently changing its base.
		latestScale := &autoscalingv1.Scale{}
		if err := r.SubResource("scale").Get(ctx, &deploy, latestScale); err != nil {
			return ctrl.Result{}, fmt.Errorf("recheck Deployment scale: %w", err)
		}
		if latestScale.UID != targetUID || latestScale.ResourceVersion != scale.ResourceVersion {
			return ctrl.Result{}, fmt.Errorf("deployment scale changed during decision; retry with fresh state")
		}
		if err := validateCPUHistory(cpuHistory, r.now(), window); err != nil {
			return r.metricsUnavailable(ctx, &phpa, err, requeueInterval)
		}
		previousDesiredReplicas := scale.Spec.Replicas
		scale.Spec.Replicas = finalDesired
		scaleWriteStarted := time.Now()
		if err := r.SubResource("scale").Update(
			ctx, &deploy, client.WithSubResourceBody(scale),
		); err != nil {
			return ctrl.Result{}, fmt.Errorf("update Deployment scale: %w", err)
		}
		scaleWriteFinished := time.Now()
		scaled = true
		// Log at the successful write boundary, before a possibly failing status
		// update. Replica sampling can observe this action on a later scrape.
		log.Info("Scaled Deployment", "deployment", deploy.Name,
			"scaleWriteStartedAt", scaleWriteStarted.UTC().Format(time.RFC3339Nano),
			"scaleWriteFinishedAt", scaleWriteFinished.UTC().Format(time.RFC3339Nano),
			"decisionMode", mode, "decisionCPU%", decisionCPU,
			"currentReplicas", currentReplicas, "previousDesiredReplicas", previousDesiredReplicas,
			"finalDesired", finalDesired, "scaled", true)
	}

	// 10. Publish only controller-owned status fields using a fresh API version.
	if err := r.publishDecisionStatus(ctx, &phpa, decisionStatusUpdate{
		currentCPU: currentCPU, predictedCPU: predicted, currentReplicas: currentReplicas,
		desiredReplicas: desiredReplicas, stabilization: stabilization, scaled: scaled,
	}); err != nil {
		return ctrl.Result{}, fmt.Errorf("update status: %w", err)
	}

	log.Info("Reconciled PredictiveHPA",
		"decisionMode", mode,
		"decisionCPU%", fmt.Sprintf("%.2f", decisionCPU),
		"rawPredictedCPU%", fmt.Sprintf("%.2f", rawPredicted),
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

	return ctrl.Result{RequeueAfter: requeueInterval}, nil
}

func (r *PredictiveHPAReconciler) requeueInterval() (time.Duration, error) {
	interval := r.RequeueInterval
	if interval == 0 {
		interval = DefaultRequeueInterval
	}
	if interval < time.Second {
		return 0, fmt.Errorf("requeue interval must be at least 1s, got %s", interval)
	}
	return interval, nil
}

func logReconciliationCompletion(log logr.Logger, started time.Time, result ctrl.Result, err error) {
	finished := time.Now()
	errorMessage := ""
	if err != nil {
		errorMessage = err.Error()
	}
	log.Info("Finished PredictiveHPA reconciliation",
		"reconcileFinishedAt", finished.UTC().Format(time.RFC3339Nano),
		"reconcileDurationSeconds", finished.Sub(started).Seconds(),
		"reconcileError", errorMessage, "requeueAfterSeconds", result.RequeueAfter.Seconds())
}

// SetupWithManager sets up the controller with the Manager.
func (r *PredictiveHPAReconciler) SetupWithManager(mgr ctrl.Manager) error {
	if _, err := r.requeueInterval(); err != nil {
		return err
	}
	r.history = make(map[types.NamespacedName]*scaleHistory)
	if r.APIReader == nil {
		r.APIReader = mgr.GetAPIReader()
	}
	if r.Clock == nil {
		r.Clock = clock.RealClock{}
	}
	if err := mgr.GetFieldIndexer().IndexField(context.Background(), &autoscalingv1alpha1.PredictiveHPA{}, scaleTargetIndex, func(obj client.Object) []string {
		phpa := obj.(*autoscalingv1alpha1.PredictiveHPA)
		if phpa.Spec.ScaleTargetRef.Kind != "Deployment" {
			return nil
		}
		return []string{phpa.Spec.ScaleTargetRef.Name}
	}); err != nil {
		return fmt.Errorf("index PredictiveHPA target: %w", err)
	}
	return ctrl.NewControllerManagedBy(mgr).
		// Status patches must not create a self-triggered query/write loop.
		// Annotation changes remain useful explicit reconciliation requests.
		For(&autoscalingv1alpha1.PredictiveHPA{}, builder.WithPredicates(predicate.Or(predicate.GenerationChangedPredicate{}, predicate.AnnotationChangedPredicate{}))).
		Watches(&appsv1.Deployment{}, handler.EnqueueRequestsFromMapFunc(r.requestsForDeployment)).
		Named("predictivehpa").
		Complete(r)
}

func (r *PredictiveHPAReconciler) requestsForDeployment(ctx context.Context, obj client.Object) []ctrl.Request {
	var list autoscalingv1alpha1.PredictiveHPAList
	if err := r.List(ctx, &list, client.InNamespace(obj.GetNamespace()), client.MatchingFields{scaleTargetIndex: obj.GetName()}); err != nil {
		logf.FromContext(ctx).Error(err, "Could not list PredictiveHPA targets", "deployment", client.ObjectKeyFromObject(obj))
		return nil
	}
	requests := make([]ctrl.Request, 0, len(list.Items))
	for i := range list.Items {
		requests = append(requests, ctrl.Request{NamespacedName: client.ObjectKeyFromObject(&list.Items[i])})
	}
	return requests
}
