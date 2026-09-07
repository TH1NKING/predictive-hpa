package metricsprovider

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
)

func TestQueryDiagnosticsKeepEvaluationTimeSeparate(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(promFakeResponse([][2]any{
			{1700000000.0, "10.5"}, {1700000015.0, "12.0"},
		}))
	}))
	defer server.Close()
	provider, err := NewPrometheus(server.URL)
	if err != nil {
		t.Fatal(err)
	}
	var output bytes.Buffer
	logger := zap.New(zap.WriteTo(&output), zap.UseDevMode(false)).WithValues("reconcileStartedAt", "test-cycle")
	ctx := logf.IntoContext(context.Background(), logger)
	before := time.Now()
	samples, err := provider.AverageCPUUtilizationPercentage(ctx, "default", "web", 5*time.Minute)
	after := time.Now()
	if err != nil || len(samples) != 2 || samples[1].Value != 12 {
		t.Fatalf("diagnostics changed query results: samples=%v err=%v", samples, err)
	}
	var record map[string]any
	if err := json.Unmarshal(bytes.TrimSpace(output.Bytes()), &record); err != nil {
		t.Fatalf("expected a query diagnostic record, got %q: %v", output.String(), err)
	}
	if record["msg"] != "Queried CPU utilization" || record["reconcileStartedAt"] != "test-cycle" {
		t.Fatalf("query is not correlated to its reconciliation: %v", record)
	}
	// These are stored query evaluation points, independently specified by the
	// external API fixture. They must not be relabeled as raw scrape times.
	if record["latestEvaluationAt"] != "2023-11-14T22:13:35Z" || record["samples"] != float64(2) {
		t.Fatalf("missing evaluation metadata: %v", record)
	}
	if record["queryStepSeconds"] != float64(15) || record["cpuRateWindowSeconds"] != float64(60) {
		t.Fatalf("missing query time scales: %v", record)
	}
	for _, field := range []string{"queryStartedAt", "queryFinishedAt"} {
		value, ok := record[field].(string)
		stamp, parseErr := time.Parse(time.RFC3339Nano, value)
		if !ok || parseErr != nil || stamp.Before(before) || stamp.After(after) {
			t.Fatalf("%s is not an observed query boundary: %v", field, record[field])
		}
	}
	if record["queryError"] != "" {
		t.Fatalf("successful query was reported as an error: %v", record)
	}
}

func TestQueryDiagnosticsRetainFailureWithoutInventingAnEvaluation(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write([]byte(`{"status":"error","errorType":"unavailable","error":"offline"}`))
	}))
	defer server.Close()
	provider, err := NewPrometheus(server.URL)
	if err != nil {
		t.Fatal(err)
	}
	var output bytes.Buffer
	ctx := logf.IntoContext(context.Background(), zap.New(zap.WriteTo(&output), zap.UseDevMode(false)))
	samples, err := provider.AverageCPUUtilizationPercentage(ctx, "default", "web", time.Minute)
	if err == nil || len(samples) != 0 {
		t.Fatalf("expected the original query failure, got samples=%v err=%v", samples, err)
	}
	var record map[string]any
	if decodeErr := json.Unmarshal(bytes.TrimSpace(output.Bytes()), &record); decodeErr != nil {
		t.Fatalf("query failure lost its diagnostics: %v", decodeErr)
	}
	errorMessage, _ := record["queryError"].(string)
	if !strings.Contains(errorMessage, "query_range") || record["samples"] != float64(0) {
		t.Fatalf("failed query was not identified: %v", record)
	}
	if value, exists := record["latestEvaluationAt"]; !exists || value != nil {
		t.Fatalf("failed query must expose an unknown evaluation time: %v", record)
	}
}
