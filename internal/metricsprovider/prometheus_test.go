package metricsprovider

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clocktesting "k8s.io/utils/clock/testing"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

type providerFixture struct {
	target     *appsv1.Deployment
	pod        *corev1.Pod
	replicaSet *appsv1.ReplicaSet
	reader     client.Client
	clock      *clocktesting.FakeClock
	rates      []map[string]any
	sources    []map[string]any
	status     int
	afterQuery func()
}

func newProviderFixture(t *testing.T) *providerFixture {
	t.Helper()
	controller := true
	target := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Namespace: "default", Name: "web", UID: "deployment-uid"},
		Spec: appsv1.DeploymentSpec{Selector: &metav1.LabelSelector{MatchLabels: map[string]string{"app": "web"}}}}
	rs := &appsv1.ReplicaSet{ObjectMeta: metav1.ObjectMeta{Namespace: "default", Name: "web-rs", UID: "rs-uid",
		OwnerReferences: []metav1.OwnerReference{{Kind: "Deployment", Name: "web", UID: target.UID, Controller: &controller}}}}
	pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Namespace: "default", Name: "web-rs-pod", UID: "12345678-1234-1234-1234-123456789abc",
		Labels: map[string]string{"app": "web"}, OwnerReferences: []metav1.OwnerReference{{Kind: "ReplicaSet", Name: rs.Name, UID: rs.UID, Controller: &controller}}},
		Spec: corev1.PodSpec{Containers: []corev1.Container{{Name: "app", Resources: corev1.ResourceRequirements{Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("200m")}}}}},
		Status: corev1.PodStatus{Phase: corev1.PodRunning, Conditions: []corev1.PodCondition{{Type: corev1.PodReady, Status: corev1.ConditionTrue}},
			ContainerStatuses: []corev1.ContainerStatus{{Name: "app", Ready: true, ContainerID: "containerd://aabbcc", State: corev1.ContainerState{Running: &corev1.ContainerStateRunning{}}}}}}
	scheme := runtime.NewScheme()
	if err := appsv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	f := &providerFixture{target: target, replicaSet: rs, pod: pod,
		reader: fake.NewClientBuilder().WithScheme(scheme).WithObjects(target, rs, pod).WithStatusSubresource(pod).Build(),
		clock:  clocktesting.NewFakeClock(time.Unix(1700000100, 0))}
	f.setCPU("0.1")
	return f
}

func (f *providerFixture) sample(value string) map[string]any {
	return map[string]any{"metric": map[string]string{"namespace": f.pod.Namespace, "pod": f.pod.Name, "container": "app", "cpu": "total",
		"id": "/kubepods/burstable/pod" + string(f.pod.UID) + "/aabbcc"}, "value": []any{float64(f.clock.Now().Unix()), value}}
}

func (f *providerFixture) setCPU(value string) {
	f.rates = []map[string]any{f.sample(value)}
	f.sources = []map[string]any{f.sample(fmt.Sprint(f.clock.Now().Add(-5 * time.Second).Unix()))}
}

func (f *providerFixture) provider(t *testing.T) *PrometheusProvider {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if f.status != 0 {
			w.WriteHeader(f.status)
			_, _ = w.Write([]byte(`{"status":"error","errorType":"unavailable","error":"offline"}`))
			return
		}
		values := f.rates
		if strings.HasPrefix(r.FormValue("query"), "timestamp(") {
			values = f.sources
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "success", "data": map[string]any{"resultType": "vector", "result": values}})
		if f.afterQuery != nil {
			f.afterQuery()
		}
	}))
	t.Cleanup(server.Close)
	p, err := NewPrometheus(server.URL, f.reader)
	if err != nil {
		t.Fatal(err)
	}
	p.Clock = f.clock
	return p
}

func TestProviderBuildsHistoryFromVerifiedLiveObservations(t *testing.T) {
	f := newProviderFixture(t)
	p := f.provider(t)
	first, err := p.AverageCPUUtilizationPercentage(context.Background(), f.target, 5*time.Minute)
	if err != nil || len(first.Samples) != 1 || first.Samples[0].Value != 50 {
		t.Fatalf("first live observation: %+v, %v", first, err)
	}
	if !first.ObservedAt.Equal(f.clock.Now()) || !first.SourceTimestamp.Equal(f.clock.Now().Add(-5*time.Second)) {
		t.Fatalf("source and observation timestamps differ: %+v", first)
	}
	f.clock.Step(30 * time.Second)
	f.setCPU("0.2")
	second, err := p.AverageCPUUtilizationPercentage(context.Background(), f.target, 5*time.Minute)
	if err != nil || len(second.Samples) != 2 || second.Samples[0].Value != 50 || second.Samples[1].Value != 100 {
		t.Fatalf("live history: %+v, %v", second, err)
	}
}

func TestProviderRejectsUnsafeSourcesAndRequiresNewWarmup(t *testing.T) {
	cases := []struct {
		name   string
		change func(*providerFixture)
		want   error
	}{
		{"nan CPU", func(f *providerFixture) { f.rates = []map[string]any{f.sample("NaN")} }, ErrInvalidData},
		{"infinite CPU", func(f *providerFixture) { f.rates = []map[string]any{f.sample("+Inf")} }, ErrInvalidData},
		{"negative CPU", func(f *providerFixture) { f.rates = []map[string]any{f.sample("-0.1")} }, ErrInvalidData},
		{"stale source", func(f *providerFixture) {
			f.sources = []map[string]any{f.sample(fmt.Sprint(f.clock.Now().Add(-46 * time.Second).Unix()))}
		}, ErrStaleData},
		{"future source", func(f *providerFixture) {
			f.sources = []map[string]any{f.sample(fmt.Sprint(f.clock.Now().Add(time.Second).Unix()))}
		}, ErrInvalidData},
		{"infinite source", func(f *providerFixture) { f.sources = []map[string]any{f.sample("+Inf")} }, ErrInvalidData},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := newProviderFixture(t)
			p := f.provider(t)
			if _, err := p.AverageCPUUtilizationPercentage(context.Background(), f.target, time.Minute); err != nil {
				t.Fatal(err)
			}
			f.clock.Step(30 * time.Second)
			f.setCPU("0.1")
			tc.change(f)
			if got, err := p.AverageCPUUtilizationPercentage(context.Background(), f.target, time.Minute); !errors.Is(err, tc.want) || len(got.Samples) != 0 {
				t.Fatalf("unsafe source accepted: %+v %v, want %v", got, err, tc.want)
			}
			f.clock.Step(30 * time.Second)
			f.setCPU("0.1")
			got, err := p.AverageCPUUtilizationPercentage(context.Background(), f.target, time.Minute)
			if err != nil || len(got.Samples) != 1 {
				t.Fatalf("failure history bridged recovery: %+v %v", got, err)
			}
		})
	}
}

func (f *providerFixture) savePod(t *testing.T) {
	t.Helper()
	var current corev1.Pod
	if err := f.reader.Get(context.Background(), client.ObjectKeyFromObject(f.pod), &current); err != nil {
		t.Fatal(err)
	}
	f.pod.ResourceVersion = current.ResourceVersion
	status := f.pod.Status.DeepCopy()
	if err := f.reader.Update(context.Background(), f.pod); err != nil {
		t.Fatal(err)
	}
	f.pod.Status = *status
	if err := f.reader.Status().Update(context.Background(), f.pod); err != nil {
		t.Fatal(err)
	}
}

func TestProviderUsesOwnerUIDAndWeightedContainerRequests(t *testing.T) {
	f := newProviderFixture(t)
	foreignRS := f.replicaSet.DeepCopy()
	foreignRS.ResourceVersion = ""
	foreignRS.Name = "web-canary-rs"
	foreignRS.UID = "foreign-rs"
	foreignRS.OwnerReferences[0].Name = "web-canary"
	foreignRS.OwnerReferences[0].UID = "foreign-deployment"
	foreignPod := f.pod.DeepCopy()
	foreignPod.ResourceVersion = ""
	foreignPod.Name = "web-canary-rs-pod"
	foreignPod.UID = "aaaaaaaa-1234-1234-1234-123456789abc"
	foreignPod.OwnerReferences[0].Name = foreignRS.Name
	foreignPod.OwnerReferences[0].UID = foreignRS.UID
	if err := f.reader.Create(context.Background(), foreignRS); err != nil {
		t.Fatal(err)
	}
	if err := f.reader.Create(context.Background(), foreignPod); err != nil {
		t.Fatal(err)
	}
	f.pod.Spec.Containers = append(f.pod.Spec.Containers, corev1.Container{Name: "worker", Resources: corev1.ResourceRequirements{Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("800m")}}})
	f.pod.Status.ContainerStatuses = append(f.pod.Status.ContainerStatuses, corev1.ContainerStatus{Name: "worker", ContainerID: "containerd://ddeeff", Ready: true, State: corev1.ContainerState{Running: &corev1.ContainerStateRunning{}}})
	f.savePod(t)
	worker := f.sample("0.8")
	worker["metric"].(map[string]string)["container"] = "worker"
	worker["metric"].(map[string]string)["id"] = "/kubepods/pod" + string(f.pod.UID) + "/ddeeff"
	workerSource := f.sample(fmt.Sprint(f.clock.Now().Add(-10 * time.Second).Unix()))
	workerSource["metric"] = worker["metric"]
	foreign := f.sample("9.0")
	foreign["metric"].(map[string]string)["pod"] = foreignPod.Name
	f.rates = append(f.rates, worker, foreign)
	f.sources = append(f.sources, workerSource)
	got, err := f.provider(t).AverageCPUUtilizationPercentage(context.Background(), f.target, time.Minute)
	if err != nil || len(got.Samples) != 1 || got.Samples[0].Value != 90 {
		t.Fatalf("wanted 90%% from 900m usage / 1000m requests, got %+v %v", got, err)
	}
	if !got.SourceTimestamp.Equal(f.clock.Now().Add(-10 * time.Second)) {
		t.Fatalf("oldest container timestamp lost: %+v", got)
	}
}

func TestProviderRejectsUnusableRostersAndAmbiguousInstances(t *testing.T) {
	cases := []struct {
		name   string
		change func(*providerFixture)
		want   error
	}{
		{"wrong namespace CPU", func(f *providerFixture) {
			f.rates[0]["metric"].(map[string]string)["namespace"] = "other"
			f.sources[0]["metric"].(map[string]string)["namespace"] = "other"
		}, ErrIncompleteData},
		{"different source series", func(f *providerFixture) { f.sources[0]["metric"].(map[string]string)["instance"] = "another-kubelet" }, ErrIncompleteData},
		{"old ReplicaSet UID", func(f *providerFixture) { f.pod.OwnerReferences[0].UID = "old-rs-uid" }, ErrNoData},
		{"missing request", func(f *providerFixture) { f.pod.Spec.Containers[0].Resources.Requests = nil }, ErrInvalidData},
		{"pending Pod", func(f *providerFixture) { f.pod.Status.Phase = corev1.PodPending }, ErrIncompleteData},
		{"unready Pod", func(f *providerFixture) { f.pod.Status.Conditions[0].Status = corev1.ConditionFalse }, ErrIncompleteData},
		{"unknown container instance", func(f *providerFixture) { f.pod.Status.ContainerStatuses[0].ContainerID = "" }, ErrIncompleteData},
		{"init sidecar", func(f *providerFixture) {
			policy := corev1.ContainerRestartPolicyAlways
			f.pod.Spec.InitContainers = []corev1.Container{{Name: "init", RestartPolicy: &policy}}
		}, ErrInvalidData},
		{"old container instance", func(f *providerFixture) { f.pod.Status.ContainerStatuses[0].ContainerID = "containerd://newcontainer" }, ErrIncompleteData},
		{"ambiguous scrape", func(f *providerFixture) {
			duplicate := f.sample("0.1")
			duplicate["metric"].(map[string]string)["instance"] = "duplicate"
			f.rates = append(f.rates, duplicate)
		}, ErrIncompleteData},
		{"empty response", func(f *providerFixture) { f.rates = []map[string]any{}; f.sources = []map[string]any{} }, ErrNoData},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := newProviderFixture(t)
			tc.change(f)
			f.savePod(t)
			got, err := f.provider(t).AverageCPUUtilizationPercentage(context.Background(), f.target, time.Minute)
			if !errors.Is(err, tc.want) || len(got.Samples) != 0 {
				t.Fatalf("got %+v %v, want %v", got, err, tc.want)
			}
		})
	}
}

func TestProviderRetainsVerifiedObservationsAfterRollout(t *testing.T) {
	f := newProviderFixture(t)
	p := f.provider(t)
	if _, err := p.AverageCPUUtilizationPercentage(context.Background(), f.target, time.Minute); err != nil {
		t.Fatal(err)
	}
	if err := f.reader.Delete(context.Background(), f.pod); err != nil {
		t.Fatal(err)
	}
	f.pod = f.pod.DeepCopy()
	f.pod.Name = "web-new-rs-pod"
	f.pod.UID = "bbbbbbbb-1234-1234-1234-123456789abc"
	f.pod.ResourceVersion = ""
	if err := f.reader.Create(context.Background(), f.pod); err != nil {
		t.Fatal(err)
	}
	f.clock.Step(30 * time.Second)
	f.setCPU("0.2")
	got, err := p.AverageCPUUtilizationPercentage(context.Background(), f.target, time.Minute)
	if err != nil || len(got.Samples) != 2 || got.Samples[0].Value != 50 || got.Samples[1].Value != 100 {
		t.Fatalf("rollout erased validated past observations: %+v %v", got, err)
	}
}

func TestProviderRetainsAcceptedHistoryWhileRolloutMetricsWarmUp(t *testing.T) {
	f := newProviderFixture(t)
	p := f.provider(t)
	if _, err := p.AverageCPUUtilizationPercentage(context.Background(), f.target, 2*time.Minute); err != nil {
		t.Fatal(err)
	}
	f.clock.Step(30 * time.Second)
	f.setCPU("0.2")
	before, err := p.AverageCPUUtilizationPercentage(context.Background(), f.target, 2*time.Minute)
	if err != nil || len(before.Samples) != 2 {
		t.Fatalf("old Pod observations unavailable: %+v %v", before, err)
	}
	if err := f.reader.Delete(context.Background(), f.pod); err != nil {
		t.Fatal(err)
	}
	f.pod = f.pod.DeepCopy()
	f.pod.Name = "web-rollout-pod"
	f.pod.UID = "cccccccc-1234-1234-1234-123456789abc"
	f.pod.ResourceVersion = ""
	f.pod.Status.Phase = corev1.PodPending
	if err := f.reader.Create(context.Background(), f.pod); err != nil {
		t.Fatal(err)
	}
	f.clock.Step(15 * time.Second)
	if got, err := p.AverageCPUUtilizationPercentage(context.Background(), f.target, 2*time.Minute); !errors.Is(err, ErrIncompleteData) || len(got.Samples) != 0 {
		t.Fatalf("Pending Pod must hold the current observation: %+v %v", got, err)
	}
	f.pod.Status.Phase = corev1.PodRunning
	f.savePod(t)
	f.clock.Step(15 * time.Second)
	f.rates, f.sources = []map[string]any{}, []map[string]any{}
	if got, err := p.AverageCPUUtilizationPercentage(context.Background(), f.target, 2*time.Minute); !errors.Is(err, ErrNoData) || len(got.Samples) != 0 {
		t.Fatalf("Ready Pod without CPU samples must remain unavailable: %+v %v", got, err)
	}
	f.clock.Step(15 * time.Second)
	f.setCPU("0.4")
	got, err := p.AverageCPUUtilizationPercentage(context.Background(), f.target, 2*time.Minute)
	if err != nil || len(got.Samples) != 3 {
		t.Fatalf("rollout erased accepted history: %+v %v", got, err)
	}
	if got.Samples[0] != before.Samples[0] || got.Samples[1] != before.Samples[1] || got.Samples[2].Value != 200 {
		t.Fatalf("past observations were recomputed using the new Pod: before=%+v after=%+v", before, got)
	}
}

func TestProviderRejectsRosterChangesDuringQuery(t *testing.T) {
	cases := []struct {
		name   string
		change func(*corev1.Pod)
	}{
		{"CPU request", func(pod *corev1.Pod) {
			pod.Spec.Containers[0].Resources.Requests[corev1.ResourceCPU] = resource.MustParse("400m")
		}},
		{"readiness", func(pod *corev1.Pod) { pod.Status.Conditions[0].Status = corev1.ConditionFalse }},
		{"container restart", func(pod *corev1.Pod) { pod.Status.ContainerStatuses[0].ContainerID = "containerd://newcontainer" }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := newProviderFixture(t)
			p := f.provider(t)
			if _, err := p.AverageCPUUtilizationPercentage(context.Background(), f.target, time.Minute); err != nil {
				t.Fatal(err)
			}
			f.afterQuery = func() { tc.change(f.pod); f.savePod(t) }
			got, err := p.AverageCPUUtilizationPercentage(context.Background(), f.target, time.Minute)
			if !errors.Is(err, ErrTargetChanged) || len(got.Samples) != 0 {
				t.Fatalf("mixed roster was accepted: %+v %v", got, err)
			}
		})
	}
}

func TestFrequentEventsPreserveRealTimestampsWithoutDelayingWarmup(t *testing.T) {
	f := newProviderFixture(t)
	p := f.provider(t)
	start := f.clock.Now()
	for second := 0; second <= 30; second += 5 {
		f.clock.SetTime(start.Add(time.Duration(second) * time.Second))
		f.setCPU("0.1")
		got, err := p.AverageCPUUtilizationPercentage(context.Background(), f.target, time.Minute)
		if err != nil {
			t.Fatal(err)
		}
		if !got.Samples[len(got.Samples)-1].Timestamp.Equal(f.clock.Now()) {
			t.Fatalf("latest observation was backdated: %+v", got)
		}
		for i := 1; i < len(got.Samples); i++ {
			if got.Samples[i].Timestamp.Sub(got.Samples[i-1].Timestamp) < 15*time.Second {
				t.Fatalf("samples are too close: %+v", got)
			}
		}
		if second == 15 && len(got.Samples) != 2 {
			t.Fatalf("frequent events prevented warmup: %+v", got)
		}
	}
}

func TestProviderBoundsHistoryAndResetsAfterHTTPFailure(t *testing.T) {
	f := newProviderFixture(t)
	p := f.provider(t)
	for range 245 {
		f.clock.Step(15 * time.Second)
		f.setCPU("0.1")
		got, err := p.AverageCPUUtilizationPercentage(context.Background(), f.target, time.Hour)
		if err != nil || len(got.Samples) > 242 {
			t.Fatalf("unbounded history: %d %v", len(got.Samples), err)
		}
	}
	f.status = http.StatusServiceUnavailable
	if _, err := p.AverageCPUUtilizationPercentage(context.Background(), f.target, time.Hour); err == nil || !strings.Contains(err.Error(), "instant query") {
		t.Fatalf("HTTP failure was lost: %v", err)
	}
	f.status = 0
	f.clock.Step(30 * time.Second)
	f.setCPU("0.1")
	got, err := p.AverageCPUUtilizationPercentage(context.Background(), f.target, time.Hour)
	if err != nil || len(got.Samples) != 1 {
		t.Fatalf("outage did not reset prediction history: %+v %v", got, err)
	}
	if _, err := p.AverageCPUUtilizationPercentage(context.Background(), f.target, time.Hour+time.Second); !errors.Is(err, ErrInvalidData) {
		t.Fatalf("unsupported window accepted: %v", err)
	}
}

func TestCanceledQueuedObservationDoesNotEraseActiveHistory(t *testing.T) {
	f := newProviderFixture(t)
	entered, release := make(chan struct{}), make(chan struct{})
	var blockNext atomic.Bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		query := r.FormValue("query")
		seconds, err := strconv.ParseFloat(r.FormValue("time"), 64)
		if err != nil {
			t.Errorf("invalid query time: %v", err)
			w.WriteHeader(500)
			return
		}
		if strings.HasPrefix(query, "rate(") && blockNext.CompareAndSwap(true, false) {
			close(entered)
			<-release
		}
		value := "0.1"
		if strings.HasPrefix(query, "timestamp(") {
			value = fmt.Sprint(seconds - 5)
		}
		sample := f.sample(value)
		sample["value"] = []any{seconds, value}
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "success", "data": map[string]any{"resultType": "vector", "result": []map[string]any{sample}}})
	}))
	defer server.Close()
	p, err := NewPrometheus(server.URL, f.reader)
	if err != nil {
		t.Fatal(err)
	}
	p.Clock = f.clock
	if _, err := p.AverageCPUUtilizationPercentage(context.Background(), f.target, time.Minute); err != nil {
		t.Fatal(err)
	}
	f.clock.Step(30 * time.Second)
	blockNext.Store(true)
	type observation struct {
		history CPUHistory
		err     error
	}
	finished := make(chan observation, 1)
	go func() {
		history, err := p.AverageCPUUtilizationPercentage(context.Background(), f.target, time.Minute)
		finished <- observation{history, err}
	}()
	<-entered
	canceled, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := p.AverageCPUUtilizationPercentage(canceled, f.target, time.Minute); err == nil {
		t.Error("canceled queued observation succeeded")
	}
	close(release)
	got := <-finished
	if got.err != nil || len(got.history.Samples) != 2 {
		t.Fatalf("queued cancellation erased active history: %+v %v", got.history, got.err)
	}
}

func TestProviderSupportsSystemdIdentityAndRejectsLookalikes(t *testing.T) {
	for _, lookalike := range []bool{false, true} {
		t.Run(fmt.Sprint(lookalike), func(t *testing.T) {
			f := newProviderFixture(t)
			uid := strings.ReplaceAll(string(f.pod.UID), "-", "_")
			if lookalike {
				uid += "f"
			}
			id := "/kubelet.slice/kubelet-kubepods.slice/kubelet-kubepods-burstable.slice/kubelet-kubepods-burstable-pod" + uid + ".slice/cri-containerd-aabbcc.scope"
			f.rates[0]["metric"].(map[string]string)["id"] = id
			f.sources[0]["metric"].(map[string]string)["id"] = id
			got, err := f.provider(t).AverageCPUUtilizationPercentage(context.Background(), f.target, time.Minute)
			if lookalike {
				if !errors.Is(err, ErrIncompleteData) {
					t.Fatalf("Pod UID substring accepted: %+v %v", got, err)
				}
				return
			}
			if err != nil || len(got.Samples) != 1 || got.Samples[0].Value != 50 {
				t.Fatalf("systemd cgroup rejected: %+v %v", got, err)
			}
		})
	}
}

func TestProviderDoesNotReuseHistoryForRecreatedDeployment(t *testing.T) {
	f := newProviderFixture(t)
	p := f.provider(t)
	if _, err := p.AverageCPUUtilizationPercentage(context.Background(), f.target, time.Minute); err != nil {
		t.Fatal(err)
	}
	if err := f.reader.Delete(context.Background(), f.target); err != nil {
		t.Fatal(err)
	}
	f.target = f.target.DeepCopy()
	f.target.ResourceVersion = ""
	f.target.UID = "recreated-deployment"
	if err := f.reader.Create(context.Background(), f.target); err != nil {
		t.Fatal(err)
	}
	var rs appsv1.ReplicaSet
	if err := f.reader.Get(context.Background(), client.ObjectKeyFromObject(f.replicaSet), &rs); err != nil {
		t.Fatal(err)
	}
	rs.OwnerReferences[0].UID = f.target.UID
	if err := f.reader.Update(context.Background(), &rs); err != nil {
		t.Fatal(err)
	}
	f.clock.Step(30 * time.Second)
	f.setCPU("0.2")
	got, err := p.AverageCPUUtilizationPercentage(context.Background(), f.target, time.Minute)
	if err != nil || len(got.Samples) != 1 || got.Samples[0].Value != 100 {
		t.Fatalf("old target incarnation history reused: %+v %v", got, err)
	}
}

func TestProviderAcceptsReadyPodAfterOrdinaryInitContainer(t *testing.T) {
	f := newProviderFixture(t)
	f.pod.Spec.InitContainers = []corev1.Container{{Name: "initialize"}}
	f.savePod(t)
	got, err := f.provider(t).AverageCPUUtilizationPercentage(context.Background(), f.target, time.Minute)
	if err != nil || len(got.Samples) != 1 || got.Samples[0].Value != 50 {
		t.Fatalf("completed init was treated as workload CPU: %+v %v", got, err)
	}
}
