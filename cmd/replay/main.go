// Command replay compares scaling modes against the same recorded inputs.
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"

	"github.com/th1nking/predictive-hpa/internal/controller"
)

func main() {
	os.Exit(run(os.Args[1:], os.Stdin, os.Stdout, os.Stderr))
}

func run(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	flags := flag.NewFlagSet("replay", flag.ContinueOnError)
	flags.SetOutput(stderr)
	inputPath := flags.String("input", "-", "recording JSON file, or - for stdin")
	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		return 2
	}
	if flags.NArg() != 0 {
		_, _ = fmt.Fprintln(stderr, "replay accepts -input FILE or JSON on stdin")
		return 2
	}
	input := stdin
	if *inputPath != "-" {
		file, err := os.Open(*inputPath)
		if err != nil {
			_, _ = fmt.Fprintln(stderr, err)
			return 2
		}
		defer func() { _ = file.Close() }()
		input = file
	}
	report, err := controller.ReplayDecisions(input)
	if err != nil {
		_, _ = fmt.Fprintln(stderr, err)
		if errors.Is(err, controller.ErrReplayInvalidInput) {
			return 2
		}
		return 1
	}
	encoder := json.NewEncoder(stdout)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(report); err != nil {
		_, _ = fmt.Fprintln(stderr, err)
		return 1
	}
	return 0
}
