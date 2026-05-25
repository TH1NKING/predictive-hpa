/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

	http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/
package controller

import (
	"testing"
	"time"
)

const testStabilizationWindow = 60 * time.Second

func TestScaleHistory_Empty(t *testing.T) {
	h := &scaleHistory{}
	now := time.Now()

	got := h.maxInWindow(now, testStabilizationWindow)
	if got != 0 {
		t.Errorf("maxInWindow on empty history = %d, want 0", got)
	}
	if h.len() != 0 {
		t.Errorf("len on empty history = %d, want 0", h.len())
	}
}

func TestScaleHistory_SingleEntry(t *testing.T) {
	h := &scaleHistory{}
	now := time.Now()

	h.record(now, 5)

	if h.len() != 1 {
		t.Fatalf("len after single record = %d, want 1", h.len())
	}
	got := h.maxInWindow(now, testStabilizationWindow)
	if got != 5 {
		t.Errorf("maxInWindow = %d, want 5", got)
	}
}

func TestScaleHistory_MultipleWithinWindow(t *testing.T) {
	h := &scaleHistory{}
	now := time.Now()

	h.record(now.Add(-50*time.Second), 3)
	h.record(now.Add(-30*time.Second), 8)
	h.record(now.Add(-10*time.Second), 5)

	got := h.maxInWindow(now, testStabilizationWindow)
	if got != 8 {
		t.Errorf("maxInWindow = %d, want 8", got)
	}
	if h.len() != 3 {
		t.Errorf("no prune expected, len = %d, want 3", h.len())
	}
}

func TestScaleHistory_SomeExpired(t *testing.T) {
	h := &scaleHistory{}
	now := time.Now()

	h.record(now.Add(-90*time.Second), 8)
	h.record(now.Add(-30*time.Second), 5)
	h.record(now.Add(-10*time.Second), 3)

	got := h.maxInWindow(now, testStabilizationWindow)
	if got != 5 {
		t.Errorf("maxInWindow = %d, want 5 (expired 8 should be ignored)", got)
	}
	if h.len() != 2 {
		t.Errorf("expected 1 entry pruned, len = %d, want 2", h.len())
	}
}

func TestScaleHistory_AllExpired(t *testing.T) {
	h := &scaleHistory{}
	now := time.Now()

	h.record(now.Add(-120*time.Second), 8)
	h.record(now.Add(-90*time.Second), 5)

	got := h.maxInWindow(now, testStabilizationWindow)
	if got != 0 {
		t.Errorf("maxInWindow = %d, want 0 (all expired)", got)
	}
	if h.len() != 0 {
		t.Errorf("all entries should be pruned, len = %d, want 0", h.len())
	}
}

func TestScaleHistory_PruneSideEffect(t *testing.T) {
	h := &scaleHistory{}
	now := time.Now()

	h.record(now.Add(-90*time.Second), 8)
	h.record(now.Add(-30*time.Second), 5)

	if h.len() != 2 {
		t.Fatalf("len before maxInWindow = %d, want 2", h.len())
	}

	h.maxInWindow(now, testStabilizationWindow)

	if h.len() != 1 {
		t.Errorf("len after maxInWindow = %d, want 1 (expired entry should be pruned)", h.len())
	}
}

func TestScaleHistory_BoundaryTimestampOnCutoff(t *testing.T) {
	h := &scaleHistory{}
	now := time.Now()

	// timestamp == cutoff (now - window). Before(cutoff) is false for equal
	// times, so the entry should NOT be pruned.
	h.record(now.Add(-testStabilizationWindow), 7)

	got := h.maxInWindow(now, testStabilizationWindow)
	if got != 7 {
		t.Errorf("maxInWindow with timestamp at cutoff = %d, want 7 (kept)", got)
	}
	if h.len() != 1 {
		t.Errorf("entry at boundary should not be pruned, len = %d, want 1", h.len())
	}
}
