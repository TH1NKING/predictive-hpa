package main

import (
	"bytes"
	"encoding/json"
	"os"
	"os/exec"
	"strings"
	"testing"
)

// The helper invokes the real command entry point in a child process, preserving
// stdin/stdout and exit-code behavior without mocking any controller component.
func TestReplayProcess(_ *testing.T) {
	if os.Getenv("PHPA_REPLAY_TEST_PROCESS") != "1" {
		return
	}
	os.Args = []string{"replay", "-input", "-"}
	main()
	os.Exit(0)
}

func invokeReplay(t *testing.T, input string) (int, string, string) {
	t.Helper()
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	command := exec.Command(executable, "-test.run=^TestReplayProcess$")
	command.Env = append(os.Environ(), "PHPA_REPLAY_TEST_PROCESS=1")
	command.Stdin = strings.NewReader(input)
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

const fixedReplicaInput = `{
  "schemaVersion": 1,
  "policyHistoryCompleteness": "complete",
  "config": {"minReplicas":1,"maxReplicas":20,"targetCPU":50,"alphaPercent":30,
             "windowSeconds":300,"horizonSeconds":30,"stabilizationSeconds":0},
  "cycles": [
    {"at":"2026-09-11T00:00:00Z","predictionSource":"recorded-forecast",
     "observedReplicas":2,"requestedReplicas":2,"currentCPU":100,"rawPrediction":130,
     "actualMode":"Predictive","expected":{"decisionCPU":130,"boundedPrediction":130,
       "desiredReplicas":6,"finalDesired":6,"skipReason":"","stabilized":false}},
    {"at":"2026-09-11T00:00:30Z","predictionSource":"recorded-forecast",
     "observedReplicas":2,"requestedReplicas":2,"currentCPU":25,"rawPrediction":25,
     "actualMode":"Predictive","expected":{"decisionCPU":25,"boundedPrediction":25,
       "desiredReplicas":1,"finalDesired":1,"skipReason":"","stabilized":false}}
  ]
}`

func TestReplayComparesModesAgainstFixedReplicaInputs(t *testing.T) {
	code, stdout, stderr := invokeReplay(t, fixedReplicaInput)
	if code != 0 {
		t.Fatalf("exit %d: %s", code, stderr)
	}
	var report struct {
		Verified bool `json:"verified"`
		Cycles   []struct {
			Modes map[string]struct {
				FinalDesired int `json:"finalDesired"`
			} `json:"modes"`
		} `json:"cycles"`
	}
	if err := json.Unmarshal([]byte(stdout), &report); err != nil {
		t.Fatal(err)
	}
	if !report.Verified || len(report.Cycles) != 2 {
		t.Fatalf("missing verified comparisons: %s", stdout)
	}
	for mode, want := range map[string]int{"Current": 4, "Predictive": 6, "Hybrid": 4} {
		if got := report.Cycles[0].Modes[mode].FinalDesired; got != want {
			t.Errorf("first %s recommendation = %d, want %d", mode, got, want)
		}
		if got := report.Cycles[1].Modes[mode].FinalDesired; got != 1 {
			t.Errorf("second %s recommendation = %d, want 1 from the supplied two replicas", mode, got)
		}
	}
}

func TestReplayWithholdsComparisonsWhenRecordedDecisionDoesNotMatch(t *testing.T) {
	input := strings.Replace(fixedReplicaInput, `"finalDesired":1`, `"finalDesired":2`, 1)
	code, stdout, stderr := invokeReplay(t, input)
	if code != 1 || stdout != "" || !strings.Contains(stderr, "finalDesired") {
		t.Fatalf("want mismatch exit 1 with no comparisons and named field, got %d stdout=%s stderr=%s",
			code, stdout, stderr)
	}
}

const computedHistoryInput = `{
      "schemaVersion":1,"policyHistoryCompleteness":"complete",
      "config":{"minReplicas":1,"maxReplicas":20,"targetCPU":50,"alphaPercent":50,
                "windowSeconds":300,"horizonSeconds":30,"stabilizationSeconds":0},
      "cycles":[{"at":"2026-09-11T00:00:00Z","predictionSource":"computed-history",
        "observedReplicas":2,"requestedReplicas":2,"currentCPU":100,
        "samples":[{"timestamp":"2026-09-10T23:59:30Z","value":20},
                   {"timestamp":"2026-09-11T00:00:00Z","value":100}],
        "actualMode":"Predictive","expected":{"rawPrediction":94,"decisionCPU":94,
          "boundedPrediction":94,"desiredReplicas":4,"finalDesired":4,"skipReason":"","stabilized":false}}]
    }`

func TestReplayComputesForecastFromCompleteObservationHistory(t *testing.T) {
	code, stdout, stderr := invokeReplay(t, computedHistoryInput)
	if code != 0 || !strings.Contains(stdout, `"rawPrediction": 94`) {
		t.Fatalf("want the worked EWMA+damped trend forecast 94, got %d stdout=%s stderr=%s", code, stdout, stderr)
	}
}

func TestReplayRejectsIncompleteForecastEvidence(t *testing.T) {
	for name, input := range map[string]string{
		"missing sample value":  strings.Replace(computedHistoryInput, `,"value":20`, ``, 1),
		"negative sample":       strings.Replace(computedHistoryInput, `"value":20`, `"value":-20`, 1),
		"CPU differs from tail": strings.Replace(computedHistoryInput, `"currentCPU":100`, `"currentCPU":80`, 1),
		"future sample": strings.Replace(computedHistoryInput,
			`"at":"2026-09-11T00:00:00Z"`, `"at":"2026-09-10T23:59:59Z"`, 1),
		"stale observation": strings.Replace(computedHistoryInput,
			`"at":"2026-09-11T00:00:00Z"`, `"at":"2026-09-11T00:01:00Z"`, 1),
		"too close observations": strings.Replace(computedHistoryInput, `2026-09-10T23:59:30Z`, `2026-09-10T23:59:59Z`, 1),
		"outside history window": strings.Replace(computedHistoryInput, `2026-09-10T23:59:30Z`, `2026-09-10T23:54:00Z`, 1),
		"missing forecast":       strings.Replace(fixedReplicaInput, `,"rawPrediction":130`, ``, 1),
		"ambiguous prediction source": strings.Replace(fixedReplicaInput,
			`"rawPrediction":130`, `"rawPrediction":130,"samples":[]`, 1),
	} {
		t.Run(name, func(t *testing.T) {
			code, stdout, stderr := invokeReplay(t, input)
			if code != 2 || stdout != "" || stderr == "" {
				t.Fatalf("want incomplete forecast evidence exit 2, got %d stdout=%s stderr=%s", code, stdout, stderr)
			}
		})
	}
}

func TestReplayRejectsIncompleteOrUnusableRecording(t *testing.T) {
	for name, input := range map[string]string{
		"partial policy history": strings.Replace(fixedReplicaInput, `"complete"`, `"partial"`, 1),
		"unknown field": strings.Replace(fixedReplicaInput,
			`"schemaVersion": 1`, `"schemaVersion": 1,"typo":true`, 1),
		"trailing document":   fixedReplicaInput + `{}`,
		"unknown schema":      strings.Replace(fixedReplicaInput, `"schemaVersion": 1`, `"schemaVersion": 2`, 1),
		"missing observation": strings.Replace(fixedReplicaInput, `"observedReplicas":2,`, ``, 1),
		"missing requested":   strings.Replace(fixedReplicaInput, `"requestedReplicas":2,`, ``, 1),
		"missing CPU":         strings.Replace(fixedReplicaInput, `"currentCPU":100,`, ``, 1),
		"missing expectation": strings.Replace(fixedReplicaInput, `"stabilized":false`, `"other":false`, 1),
		"negative CPU":        strings.Replace(fixedReplicaInput, `"currentCPU":100`, `"currentCPU":-1`, 1),
		"backward clock":      strings.Replace(fixedReplicaInput, `2026-09-11T00:00:30Z`, `2026-09-10T23:59:30Z`, 1),
		"unknown mode":        strings.Replace(fixedReplicaInput, `"actualMode":"Predictive"`, `"actualMode":"Typo"`, 1),
		"changing actual mode": strings.Replace(fixedReplicaInput,
			`"actualMode":"Predictive","expected":{"decisionCPU":25`,
			`"actualMode":"Current","expected":{"decisionCPU":25`, 1),
		"unknown source":        strings.Replace(fixedReplicaInput, `"recorded-forecast"`, `"guessed"`, 1),
		"contradictory bounds":  strings.Replace(fixedReplicaInput, `"minReplicas":1`, `"minReplicas":21`, 1),
		"missing stabilization": strings.Replace(fixedReplicaInput, `,"stabilizationSeconds":0`, ``, 1),
		"invalid alpha":         strings.Replace(fixedReplicaInput, `"alphaPercent":30`, `"alphaPercent":100`, 1),
		"invalid window":        strings.Replace(fixedReplicaInput, `"windowSeconds":300`, `"windowSeconds":1`, 1),
		"backward forecast":     strings.Replace(fixedReplicaInput, `"horizonSeconds":30`, `"horizonSeconds":-1`, 1),
	} {
		t.Run(name, func(t *testing.T) {
			code, stdout, stderr := invokeReplay(t, input)
			if code != 2 || stdout != "" || stderr == "" {
				t.Fatalf("want invalid-input exit 2 and no comparisons, got %d stdout=%s stderr=%s", code, stdout, stderr)
			}
		})
	}
}

func TestReplayExplainsPendingRequestToleranceAndExpiringStabilization(t *testing.T) {
	input := `{
      "schemaVersion":1,"policyHistoryCompleteness":"complete",
      "config":{"minReplicas":1,"maxReplicas":20,"targetCPU":50,"alphaPercent":30,
                "windowSeconds":300,"horizonSeconds":30,"stabilizationSeconds":60},
      "cycles":[
        {"at":"2026-09-11T00:00:00Z","predictionSource":"recorded-forecast",
         "observedReplicas":8,"requestedReplicas":4,"currentCPU":25,"rawPrediction":25,
         "actualMode":"Current","expected":{"decisionCPU":25,"boundedPrediction":25,
           "desiredReplicas":4,"finalDesired":4,"skipReason":"DesiredEqualsCurrent","stabilized":false,
           "coldStartProtection":true,"protectedUntil":"2026-09-11T00:01:00Z","historyEntries":1,
           "historyOldestAt":"2026-09-11T00:00:00Z"}},
        {"at":"2026-09-11T00:00:30Z","predictionSource":"recorded-forecast",
         "observedReplicas":8,"requestedReplicas":2,"currentCPU":25,"rawPrediction":25,
         "actualMode":"Current","expected":{"decisionCPU":25,"boundedPrediction":25,
           "desiredReplicas":2,"finalDesired":2,"skipReason":"DesiredEqualsCurrent","stabilized":false}},
        {"at":"2026-09-11T00:01:01Z","predictionSource":"recorded-forecast",
         "observedReplicas":3,"requestedReplicas":3,"currentCPU":52,"rawPrediction":52,
         "actualMode":"Current","expected":{"decisionCPU":52,"boundedPrediction":52,
           "desiredReplicas":4,"finalDesired":4,"skipReason":"WithinToleranceBand","stabilized":false}},
        {"at":"2026-09-11T00:01:32Z","predictionSource":"recorded-forecast",
         "observedReplicas":4,"requestedReplicas":4,"currentCPU":5,"rawPrediction":5,
         "actualMode":"Current","expected":{"decisionCPU":5,"boundedPrediction":5,
           "desiredReplicas":1,"finalDesired":4,"skipReason":"DesiredEqualsCurrent","stabilized":true}},
        {"at":"2026-09-11T00:02:03Z","predictionSource":"recorded-forecast",
         "observedReplicas":4,"requestedReplicas":4,"currentCPU":5,"rawPrediction":5,
         "actualMode":"Current","expected":{"decisionCPU":5,"boundedPrediction":5,
           "desiredReplicas":1,"finalDesired":1,"skipReason":"","stabilized":false}}
      ]
    }`
	code, stdout, stderr := invokeReplay(t, input)
	if code != 0 {
		t.Fatalf("exit %d: %s", code, stderr)
	}
	var report struct {
		Cycles []struct {
			Modes map[string]struct {
				FormulaReplicas  int   `json:"formulaReplicas"`
				DirectionClamped *bool `json:"directionClamped"`
				WithinTolerance  *bool `json:"withinTolerance"`
			} `json:"modes"`
		} `json:"cycles"`
	}
	if err := json.Unmarshal([]byte(stdout), &report); err != nil {
		t.Fatal(err)
	}
	if len(report.Cycles) != 5 {
		t.Fatalf("missing cycles: %s", stdout)
	}
	pending := report.Cycles[1].Modes["Current"]
	if pending.FormulaReplicas != 4 || pending.DirectionClamped == nil || !*pending.DirectionClamped {
		t.Fatalf("missing explanation that the formula's four replicas cannot reverse a pending reduction to two: %s", stdout)
	}
	inBand := report.Cycles[2].Modes["Current"].WithinTolerance
	if inBand == nil || !*inBand {
		t.Fatalf("missing tolerance explanation: %s", stdout)
	}
}
