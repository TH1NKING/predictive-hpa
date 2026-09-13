package controller

import (
	"encoding/json"
	"strings"
	"time"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"

	autoscalingv2 "k8s.io/api/autoscaling/v2"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	autoscalingv1alpha1 "github.com/th1nking/predictive-hpa/api/v1alpha1"
)

var _ = Describe("PredictiveHPA CRD validation", func() {
	const testNamespace = "phpa-validation"

	// envtest's apiserver does not guarantee a usable default namespace,
	// so create a dedicated one. Idempotent across specs.
	BeforeEach(func() {
		ns := &corev1.Namespace{
			ObjectMeta: metav1.ObjectMeta{Name: testNamespace},
		}
		err := k8sClient.Create(ctx, ns)
		if err != nil && !apierrors.IsAlreadyExists(err) {
			Expect(err).NotTo(HaveOccurred())
		}
	})

	It("rejects malformed duration input before it can break typed clients", func() {
		phpa := durationValidationResource("malformed-duration", testNamespace, "not-a-duration", "30s")
		// An unstructured client reaches admission with the original text; a
		// typed client would fail to decode the duration before making a request.
		DeferCleanup(func() {
			err := k8sClient.Delete(ctx, phpa)
			Expect(err == nil || apierrors.IsNotFound(err)).To(BeTrue())
		})
		err := k8sClient.Create(ctx, phpa)
		Expect(apierrors.IsInvalid(err)).To(BeTrue(), "malformed duration must be rejected by admission: %v", err)
		Expect(err.Error()).To(ContainSubstring("window"))
		Expect(k8sClient.List(ctx, &autoscalingv1alpha1.PredictiveHPAList{})).To(Succeed())
	})

	It("rejects a negative prediction horizon", func() {
		phpa := durationValidationResource("negative-horizon", testNamespace, "5m", "-1m")
		DeferCleanup(func() {
			err := k8sClient.Delete(ctx, phpa)
			Expect(err == nil || apierrors.IsNotFound(err)).To(BeTrue())
		})
		err := k8sClient.Create(ctx, phpa)
		Expect(apierrors.IsInvalid(err)).To(BeTrue(), "negative horizon must be rejected by admission: %v", err)
		Expect(err.Error()).To(ContainSubstring("horizon"))
	})

	It("rejects a minimum replica count above the maximum", func() {
		phpa := durationValidationResource("inverted-replica-bounds", testNamespace, "5m", "30s")
		Expect(unstructured.SetNestedField(phpa.Object, int64(11), "spec", "minReplicas")).To(Succeed())
		DeferCleanup(func() {
			err := k8sClient.Delete(ctx, phpa)
			Expect(err == nil || apierrors.IsNotFound(err)).To(BeTrue())
		})
		err := k8sClient.Create(ctx, phpa)
		Expect(apierrors.IsInvalid(err)).To(BeTrue(), "inverted replica bounds must be rejected by admission: %v", err)
		Expect(err.Error()).To(ContainSubstring("maxReplicas"))
	})

	DescribeTable("rejects unsupported duration values through admission", func(name, window, horizon, invalidField string) {
		phpa := durationValidationResource(name, testNamespace, window, horizon)
		DeferCleanup(func() {
			err := k8sClient.Delete(ctx, phpa)
			Expect(err == nil || apierrors.IsNotFound(err)).To(BeTrue())
		})
		err := k8sClient.Create(ctx, phpa)
		Expect(apierrors.IsInvalid(err)).To(BeTrue(), "unsupported duration must be rejected by admission: %v", err)
		Expect(err.Error()).To(ContainSubstring(invalidField))
		Expect(k8sClient.List(ctx, &autoscalingv1alpha1.PredictiveHPAList{})).To(Succeed())
	},
		Entry("empty window", "empty-window", "", "30s", "window"),
		Entry("window below minimum", "short-window", "14.999999999s", "30s", "window"),
		Entry("window above maximum", "long-window", "1h1ns", "30s", "window"),
		Entry("window duration overflow", "overflow-window", "9223372036854775808ns", "30s", "window"),
		Entry("malformed horizon", "malformed-horizon", "5m", "tomorrow", "horizon"),
		Entry("zero horizon", "zero-horizon", "5m", "0s", "horizon"),
		Entry("horizon above maximum", "long-horizon", "5m", "1h1ns", "horizon"),
		Entry("horizon duration overflow", "overflow-horizon", "5m", "9223372036854775808ns", "horizon"),
		Entry("overlong window text", "overlong-window", strings.Repeat("0", 64)+"15s", "30s", "window"),
		Entry("overlong horizon text", "overlong-horizon", "5m", strings.Repeat("0", 64)+"1s", "horizon"),
	)

	DescribeTable("accepts supported Go duration representations", func(name, window, horizon string, expectedWindow, expectedHorizon time.Duration) {
		phpa := durationValidationResource(name, testNamespace, window, horizon)
		Expect(k8sClient.Create(ctx, phpa)).To(Succeed())
		DeferCleanup(func() { Expect(k8sClient.Delete(ctx, phpa)).To(Succeed()) })
		var stored autoscalingv1alpha1.PredictiveHPA
		Expect(k8sClient.Get(ctx, client.ObjectKeyFromObject(phpa), &stored)).To(Succeed())
		Expect(stored.Spec.Prediction.Window.Duration).To(Equal(expectedWindow))
		Expect(stored.Spec.Prediction.Horizon.Duration).To(Equal(expectedHorizon))
	},
		Entry("lower bounds", "duration-lower-bounds", "15s", "1ns", 15*time.Second, time.Nanosecond),
		Entry("upper bounds", "duration-upper-bounds", "1h", "1h", time.Hour, time.Hour),
		Entry("composite durations", "composite-durations", "1m30s", "1m2.5s", 90*time.Second, 62500*time.Millisecond),
		Entry("fractional durations", "fractional-durations", "15.25s", "0.5s", 15250*time.Millisecond, 500*time.Millisecond),
		Entry("microsecond symbol", "microsecond-duration", "15s", "1µs", 15*time.Second, time.Microsecond),
		Entry("Greek microsecond symbol", "greek-microsecond-duration", "15s", "1μs", 15*time.Second, time.Microsecond),
		Entry("metav1 canonical duration", "canonical-durations", time.Hour.String(), (30*time.Second).String(), time.Hour, 30*time.Second),
	)

	DescribeTable("accepts supported replica bounds", func(name string, minimum int64) {
		phpa := durationValidationResource(name, testNamespace, "5m", "30s")
		Expect(unstructured.SetNestedField(phpa.Object, minimum, "spec", "minReplicas")).To(Succeed())
		Expect(k8sClient.Create(ctx, phpa)).To(Succeed())
		DeferCleanup(func() { Expect(k8sClient.Delete(ctx, phpa)).To(Succeed()) })
	},
		Entry("zero minimum retains runtime fallback", "zero-minimum", int64(0)),
		Entry("equal minimum and maximum", "equal-replica-bounds", int64(10)),
	)

	It("rejects invalid policy updates and preserves typed reads", func() {
		phpa := durationValidationResource("duration-update", testNamespace, "5m", "30s")
		Expect(k8sClient.Create(ctx, phpa)).To(Succeed())
		DeferCleanup(func() { Expect(k8sClient.Delete(ctx, phpa)).To(Succeed()) })
		for _, patch := range []map[string]any{
			{"spec": map[string]any{"prediction": map[string]any{"window": "not-a-duration"}}},
			{"spec": map[string]any{"prediction": map[string]any{"horizon": "-1m"}}},
			{"spec": map[string]any{"minReplicas": int64(11)}},
		} {
			data, err := json.Marshal(patch)
			Expect(err).NotTo(HaveOccurred())
			err = k8sClient.Patch(ctx, phpa, client.RawPatch(types.MergePatchType, data))
			Expect(apierrors.IsInvalid(err)).To(BeTrue(), "invalid policy patch must be rejected: %v", err)
			var stored autoscalingv1alpha1.PredictiveHPA
			Expect(k8sClient.Get(ctx, client.ObjectKeyFromObject(phpa), &stored)).To(Succeed())
			Expect(stored.Spec.Prediction.Window.Duration).To(Equal(5 * time.Minute))
			Expect(stored.Spec.Prediction.Horizon.Duration).To(Equal(30 * time.Second))
			Expect(stored.Spec.MinReplicas).To(BeNil())
			Expect(k8sClient.List(ctx, &autoscalingv1alpha1.PredictiveHPAList{})).To(Succeed())
		}
	})

	It("accepts a valid PredictiveHPA", func() {
		phpa := &autoscalingv1alpha1.PredictiveHPA{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "valid-sample",
				Namespace: testNamespace,
			},
			Spec: autoscalingv1alpha1.PredictiveHPASpec{
				ScaleTargetRef: autoscalingv2.CrossVersionObjectReference{
					APIVersion: "apps/v1",
					Kind:       "Deployment",
					Name:       "php-apache",
				},
				MaxReplicas:                    10,
				TargetCPUUtilizationPercentage: 50,
				Prediction: autoscalingv1alpha1.PredictionConfig{
					Algorithm:    autoscalingv1alpha1.PredictionAlgorithm("EWMA"),
					AlphaPercent: 30,
					Window:       metav1.Duration{Duration: 5 * time.Minute},
					Horizon:      metav1.Duration{Duration: 30 * time.Second},
				},
			},
		}
		Expect(k8sClient.Create(ctx, phpa)).To(Succeed())
		Expect(phpa.Spec.DecisionMode).To(Equal(autoscalingv1alpha1.DecisionModePredictive))

		// The public admission boundary rejects unknown modes, independently
		// of whether a controller is running or a target Deployment exists.
		phpa.Spec.DecisionMode = "Unknown"
		err := k8sClient.Update(ctx, phpa)
		Expect(apierrors.IsInvalid(err)).To(BeTrue())
		Expect(err.Error()).To(ContainSubstring("decisionMode"))
	})

	It("rejects an invalid prediction algorithm", func() {
		phpa := &autoscalingv1alpha1.PredictiveHPA{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "bad-algorithm",
				Namespace: testNamespace,
			},
			Spec: autoscalingv1alpha1.PredictiveHPASpec{
				ScaleTargetRef: autoscalingv2.CrossVersionObjectReference{
					APIVersion: "apps/v1",
					Kind:       "Deployment",
					Name:       "php-apache",
				},
				MaxReplicas:                    10,
				TargetCPUUtilizationPercentage: 50,
				Prediction: autoscalingv1alpha1.PredictionConfig{
					Algorithm:    autoscalingv1alpha1.PredictionAlgorithm("ARIMA"),
					AlphaPercent: 30,
					Window:       metav1.Duration{Duration: 5 * time.Minute},
					Horizon:      metav1.Duration{Duration: 30 * time.Second},
				},
			},
		}
		err := k8sClient.Create(ctx, phpa)
		Expect(err).To(HaveOccurred())
		Expect(err.Error()).To(ContainSubstring("algorithm"))
	})
})

func durationValidationResource(name, namespace, window, horizon string) *unstructured.Unstructured {
	return &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "autoscaling.brian.io/v1alpha1", "kind": "PredictiveHPA",
		"metadata": map[string]any{"name": name, "namespace": namespace},
		"spec": map[string]any{
			"scaleTargetRef": map[string]any{"apiVersion": "apps/v1", "kind": "Deployment", "name": "validation-target"},
			"maxReplicas":    int64(10), "targetCPUUtilizationPercentage": int64(50),
			"prediction": map[string]any{"algorithm": "EWMA", "alphaPercent": int64(30), "window": window, "horizon": horizon},
		},
	}}
}
