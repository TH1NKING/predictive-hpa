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
	"time"

	appsv1 "k8s.io/api/apps/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	autoscalingv1alpha1 "github.com/th1nking/predictive-hpa/api/v1alpha1"
)

// scaleHistory retains a conservative rolling maximum for one PHPA/target
// incarnation. Recommendations in the same time bucket are combined using the
// maximum value and newest timestamp. This never expires a recommendation early;
// it can retain a peak for up to one extra bucket width. With width >= window/128,
// at most 129 buckets intersect the window, even during an event storm.
//
// scaleHistory is NOT goroutine-safe. Concurrent access from multiple
// reconciliations must be synchronized via the reconciler's mutex.
type scaleHistory struct {
	entries           []scaleEntry // ordered by timestamp ascending
	phpaUID           types.UID
	targetUID         types.UID
	targetName        string
	window            time.Duration
	lastRecordedAt    time.Time
	protectedAt       time.Time
	protectedReplicas int32
}

type scaleEntry struct {
	timestamp time.Time
	desired   int32
}

// record merges or appends a raw recommendation. Caller holds the mutex.
func (h *scaleHistory) record(now time.Time, desired int32) {
	width := max(time.Second, (h.window+127)/128)
	if n := len(h.entries); n > 0 && h.entries[n-1].timestamp.Truncate(width).Equal(now.Truncate(width)) {
		h.entries[n-1].timestamp = now
		h.entries[n-1].desired = max(h.entries[n-1].desired, desired)
		return
	}
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

// len returns the number of retained recommendation buckets.
func (h *scaleHistory) len() int {
	return len(h.entries)
}

// stabilizationResult is an immutable policy/diagnostic snapshot taken while
// the history lock is held. Logging and API writes never retain that lock.
type stabilizationResult struct {
	finalDesired   int32
	windowSeconds  int32
	stabilized     bool
	coldStart      bool
	protectedUntil time.Time
	historyEntries int
	historyOldest  time.Time
}

func (r *PredictiveHPAReconciler) stabilizeRecommendation(
	phpa *autoscalingv1alpha1.PredictiveHPA, deploy *appsv1.Deployment,
	now time.Time, desired, requested, minimum int32,
) stabilizationResult {
	windowSeconds := int32(60)
	if phpa.Spec.ScaleDownStabilizationWindowSeconds != nil {
		windowSeconds = *phpa.Spec.ScaleDownStabilizationWindowSeconds
	}
	window := time.Duration(windowSeconds) * time.Second
	key := client.ObjectKeyFromObject(phpa)
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.history == nil {
		r.history = make(map[types.NamespacedName]*scaleHistory)
	}
	hist := r.history[key]
	// A changed identity/window or clock rollback invalidates the old geometry
	// and starts a full guard using this target's own live requested capacity.
	if hist == nil || hist.phpaUID != phpa.UID || hist.targetUID != deploy.UID || hist.targetName != deploy.Name ||
		window != hist.window || now.Before(hist.lastRecordedAt) {
		hist = &scaleHistory{phpaUID: phpa.UID, targetUID: deploy.UID, targetName: deploy.Name,
			window: window, protectedAt: now, protectedReplicas: requested}
		r.history[key] = hist
	}
	hist.lastRecordedAt = now
	if window == 0 {
		hist.entries = nil
	}
	// Store raw recommendations only; stabilized output must not renew itself.
	hist.record(now, desired)
	peak := hist.maxInWindow(now, window)
	protectedUntil := hist.protectedAt.Add(window)
	coldStart := window > 0 && !now.After(protectedUntil)
	finalDesired := desired
	if window > 0 && desired < requested {
		if coldStart {
			peak = max(peak, hist.protectedReplicas)
		}
		// History can retain a request but cannot initiate expansion.
		finalDesired = min(requested, peak)
	}
	finalDesired = min(max(finalDesired, max(minimum, 1)), phpa.Spec.MaxReplicas)
	return stabilizationResult{
		finalDesired: finalDesired, windowSeconds: windowSeconds, stabilized: finalDesired > desired,
		coldStart: coldStart, protectedUntil: protectedUntil,
		historyEntries: hist.len(), historyOldest: hist.entries[0].timestamp,
	}
}
