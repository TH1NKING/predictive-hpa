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

import "time"

// scaleHistory tracks recent desiredReplicas values for a single PredictiveHPA
// and supports a "max over rolling window" query that implements the
// spec.scaleDownStabilizationWindowSeconds behavior: when the reconciler would
// scale down, the final replicas is capped to the max value within the past
// window, preventing rapid downscaling on transient metric drops.
//
// scaleHistory is NOT goroutine-safe. Concurrent access from multiple
// reconciliations must be synchronized via the reconciler's mutex.
type scaleHistory struct {
	entries []scaleEntry // ordered by timestamp ascending
}

type scaleEntry struct {
	timestamp time.Time
	desired   int32
}

// record appends a new entry. Caller must hold the reconciler's mutex.
func (h *scaleHistory) record(now time.Time, desired int32) {
	h.entries = append(h.entries, scaleEntry{timestamp: now, desired: desired})
}

// maxInWindow returns the maximum desired value among entries with timestamp
// >= now - window. Expired entries are pruned from the underlying slice as a
// side effect. Returns 0 if no entries remain within the window.
// Caller must hold the reconciler's mutex.
func (h *scaleHistory) maxInWindow(now time.Time, window time.Duration) int32 {
	cutoff := now.Add(-window)

	// Entries are time-ordered; find first non-expired index and slice.
	i := 0
	for i < len(h.entries) && h.entries[i].timestamp.Before(cutoff) {
		i++
	}
	h.entries = h.entries[i:]

	var maxDesired int32
	for _, e := range h.entries {
		if e.desired > maxDesired {
			maxDesired = e.desired
		}
	}
	return maxDesired
}

// len returns the number of entries currently stored. Used by tests and
// cold-start detection (a history with len == 1 immediately after record
// means no prior data, indicating a controller restart or first reconcile
// of a PHPA).
func (h *scaleHistory) len() int {
	return len(h.entries)
}
