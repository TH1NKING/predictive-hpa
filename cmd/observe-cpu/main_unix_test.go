//go:build !windows

package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"io"
	"os"
	"os/exec"
	"syscall"
	"testing"
	"time"
)

func TestObserverTreatsUnexpectedSIGTERMAsFailure(t *testing.T) {
	f := newObserverFixture(t, observerFixtureOptions{noStop: true})
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	arguments, err := json.Marshal(f.args)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, executable, "-test.run=^TestObserveCPUProcess$")
	command.Env = append(os.Environ(), "PHPA_OBSERVE_CPU_TEST_PROCESS=1",
		"PHPA_OBSERVE_CPU_TEST_ARGS="+string(arguments))
	var stderr bytes.Buffer
	command.Stderr = &stderr
	stdout, err := command.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := command.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if command.ProcessState == nil {
			_ = command.Process.Kill()
			_ = command.Wait()
		}
	})
	reader := bufio.NewReader(stdout)
	first, err := reader.ReadString('\n')
	if err != nil {
		t.Fatalf("read first observation: %v", err)
	}
	if err := command.Process.Signal(syscall.SIGTERM); err != nil {
		t.Fatal(err)
	}
	rest, err := io.ReadAll(reader)
	if err != nil {
		t.Fatal(err)
	}
	if err := command.Wait(); err == nil {
		t.Fatalf("unexpected SIGTERM succeeded: %s%s", first, rest)
	}
	records := decodeObserverRecords(t, first+string(rest))
	if len(records) < 2 || records[len(records)-1]["status"] != "interrupted" ||
		records[len(records)-1]["kind"] != "cpu_observer_summary" {
		t.Fatalf("SIGTERM did not retain an interrupted summary: %s%s stderr=%s", first, rest, stderr.String())
	}
}
