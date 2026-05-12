package metricsprovider

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// promFakeResponse builds a minimal Prometheus query_range response payload.
func promFakeResponse(values [][2]any) map[string]any {
	formattedValues := make([][]any, len(values))
	for i, v := range values {
		formattedValues[i] = []any{v[0], v[1]}
	}
	return map[string]any{
		"status": "success",
		"data": map[string]any{
			"resultType": "matrix",
			"result": []map[string]any{
				{
					"metric": map[string]string{},
					"values": formattedValues,
				},
			},
		},
	}
}

func TestPrometheusProvider_HappyPath(t *testing.T) {
	var capturedQuery string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		capturedQuery = r.FormValue("query")
		_ = json.NewEncoder(w).Encode(promFakeResponse([][2]any{
			{1700000000.0, "10.5"},
			{1700000015.0, "12.0"},
			{1700000030.0, "11.5"},
		}))
	}))
	defer server.Close()

	p, err := NewPrometheus(server.URL)
	if err != nil {
		t.Fatalf("NewPrometheus: %v", err)
	}

	samples, err := p.AverageCPUUtilizationPercentage(
		context.Background(), "default", "web", 5*time.Minute,
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if len(samples) != 3 {
		t.Fatalf("expected 3 samples, got %d", len(samples))
	}
	if samples[0].Value != 10.5 || samples[1].Value != 12.0 || samples[2].Value != 11.5 {
		t.Errorf("unexpected values: %+v", samples)
	}

	// Verify the PromQL we sent contains the critical fragments.
	for _, fragment := range []string{
		`namespace="default"`,
		`pod=~"web-.*"`,
		`container!=""`,
		`resource="cpu"`,
		`*100`,
	} {
		if !strings.Contains(capturedQuery, fragment) {
			t.Errorf("query missing %q\nfull query: %s", fragment, capturedQuery)
		}
	}
}

func TestPrometheusProvider_EmptyResultReturnsErrNoData(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{
			"status": "success",
			"data": map[string]any{
				"resultType": "matrix",
				"result":     []map[string]any{},
			},
		})
	}))
	defer server.Close()

	p, _ := NewPrometheus(server.URL)
	_, err := p.AverageCPUUtilizationPercentage(
		context.Background(), "default", "web", 5*time.Minute,
	)
	if !errors.Is(err, ErrNoData) {
		t.Errorf("expected ErrNoData, got %v", err)
	}
}

func TestPrometheusProvider_PrometheusErrorIsWrapped(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte(`{"status":"error","errorType":"internal","error":"boom"}`))
	}))
	defer server.Close()

	p, _ := NewPrometheus(server.URL)
	_, err := p.AverageCPUUtilizationPercentage(
		context.Background(), "default", "web", 5*time.Minute,
	)
	if err == nil {
		t.Fatal("expected error, got nil")
	}
	if !strings.Contains(err.Error(), "query_range") {
		t.Errorf("expected wrapped error mentioning query_range, got %v", err)
	}
}
