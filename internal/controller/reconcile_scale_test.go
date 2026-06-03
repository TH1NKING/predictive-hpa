package controller

import (
	"time"
	"fmt"
	"sigs.k8s.io/controller-runtime/pkg/client"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"

	appsv1 "k8s.io/api/apps/v1"
	autoscalingv2 "k8s.io/api/autoscaling/v2"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/intstr"

	autoscalingv1alpha1 "github.com/th1nking/predictive-hpa/api/v1alpha1"
)

// These specs exercise the full reconcile loop in envtest: a real API server
// receives Deployment + PredictiveHPA objects, the controller (registered in
// BeforeSuite) reconciles, the fake MetricsProvider supplies the CPU series,
// and assertions are made on the Deployment scale subresource the controller
// writes back. Each spec uses its own namespace for isolation.
var _ = Describe("PredictiveHPA reconcile loop", func() {
	var testNamespace string

	// Each It gets a fresh namespace so Deployment/PHPA/sample data are
	// isolated. Namespace name is timestamped to avoid envtest's lack of
	// namespace garbage collection across specs.
	BeforeEach(func() {
			syncFakeClock()
			testNamespace = fmt.Sprintf("phpa-reconcile-%d", time.Now().UnixNano())
			ns := &corev1.Namespace{
			ObjectMeta: metav1.ObjectMeta{Name: testNamespace},
		}
		err := k8sClient.Create(ctx, ns)
		if err != nil && !apierrors.IsAlreadyExists(err) {
			Expect(err).NotTo(HaveOccurred())
		}
	})

	makeDeployment := func(namespace, name string, specReplicas int32) *appsv1.Deployment {
		return &appsv1.Deployment{
			ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: namespace},
			Spec: appsv1.DeploymentSpec{
				Replicas: &specReplicas,
				Selector: &metav1.LabelSelector{
					MatchLabels: map[string]string{"app": name},
				},
				Template: corev1.PodTemplateSpec{
					ObjectMeta: metav1.ObjectMeta{
						Labels: map[string]string{"app": name},
					},
					Spec: corev1.PodSpec{
						Containers: []corev1.Container{{
							Name:  name,
							Image: "k8s.gcr.io/hpa-example",
							Ports: []corev1.ContainerPort{{
								Name:          "http",
								ContainerPort: 80,
								Protocol:      corev1.ProtocolTCP,
							}},
							Resources: corev1.ResourceRequirements{
								Requests: corev1.ResourceList{
									corev1.ResourceCPU: resource.MustParse("200m"),
								},
								Limits: corev1.ResourceList{
									corev1.ResourceCPU: resource.MustParse("500m"),
								},
							},
							ReadinessProbe: &corev1.Probe{
								ProbeHandler: corev1.ProbeHandler{
									HTTPGet: &corev1.HTTPGetAction{
										Path: "/",
										Port: intstr.FromString("http"),
									},
								},
							},
						}},
					},
				},
			},
		}
	}

	makePHPA := func(namespace, name, deployName string, min *int32, max int32, target int32) *autoscalingv1alpha1.PredictiveHPA {
		return &autoscalingv1alpha1.PredictiveHPA{
			ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: namespace},
			Spec: autoscalingv1alpha1.PredictiveHPASpec{
				ScaleTargetRef: autoscalingv2.CrossVersionObjectReference{
					APIVersion: "apps/v1",
					Kind:       "Deployment",
					Name:       deployName,
				},
				MinReplicas:                    min,
				MaxReplicas:                    max,
				TargetCPUUtilizationPercentage: target,
				Prediction: autoscalingv1alpha1.PredictionConfig{
					Algorithm:    autoscalingv1alpha1.PredictionAlgorithm("EWMA"),
					AlphaPercent: 30,
					Window:       metav1.Duration{Duration: 5 * time.Minute},
					Horizon:      metav1.Duration{Duration: 30 * time.Second},
				},
			},
		}
	}

	It("scales up when CPU consistently exceeds target", func() {
		const (
			deployName = "demo-app"
			phpaName   = "demo-phpa"
		)

		// 1. Create Deployment. envtest's API server stores it, but no
		//    Pods or status are created automatically.
		deploy := makeDeployment(testNamespace, deployName, 1)
		Expect(k8sClient.Create(ctx, deploy)).To(Succeed())

		// 2. Patch Deployment.status.replicas = 1 via the status subresource.
		//    The Reconciler reads deploy.Status.Replicas as currentReplicas.
		//    Without this patch it stays at 0 and the scaling formula yields 0
		//    (clamped to minReplicas), making upscale assertions impossible.
		deploy.Status.Replicas = 1
		Expect(k8sClient.Status().Update(ctx, deploy)).To(Succeed())

		// 3. Feed the fake provider a sustained high-CPU signal. 200% utilization
		//    is well above target=50%, so the formula expects ceil(1*200/50)=4.
		//    EWMA + horizon extrapolation may push the controller's decision
		//    slightly higher; assert lower-bound rather than exact equality.
		fakeMetrics.SetConstantCPU(testNamespace, deployName, 200.0, 5, 30*time.Second)

		// 4. Create PHPA. Once created, the controller (running in BeforeSuite)
		//    will start reconciling it on its watch.
		minR := int32(1)
		phpa := makePHPA(testNamespace, phpaName, deployName, &minR, 10, 50)
		Expect(k8sClient.Create(ctx, phpa)).To(Succeed())

		// 5. Poll Deployment.spec.replicas until the controller writes >= 4.
		//    Timeout = 60s gives 1-2 reconcile cycles (RequeueAfter=30s) plus
		//    cushion for envtest manager startup and informer sync.
		Eventually(func(g Gomega) {
			updated := &appsv1.Deployment{}
			g.Expect(k8sClient.Get(ctx, types.NamespacedName{
				Namespace: testNamespace,
				Name:      deployName,
			}, updated)).To(Succeed())
			g.Expect(updated.Spec.Replicas).NotTo(BeNil())
			g.Expect(*updated.Spec.Replicas).To(BeNumerically(">=", 4),
				"expected controller to scale up to at least 4 replicas under sustained 200%% CPU (formula: ceil(1*200/50)=4); got %d",
				*updated.Spec.Replicas,
			)
		}, 60*time.Second, 500*time.Millisecond).Should(Succeed())
	})

	It("scales down when CPU drops below target", func() {
		const (
			deployName = "demo-app"
			phpaName   = "demo-phpa"
		)

		// 1. Create Deployment starting at 5 replicas. envtest stores it
		//    but creates no Pods; status is patched manually below.
		deploy := makeDeployment(testNamespace, deployName, 5)
		Expect(k8sClient.Create(ctx, deploy)).To(Succeed())

		// 2. Patch Deployment.status.replicas = 5 so the Reconciler reads
		//    currentReplicas=5. Without this patch status stays at 0 and
		//    there is nothing to scale down from.
		deployPatch := client.MergeFrom(deploy.DeepCopy())
		deploy.Status.Replicas = 5
		Expect(k8sClient.Status().Patch(ctx, deploy, deployPatch)).To(Succeed())

		// 3. Feed sustained 0% CPU. EWMA over a flat-zero series predicts
		//    ~0; formula yields ceil(5*0/50)=0, clamped to minReplicas=1.
		fakeMetrics.SetConstantCPU(testNamespace, deployName, 0.0, 5, 30*time.Second)

		// 4. Create PHPA. Critical: set scaleDownStabilizationWindowSeconds=0
		//    so the stabilization window does not gate the first scale-down
		//    decision. This isolates the scale-down decision logic from the
		//    window's anti-flap behavior, which is covered by a separate spec.
		minR := int32(1)
		stabilizationZero := int32(0)
		phpa := makePHPA(testNamespace, phpaName, deployName, &minR, 10, 50)
		phpa.Spec.ScaleDownStabilizationWindowSeconds = &stabilizationZero
		Expect(k8sClient.Create(ctx, phpa)).To(Succeed())

		// 5. Poll Deployment.spec.replicas until the controller writes <= 2.
		//    Lower-bound is 1 (minReplicas); upper-bound 2 tolerates a small
		//    EWMA lag before the prediction fully settles to zero.
		Eventually(func(g Gomega) {
			updated := &appsv1.Deployment{}
			g.Expect(k8sClient.Get(ctx, types.NamespacedName{
				Namespace: testNamespace,
				Name:      deployName,
			}, updated)).To(Succeed())
			g.Expect(updated.Spec.Replicas).NotTo(BeNil())
			g.Expect(*updated.Spec.Replicas).To(BeNumerically("<=", 2),
				"expected controller to scale down to <= 2 replicas under sustained 0%% CPU (formula clamped to minReplicas=1); got %d",
				*updated.Spec.Replicas,
			)
		}, 60*time.Second, 500*time.Millisecond).Should(Succeed())
	})

	It("blocks scale-down while inside the stabilization window, then releases after expiry", func() {
		const (
			deployName = "demo-app"
			phpaName   = "demo-phpa"
		)

		// 1. Start at 5 replicas; status patched so Reconciler reads curr=5.
		deploy := makeDeployment(testNamespace, deployName, 5)
		Expect(k8sClient.Create(ctx, deploy)).To(Succeed())
		deploy.Status.Replicas = 5
		Expect(k8sClient.Status().Update(ctx, deploy)).To(Succeed())

		// 2. Sustained 200% CPU drives upscale. Formula ceil(5*200/50)=20,
		//    clamped to maxReplicas=10.
		fakeMetrics.SetConstantCPU(testNamespace, deployName, 200.0, 5, 30*time.Second)

		// 3. Create PHPA with a 30s stabilization window. The window is long
		//    enough to be unambiguously observable; 60s default would just
		//    burn test time.
		minR := int32(1)
		stabSec := int32(30)
		phpa := makePHPA(testNamespace, phpaName, deployName, &minR, 10, 50)
		phpa.Spec.ScaleDownStabilizationWindowSeconds = &stabSec
		Expect(k8sClient.Create(ctx, phpa)).To(Succeed())

		// 4. Wait for upscale to saturate at maxReplicas=10. The formula
		//    ceil(5*200/50)=20 is clamped to max=10. Asserting on the
		//    ceiling (rather than `>=4`) avoids catching a mid-scale snapshot:
		//    Eventually returns the moment the predicate first holds, but the
		//    controller may not have reached steady state yet. Pinning to 10
		//    guarantees upscale is settled before we proceed.
		Eventually(func(g Gomega) {
			d := &appsv1.Deployment{}
			g.Expect(k8sClient.Get(ctx, types.NamespacedName{
				Namespace: testNamespace, Name: deployName,
			}, d)).To(Succeed())
			g.Expect(d.Spec.Replicas).NotTo(BeNil())
			g.Expect(*d.Spec.Replicas).To(Equal(int32(10)),
				"expected upscale to reach maxReplicas ceiling under sustained 200%% CPU; got %d",
				*d.Spec.Replicas)
		}, 30*time.Second, 500*time.Millisecond).Should(Succeed())

		const upscaled int32 = 10 // guaranteed by the Eventually above

		// Simulate the upscale being applied by the (absent) Deployment
		// controller: patch Deployment.status.replicas to match the new
		// spec, so subsequent reconciles see currentReplicas=10.
		updated := &appsv1.Deployment{}
		Expect(k8sClient.Get(ctx, types.NamespacedName{
			Namespace: testNamespace, Name: deployName,
		}, updated)).To(Succeed())
		updatedPatch := client.MergeFrom(updated.DeepCopy())
		updated.Status.Replicas = upscaled
		Expect(k8sClient.Status().Patch(ctx, updated, updatedPatch)).To(Succeed())

		// 5. Flip to 0% CPU. Formula now wants ceil(upscaled*0/50)=0 clamped
		//    to minReplicas=1 -- but the window should pin replicas at the
		//    earlier high value because history.maxInWindow returns it.
		fakeMetrics.SetConstantCPU(testNamespace, deployName, 0.0, 5, 30*time.Second)

		// Nudge the PHPA to trigger a fresh reconcile (annotation patch).
		// Without this, the controller relies on RequeueAfter=30s in real time.
		pollPHPA := func() *autoscalingv1alpha1.PredictiveHPA {
			p := &autoscalingv1alpha1.PredictiveHPA{}
			Expect(k8sClient.Get(ctx, types.NamespacedName{
				Namespace: testNamespace, Name: phpaName,
			}, p)).To(Succeed())
			return p
		}
		nudge := func() {
			p := pollPHPA()
			phpaPatch := client.MergeFrom(p.DeepCopy())
			if p.Annotations == nil {
				p.Annotations = map[string]string{}
			}
			p.Annotations["phpa.test/tick"] = fmt.Sprintf("%d", time.Now().UnixNano())
			Expect(k8sClient.Patch(ctx, p, phpaPatch)).To(Succeed())
		}
		nudge()

		// 6. CRITICAL ASSERTION: replicas must NOT drop while inside the
		//    window. Consistently for 3 seconds -- long enough to span
		//    multiple reconcile triggers but short enough not to bloat the
		//    suite. If the window logic is broken, the controller will
		//    write 1 and this fails fast.
		Consistently(func(g Gomega) {
			d := &appsv1.Deployment{}
			g.Expect(k8sClient.Get(ctx, types.NamespacedName{
				Namespace: testNamespace, Name: deployName,
			}, d)).To(Succeed())
			g.Expect(d.Spec.Replicas).NotTo(BeNil())
			g.Expect(*d.Spec.Replicas).To(Equal(upscaled),
				"expected stabilization window to hold replicas at %d under 0%% CPU; got %d",
				upscaled, *d.Spec.Replicas)
		}, 3*time.Second, 500*time.Millisecond).Should(Succeed())

		// 7. Advance fake clock past the window. The 'now' Reconcile sees
		//    on its next invocation will be far enough ahead that
		//    history.maxInWindow prunes the high-desired entry.
		fakeClock.Step(31 * time.Second)
		nudge()

		// 8. Now the window allows scale-down. Replicas should drop toward
		//    minReplicas=1.
		Eventually(func(g Gomega) {
			d := &appsv1.Deployment{}
			g.Expect(k8sClient.Get(ctx, types.NamespacedName{
				Namespace: testNamespace, Name: deployName,
			}, d)).To(Succeed())
			g.Expect(d.Spec.Replicas).NotTo(BeNil())
			g.Expect(*d.Spec.Replicas).To(BeNumerically("<=", 2),
				"expected scale-down to release after stabilization window expired; got %d",
				*d.Spec.Replicas)
		}, 30*time.Second, 500*time.Millisecond).Should(Succeed())
	})

})

