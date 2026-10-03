package controller

import (
	"context"
	"testing"
	"time"

	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	autoscalingv1alpha1 "github.com/th1nking/predictive-hpa/api/v1alpha1"
)

// Exercise the public reconciliation boundary: malformed stored configuration
// must be reported before either querying CPU or touching the target Scale.
func TestReconcileConfigurationRejectsUnsafePolicyAndRecovers(t *testing.T) {
	for _, tc := range []struct {
		name   string
		change func(*autoscalingv1alpha1.PredictiveHPASpec)
	}{
		{name: "wrong target API group", change: func(s *autoscalingv1alpha1.PredictiveHPASpec) { s.ScaleTargetRef.APIVersion = "example.com/v1" }},
		{name: "wrong target API version", change: func(s *autoscalingv1alpha1.PredictiveHPASpec) { s.ScaleTargetRef.APIVersion = "apps/v2" }},
		{name: "missing target API version", change: func(s *autoscalingv1alpha1.PredictiveHPASpec) { s.ScaleTargetRef.APIVersion = "" }},
		{name: "unsupported target kind", change: func(s *autoscalingv1alpha1.PredictiveHPASpec) { s.ScaleTargetRef.Kind = "StatefulSet" }},
		{name: "missing target kind", change: func(s *autoscalingv1alpha1.PredictiveHPASpec) { s.ScaleTargetRef.Kind = "" }},
		{name: "missing target name", change: func(s *autoscalingv1alpha1.PredictiveHPASpec) { s.ScaleTargetRef.Name = "" }},
		{name: "invalid target name", change: func(s *autoscalingv1alpha1.PredictiveHPASpec) { s.ScaleTargetRef.Name = "web/scale" }},
		{name: "zero target CPU", change: func(s *autoscalingv1alpha1.PredictiveHPASpec) { s.TargetCPUUtilizationPercentage = 0 }},
		{name: "negative target CPU", change: func(s *autoscalingv1alpha1.PredictiveHPASpec) { s.TargetCPUUtilizationPercentage = -1 }},
		{name: "target CPU above maximum", change: func(s *autoscalingv1alpha1.PredictiveHPASpec) { s.TargetCPUUtilizationPercentage = 101 }},
		{name: "zero alpha", change: func(s *autoscalingv1alpha1.PredictiveHPASpec) { s.Prediction.AlphaPercent = 0 }},
		{name: "negative alpha", change: func(s *autoscalingv1alpha1.PredictiveHPASpec) { s.Prediction.AlphaPercent = -1 }},
		{name: "alpha above maximum", change: func(s *autoscalingv1alpha1.PredictiveHPASpec) { s.Prediction.AlphaPercent = 100 }},
		{name: "unsupported algorithm", change: func(s *autoscalingv1alpha1.PredictiveHPASpec) { s.Prediction.Algorithm = "ARIMA" }},
		{name: "unsupported decision mode", change: func(s *autoscalingv1alpha1.PredictiveHPASpec) { s.DecisionMode = "Unknown" }},
		{name: "negative stabilization window", change: func(s *autoscalingv1alpha1.PredictiveHPASpec) {
			s.ScaleDownStabilizationWindowSeconds = ptr.To(int32(-1))
		}},
		{name: "negative minimum replicas", change: func(s *autoscalingv1alpha1.PredictiveHPASpec) { s.MinReplicas = ptr.To(int32(-1)) }},
		{name: "zero maximum replicas", change: func(s *autoscalingv1alpha1.PredictiveHPASpec) { s.MaxReplicas = 0 }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r, provider, req := safetyFixture(t)
			var phpa autoscalingv1alpha1.PredictiveHPA
			if err := r.Get(context.Background(), req.NamespacedName, &phpa); err != nil {
				t.Fatal(err)
			}
			validSpec := phpa.DeepCopy().Spec
			previousScaleTime := phpa.Status.LastScaleTime.DeepCopy()
			// A previously successful object must not retain a misleading success
			// condition when the next policy generation is invalid.
			meta.SetStatusCondition(&phpa.Status.Conditions, metav1.Condition{
				Type: conditionMetricsReady, Status: metav1.ConditionTrue, Reason: "ValidMetrics",
				Message: "Previously verified CPU", ObservedGeneration: phpa.Generation,
			})
			if err := r.Status().Update(context.Background(), &phpa); err != nil {
				t.Fatal(err)
			}
			tc.change(&phpa.Spec)
			phpa.Generation++
			if err := r.Update(context.Background(), &phpa); err != nil {
				t.Fatal(err)
			}
			safetyCPU(r, provider, 100)
			metricCalls, scaleCalls := 0, 0
			r.MetricsProvider = callbackMetricsProvider{Provider: provider, after: func() { metricCalls++ }}
			r.Client = interceptor.NewClient(r.Client.(client.WithWatch), interceptor.Funcs{
				SubResourceGet: func(ctx context.Context, c client.Client, name string, obj client.Object, subResource client.Object, opts ...client.SubResourceGetOption) error {
					if name == scaleSubresource {
						scaleCalls++
					}
					return c.SubResource(name).Get(ctx, obj, subResource, opts...)
				},
				SubResourceUpdate: func(ctx context.Context, c client.Client, name string, obj client.Object, opts ...client.SubResourceUpdateOption) error {
					if name == scaleSubresource {
						scaleCalls++
					}
					return c.SubResource(name).Update(ctx, obj, opts...)
				},
			})
			result, err := r.Reconcile(context.Background(), req)
			if err != nil {
				t.Fatal(err)
			}
			if metricCalls != 0 || scaleCalls != 0 || safetyReplicas(t, r) != 5 {
				t.Fatalf("invalid policy reached inputs or Scale: metrics=%d scale=%d replicas=%d", metricCalls, scaleCalls, safetyReplicas(t, r))
			}
			assertInvalidConfigurationStatus(t, safetyStatus(t, r, req), phpa.Generation, previousScaleTime)
			if result.RequeueAfter != time.Minute {
				t.Fatalf("invalid policy retry = %s, want 1m", result.RequeueAfter)
			}

			if err := r.Get(context.Background(), req.NamespacedName, &phpa); err != nil {
				t.Fatal(err)
			}
			phpa.Spec = validSpec
			phpa.Generation++
			if err := r.Update(context.Background(), &phpa); err != nil {
				t.Fatal(err)
			}
			if _, err := r.Reconcile(context.Background(), req); err != nil {
				t.Fatal(err)
			}
			status := safetyStatus(t, r, req)
			condition := meta.FindStatusCondition(status.Conditions, conditionMetricsReady)
			if metricCalls == 0 || scaleCalls == 0 || safetyReplicas(t, r) != 10 || condition == nil || condition.Status != metav1.ConditionTrue || condition.ObservedGeneration != phpa.Generation {
				t.Fatalf("repaired policy did not resume scaling: metrics=%d scale=%d replicas=%d condition=%+v", metricCalls, scaleCalls, safetyReplicas(t, r), condition)
			}
		})
	}
}

func assertInvalidConfigurationStatus(t *testing.T, status autoscalingv1alpha1.PredictiveHPAStatus, generation int64, previousScaleTime *metav1.Time) {
	t.Helper()
	condition := meta.FindStatusCondition(status.Conditions, conditionMetricsReady)
	if condition == nil || condition.Status != metav1.ConditionFalse || condition.Reason != invalidConfigurationReason || condition.ObservedGeneration != generation {
		t.Fatalf("invalid policy condition = %+v", condition)
	}
	stabilization := meta.FindStatusCondition(status.Conditions, conditionScaleDownStabilized)
	if stabilization == nil || stabilization.Status != metav1.ConditionUnknown {
		t.Fatalf("invalid policy stabilization condition = %+v", stabilization)
	}
	if status.CurrentCPUUtilizationPercentage != nil || status.PredictedCPUUtilizationPercentage != nil {
		t.Fatalf("invalid policy did not clear obsolete CPU values: %+v", status)
	}
	if status.LastScaleTime == nil || !status.LastScaleTime.Equal(previousScaleTime) {
		t.Fatal("invalid policy changed the last successful scale time")
	}
}

func TestReconcileConfigurationPreservesSupportedDefaultsAndBounds(t *testing.T) {
	for _, tc := range []struct {
		name   string
		change func(*autoscalingv1alpha1.PredictiveHPASpec)
	}{
		{name: "defaults", change: func(s *autoscalingv1alpha1.PredictiveHPASpec) {
			s.DecisionMode = ""
			s.Prediction.Algorithm = ""
			s.MinReplicas = nil
			s.ScaleDownStabilizationWindowSeconds = nil
		}},
		{name: "lower bounds", change: func(s *autoscalingv1alpha1.PredictiveHPASpec) {
			s.MinReplicas = ptr.To(int32(0))
			s.TargetCPUUtilizationPercentage = 1
			s.Prediction.AlphaPercent = 1
			s.ScaleDownStabilizationWindowSeconds = ptr.To(int32(0))
		}},
		{name: "upper bounds and hybrid", change: func(s *autoscalingv1alpha1.PredictiveHPASpec) {
			s.DecisionMode = autoscalingv1alpha1.DecisionModeHybrid
			s.TargetCPUUtilizationPercentage = 100
			s.Prediction.AlphaPercent = 99
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r, provider, req := safetyFixture(t)
			var phpa autoscalingv1alpha1.PredictiveHPA
			if err := r.Get(context.Background(), req.NamespacedName, &phpa); err != nil {
				t.Fatal(err)
			}
			tc.change(&phpa.Spec)
			if err := r.Update(context.Background(), &phpa); err != nil {
				t.Fatal(err)
			}
			safetyCPU(r, provider, 200)
			if _, err := r.Reconcile(context.Background(), req); err != nil {
				t.Fatal(err)
			}
			status := safetyStatus(t, r, req)
			condition := meta.FindStatusCondition(status.Conditions, conditionMetricsReady)
			if safetyReplicas(t, r) != 10 || condition == nil || condition.Status != metav1.ConditionTrue {
				t.Fatalf("valid policy failed to scale: replicas=%d condition=%+v", safetyReplicas(t, r), condition)
			}
		})
	}
}
