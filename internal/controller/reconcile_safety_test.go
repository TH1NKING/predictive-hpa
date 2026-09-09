package controller

import (
	"context"
	"errors"
	"math"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	autoscalingv2 "k8s.io/api/autoscaling/v2"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	testingclock "k8s.io/utils/clock/testing"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"

	autoscalingv1alpha1 "github.com/th1nking/predictive-hpa/api/v1alpha1"
	"github.com/th1nking/predictive-hpa/internal/metricsprovider"
	"github.com/th1nking/predictive-hpa/internal/predictor"
)

// These tests invoke the public reconciliation boundary and inspect only the
// Kubernetes Scale/status API. The fake API allows deterministic failure and
// recreation sequencing; the existing envtest suite also runs against etcd.
func safetyFixture(t *testing.T) (*PredictiveHPAReconciler, *fakeMetricsProvider, ctrl.Request) {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := appsv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := autoscalingv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	replicas, window, cpu := int32(5), int32(60), int32(80)
	now := time.Date(2026, 9, 9, 8, 0, 0, 0, time.UTC)
	lastScale := metav1.NewTime(now.Add(-time.Hour))
	deploy := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: "web", Namespace: "test", UID: "deployment-one"},
		Spec:       appsv1.DeploymentSpec{Replicas: &replicas},
		Status:     appsv1.DeploymentStatus{Replicas: replicas},
	}
	phpa := &autoscalingv1alpha1.PredictiveHPA{
		ObjectMeta: metav1.ObjectMeta{Name: "web", Namespace: "test", UID: "phpa-one", Generation: 1},
		Spec: autoscalingv1alpha1.PredictiveHPASpec{
			ScaleTargetRef: autoscalingv2.CrossVersionObjectReference{APIVersion: "apps/v1", Kind: "Deployment", Name: "web"},
			MaxReplicas:    10, TargetCPUUtilizationPercentage: 50,
			DecisionMode: autoscalingv1alpha1.DecisionModeCurrent,
			Prediction: autoscalingv1alpha1.PredictionConfig{Algorithm: autoscalingv1alpha1.PredictionAlgorithmEWMA,
				AlphaPercent: 30, Window: metav1.Duration{Duration: 5 * time.Minute}, Horizon: metav1.Duration{Duration: 30 * time.Second}},
			ScaleDownStabilizationWindowSeconds: &window,
		},
		Status: autoscalingv1alpha1.PredictiveHPAStatus{CurrentCPUUtilizationPercentage: &cpu,
			PredictedCPUUtilizationPercentage: &cpu, LastScaleTime: &lastScale},
	}
	c := fake.NewClientBuilder().WithScheme(scheme).WithStatusSubresource(phpa, deploy).WithObjects(phpa, deploy).Build()
	provider := newFakeMetricsProvider()
	r := &PredictiveHPAReconciler{Client: c, Scheme: scheme, MetricsProvider: provider,
		Clock: testingclock.NewFakeClock(now), history: make(map[types.NamespacedName]*scaleHistory)}
	provider.Clock = r.Clock
	return r, provider, ctrl.Request{NamespacedName: client.ObjectKeyFromObject(phpa)}
}

func safetyStatus(t *testing.T, r *PredictiveHPAReconciler, req ctrl.Request) autoscalingv1alpha1.PredictiveHPAStatus {
	t.Helper()
	var phpa autoscalingv1alpha1.PredictiveHPA
	if err := r.Get(context.Background(), req.NamespacedName, &phpa); err != nil {
		t.Fatal(err)
	}
	return phpa.Status
}

func safetyReplicas(t *testing.T, r *PredictiveHPAReconciler) int32 {
	t.Helper()
	var deploy appsv1.Deployment
	if err := r.Get(context.Background(), types.NamespacedName{Namespace: "test", Name: "web"}, &deploy); err != nil {
		t.Fatal(err)
	}
	return *deploy.Spec.Replicas
}

func TestReconcileSafetyMissingMetricsClearsObsoleteValues(t *testing.T) {
	r, _, req := safetyFixture(t)
	previous := safetyStatus(t, r, req)
	result, err := r.Reconcile(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	if result.RequeueAfter != DefaultRequeueInterval {
		t.Fatalf("requeue = %s", result.RequeueAfter)
	}
	if got := safetyReplicas(t, r); got != 5 {
		t.Fatalf("missing metrics changed replicas to %d", got)
	}
	status := safetyStatus(t, r, req)
	condition := meta.FindStatusCondition(status.Conditions, "MetricsReady")
	if condition == nil || condition.Status != metav1.ConditionFalse || condition.Reason != "NoData" {
		t.Fatalf("missing metrics condition = %+v", condition)
	}
	if status.CurrentCPUUtilizationPercentage != nil || status.PredictedCPUUtilizationPercentage != nil {
		t.Fatal("unavailable metrics must clear obsolete CPU values")
	}
	if !status.LastScaleTime.Equal(previous.LastScaleTime) {
		t.Fatal("metrics outage changed lastScaleTime")
	}
}

func safetyCPU(r *PredictiveHPAReconciler, p *fakeMetricsProvider, cpu float64) {
	now := r.Clock.Now()
	p.SetSamples("test", "web", []predictor.Sample{
		{Timestamp: now.Add(-30 * time.Second), Value: cpu},
		{Timestamp: now, Value: cpu},
	})
}

func TestReconcileSafetyColdStartProtectsFullWindow(t *testing.T) {
	r, p, req := safetyFixture(t)
	for _, step := range []time.Duration{0, time.Minute, time.Second} {
		r.Clock.(*testingclock.FakeClock).Step(step)
		safetyCPU(r, p, 0)
		if _, err := r.Reconcile(context.Background(), req); err != nil {
			t.Fatal(err)
		}
		want := int32(5)
		if step == time.Second {
			want = 1
		}
		if got := safetyReplicas(t, r); got != want {
			t.Fatalf("after step %s replicas=%d, want %d", step, got, want)
		}
	}
}

func TestReconcileSafetyTargetRecreationDoesNotInheritOldPeak(t *testing.T) {
	r, p, req := safetyFixture(t)
	safetyCPU(r, p, 200)
	if _, err := r.Reconcile(context.Background(), req); err != nil {
		t.Fatal(err)
	}
	var deploy appsv1.Deployment
	key := types.NamespacedName{Namespace: "test", Name: "web"}
	if err := r.Get(context.Background(), key, &deploy); err != nil {
		t.Fatal(err)
	}
	if err := r.Delete(context.Background(), &deploy); err != nil {
		t.Fatal(err)
	}
	deploy.UID, deploy.ResourceVersion = "deployment-two", ""
	replicas := int32(3)
	deploy.Spec.Replicas, deploy.Status.Replicas = &replicas, replicas
	if err := r.Create(context.Background(), &deploy); err != nil {
		t.Fatal(err)
	}
	safetyCPU(r, p, 0)
	if _, err := r.Reconcile(context.Background(), req); err != nil {
		t.Fatal(err)
	}
	if got := safetyReplicas(t, r); got != 3 {
		t.Fatalf("replacement inherited old peak: replicas=%d, want 3", got)
	}
}

func TestReconcileSafetyHistoricalPeakCannotReverseScaleRequest(t *testing.T) {
	r, p, req := safetyFixture(t)
	safetyCPU(r, p, 200)
	if _, err := r.Reconcile(context.Background(), req); err != nil {
		t.Fatal(err)
	}
	var deploy appsv1.Deployment
	if err := r.Get(context.Background(), req.NamespacedName, &deploy); err != nil {
		t.Fatal(err)
	}
	replicas := int32(3)
	deploy.Spec.Replicas = &replicas
	if err := r.Update(context.Background(), &deploy); err != nil {
		t.Fatal(err)
	}
	// Observed status still says five Pods. The old peak must not turn the
	// new low recommendation into an upscale that reverses the live request.
	safetyCPU(r, p, 0)
	if _, err := r.Reconcile(context.Background(), req); err != nil {
		t.Fatal(err)
	}
	if got := safetyReplicas(t, r); got != 3 {
		t.Fatalf("history reversed live Scale request to %d, want 3", got)
	}
}

func TestReconcileSafetyRejectsUnusableHistoryAndRecovers(t *testing.T) {
	cases := []struct {
		name, reason string
		change       func(*metricsprovider.CPUHistory)
		err          error
	}{
		{name: "stale source", reason: "StaleData", change: func(h *metricsprovider.CPUHistory) { h.SourceTimestamp = h.ObservedAt.Add(-46 * time.Second) }},
		{name: "missing source time", reason: "InvalidData", change: func(h *metricsprovider.CPUHistory) { h.SourceTimestamp = time.Time{} }},
		{name: "fresh metadata on an old tail", reason: "InvalidData", change: func(h *metricsprovider.CPUHistory) { h.Samples[1].Timestamp = h.ObservedAt.Add(-15 * time.Second) }},
		{name: "nonfinite value", reason: "InvalidData", change: func(h *metricsprovider.CPUHistory) { h.Samples[1].Value = math.NaN() }},
		{name: "negative value", reason: "InvalidData", change: func(h *metricsprovider.CPUHistory) { h.Samples[1].Value = -1 }},
		{name: "duplicate timestamps", reason: "InvalidData", change: func(h *metricsprovider.CPUHistory) { h.Samples[0].Timestamp = h.Samples[1].Timestamp }},
		{name: "insufficient samples", reason: "InsufficientSamples", change: func(h *metricsprovider.CPUHistory) { h.Samples = h.Samples[1:] }},
		{name: "incomplete container set", reason: "IncompleteData", err: metricsprovider.ErrIncompleteData},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r, p, req := safetyFixture(t)
			now := r.Clock.Now()
			h := metricsprovider.CPUHistory{ObservedAt: now, SourceTimestamp: now, Samples: []predictor.Sample{{Timestamp: now.Add(-30 * time.Second), Value: 0}, {Timestamp: now, Value: 0}}}
			if tc.change != nil {
				tc.change(&h)
			}
			p.SetHistory("test", "web", h, tc.err)
			if _, err := r.Reconcile(context.Background(), req); err != nil {
				t.Fatal(err)
			}
			if got := safetyReplicas(t, r); got != 5 {
				t.Fatalf("unusable metrics changed Scale to %d", got)
			}
			status := safetyStatus(t, r, req)
			c := meta.FindStatusCondition(status.Conditions, "MetricsReady")
			if c == nil || c.Status != metav1.ConditionFalse || c.Reason != tc.reason {
				t.Fatalf("MetricsReady=%+v, want false/%s", c, tc.reason)
			}
			if status.CurrentCPUUtilizationPercentage != nil || status.PredictedCPUUtilizationPercentage != nil {
				t.Fatal("unusable history retained obsolete CPU status")
			}
			safetyCPU(r, p, 200)
			if _, err := r.Reconcile(context.Background(), req); err != nil {
				t.Fatal(err)
			}
			status = safetyStatus(t, r, req)
			c = meta.FindStatusCondition(status.Conditions, "MetricsReady")
			if c == nil || c.Status != metav1.ConditionTrue || safetyReplicas(t, r) != 10 {
				t.Fatalf("recovery did not resume valid scaling: condition=%+v", c)
			}
		})
	}
}

func TestReconcileSafetyExtremeFiniteCPUHonorsMaximum(t *testing.T) {
	r, p, req := safetyFixture(t)
	safetyCPU(r, p, 1e20)
	if _, err := r.Reconcile(context.Background(), req); err != nil {
		t.Fatal(err)
	}
	if got := safetyReplicas(t, r); got != 10 {
		t.Fatalf("finite extreme CPU requested %d, want maximum 10", got)
	}
	status := safetyStatus(t, r, req)
	if status.CurrentCPUUtilizationPercentage == nil || *status.CurrentCPUUtilizationPercentage != math.MaxInt32 {
		t.Fatal("extreme CPU display must saturate without wrapping negative")
	}
}

type callbackMetricsProvider struct {
	metricsprovider.Provider
	after func()
}

func (p callbackMetricsProvider) AverageCPUUtilizationPercentage(ctx context.Context, target *appsv1.Deployment, window time.Duration) (metricsprovider.CPUHistory, error) {
	history, err := p.Provider.AverageCPUUtilizationPercentage(ctx, target, window)
	p.after()
	return history, err
}

func TestReconcileSafetyPolicyChangeDuringQueryCannotScale(t *testing.T) {
	r, p, req := safetyFixture(t)
	safetyCPU(r, p, 200)
	r.MetricsProvider = callbackMetricsProvider{Provider: p, after: func() {
		var phpa autoscalingv1alpha1.PredictiveHPA
		if err := r.Get(context.Background(), req.NamespacedName, &phpa); err != nil {
			t.Fatal(err)
		}
		phpa.Generation++
		phpa.Spec.MaxReplicas = 1
		if err := r.Update(context.Background(), &phpa); err != nil {
			t.Fatal(err)
		}
	}}
	if _, err := r.Reconcile(context.Background(), req); err == nil {
		t.Fatal("expected stale policy conflict")
	}
	if got := safetyReplicas(t, r); got != 5 {
		t.Fatalf("stale policy wrote Scale=%d", got)
	}
}

func TestReconcileSafetyWindowGrowthRestartsProtectionAndZeroDisables(t *testing.T) {
	r, p, req := safetyFixture(t)
	safetyCPU(r, p, 0)
	if _, err := r.Reconcile(context.Background(), req); err != nil {
		t.Fatal(err)
	}
	r.Clock.(*testingclock.FakeClock).Step(61 * time.Second)
	safetyCPU(r, p, 0)
	if _, err := r.Reconcile(context.Background(), req); err != nil {
		t.Fatal(err)
	}
	var deploy appsv1.Deployment
	if err := r.Get(context.Background(), req.NamespacedName, &deploy); err != nil {
		t.Fatal(err)
	}
	replicas := int32(5)
	deploy.Spec.Replicas = &replicas
	if err := r.Update(context.Background(), &deploy); err != nil {
		t.Fatal(err)
	}
	for _, window := range []int32{120, 0} {
		var phpa autoscalingv1alpha1.PredictiveHPA
		if err := r.Get(context.Background(), req.NamespacedName, &phpa); err != nil {
			t.Fatal(err)
		}
		phpa.Spec.ScaleDownStabilizationWindowSeconds = &window
		if err := r.Update(context.Background(), &phpa); err != nil {
			t.Fatal(err)
		}
		if _, err := r.Reconcile(context.Background(), req); err != nil {
			t.Fatal(err)
		}
		want := int32(5)
		if window == 0 {
			want = 1
		}
		if got := safetyReplicas(t, r); got != want {
			t.Fatalf("window=%d replicas=%d, want %d", window, got, want)
		}
	}
}

func TestReconcileSafetyRecommendationStorageRemainsBoundedDuringEvents(t *testing.T) {
	r, p, req := safetyFixture(t)
	var diagnostics diagnosticLogBuffer
	ctx := logf.IntoContext(context.Background(), zap.New(zap.WriteTo(&diagnostics), zap.UseDevMode(false)).WithValues("namespace", "test"))
	safetyCPU(r, p, 200)
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatal(err)
	}
	for range 400 {
		r.Clock.(*testingclock.FakeClock).Step(100 * time.Millisecond)
		safetyCPU(r, p, 0)
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatal(err)
		}
	}
	if got := safetyReplicas(t, r); got != 10 {
		t.Fatalf("high recommendation expired early, replicas=%d", got)
	}
	for _, record := range diagnostics.records("test") {
		if record["msg"] == decisionDiagnosticMessage && record["stabilizationHistoryEntries"].(float64) > 129 {
			t.Fatalf("unbounded recommendation history: %v", record["stabilizationHistoryEntries"])
		}
	}
	r.Clock.(*testingclock.FakeClock).Step(22 * time.Second)
	safetyCPU(r, p, 0)
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatal(err)
	}
	if got := safetyReplicas(t, r); got != 1 {
		t.Fatalf("expired bucket retained high recommendation indefinitely: %d", got)
	}
}

func TestReconcileSafetyDoesNotReversePendingDirection(t *testing.T) {
	for _, tc := range []struct {
		name                string
		observed, requested int32
		cpu                 float64
	}{
		{name: "pending downscale with low CPU", observed: 5, requested: 1, cpu: 20},
		{name: "pending upscale with high CPU", observed: 1, requested: 3, cpu: 100},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r, p, req := safetyFixture(t)
			var deploy appsv1.Deployment
			if err := r.Get(context.Background(), req.NamespacedName, &deploy); err != nil {
				t.Fatal(err)
			}
			deploy.Spec.Replicas = &tc.requested
			if err := r.Update(context.Background(), &deploy); err != nil {
				t.Fatal(err)
			}
			deploy.Status.Replicas = tc.observed
			if err := r.Status().Update(context.Background(), &deploy); err != nil {
				t.Fatal(err)
			}
			var phpa autoscalingv1alpha1.PredictiveHPA
			if err := r.Get(context.Background(), req.NamespacedName, &phpa); err != nil {
				t.Fatal(err)
			}
			zero := int32(0)
			phpa.Spec.ScaleDownStabilizationWindowSeconds = &zero
			if err := r.Update(context.Background(), &phpa); err != nil {
				t.Fatal(err)
			}
			safetyCPU(r, p, tc.cpu)
			if _, err := r.Reconcile(context.Background(), req); err != nil {
				t.Fatal(err)
			}
			if got := safetyReplicas(t, r); got != tc.requested {
				t.Fatalf("reversed pending request to %d, want %d", got, tc.requested)
			}
		})
	}
}

func TestReconcileSafetyBoundsOverrideTolerance(t *testing.T) {
	for _, tc := range []struct {
		name                   string
		minimum, maximum, want int32
	}{
		{name: "lower maximum", minimum: 1, maximum: 3, want: 3},
		{name: "raise minimum", minimum: 7, maximum: 10, want: 7},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r, p, req := safetyFixture(t)
			var phpa autoscalingv1alpha1.PredictiveHPA
			if err := r.Get(context.Background(), req.NamespacedName, &phpa); err != nil {
				t.Fatal(err)
			}
			phpa.Spec.MinReplicas, phpa.Spec.MaxReplicas = &tc.minimum, tc.maximum
			if err := r.Update(context.Background(), &phpa); err != nil {
				t.Fatal(err)
			}
			safetyCPU(r, p, 50)
			if _, err := r.Reconcile(context.Background(), req); err != nil {
				t.Fatal(err)
			}
			if got := safetyReplicas(t, r); got != tc.want {
				t.Fatalf("tolerance ignored explicit bounds, replicas=%d want %d", got, tc.want)
			}
		})
	}
}

func TestReconcileSafetyRestartAfterScaleSuccessStatusConflict(t *testing.T) {
	r, p, req := safetyFixture(t)
	failed := false
	r.Client = interceptor.NewClient(r.Client.(client.WithWatch), interceptor.Funcs{
		SubResourcePatch: func(ctx context.Context, c client.Client, name string, obj client.Object, patch client.Patch, opts ...client.SubResourcePatchOption) error {
			if name == "status" && !failed {
				failed = true
				return apierrors.NewConflict(schema.GroupResource{Group: "autoscaling.brian.io", Resource: "predictivehpas"}, obj.GetName(), errors.New("injected concurrent status write"))
			}
			return c.SubResource(name).Patch(ctx, obj, patch, opts...)
		},
	})
	safetyCPU(r, p, 200)
	if _, err := r.Reconcile(context.Background(), req); !apierrors.IsConflict(err) {
		t.Fatalf("expected status conflict, got %v", err)
	}
	if got := safetyReplicas(t, r); got != 10 {
		t.Fatalf("successful Scale write was lost: %d", got)
	}
	// A fresh reconciler has no process history and observed status still says
	// five replicas; it must protect the successful live request for ten.
	restarted := &PredictiveHPAReconciler{Client: r.Client, Scheme: r.Scheme, Clock: r.Clock, MetricsProvider: p}
	safetyCPU(restarted, p, 0)
	if _, err := restarted.Reconcile(context.Background(), req); err != nil {
		t.Fatal(err)
	}
	if got := safetyReplicas(t, restarted); got != 10 {
		t.Fatalf("restart shrank successful Scale request to %d", got)
	}
	status := safetyStatus(t, restarted, req)
	condition := meta.FindStatusCondition(status.Conditions, conditionScaleDownStabilized)
	if condition == nil || condition.Reason != "ColdStartProtection" || condition.Status != metav1.ConditionTrue {
		t.Fatalf("restart condition = %+v", condition)
	}
}

func TestReconcileSafetyReplacementAndClockRollbackRebuildProtection(t *testing.T) {
	for _, reset := range []string{"PHPA replacement", "Deployment replacement", "clock rollback"} {
		t.Run(reset, func(t *testing.T) {
			r, p, req := safetyFixture(t)
			safetyCPU(r, p, 0)
			if _, err := r.Reconcile(context.Background(), req); err != nil {
				t.Fatal(err)
			}
			r.Clock.(*testingclock.FakeClock).Step(61 * time.Second)
			safetyCPU(r, p, 0)
			if _, err := r.Reconcile(context.Background(), req); err != nil {
				t.Fatal(err)
			}
			var deploy appsv1.Deployment
			if err := r.Get(context.Background(), req.NamespacedName, &deploy); err != nil {
				t.Fatal(err)
			}
			replicas := int32(5)
			deploy.Spec.Replicas = &replicas
			if reset == "Deployment replacement" {
				if err := r.Delete(context.Background(), &deploy); err != nil {
					t.Fatal(err)
				}
				deploy.UID, deploy.ResourceVersion = "deployment-replaced", ""
				if err := r.Create(context.Background(), &deploy); err != nil {
					t.Fatal(err)
				}
			} else if err := r.Update(context.Background(), &deploy); err != nil {
				t.Fatal(err)
			}
			switch reset {
			case "PHPA replacement":
				var phpa autoscalingv1alpha1.PredictiveHPA
				if err := r.Get(context.Background(), req.NamespacedName, &phpa); err != nil {
					t.Fatal(err)
				}
				if err := r.Delete(context.Background(), &phpa); err != nil {
					t.Fatal(err)
				}
				phpa.UID, phpa.ResourceVersion = "phpa-replaced", ""
				if err := r.Create(context.Background(), &phpa); err != nil {
					t.Fatal(err)
				}
			case "clock rollback":
				r.Clock.(*testingclock.FakeClock).Step(-120 * time.Second)
			}
			safetyCPU(r, p, 0)
			if _, err := r.Reconcile(context.Background(), req); err != nil {
				t.Fatal(err)
			}
			if got := safetyReplicas(t, r); got != 5 {
				t.Fatalf("%s reused expired old protection: replicas=%d", reset, got)
			}
		})
	}
}

func TestReconcileSafetyStatusPreservesConcurrentConditions(t *testing.T) {
	r, p, req := safetyFixture(t)
	safetyCPU(r, p, 200)
	r.MetricsProvider = callbackMetricsProvider{Provider: p, after: func() {
		var phpa autoscalingv1alpha1.PredictiveHPA
		if err := r.Get(context.Background(), req.NamespacedName, &phpa); err != nil {
			t.Fatal(err)
		}
		meta.SetStatusCondition(&phpa.Status.Conditions, metav1.Condition{Type: "ExternalReady", Status: metav1.ConditionTrue, Reason: "ExternalController", Message: "Preserve this condition"})
		if err := r.Status().Update(context.Background(), &phpa); err != nil {
			t.Fatal(err)
		}
	}}
	if _, err := r.Reconcile(context.Background(), req); err != nil {
		t.Fatal(err)
	}
	status := safetyStatus(t, r, req)
	if condition := meta.FindStatusCondition(status.Conditions, "ExternalReady"); condition == nil || condition.Status != metav1.ConditionTrue {
		t.Fatalf("concurrent condition lost: %+v", condition)
	}
}
