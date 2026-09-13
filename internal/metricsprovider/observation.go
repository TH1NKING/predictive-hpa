package metricsprovider

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"sync"
	"time"

	appsv1 "k8s.io/api/apps/v1"
)

// ContainerSource identifies each accepted raw CPU counter sample. RuntimeID is
// the container runtime's instance ID, without the transport prefix.
type ContainerSource struct {
	Pod             string    `json:"pod"`
	PodUID          string    `json:"pod_uid"`
	Container       string    `json:"container"`
	RuntimeID       string    `json:"runtime_id"`
	SourceTimestamp time.Time `json:"source_timestamp"`
}

// QueryReceipt describes one actual HTTP round trip, including failed attempts.
// FinishedAt includes reading the response body, or the transport/read failure.
// A JSON parsing failure is reported by CPUObservation.Error, not as HTTP I/O.
type QueryReceipt struct {
	QueryKind       string    `json:"query_kind"`
	Method          string    `json:"method"`
	StartedAt       time.Time `json:"started_at"`
	FinishedAt      time.Time `json:"finished_at"`
	DurationSeconds float64   `json:"duration_seconds"`
	HTTPStatus      int       `json:"http_status"`
	Error           string    `json:"error"`
}

// CPUObservation is a single attempt, including rejected attempts. Accepted
// container evidence is published only after the entire production validation
// path succeeds; rejected attempts retain their actual HTTP receipts.
type CPUObservation struct {
	StartedAt          time.Time         `json:"observation_started_at"`
	FinishedAt         time.Time         `json:"observation_finished_at"`
	TargetUID          string            `json:"target_uid"`
	Status             string            `json:"status"`
	Error              string            `json:"error"`
	EvaluatedAt        *time.Time        `json:"evaluated_at"`
	SourceTimestamp    *time.Time        `json:"source_timestamp"`
	UtilizationPercent *float64          `json:"utilization_percent"`
	Containers         []ContainerSource `json:"containers"`
	Queries            []QueryReceipt    `json:"queries"`
}

type observationTraceKey struct{}
type queryKindKey struct{}

type observationTrace struct {
	queries    []QueryReceipt
	containers []ContainerSource
	result     CPUObservation
}

// ObserveCPU provides the production provider's latest verified observation and
// diagnostics without exposing or reconstructing its private history policy.
func (p *PrometheusProvider) ObserveCPU(ctx context.Context, target *appsv1.Deployment) (CPUObservation, error) {
	trace := &observationTrace{}
	_, err := p.AverageCPUUtilizationPercentage(context.WithValue(ctx, observationTraceKey{}, trace), target, observationSpacing)
	return trace.result, err
}

type receiptTransport struct{ next http.RoundTripper }

func (transport receiptTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	started := time.Now()
	response, err := transport.next.RoundTrip(request)
	if trace, ok := request.Context().Value(observationTraceKey{}).(*observationTrace); ok {
		kind, _ := request.Context().Value(queryKindKey{}).(string)
		receipt := QueryReceipt{QueryKind: kind, Method: request.Method, StartedAt: started.UTC()}
		if response != nil {
			receipt.HTTPStatus = response.StatusCode
			if response.StatusCode >= http.StatusBadRequest {
				receipt.Error = fmt.Sprintf("HTTP %d", response.StatusCode)
			}
		}
		finish := func(failure error) {
			finished := time.Now()
			receipt.FinishedAt = finished.UTC()
			receipt.DurationSeconds = finished.Sub(started).Seconds()
			if failure == nil {
				failure = request.Context().Err()
			}
			if failure != nil {
				receipt.Error = failure.Error()
			}
			trace.queries = append(trace.queries, receipt)
		}
		if err != nil || response == nil || response.Body == nil {
			finish(err)
		} else {
			response.Body = &receiptBody{ReadCloser: response.Body, finish: finish}
		}
	}
	return response, err
}

type receiptBody struct {
	io.ReadCloser
	once   sync.Once
	finish func(error)
}

func (body *receiptBody) Read(buffer []byte) (int, error) {
	n, err := body.ReadCloser.Read(buffer)
	if err != nil {
		body.once.Do(func() {
			failure := err
			if failure == io.EOF {
				failure = nil
			}
			body.finish(failure)
		})
	}
	return n, err
}

func (body *receiptBody) Close() error {
	err := body.ReadCloser.Close()
	body.once.Do(func() { body.finish(err) })
	return err
}
