package utils

import (
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

func TestRunRefusesDefaultContextWithoutOwnedRunner(t *testing.T) {
	t.Setenv("PHPA_E2E_STATE", "")
	t.Setenv("KUBECONFIG", filepath.Join(t.TempDir(), "user.kubeconfig"))
	if _, err := Run(exec.Command("a-command-that-must-never-run")); err == nil {
		t.Fatal("Run allowed a command outside the owned E2E runner")
	}
}

func TestEnvironmentFlagAloneDoesNotAuthorizeClusterCommands(t *testing.T) {
	state := filepath.Join(t.TempDir(), "e2e-owner.json")
	if err := os.WriteFile(state, []byte(`{}`), 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PHPA_E2E_STATE", state)
	if err := ValidateE2EEnvironment(); err == nil {
		t.Fatal("Empty ownership state authorized E2E")
	}
}
