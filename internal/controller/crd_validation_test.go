package controller

import (
	"time"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"

	autoscalingv2 "k8s.io/api/autoscaling/v2"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

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
