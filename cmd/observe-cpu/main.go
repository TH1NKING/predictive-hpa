// observe-cpu records verified live CPU observations for a pinned Kind target.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/go-logr/logr"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/clientcmd"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	"github.com/th1nking/predictive-hpa/internal/metricsprovider"
)

const protocolVersion = "cadence-cpu-v1"

type options struct {
	context, kubeconfig, namespace, deployment, targetUID, prometheusAddress, stopFile string
	interval, timeout                                                                  time.Duration
}

type observationRecord struct {
	ProtocolVersion string `json:"protocol_version"`
	Kind            string `json:"kind"`
	Sequence        uint64 `json:"sequence"`
	metricsprovider.CPUObservation
}

type summaryRecord struct {
	ProtocolVersion        string    `json:"protocol_version"`
	Kind                   string    `json:"kind"`
	Status                 string    `json:"status"`
	TargetUID              string    `json:"target_uid"`
	Observations           uint64    `json:"observations"`
	SuccessfulObservations uint64    `json:"successful_observations"`
	RejectedObservations   uint64    `json:"rejected_observations"`
	FinishedAt             time.Time `json:"finished_at"`
	Error                  string    `json:"error"`
}

func main() {
	logf.SetLogger(logr.Discard())
	if err := run(os.Args[1:], os.Stdout, os.Stderr); err != nil {
		_, _ = fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func parseOptions(arguments []string, stderr io.Writer) (options, error) {
	var opts options
	flags := flag.NewFlagSet("observe-cpu", flag.ContinueOnError)
	flags.SetOutput(stderr)
	flags.StringVar(&opts.context, "context", "", "Explicit Kind kubeconfig context")
	flags.StringVar(&opts.kubeconfig, "kubeconfig", "", "Optional kubeconfig path")
	flags.StringVar(&opts.namespace, "namespace", "default", "Target namespace")
	flags.StringVar(&opts.deployment, "deployment", "php-apache", "Target Deployment name")
	flags.StringVar(&opts.targetUID, "target-uid", "", "Required target Deployment UID")
	flags.StringVar(&opts.prometheusAddress, "prometheus-address", "", "Prometheus HTTP address")
	flags.StringVar(&opts.stopFile, "stop-file", "", "New regular file whose creation requests successful completion")
	flags.DurationVar(&opts.interval, "interval", time.Second, "Observation interval (must remain 1s)")
	flags.DurationVar(&opts.timeout, "timeout", 1500*time.Second, "Maximum runtime (positive and at most 1500s)")
	if err := flags.Parse(arguments); err != nil {
		return opts, err
	}
	if len(flags.Args()) != 0 {
		return opts, fmt.Errorf("unexpected positional arguments")
	}
	if !strings.HasPrefix(opts.context, "kind-") || len(opts.context) == len("kind-") {
		return opts, fmt.Errorf("--context must explicitly name a Kind context (kind-<cluster>)")
	}
	if strings.TrimSpace(opts.targetUID) == "" || strings.TrimSpace(opts.namespace) == "" ||
		strings.TrimSpace(opts.deployment) == "" {
		return opts, fmt.Errorf("--target-uid, --namespace and --deployment must be nonempty")
	}
	address, err := url.Parse(opts.prometheusAddress)
	if err != nil || address.Host == "" || (address.Scheme != "http" && address.Scheme != "https") {
		return opts, fmt.Errorf("--prometheus-address must be an HTTP(S) address")
	}
	if opts.interval != time.Second {
		return opts, fmt.Errorf("--interval must remain 1s for the cadence protocol")
	}
	if opts.timeout <= 0 || opts.timeout > 1500*time.Second {
		return opts, fmt.Errorf("--timeout must be positive and at most 1500s")
	}
	if opts.stopFile == "" {
		return opts, fmt.Errorf("--stop-file is required")
	}
	if _, err := os.Lstat(opts.stopFile); err == nil {
		return opts, fmt.Errorf("--stop-file must not exist at startup")
	} else if !errors.Is(err, os.ErrNotExist) {
		return opts, fmt.Errorf("inspect --stop-file: %w", err)
	}
	return opts, nil
}

func newProvider(opts options) (*metricsprovider.PrometheusProvider, error) {
	loading := clientcmd.NewDefaultClientConfigLoadingRules()
	loading.ExplicitPath = opts.kubeconfig
	overrides := &clientcmd.ConfigOverrides{CurrentContext: opts.context}
	config, err := clientcmd.NewNonInteractiveDeferredLoadingClientConfig(loading, overrides).ClientConfig()
	if err != nil {
		return nil, fmt.Errorf("load explicit Kind context: %w", err)
	}
	config.Timeout = 10 * time.Second
	scheme := runtime.NewScheme()
	if err := appsv1.AddToScheme(scheme); err != nil {
		return nil, err
	}
	if err := corev1.AddToScheme(scheme); err != nil {
		return nil, err
	}
	// These built-in v1 kinds avoid discovery requests; the client itself remains
	// uncached so the provider reads both authoritative roster snapshots.
	mapper := meta.NewDefaultRESTMapper([]schema.GroupVersion{appsv1.SchemeGroupVersion, corev1.SchemeGroupVersion})
	mapper.Add(appsv1.SchemeGroupVersion.WithKind("Deployment"), meta.RESTScopeNamespace)
	mapper.Add(appsv1.SchemeGroupVersion.WithKind("ReplicaSet"), meta.RESTScopeNamespace)
	mapper.Add(corev1.SchemeGroupVersion.WithKind("Pod"), meta.RESTScopeNamespace)
	reader, err := client.New(config, client.Options{Scheme: scheme, Mapper: mapper})
	if err != nil {
		return nil, fmt.Errorf("build uncached Kubernetes reader: %w", err)
	}
	return metricsprovider.NewPrometheus(opts.prometheusAddress, reader)
}

func run(arguments []string, stdout, stderr io.Writer) error {
	opts, err := parseOptions(arguments, stderr)
	if err != nil {
		return err
	}
	provider, err := newProvider(opts)
	if err != nil {
		return err
	}
	signalContext, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	ctx, cancel := context.WithTimeout(signalContext, opts.timeout)
	defer cancel()
	target := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{
		Namespace: opts.namespace, Name: opts.deployment, UID: types.UID(opts.targetUID),
	}}
	encoder := json.NewEncoder(stdout)
	summary := summaryRecord{ProtocolVersion: protocolVersion, Kind: "cpu_observer_summary", TargetUID: opts.targetUID}
	finish := func(status string, cause error) error {
		summary.Status = status
		summary.FinishedAt = time.Now().UTC()
		if cause != nil {
			summary.Error = cause.Error()
		}
		if err := encoder.Encode(summary); err != nil {
			return fmt.Errorf("write observer summary: %w", err)
		}
		return cause
	}
	ticker := time.NewTicker(opts.interval)
	defer ticker.Stop()
	for {
		if err := ctx.Err(); err != nil {
			status := "interrupted"
			if errors.Is(err, context.DeadlineExceeded) {
				status = "timeout"
			}
			return finish(status, err)
		}
		if info, err := os.Lstat(opts.stopFile); err == nil {
			if !info.Mode().IsRegular() {
				return finish("error", fmt.Errorf("--stop-file must be a regular file"))
			}
			return finish("completed", nil)
		} else if !errors.Is(err, os.ErrNotExist) {
			return finish("error", fmt.Errorf("inspect --stop-file: %w", err))
		}
		observation, observationErr := provider.ObserveCPU(ctx, target)
		summary.Observations++
		if observationErr == nil {
			summary.SuccessfulObservations++
		} else {
			summary.RejectedObservations++
		}
		record := observationRecord{ProtocolVersion: protocolVersion, Kind: "cpu_observation",
			Sequence: summary.Observations, CPUObservation: observation}
		if err := encoder.Encode(record); err != nil {
			return fmt.Errorf("write CPU observation: %w", err)
		}
		select {
		case <-ctx.Done():
		case <-ticker.C:
		}
	}
}
