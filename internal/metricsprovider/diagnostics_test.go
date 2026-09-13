package metricsprovider

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"

	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
)

const diagnosticEvaluation = "2023-11-14T22:15:00Z"

func TestQueryDiagnosticsRecordFirstCPUWithoutInventingRejectedCPU(t *testing.T) {
	f := newProviderFixture(t)
	p := f.provider(t)
	var output bytes.Buffer
	ctx := logf.IntoContext(context.Background(), zap.New(zap.WriteTo(&output), zap.UseDevMode(false)))
	accepted, err := p.AverageCPUUtilizationPercentage(ctx, f.target, time.Minute)
	if err != nil || len(accepted.Samples) != 1 || accepted.Samples[0].Value != 50 {
		t.Fatalf("initial observation changed: %+v %v", accepted, err)
	}
	f.status = http.StatusServiceUnavailable
	if _, err := p.AverageCPUUtilizationPercentage(ctx, f.target, time.Minute); err == nil {
		t.Fatal("missing rejected HTTP query")
	}
	decoder := json.NewDecoder(&output)
	var first, rejected map[string]any
	if err := decoder.Decode(&first); err != nil {
		t.Fatal(err)
	}
	if err := decoder.Decode(&rejected); err != nil {
		t.Fatal(err)
	}
	if first["currentCPU%"] != float64(50) || first["samples"] != float64(1) {
		t.Fatalf("first accepted observation lost its measured CPU: %v", first)
	}
	value, present := rejected["currentCPU%"]
	if !present || value != nil || rejected["samples"] != float64(0) {
		t.Fatalf("rejection retained a previous or invented CPU value: %v", rejected)
	}
}

func TestQueryDiagnosticsDescribeEveryAcceptedContainerAndActualHTTPRequest(t *testing.T) {
	f := newProviderFixture(t)
	f.pod.Spec.Containers = append(f.pod.Spec.Containers, corev1.Container{Name: "worker", Resources: corev1.ResourceRequirements{Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("800m")}}})
	f.pod.Status.ContainerStatuses = append(f.pod.Status.ContainerStatuses, corev1.ContainerStatus{Name: "worker", ContainerID: "containerd://ddeeff", Ready: true, State: corev1.ContainerState{Running: &corev1.ContainerStateRunning{}}})
	f.savePod(t)
	worker := f.sample("0.8")
	worker["metric"].(map[string]string)["container"] = "worker"
	worker["metric"].(map[string]string)["id"] = "/kubepods/pod" + string(f.pod.UID) + "/ddeeff"
	workerSource := f.sample("1700000090")
	workerSource["metric"] = worker["metric"]
	f.rates = append(f.rates, worker)
	f.sources = append(f.sources, workerSource)
	var output bytes.Buffer
	ctx := logf.IntoContext(context.Background(), zap.New(zap.WriteTo(&output), zap.UseDevMode(false)))
	before := time.Now()
	history, err := f.provider(t).AverageCPUUtilizationPercentage(ctx, f.target, time.Minute)
	after := time.Now()
	if err != nil || len(history.Samples) != 1 || history.Samples[0].Value != 90 {
		t.Fatalf("observation changed: %+v %v", history, err)
	}
	var record struct {
		TargetUID  string `json:"targetUID"`
		Containers []struct {
			Pod             string    `json:"pod"`
			PodUID          string    `json:"pod_uid"`
			Container       string    `json:"container"`
			RuntimeID       string    `json:"runtime_id"`
			SourceTimestamp time.Time `json:"source_timestamp"`
		} `json:"containerSources"`
		Queries []struct {
			Kind     string    `json:"query_kind"`
			Status   int       `json:"http_status"`
			Started  time.Time `json:"started_at"`
			Finished time.Time `json:"finished_at"`
			Duration float64   `json:"duration_seconds"`
			Error    string    `json:"error"`
		} `json:"queries"`
	}
	if err := json.Unmarshal(bytes.TrimSpace(output.Bytes()), &record); err != nil {
		t.Fatal(err)
	}
	if record.TargetUID != "deployment-uid" || len(record.Containers) != 2 || len(record.Queries) != 2 {
		t.Fatalf("missing target, full container identities or actual requests: %s", output.String())
	}
	for index, expected := range []struct{ name, id, source string }{{"app", "aabbcc", "2023-11-14T22:14:55Z"}, {"worker", "ddeeff", "2023-11-14T22:14:50Z"}} {
		container := record.Containers[index]
		if container.Pod != "web-rs-pod" || container.PodUID != string(f.pod.UID) || container.Container != expected.name || container.RuntimeID != expected.id || container.SourceTimestamp.Format(time.RFC3339) != expected.source {
			t.Fatalf("incorrect accepted container %d: %+v", index, container)
		}
	}
	for index, query := range record.Queries {
		if query.Kind != []string{"rate", "timestamp"}[index] || query.Status != 200 || query.Error != "" || query.Started.Before(before) || query.Finished.After(after) || query.Finished.Before(query.Started) || query.Duration < 0 {
			t.Fatalf("invalid HTTP receipt: %+v", query)
		}
	}
}

func TestQueryDiagnosticsKeepEvaluationTimeSeparate(t *testing.T) {
	f := newProviderFixture(t)
	p := f.provider(t)
	var output bytes.Buffer
	ctx := logf.IntoContext(context.Background(), zap.New(zap.WriteTo(&output), zap.UseDevMode(false)).WithValues("reconcileStartedAt", "test-cycle"))
	before := time.Now()
	got, err := p.AverageCPUUtilizationPercentage(ctx, f.target, time.Minute)
	after := time.Now()
	if err != nil || len(got.Samples) != 1 {
		t.Fatalf("diagnostics changed observation: %+v %v", got, err)
	}
	var record map[string]any
	if err := json.Unmarshal(bytes.TrimSpace(output.Bytes()), &record); err != nil {
		t.Fatal(err)
	}
	if record["msg"] != "Queried CPU utilization" || record["reconcileStartedAt"] != "test-cycle" {
		t.Fatalf("query correlation lost: %v", record)
	}
	if record["queryInstantAt"] != diagnosticEvaluation || record["latestEvaluationAt"] != diagnosticEvaluation || record["sourceTimestamp"] != "2023-11-14T22:14:55Z" {
		t.Fatalf("source and evaluation timestamps were confused: %v", record)
	}
	if record["cpuRateWindowSeconds"] != float64(60) || record["observationSpacingSeconds"] != float64(15) {
		t.Fatalf("sampling contract missing: %v", record)
	}
	for _, field := range []string{"queryStartedAt", "queryFinishedAt"} {
		value, ok := record[field].(string)
		stamp, parseErr := time.Parse(time.RFC3339Nano, value)
		if !ok || parseErr != nil || stamp.Before(before) || stamp.After(after) {
			t.Fatalf("invalid query boundary %s: %v", field, record[field])
		}
	}
	if _, exists := record["queryStepSeconds"]; exists {
		t.Fatalf("live observations mislabeled as query_range grid: %v", record)
	}
}

func TestQueryDiagnosticsRetainFailureWithoutInventingAnEvaluation(t *testing.T) {
	f := newProviderFixture(t)
	f.status = http.StatusServiceUnavailable
	p := f.provider(t)
	var output bytes.Buffer
	ctx := logf.IntoContext(context.Background(), zap.New(zap.WriteTo(&output), zap.UseDevMode(false)))
	if _, err := p.AverageCPUUtilizationPercentage(ctx, f.target, time.Minute); err == nil {
		t.Fatal("missing query failure")
	}
	var record map[string]any
	if err := json.Unmarshal(bytes.TrimSpace(output.Bytes()), &record); err != nil {
		t.Fatal(err)
	}
	message, _ := record["queryError"].(string)
	if !strings.Contains(message, "instant query") || record["samples"] != float64(0) {
		t.Fatalf("query failure lost: %v", record)
	}
	if record["latestEvaluationAt"] != nil || record["sourceTimestamp"] != nil {
		t.Fatalf("failed query invented accepted observation: %v", record)
	}
	if record["queryInstantAt"] != diagnosticEvaluation {
		t.Fatalf("requested instant lost: %v", record)
	}
}
