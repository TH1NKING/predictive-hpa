package metricsprovider

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"testing"
	"time"

	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
)

const diagnosticEvaluation = "2023-11-14T22:15:00Z"

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
