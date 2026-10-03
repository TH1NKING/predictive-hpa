//go:build e2e

package e2e

import (
	_ "embed"
	"fmt"
	"net"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
)

//go:embed curl-image.txt
var curlImageReference string

// The projected token only enters curl through stdin. Neither Pod arguments nor
// debug output contain the credential, including when an HTTP request fails.
const metricsProbeScript = `set -eu
for i in $(seq 1 30); do
  if printf 'header = "Authorization: Bearer %s"\n' "$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" |
    curl --config - --silent --show-error --fail --insecure --noproxy '*' \
      --connect-timeout 3 --max-time 5 --resolve "$METRICS_HOST:8443:$METRICS_ADDRESS" \
      --write-out '\nHTTP_STATUS=%{http_code}\n' "https://$METRICS_HOST:8443/metrics"; then
    exit 0
  fi
  sleep 2
done
exit 1`

func metricsProbePod(serviceIP string) (*corev1.Pod, error) {
	parsed := net.ParseIP(strings.TrimSpace(serviceIP))
	if parsed == nil {
		return nil, fmt.Errorf("invalid metrics Service ClusterIP %q", serviceIP)
	}
	address := parsed.String()
	if parsed.To4() == nil {
		address = "[" + address + "]"
	}
	return &corev1.Pod{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "Pod"},
		ObjectMeta: metav1.ObjectMeta{Name: "curl-metrics", Namespace: namespace},
		Spec: corev1.PodSpec{
			ServiceAccountName: serviceAccountName, AutomountServiceAccountToken: ptr.To(true),
			RestartPolicy: corev1.RestartPolicyNever,
			Containers: []corev1.Container{{
				Name: "curl", Image: strings.TrimSpace(curlImageReference), ImagePullPolicy: corev1.PullIfNotPresent,
				Command: []string{"/bin/sh", "-c"}, Args: []string{metricsProbeScript},
				Env: []corev1.EnvVar{
					{Name: "METRICS_HOST", Value: metricsServiceName + "." + namespace + ".svc.cluster.local"},
					{Name: "METRICS_ADDRESS", Value: address},
				},
				SecurityContext: &corev1.SecurityContext{
					ReadOnlyRootFilesystem: ptr.To(true), AllowPrivilegeEscalation: ptr.To(false),
					RunAsNonRoot: ptr.To(true), RunAsUser: ptr.To[int64](1000),
					Capabilities:   &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
					SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
				},
			}},
		},
	}, nil
}

func TestMetricsProbeUsesServiceIPAndProjectedCredentials(t *testing.T) {
	for _, test := range []struct{ input, address string }{
		{"10.96.12.3", "10.96.12.3"}, {"fd00::123", "[fd00::123]"},
	} {
		pod, err := metricsProbePod(test.input)
		if err != nil {
			t.Fatal(err)
		}
		container := pod.Spec.Containers[0]
		if container.Env[1].Value != test.address || container.ImagePullPolicy != corev1.PullIfNotPresent {
			t.Fatalf("probe lost Service routing or preloaded image policy: %+v", container)
		}
		if pod.Spec.ServiceAccountName != serviceAccountName || !*pod.Spec.AutomountServiceAccountToken {
			t.Fatal("probe must use the authorized projected service account token")
		}
	}
	for _, input := range []string{"", "None", "example.com", "10.96.0.1; echo unsafe"} {
		if _, err := metricsProbePod(input); err == nil {
			t.Fatalf("accepted invalid ClusterIP %q", input)
		}
	}
}
