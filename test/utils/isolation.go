package utils

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
)

// ValidateE2EEnvironment refuses direct E2E execution against the user's context.
// The runner's wrappers additionally verify node ownership and cluster UID before
// every Kubernetes command, including kubectl invoked by nested make recipes.
func ValidateE2EEnvironment() error {
	statePath := os.Getenv("PHPA_E2E_STATE")
	if !filepath.IsAbs(statePath) {
		return fmt.Errorf("E2E requires the owned runner; use make test-e2e")
	}
	contents, err := os.ReadFile(statePath)
	if err != nil {
		return fmt.Errorf("read E2E ownership state: %w", err)
	}
	var state struct {
		Kubeconfig string `json:"kubeconfig"`
		Cluster    string `json:"cluster_name"`
		NodeID     string `json:"node_container_id"`
		ClusterUID string `json:"cluster_uid"`
	}
	if err := json.Unmarshal(contents, &state); err != nil {
		return fmt.Errorf("decode E2E ownership state: %w", err)
	}
	private := filepath.Dir(statePath)
	if state.Kubeconfig != filepath.Join(private, "cluster.kubeconfig") || state.NodeID == "" || state.ClusterUID == "" {
		return fmt.Errorf("E2E ownership state is incomplete")
	}
	if os.Getenv("KUBECONFIG") != state.Kubeconfig || os.Getenv("KIND_CLUSTER") != state.Cluster {
		return fmt.Errorf("E2E cluster environment differs from the owned state")
	}
	for _, tool := range []string{"kubectl", "kind"} {
		expected := filepath.Join(private, "bin", tool)
		resolved, err := exec.LookPath(tool)
		if err != nil || resolved != expected {
			return fmt.Errorf("E2E %s must resolve to its owned guard wrapper", tool)
		}
		variable := "KUBECTL"
		if tool == "kind" {
			variable = "KIND"
		}
		if os.Getenv(variable) != expected {
			return fmt.Errorf("E2E %s must point to its owned guard wrapper", variable)
		}
	}
	return nil
}
