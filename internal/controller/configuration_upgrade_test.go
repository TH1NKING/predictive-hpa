package controller

import (
	"time"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"

	corev1 "k8s.io/api/core/v1"
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	kubescheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"

	autoscalingv1alpha1 "github.com/th1nking/predictive-hpa/api/v1alpha1"
)

var _ = Describe("PredictiveHPA configuration schema upgrade", func() {
	It("publishes a legacy configuration hold after admission rules are strengthened and accepts repair", func() {
		// Use another API server without a manager: weakening the shared suite's
		// schema would expose its background reconciler to a changing contract.
		var hardened *apiextensionsv1.CustomResourceDefinition
		for _, crd := range testEnv.CRDs {
			if crd.Name == "predictivehpas.autoscaling.brian.io" {
				hardened = crd.DeepCopy()
				break
			}
		}
		Expect(hardened).NotTo(BeNil())
		legacy := hardened.DeepCopy()
		legacy.ObjectMeta = metav1.ObjectMeta{Name: hardened.Name}
		legacy.Status = apiextensionsv1.CustomResourceDefinitionStatus{}
		legacySpec := legacy.Spec.Versions[0].Schema.OpenAPIV3Schema.Properties["spec"]
		prediction := legacySpec.Properties["prediction"]
		horizon := prediction.Properties["horizon"]
		Expect(horizon.XValidations).NotTo(BeEmpty())
		horizon.XValidations = nil
		prediction.Properties["horizon"] = horizon
		for _, field := range []string{"targetCPUUtilizationPercentage", "scaleDownStabilizationWindowSeconds"} {
			property := legacySpec.Properties[field]
			Expect(property.Minimum).NotTo(BeNil())
			property.Minimum = nil
			legacySpec.Properties[field] = property
		}

		upgradeEnv := &envtest.Environment{
			CRDs:                  []*apiextensionsv1.CustomResourceDefinition{legacy},
			BinaryAssetsDirectory: testEnv.BinaryAssetsDirectory,
			UseExistingCluster:    ptr.To(false),
		}
		DeferCleanup(func() { Expect(upgradeEnv.Stop()).To(Succeed()) })
		upgradeConfig, err := upgradeEnv.Start()
		Expect(err).NotTo(HaveOccurred())
		upgradeScheme := runtime.NewScheme()
		Expect(kubescheme.AddToScheme(upgradeScheme)).To(Succeed())
		Expect(autoscalingv1alpha1.AddToScheme(upgradeScheme)).To(Succeed())
		Expect(apiextensionsv1.AddToScheme(upgradeScheme)).To(Succeed())
		upgradeClient, err := client.New(upgradeConfig, client.Options{Scheme: upgradeScheme})
		Expect(err).NotTo(HaveOccurred())
		const namespace = "phpa-schema-upgrade"
		Expect(upgradeClient.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: namespace}})).To(Succeed())
		cases := []struct {
			name   string
			change func(*autoscalingv1alpha1.PredictiveHPASpec)
			repair func(*autoscalingv1alpha1.PredictiveHPASpec)
		}{
			{name: "legacy-negative-horizon", change: func(s *autoscalingv1alpha1.PredictiveHPASpec) { s.Prediction.Horizon.Duration = -time.Minute },
				repair: func(s *autoscalingv1alpha1.PredictiveHPASpec) { s.Prediction.Horizon.Duration = 30 * time.Second }},
			{name: "legacy-negative-stabilization", change: func(s *autoscalingv1alpha1.PredictiveHPASpec) {
				s.ScaleDownStabilizationWindowSeconds = ptr.To(int32(-1))
			},
				repair: func(s *autoscalingv1alpha1.PredictiveHPASpec) {
					s.ScaleDownStabilizationWindowSeconds = ptr.To(int32(60))
				}},
			{name: "legacy-zero-target-cpu", change: func(s *autoscalingv1alpha1.PredictiveHPASpec) { s.TargetCPUUtilizationPercentage = 0 },
				repair: func(s *autoscalingv1alpha1.PredictiveHPASpec) { s.TargetCPUUtilizationPercentage = 50 }},
		}
		for _, tc := range cases {
			resource := durationValidationResource(tc.name, namespace, "5m", "30s")
			var stored autoscalingv1alpha1.PredictiveHPA
			Expect(runtime.DefaultUnstructuredConverter.FromUnstructured(resource.Object, &stored)).To(Succeed())
			tc.change(&stored.Spec)
			Expect(upgradeClient.Create(ctx, &stored)).To(Succeed())
			stored.Status.CurrentCPUUtilizationPercentage = ptr.To(int32(20))
			stored.Status.PredictedCPUUtilizationPercentage = ptr.To(int32(40))
			Expect(upgradeClient.Status().Update(ctx, &stored)).To(Succeed())
		}

		// Updating a CRD does not revalidate stored objects. Fetch its current
		// resourceVersion and wait until dry-run admission sees the harder rule.
		var currentCRD apiextensionsv1.CustomResourceDefinition
		Expect(upgradeClient.Get(ctx, client.ObjectKey{Name: hardened.Name}, &currentCRD)).To(Succeed())
		currentCRD.Spec = hardened.Spec
		Expect(upgradeClient.Update(ctx, &currentCRD)).To(Succeed())
		for _, tc := range cases {
			Eventually(func() bool {
				resource := durationValidationResource(tc.name+"-probe", namespace, "5m", "30s")
				var probe autoscalingv1alpha1.PredictiveHPA
				Expect(runtime.DefaultUnstructuredConverter.FromUnstructured(resource.Object, &probe)).To(Succeed())
				tc.change(&probe.Spec)
				return apierrors.IsInvalid(upgradeClient.Create(ctx, &probe, client.DryRunAll))
			}, 10*time.Second, 100*time.Millisecond).Should(BeTrue())
		}

		// A fake client cannot cover this boundary: status updates re-run CEL
		// over the unchanged invalid spec and require validation ratcheting.
		reconciler := &PredictiveHPAReconciler{Client: upgradeClient, APIReader: upgradeClient, Scheme: upgradeScheme}
		for _, tc := range cases {
			key := client.ObjectKey{Namespace: namespace, Name: tc.name}
			result, err := reconciler.Reconcile(ctx, ctrl.Request{NamespacedName: key})
			Expect(err).NotTo(HaveOccurred())
			Expect(result.RequeueAfter).To(Equal(time.Minute))
			var stored autoscalingv1alpha1.PredictiveHPA
			Expect(upgradeClient.Get(ctx, key, &stored)).To(Succeed())
			condition := meta.FindStatusCondition(stored.Status.Conditions, conditionMetricsReady)
			Expect(condition).NotTo(BeNil())
			Expect(condition.Status).To(Equal(metav1.ConditionFalse))
			Expect(condition.Reason).To(Equal(invalidConfigurationReason))
			Expect(stored.Status.CurrentCPUUtilizationPercentage).To(BeNil())
			Expect(stored.Status.PredictedCPUUtilizationPercentage).To(BeNil())

			tc.repair(&stored.Spec)
			Expect(upgradeClient.Update(ctx, &stored)).To(Succeed())
			_, err = reconciler.Reconcile(ctx, ctrl.Request{NamespacedName: key})
			Expect(err).NotTo(HaveOccurred())
			Expect(upgradeClient.Get(ctx, key, &stored)).To(Succeed())
			condition = meta.FindStatusCondition(stored.Status.Conditions, conditionMetricsReady)
			Expect(condition).NotTo(BeNil())
			// No Deployment is created: repaired policy resumes normal target lookup
			// without running workload Pods or making a Scale write.
			Expect(condition.Reason).To(Equal("NoData"))
			Expect(condition.ObservedGeneration).To(Equal(stored.Generation))
		}
	})
})
