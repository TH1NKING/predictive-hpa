package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

const rejectedStatus = "rejected"

func TestObserveCPUProcess(_ *testing.T) {
	if os.Getenv("PHPA_OBSERVE_CPU_TEST_PROCESS") != "1" {
		return
	}
	var arguments []string
	if err := json.Unmarshal([]byte(os.Getenv("PHPA_OBSERVE_CPU_TEST_ARGS")), &arguments); err != nil {
		panic(err)
	}
	os.Args = append([]string{"observe-cpu"}, arguments...)
	main()
	os.Exit(0)
}

type observerFixture struct {
	args     []string
	stopFile string
	requests atomic.Int32
}

// Configure the HTTP boundaries before starting their serving goroutines.
// The options are copied into newObserverFixture and remain immutable there.
type observerFixtureOptions struct {
	rateBodyHang bool
	failure      string
	getFallback  bool
	kubeFailure  string
	noStop       bool
}

func newObserverFixture(t *testing.T, opts observerFixtureOptions) *observerFixture {
	t.Helper()
	directory := t.TempDir()
	f := &observerFixture{stopFile: filepath.Join(directory, "stop")}
	kube := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if request.Method != http.MethodGet {
			t.Errorf("read-only observer attempted %s %s", request.Method, request.URL.Path)
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		switch request.URL.Path {
		case "/apis/apps/v1/namespaces/default/deployments/php-apache":
			body := deploymentResponse
			if opts.kubeFailure == "forbidden" {
				w.WriteHeader(http.StatusForbidden)
				_, _ = w.Write([]byte(`{"apiVersion":"v1","kind":"Status","status":"Failure",
"reason":"Forbidden","message":"forbidden by test API","code":403}`))
				f.requestStop(t)
				return
			}
			if opts.kubeFailure == "post-query replacement" && f.requests.Load() > 0 {
				body = strings.ReplaceAll(body, "deployment-uid", "replacement-uid")
			}
			_, _ = w.Write([]byte(body))
		case "/apis/apps/v1/namespaces/default/replicasets/web-rs":
			_, _ = w.Write([]byte(replicaSetResponse))
		case "/api/v1/namespaces/default/pods":
			_, _ = w.Write([]byte(podsResponse))
		default:
			t.Errorf("unexpected Kubernetes request %s", request.URL.String())
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(kube.Close)
	prometheus := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		f.requests.Add(1)
		if opts.getFallback && request.Method == http.MethodPost {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		isSource := strings.HasPrefix(request.FormValue("query"), "timestamp(")
		if opts.failure == "rate unavailable" || (opts.failure == "timestamp unavailable" && isSource) {
			w.WriteHeader(http.StatusServiceUnavailable)
			_, _ = w.Write([]byte(`{"status":"error","errorType":"unavailable","error":"offline"}`))
			f.requestStop(t)
			return
		}
		if opts.rateBodyHang {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"status":"success","data":`))
			w.(http.Flusher).Flush()
			<-request.Context().Done()
			return
		}
		at, err := strconv.ParseFloat(request.FormValue("time"), 64)
		if err != nil {
			t.Errorf("missing evaluation time: %v", err)
		}
		values := []any{}
		for _, container := range []struct{ name, id, rate string }{{"app", "aabbcc", "0.1"}, {"worker", "ddeeff", "0.8"}} {
			if opts.failure == "partial coverage" && container.name == "worker" {
				continue
			}
			if opts.failure == "wrong identity" {
				container.id = "old-instance"
			}
			value := container.rate
			if isSource {
				value = strconv.FormatFloat(at-5, 'f', 3, 64)
				if opts.failure == "stale source" {
					value = strconv.FormatFloat(at-46, 'f', 3, 64)
				}
				if opts.failure == "future source" {
					value = strconv.FormatFloat(at+1, 'f', 3, 64)
				}
			}
			values = append(values, map[string]any{"metric": map[string]string{
				"namespace": "default", "pod": "web-pod", "container": container.name, "cpu": "total",
				"id": "/kubepods/pod12345678-1234-1234-1234-123456789abc/" + container.id,
			}, "value": []any{at, value}})
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "success",
			"data": map[string]any{"resultType": "vector", "result": values}})
		if isSource && !opts.noStop {
			f.requestStop(t)
		}
	}))
	t.Cleanup(prometheus.Close)
	kubeconfig := filepath.Join(directory, "config")
	config := fmt.Sprintf(`apiVersion: v1
kind: Config
clusters:
- name: kind-cadence-test
  cluster:
    server: %s
contexts:
- name: kind-cadence-test
  context:
    cluster: kind-cadence-test
    user: tester
users:
- name: tester
  user: {}
current-context: forbidden-implicit-context
`, kube.URL)
	if err := os.WriteFile(kubeconfig, []byte(config), 0600); err != nil {
		t.Fatal(err)
	}
	f.args = []string{"--context=kind-cadence-test", "--kubeconfig=" + kubeconfig,
		"--target-uid=deployment-uid", "--prometheus-address=" + prometheus.URL,
		"--stop-file=" + f.stopFile, "--timeout=5s"}
	return f
}

func (f *observerFixture) requestStop(t *testing.T) {
	t.Helper()
	if err := os.WriteFile(f.stopFile, []byte("stop"), 0600); err != nil {
		t.Errorf("create stop file: %v", err)
	}
}

func TestObserverPreservesRejectedAttemptsWithoutPublishingPartialEvidence(t *testing.T) {
	for _, scenario := range []struct {
		name, errorPart string
		queries         int
	}{
		{"partial coverage", "incomplete data", 2},
		{"wrong identity", "incomplete data", 2},
		{"stale source", "stale source data", 2},
		{"future source", "invalid data", 2},
		{"rate unavailable", "instant query", 1},
		{"timestamp unavailable", "instant query", 2},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			t.Parallel()
			f := newObserverFixture(t, observerFixtureOptions{failure: scenario.name})
			code, stdout, stderr := invokeObserver(t, f.args)
			if code != 0 {
				t.Fatalf("explicit stop failed: %d %s %s", code, stderr, stdout)
			}
			records := decodeObserverRecords(t, stdout)
			if len(records) != 2 {
				t.Fatalf("rejected attempt disappeared: %s", stdout)
			}
			observation := records[0]
			message, _ := observation["error"].(string)
			if observation["status"] != rejectedStatus || !strings.Contains(message, scenario.errorPart) ||
				observation["source_timestamp"] != nil || observation["utilization_percent"] != nil ||
				len(observation["containers"].([]any)) != 0 {
				t.Fatalf("unsafe input published accepted evidence: %s", stdout)
			}
			queries := observation["queries"].([]any)
			if len(queries) != scenario.queries || int(f.requests.Load()) != scenario.queries {
				t.Fatalf("actual request count lost on rejection: %s", stdout)
			}
			if records[1]["rejected_observations"] != float64(1) || records[1]["successful_observations"] != float64(0) {
				t.Fatalf("rejection missing from terminal summary: %s", stdout)
			}
		})
	}
}

func TestObserverCountsHTTPFallbackAttemptsInsteadOfAssumingTwoQueries(t *testing.T) {
	f := newObserverFixture(t, observerFixtureOptions{getFallback: true})
	code, stdout, stderr := invokeObserver(t, f.args)
	if code != 0 {
		t.Fatalf("fallback failed: %d %s %s", code, stderr, stdout)
	}
	records := decodeObserverRecords(t, stdout)
	queries := records[0]["queries"].([]any)
	if records[0]["status"] != "success" || len(queries) != 4 || f.requests.Load() != 4 {
		t.Fatalf("POST/GET attempts were inferred from expressions: %s", stdout)
	}
	for index, expected := range []struct {
		kind, method string
		status       int
	}{
		{"rate", "POST", 405}, {"rate", "GET", 200}, {"timestamp", "POST", 405}, {"timestamp", "GET", 200},
	} {
		query := queries[index].(map[string]any)
		if query["query_kind"] != expected.kind || query["method"] != expected.method ||
			query["http_status"] != float64(expected.status) {
			t.Fatalf("invalid fallback receipt: %v", query)
		}
	}
}

func TestObserverRejectsUnsafeInvocationBeforeSendingQueries(t *testing.T) {
	for _, scenario := range []struct{ argument, errorPart string }{
		{"--context=", "--context"}, {"--context=production", "--context"},
		{"--target-uid=", "--target-uid"}, {"--interval=15s", "--interval"},
		{"--timeout=0s", "--timeout"}, {"--timeout=1501s", "--timeout"},
		{"--prometheus-address=file:///tmp/prometheus", "--prometheus-address"},
		{"--stop-file=", "--stop-file"},
	} {
		t.Run(scenario.argument, func(t *testing.T) {
			t.Parallel()
			f := newObserverFixture(t, observerFixtureOptions{})
			code, stdout, stderr := invokeObserver(t, append(f.args, scenario.argument))
			if code == 0 || stdout != "" || !strings.Contains(stderr, scenario.errorPart) || f.requests.Load() != 0 {
				t.Fatalf("invalid invocation was not rejected: %d %s %s", code, stdout, stderr)
			}
		})
	}
}

func TestObserverRejectsStaleStopFileAtStartup(t *testing.T) {
	f := newObserverFixture(t, observerFixtureOptions{})
	f.requestStop(t)
	code, stdout, stderr := invokeObserver(t, f.args)
	if code == 0 || stdout != "" || !strings.Contains(stderr, "must not exist") || f.requests.Load() != 0 {
		t.Fatalf("stale stop file produced successful completion: %d %s %s", code, stdout, stderr)
	}
}

func TestObserverRetainsBodyReadTimeoutAndFullHTTPRequestDuration(t *testing.T) {
	f := newObserverFixture(t, observerFixtureOptions{rateBodyHang: true})
	f.args = append(f.args, "--timeout=250ms")
	code, stdout, stderr := invokeObserver(t, f.args)
	if code == 0 {
		t.Fatalf("timed out observer exited successfully: %s %s", stdout, stderr)
	}
	records := decodeObserverRecords(t, stdout)
	if len(records) != 2 || records[0]["status"] != rejectedStatus || records[1]["status"] != "timeout" {
		t.Fatalf("lost rejected attempt or timeout summary: %s", stdout)
	}
	queries, ok := records[0]["queries"].([]any)
	if !ok || len(queries) != 1 || f.requests.Load() != 1 {
		t.Fatalf("invented timestamp request after rate timeout: %s", stdout)
	}
	query := queries[0].(map[string]any)
	if query["query_kind"] != "rate" || query["http_status"] != float64(200) ||
		query["error"] == "" || query["duration_seconds"].(float64) < 0.1 {
		t.Fatalf("body timeout or HTTP duration lost after headers returned: %v", query)
	}
}

func invokeObserver(t *testing.T, arguments []string) (int, string, string) {
	t.Helper()
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	encoded, err := json.Marshal(arguments)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, executable, "-test.run=^TestObserveCPUProcess$")
	command.Env = append(os.Environ(), "PHPA_OBSERVE_CPU_TEST_PROCESS=1", "PHPA_OBSERVE_CPU_TEST_ARGS="+string(encoded))
	var stdout, stderr bytes.Buffer
	command.Stdout, command.Stderr = &stdout, &stderr
	err = command.Run()
	if err == nil {
		return 0, stdout.String(), stderr.String()
	}
	if failure, ok := err.(*exec.ExitError); ok {
		return failure.ExitCode(), stdout.String(), stderr.String()
	}
	t.Fatal(err)
	return -1, "", ""
}

func decodeObserverRecords(t *testing.T, output string) []map[string]any {
	t.Helper()
	var records []map[string]any
	scanner := bufio.NewScanner(strings.NewReader(output))
	for scanner.Scan() {
		var record map[string]any
		if err := json.Unmarshal(scanner.Bytes(), &record); err != nil {
			t.Fatalf("non-NDJSON output: %s: %v", scanner.Text(), err)
		}
		records = append(records, record)
	}
	if err := scanner.Err(); err != nil {
		t.Fatal(err)
	}
	return records
}

func TestObserverEmitsVerifiedCPUAndStopsOnlyOnExplicitNewStopFile(t *testing.T) {
	f := newObserverFixture(t, observerFixtureOptions{})
	before := time.Now()
	code, stdout, stderr := invokeObserver(t, f.args)
	after := time.Now()
	if code != 0 {
		t.Fatalf("exit=%d stderr=%s stdout=%s", code, stderr, stdout)
	}
	records := decodeObserverRecords(t, stdout)
	if len(records) != 2 {
		t.Fatalf("expected observation and terminal summary: %s", stdout)
	}
	observation, summary := records[0], records[1]
	if observation["protocol_version"] != "cadence-cpu-v1" || observation["kind"] != "cpu_observation" ||
		observation["sequence"] != float64(1) || observation["target_uid"] != "deployment-uid" ||
		observation["status"] != "success" || observation["utilization_percent"] != float64(90) {
		t.Fatalf("invalid observation: %s", stdout)
	}
	containers, ok := observation["containers"].([]any)
	if !ok || len(containers) != 2 {
		t.Fatalf("partial container evidence: %s", stdout)
	}
	for index, expected := range []struct{ name, id string }{{"app", "aabbcc"}, {"worker", "ddeeff"}} {
		container := containers[index].(map[string]any)
		if container["pod"] != "web-pod" || container["pod_uid"] != "12345678-1234-1234-1234-123456789abc" ||
			container["container"] != expected.name || container["runtime_id"] != expected.id ||
			container["source_timestamp"] == nil {
			t.Fatalf("missing exact identity: %v", container)
		}
	}
	queries, ok := observation["queries"].([]any)
	if !ok || len(queries) != 2 || f.requests.Load() != 2 {
		t.Fatalf("actual requests missing: %s", stdout)
	}
	for _, field := range []string{"observation_started_at", "observation_finished_at", "evaluated_at"} {
		stamp, err := time.Parse(time.RFC3339Nano, observation[field].(string))
		if err != nil || stamp.Before(before.Add(-time.Millisecond)) || stamp.After(after) {
			t.Fatalf("invalid %s: %v", field, observation[field])
		}
	}
	if summary["protocol_version"] != "cadence-cpu-v1" || summary["kind"] != "cpu_observer_summary" ||
		summary["status"] != "completed" || summary["observations"] != float64(1) ||
		summary["successful_observations"] != float64(1) || summary["rejected_observations"] != float64(0) {
		t.Fatalf("invalid terminal summary: %s", stdout)
	}
}

const deploymentResponse = `{
  "apiVersion":"apps/v1","kind":"Deployment",
  "metadata":{"name":"php-apache","namespace":"default","uid":"deployment-uid","generation":1},
  "spec":{"selector":{"matchLabels":{"app":"web"}}}
}`

const replicaSetResponse = `{
  "apiVersion":"apps/v1","kind":"ReplicaSet",
  "metadata":{"name":"web-rs","namespace":"default","uid":"rs-uid",
    "ownerReferences":[{"apiVersion":"apps/v1","kind":"Deployment",
      "name":"php-apache","uid":"deployment-uid","controller":true}]}
}`

const podsResponse = `{
  "apiVersion":"v1","kind":"PodList","items":[{
    "metadata":{"name":"web-pod","namespace":"default","uid":"12345678-1234-1234-1234-123456789abc",
      "labels":{"app":"web"},"ownerReferences":[{"apiVersion":"apps/v1","kind":"ReplicaSet",
        "name":"web-rs","uid":"rs-uid","controller":true}]},
    "spec":{"containers":[
      {"name":"app","resources":{"requests":{"cpu":"200m"}}},
      {"name":"worker","resources":{"requests":{"cpu":"800m"}}}]},
    "status":{"phase":"Running","conditions":[{"type":"Ready","status":"True"}],
      "containerStatuses":[
        {"name":"app","ready":true,"containerID":"containerd://aabbcc","state":{"running":{}}},
        {"name":"worker","ready":true,"containerID":"containerd://ddeeff","state":{"running":{}}}]
    }
  }]
}`

func TestObserverRetainsKubernetesRejectionsAndDoesNotPublishReplacedTargets(t *testing.T) {
	for _, scenario := range []struct {
		name    string
		queries int
	}{{"forbidden", 0}, {"post-query replacement", 2}} {
		t.Run(scenario.name, func(t *testing.T) {
			t.Parallel()
			f := newObserverFixture(t, observerFixtureOptions{kubeFailure: scenario.name})
			code, stdout, stderr := invokeObserver(t, f.args)
			if code != 0 {
				t.Fatalf("explicit stop failed: %d %s %s", code, stderr, stdout)
			}
			records := decodeObserverRecords(t, stdout)
			if len(records) != 2 {
				t.Fatalf("missing rejected observation: %s", stdout)
			}
			observation := records[0]
			if observation["status"] != rejectedStatus || observation["target_uid"] != "deployment-uid" ||
				observation["source_timestamp"] != nil || len(observation["containers"].([]any)) != 0 ||
				len(observation["queries"].([]any)) != scenario.queries || int(f.requests.Load()) != scenario.queries {
				t.Fatalf("Kubernetes failure erased or replacement accepted: %s", stdout)
			}
		})
	}
}
