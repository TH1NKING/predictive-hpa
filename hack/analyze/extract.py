#!/usr/bin/env python3
"""
Phase 3 single-experiment extractor.

Reads the raw artifacts produced by hack/run_benchmark.sh for one experiment
and emits a structured extract.json next to them. Designed as the input stage
for hack/analyze/aggregate.py.

Usage:
    python extract.py <experiment_dir>

Input files expected in <experiment_dir>:
    metadata.yaml     — required (controller, pattern, timestamps, git commit)
    k6.json           — required (JSON Lines: business-side metrics)
    prom.json         — required (3 Prometheus query_range series)
    events.yaml       — required (K8s events; source of truth for replica changes)
    controller.log    — optional (phpa only; logr text + JSON body)

Output:
    extract.json      — structured aggregate-ready data

Design notes:
- This script is idempotent. Re-running overwrites extract.json.
- It does NOT raise on partial data. Missing/malformed inputs produce null
  fields plus a warnings entry; aggregate.py is responsible for handling nulls.
- All timestamps in the output are UTC ISO 8601 with 'Z' suffix.
- Latencies are reported in milliseconds (matching k6's native unit).
- Historical scaling/resource fields preserve their original definitions.
- controlled-pilot-v1 adds an explicitly named measurement window using runner
  timestamp files and fixed offered-load-end + 360s, with coverage validation.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

# === Orchestrator constants (must match hack/run_benchmark.sh) ===
# If hack/run_benchmark.sh changes these, update here too.
METRIC_ACCUMULATION_SECONDS = 30
# k6 load durations per pattern (see hack/k6/*.js stages)
# step: quiet(30s) + ramp-up(1s) + hold(179s) + ramp-down(1s) = 211s
# Values are the exact sums of the stages in hack/k6/<pattern>.js.
PATTERN_LOAD_DURATION_S = {
    "step": 211,
    "ramp": 270,
    "spike": 241,
}

MIN_REPLICAS = 1  # matches PHPA sample / native HPA YAML
CONTROLLED_PROTOCOL = "controlled-pilot-v1"
PILOT_IDENTITY_FIELDS = (
    "protocol_version", "rps", "pre_allocated_vus", "max_vus",
    "benchmark_source_sha256", "benchmark_config_sha256", "post_load_tail_seconds",
)


def parse_iso_to_utc(s: str) -> datetime | None:
    """Parse an ISO 8601 timestamp (with or without tz) and normalize to UTC.

    Handles:
    - "2026-05-26T17:01:45Z"           (UTC explicit)
    - "2026-05-27T01:01:47+08:00"      (offset)
    - "2026-05-26T17:01:45+00:00"      (offset zero)

    Returns None on parse failure (so callers can degrade gracefully).
    """
    if not s:
        return None
    # datetime.fromisoformat handles 'Z' suffix from Python 3.11+
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def fmt_utc(dt: datetime | None) -> str | None:
    """Format a datetime as ISO 8601 with 'Z' suffix. Returns None if dt is None."""
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def percentile(values: list[float], p: float) -> float | None:
    """Compute the p-th percentile (p in [0, 100]) using linear interpolation.

    Returns None for empty input. Pure stdlib so we don't need numpy in this
    function (numpy is available, but keeping this stdlib makes the helper
    portable for future ad-hoc use).
    """
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return float(s[f])
    return float(s[f] + (s[c] - s[f]) * (k - f))


# ============================================================================
# Loaders — one per input file. All return (data, warnings).
# ============================================================================

def load_metadata(path: Path) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    try:
        with path.open() as f:
            data = yaml.safe_load(f)
        return data, warnings
    except FileNotFoundError:
        warnings.append(f"metadata.yaml not found at {path}")
        return {}, warnings
    except yaml.YAMLError as e:
        warnings.append(f"metadata.yaml parse error: {e}")
        return {}, warnings


def load_k6(path: Path) -> tuple[dict, list[str]]:
    """Parse k6 JSON Lines output and aggregate business-side metrics."""
    warnings: list[str] = []
    if not path.exists():
        warnings.append(f"k6.json not found at {path}")
        return {}, warnings

    # We need:
    # - total http_reqs (count of Point with metric=http_reqs)
    # - http_req_failed: count of Point with value=1 (failure) vs total
    # - dropped_iterations: count of Point with metric=dropped_iterations
    # - http_req_duration: all values (for p50/p95/p99), and success-only subset
    total_reqs = 0
    failed_count = 0
    dropped = 0
    all_durations_ms: list[float] = []
    success_durations_ms: list[float] = []
    http_200_count = 0
    observed_start_time: datetime | None = None

    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "Point":
                    continue
                metric = obj.get("metric")
                data = obj.get("data", {})
                value = data.get("value")
                tags = data.get("tags", {})
                point_time = parse_iso_to_utc(data.get("time"))
                if point_time is not None and (
                    observed_start_time is None or point_time < observed_start_time
                ):
                    observed_start_time = point_time

                if metric == "http_reqs":
                    total_reqs += int(value or 0)
                elif metric == "http_req_failed":
                    if int(value or 0) == 1:
                        failed_count += 1
                elif metric == "dropped_iterations":
                    dropped += int(value or 0)
                elif metric == "http_req_duration":
                    if value is None:
                        continue
                    all_durations_ms.append(float(value))
                    if tags.get("status") == "200":
                        http_200_count += 1
                    if tags.get("expected_response") == "true" and tags.get("status") == "200":
                        success_durations_ms.append(float(value))
    except OSError as e:
        warnings.append(f"k6.json read error: {e}")
        return {}, warnings

    # k6's "http_req_failed" Points fire for EVERY request (value 0 or 1),
    # so the denominator equals total http_req_duration samples, not http_reqs.
    failed_denominator = len(all_durations_ms)
    failed_rate = (failed_count / failed_denominator * 100.0) if failed_denominator else None

    return {
        "total_requests": total_reqs if total_reqs else failed_denominator,
        "failed_count": failed_count,
        "failed_rate_pct": round(failed_rate, 2) if failed_rate is not None else None,
        "successful_requests_http_200": http_200_count,
        "successful_rate_http_200_pct": (
            round(http_200_count / failed_denominator * 100.0, 2)
            if failed_denominator else None
        ),
        "dropped_iterations": dropped,
        "duration_p50_ms": round(percentile(all_durations_ms, 50), 2) if all_durations_ms else None,
        "duration_p95_ms": round(percentile(all_durations_ms, 95), 2) if all_durations_ms else None,
        "duration_p99_ms": round(percentile(all_durations_ms, 99), 2) if all_durations_ms else None,
        "duration_success_p50_ms": round(percentile(success_durations_ms, 50), 2) if success_durations_ms else None,
        "duration_success_p95_ms": round(percentile(success_durations_ms, 95), 2) if success_durations_ms else None,
        "duration_success_p99_ms": round(percentile(success_durations_ms, 99), 2) if success_durations_ms else None,
        "observed_start_time_utc": fmt_utc(observed_start_time),
    }, warnings


def load_events(path: Path) -> tuple[list[dict], list[str]]:
    """Parse events.yaml and extract SuccessfulRescale events sorted by time."""
    warnings: list[str] = []
    if not path.exists():
        warnings.append(f"events.yaml not found at {path}")
        return [], warnings

    try:
        with path.open() as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        warnings.append(f"events.yaml parse error: {e}")
        return [], warnings

    items = data.get("items", []) if isinstance(data, dict) else []
    rescales: list[dict] = []
    for ev in items:
        if ev.get("reason") != "SuccessfulRescale":
            continue
        ts = parse_iso_to_utc(ev.get("lastTimestamp") or ev.get("firstTimestamp"))
        if ts is None:
            continue
        msg = ev.get("message", "")
        # Parse "New size: N; reason: ..." — extract the new size.
        new_size = None
        if msg.startswith("New size:"):
            try:
                # "New size: 4; reason: ..."
                size_str = msg.split(";", 1)[0].replace("New size:", "").strip()
                new_size = int(size_str)
            except (ValueError, IndexError):
                pass
        rescales.append({
            "timestamp_utc": ts,
            "new_size": new_size,
            "message": msg,
        })

    # Sort chronologically.
    rescales.sort(key=lambda e: e["timestamp_utc"])
    return rescales, warnings


def load_prom(path: Path) -> tuple[dict, list[str]]:
    """Parse prom.json into time-series dicts keyed by query name.

    Returns dict with keys 'replicas', 'cpu_pct', 'rps_pkt_rate'. Each maps to
    a list of (epoch_seconds_int, value_float) tuples.
    """
    warnings: list[str] = []
    if not path.exists():
        warnings.append(f"prom.json not found at {path}")
        return {}, warnings

    try:
        with path.open() as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        warnings.append(f"prom.json parse error: {e}")
        return {}, warnings

    series: dict[str, list[tuple[int, float]]] = {}
    for key in ("replicas", "cpu_pct", "rps_pkt_rate"):
        block = data.get(key, {}).get("data", {}).get("result", [])
        if not block:
            series[key] = []
            continue
        # We only care about the first series per query (single Deployment).
        values = block[0].get("values", [])
        parsed: list[tuple[int, float]] = []
        for v in values:
            if len(v) != 2:
                continue
            try:
                parsed.append((int(v[0]), float(v[1])))
            except (ValueError, TypeError):
                continue
        series[key] = parsed
    return series, warnings


def load_controller_log(path: Path) -> tuple[list[dict], list[str]]:
    """Parse controller.log into a list of reconcile decisions.

    Each entry is dict with parsed JSON keys plus 'timestamp_utc' from the log
    prefix. Only lines containing 'reconciled' are included.
    """
    warnings: list[str] = []
    if not path.exists():
        # Not a warning for native_hpa — caller decides whether absence is normal.
        return [], warnings

    decisions: list[dict] = []
    try:
        with path.open() as f:
            for line in f:
                if "reconciled" not in line:
                    continue
                # logr format: "<timestamp>\tINFO\treconciled\t<json>"
                # Extract timestamp (everything up to first whitespace).
                ts_str, _, rest = line.partition("\t")
                if not ts_str:
                    continue
                ts = parse_iso_to_utc(ts_str.strip())
                # Extract JSON body (first '{' to last '}').
                lbrace = line.find("{")
                if lbrace < 0:
                    continue
                try:
                    body = json.loads(line[lbrace:].strip())
                except json.JSONDecodeError:
                    continue
                body["timestamp_utc"] = ts
                decisions.append(body)
    except OSError as e:
        warnings.append(f"controller.log read error: {e}")
        return [], warnings
    return decisions, warnings


# ============================================================================
# Derived metric calculators
# ============================================================================

def compute_scaling_metrics(
    prom_replicas: list[tuple[int, float]],
    k6_start: datetime,
    k6_stop: datetime,
    end_time: datetime,
) -> tuple[dict, list[str]]:
    """Compute scaling timeline + derived metrics from prom replicas time series.

    Why prom and not events.yaml:
    - events.yaml is collected via `kubectl get events` which returns cluster-wide
      events with 1h TTL. It picks up stale rescale events from previous experiments
      and conflates them with this experiment's window.
    - PHPA does not emit SuccessfulRescale Events at all — it writes to Deployment
      scale subresource directly, leaving no trace in events.yaml.
    - prom.json's replicas series is the only data source both controllers feed
      symmetrically, with the same 15s sampling cadence. Lower precision than
      events.yaml's per-second timestamps, but fair across controllers.

    Algorithm: walk the (timestamp, replicas) samples; whenever replicas changes,
    record a scale event with from/to.
    """
    warnings: list[str] = []

    if not prom_replicas:
        warnings.append("prom replicas series empty; scaling metrics unavailable")
        return {
            "events": [],
            "first_scaleup_rel_s": None,
            "convergence_rel_s": None,
            "steady_state_replicas": None,
            "first_scaledown_rel_s_after_k6_stop": None,
            "full_scaledown_rel_s_after_k6_stop": None,
            "scaledown_completed": False,
            "data_source": "prom.json replicas",
            "sampling_precision_s": 15,
        }, warnings

    # Walk samples and detect replica count changes.
    events_out: list[dict] = []
    k6_start_epoch = int(k6_start.timestamp())
    k6_stop_epoch = int(k6_stop.timestamp())
    prev_replicas = int(prom_replicas[0][1])

    for ts_epoch, replicas_f in prom_replicas[1:]:
        replicas = int(replicas_f)
        if replicas != prev_replicas:
            ts = datetime.fromtimestamp(ts_epoch, tz=timezone.utc)
            rel_s = ts_epoch - k6_start_epoch
            events_out.append({
                "timestamp_utc": fmt_utc(ts),
                "rel_s_from_k6_start": rel_s,
                "from": prev_replicas,
                "to": replicas,
            })
            prev_replicas = replicas

    # Derived metrics.
    scaleups = [e for e in events_out if e["to"] > e["from"]]
    scaledowns = [e for e in events_out if e["to"] < e["from"]]

    first_scaleup_rel_s = None
    convergence_rel_s = None
    steady_state_replicas = None
    first_scaledown_rel_s_after_k6_stop = None
    full_scaledown_rel_s_after_k6_stop = None
    scaledown_completed = False

    if scaleups:
        first_scaleup_rel_s = scaleups[0]["rel_s_from_k6_start"]
        # steady_state_replicas = peak replicas observed in the experiment.
        # Not "last scaleup's to" because scale-up events during the scale-down
        # phase (caused by transient EWMA spikes inside a stabilization window)
        # can cause that to underestimate the true peak. See phpa dry-run case:
        # 1->2->10->5->6->4->1: the 5->6 mid-scaledown bump made the old logic
        # report steady_state=6 instead of 10.
        steady_state_replicas = max(e["to"] for e in events_out)
        # convergence_rel_s = first time we reached steady_state_replicas.
        convergence_events = [
            e for e in events_out if e["to"] == steady_state_replicas
        ]
        convergence_rel_s = (
            convergence_events[0]["rel_s_from_k6_start"]
            if convergence_events
            else None
        )

    if scaledowns:
        first = scaledowns[0]
        first_ts_epoch = first["rel_s_from_k6_start"] + k6_start_epoch
        first_scaledown_rel_s_after_k6_stop = first_ts_epoch - k6_stop_epoch

        terminal = [s for s in scaledowns if s["to"] == MIN_REPLICAS]
        ends_at_min = int(prom_replicas[-1][1]) <= MIN_REPLICAS
        if ends_at_min and terminal:
            last_ts_epoch = terminal[-1]["rel_s_from_k6_start"] + k6_start_epoch
            full_scaledown_rel_s_after_k6_stop = last_ts_epoch - k6_stop_epoch
            scaledown_completed = True
        else:
            scaledown_completed = False
            warnings.append(
                "scaledown_completed=false: experiment window ended with "
                "replicas above minReplicas; tail observation may be too short"
            )
    else:
        # No scale-down detected: either load too short, or replicas never grew.
        if steady_state_replicas is None or steady_state_replicas <= MIN_REPLICAS:
            scaledown_completed = True
        else:
            warnings.append(
                "no scale-down events detected; experiment ended at "
                f"steady_state={steady_state_replicas} replicas"
            )

    return {
        "events": events_out,
        "first_scaleup_rel_s": first_scaleup_rel_s,
        "convergence_rel_s": convergence_rel_s,
        "steady_state_replicas": steady_state_replicas,
        "first_scaledown_rel_s_after_k6_stop": first_scaledown_rel_s_after_k6_stop,
        "full_scaledown_rel_s_after_k6_stop": full_scaledown_rel_s_after_k6_stop,
        "scaledown_completed": scaledown_completed,
        "data_source": "prom.json replicas",
        "sampling_precision_s": 15,
    }, warnings


def compute_phpa_metrics(decisions: list[dict]) -> dict:
    """Aggregate PHPA controller log into scalar metrics."""
    if not decisions:
        return {
            "total_reconciles": 0,
            "unique_decisions": 0,
            "scaled_true_count": 0,
            "stabilized_true_count": 0,
            "skip_reasons": {},
        }

    # Dedup by reconcileID (controller-runtime may log the same decision twice
    # when PHPA + Deployment scale subresource both trigger reconcile).
    by_id: dict[str, dict] = {}
    for d in decisions:
        rid = d.get("reconcileID")
        if rid is None:
            # No ID → keep all (better than dropping).
            by_id[f"_noid_{len(by_id)}"] = d
        elif rid not in by_id:
            by_id[rid] = d
        # If already seen, skip (idempotent dedup).

    unique = list(by_id.values())

    return {
        "total_reconciles": len(decisions),
        "unique_decisions": len(unique),
        "scaled_true_count": sum(1 for d in unique if d.get("scaled") is True),
        "stabilized_true_count": sum(1 for d in unique if d.get("stabilized") is True),
        "skip_reasons": dict(Counter(d.get("skipReason", "") for d in unique)),
    }


def compute_resource_metrics(
    prom_replicas: list[tuple[int, float]],
    k6_stop: datetime,
    end_time: datetime,
) -> dict:
    """Compute resource efficiency: pod-seconds, avg replicas, waste window.

    Definitions:
    - pod_seconds_during_experiment: trapezoidal integral of replicas(t) over
      the whole experiment window (start_time..end_time). The "area under the
      replicas curve" — directly proportional to compute cost in a pay-per-pod
      cloud model.
    - avg_replicas: pod_seconds / experiment_duration_s.
    - waste_window_s: total seconds AFTER k6_stop during which replicas > MIN_REPLICAS.
      Represents "idle pods running after load ended". Uses the timestamp of
      sample i and assumes replicas[i] holds until sample i+1 (left-continuous,
      matching prom step semantics).
    """
    if not prom_replicas:
        return {
            "pod_seconds_during_experiment": None,
            "avg_replicas": None,
            "waste_window_s": None,
        }

    # Pod-seconds: trapezoidal integration of replica count over time.
    pod_seconds = 0.0
    for i in range(len(prom_replicas) - 1):
        t0, v0 = prom_replicas[i]
        t1, v1 = prom_replicas[i + 1]
        pod_seconds += (v0 + v1) / 2.0 * (t1 - t0)

    duration_s = prom_replicas[-1][0] - prom_replicas[0][0] if len(prom_replicas) >= 2 else 0
    avg_replicas = pod_seconds / duration_s if duration_s > 0 else None

    # Waste window: time after k6_stop where replicas held above MIN_REPLICAS.
    # Each sample (t_i, v_i) covers [t_i, t_{i+1}); use left value as authoritative.
    # For the final sample, assume it holds until end_time.
    waste_window_s = 0
    k6_stop_epoch = int(k6_stop.timestamp())
    end_epoch = int(end_time.timestamp())

    for i, (t_i, v_i) in enumerate(prom_replicas):
        # Interval covered by this sample.
        t_next = prom_replicas[i + 1][0] if i + 1 < len(prom_replicas) else end_epoch
        # Intersect [t_i, t_next) with [k6_stop, end_time).
        seg_start = max(t_i, k6_stop_epoch)
        seg_end = min(t_next, end_epoch)
        if seg_end <= seg_start:
            continue
        if int(v_i) > MIN_REPLICAS:
            waste_window_s += (seg_end - seg_start)

    return {
        "pod_seconds_during_experiment": round(pod_seconds, 1),
        "avg_replicas": round(avg_replicas, 2) if avg_replicas is not None else None,
        "waste_window_s": waste_window_s,
    }


# ============================================================================
# Main
# ============================================================================

def compute_controlled_measurement(
    exp_dir: Path, metadata: dict, prom_replicas: list[tuple[int, float]],
) -> tuple[dict, list[str]]:
    """Measure one fixed load-onset-to-tail window without setup or drain bias.

    Replica samples hold their value until the next sample. A boundary may use
    a preceding sample at most 15 seconds old; larger observation gaps invalidate
    this measurement instead of silently extrapolating. Historical metric keys
    remain separate and retain their original definitions.
    """
    result: dict = {
        "window_valid": False,
        "sampling_precision_s": 15,
        "data_source": "prom.json replicas",
        "events": [],
    }
    try:
        runner_start = int((exp_dir / "k6-start-time-unix").read_text().strip())
        runner_end = int((exp_dir / "k6-end-time-unix").read_text().strip())
        schedule = PATTERN_LOAD_DURATION_S[metadata["pattern"]]
        tail_seconds = int(metadata["post_load_tail_seconds"])
        onset = runner_start + 30
        offered_end = runner_start + schedule
        tail_end = int(metadata["observation_end_time_unix"])
        if runner_start <= 0 or runner_end < offered_end - 2:
            raise ValueError("runner timestamps do not cover the declared load schedule")
        if tail_seconds != 360 or tail_end != offered_end + tail_seconds:
            raise ValueError("observation boundary does not match offered load end + 360s")
        if int(metadata["end_time_unix"]) < tail_end:
            raise ValueError("collection ended before the declared observation boundary")
        for key, expected in (("load_start_time_unix", onset),
                              ("offered_load_end_time_unix", offered_end)):
            if key in metadata and int(metadata[key]) != expected:
                raise ValueError(f"{key} disagrees with the runner timestamp and schedule")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return result, [f"controlled measurement unavailable: {exc}"]

    result.update({
        "runner_start_time_unix": runner_start,
        "runner_end_time_unix": runner_end,
        "load_onset_time_unix": onset,
        "offered_load_end_time_unix": offered_end,
        "tail_end_time_unix": tail_end,
        "window_duration_s": tail_end - onset,
        "post_load_duration_s": tail_seconds,
    })
    samples = sorted(prom_replicas)
    baseline = [sample for sample in samples if sample[0] <= onset]
    if not baseline or onset - baseline[-1][0] > 15:
        return result, ["controlled measurement unavailable: no replica sample within 15s before load onset"]
    # Retain only the last sample preceding onset and changes inside the window.
    timeline = [baseline[-1]] + [s for s in samples if onset < s[0] <= tail_end]
    if (tail_end - timeline[-1][0] > 15
            or any(b[0] - a[0] > 15 for a, b in zip(timeline, timeline[1:]))):
        return result, ["controlled measurement unavailable: replica observation gap exceeds 15s"]
    if any(not math.isfinite(v) or v < MIN_REPLICAS or v != int(v)
           for _, v in timeline):
        return result, ["controlled measurement unavailable: invalid replica sample"]
    if any(a[0] >= b[0] for a, b in zip(timeline, timeline[1:])):
        return result, ["controlled measurement unavailable: duplicate replica sample timestamp"]

    events: list[dict] = []
    previous = int(timeline[0][1])
    for ts, value in timeline[1:]:
        replicas = int(value)
        if replicas != previous:
            events.append({"time_unix": ts, "after_load_onset_s": ts - onset,
                           "from": previous, "to": replicas})
        previous = replicas
    pod_seconds = post_load_pod_seconds = excess_pod_seconds = above_min_seconds = 0.0
    replicas_at_offered_end = int(timeline[0][1])
    for index, (ts, replicas) in enumerate(timeline):
        next_ts = timeline[index + 1][0] if index + 1 < len(timeline) else tail_end
        segment_start, segment_end = max(ts, onset), min(next_ts, tail_end)
        pod_seconds += replicas * max(0, segment_end - segment_start)
        post_seconds = max(0, segment_end - max(segment_start, offered_end))
        post_load_pod_seconds += replicas * post_seconds
        excess_pod_seconds += max(0, replicas - MIN_REPLICAS) * post_seconds
        if replicas > MIN_REPLICAS:
            above_min_seconds += post_seconds
        if ts <= offered_end:
            replicas_at_offered_end = int(replicas)

    scaleups = [e for e in events if e["to"] > e["from"]]
    post_downs = [e for e in events if e["time_unix"] >= offered_end and e["to"] < e["from"]]
    completed = timeline[-1][1] == MIN_REPLICAS
    returns_to_min = [e for e in post_downs if e["to"] == MIN_REPLICAS]
    full_down = None
    if completed:
        full_down = returns_to_min[-1]["time_unix"] - offered_end if returns_to_min else 0
    result.update({
        "window_valid": True,
        "events": events,
        "initial_replicas_at_load_onset": int(timeline[0][1]),
        "replicas_at_offered_load_end": replicas_at_offered_end,
        "final_replicas": int(timeline[-1][1]),
        "peak_replicas": int(max(v for _, v in timeline)),
        "first_scaleup_after_load_onset_s": scaleups[0]["after_load_onset_s"] if scaleups else None,
        "first_scaledown_after_offered_load_end_s": (
            post_downs[0]["time_unix"] - offered_end if post_downs else None
        ),
        "full_scaledown_after_offered_load_end_s": full_down,
        "scaledown_completed": completed,
        "pod_seconds_load_onset_to_tail_end": round(pod_seconds, 1),
        "avg_replicas_load_onset_to_tail_end": round(pod_seconds / (tail_end - onset), 2),
        "pod_seconds_post_load": round(post_load_pod_seconds, 1),
        "excess_pod_seconds_post_load": round(excess_pod_seconds, 1),
        "post_load_above_min_s": round(above_min_seconds, 1),
    })
    warnings = [] if completed else [
        "controlled measurement scale-down censored: replicas remain above minReplicas at fixed tail end"
    ]
    return result, warnings


def extract(exp_dir: Path) -> dict:
    """Run the full extraction pipeline for one experiment directory."""
    warnings: list[str] = []

    # 1. Metadata
    metadata, w = load_metadata(exp_dir / "metadata.yaml")
    warnings.extend(w)
    pilot_identity = ({key: metadata.get(key) for key in PILOT_IDENTITY_FIELDS}
                      if metadata.get("protocol_version") else {})

    pattern = metadata.get("pattern")
    controller = metadata.get("controller")
    repeat = metadata.get("repeat")
    campaign = metadata.get("campaign")
    scale_down_stabilization_seconds = metadata.get(
        "scale_down_stabilization_seconds"
    )
    prediction_variant = metadata.get("prediction_variant")
    experiment_id = metadata.get("experiment_id", exp_dir.name)
    start_time_utc_str = metadata.get("start_time_utc", "")
    end_time_utc_str = metadata.get("end_time_utc", "")
    git_commit = (metadata.get("git", {}) or {}).get("commit")

    start_time = parse_iso_to_utc(start_time_utc_str)
    end_time = parse_iso_to_utc(end_time_utc_str)

    if start_time is None or end_time is None:
        warnings.append("metadata missing start/end time; relative metrics unavailable")
        return {
            **pilot_identity,
            "experiment_id": experiment_id,
            "pattern": pattern,
            "controller": controller,
            "repeat": repeat,
            "campaign": campaign,
            "scale_down_stabilization_seconds": scale_down_stabilization_seconds,
            "prediction_variant": prediction_variant,
            "warnings": warnings,
        }

    duration_s = int((end_time - start_time).total_seconds())

    # 2. k6
    k6_metrics, w = load_k6(exp_dir / "k6.json")
    warnings.extend(w)

    # Prefer the exact orchestrator timestamp when present, then the earliest
    # observed k6 Point. Legacy experiments fall back to the old approximation.
    from datetime import timedelta
    k6_start = parse_iso_to_utc(metadata.get("k6_start_time_utc", ""))
    if k6_start is None:
        k6_start = parse_iso_to_utc(k6_metrics.get("observed_start_time_utc", ""))
    if k6_start is None:
        k6_start = start_time + timedelta(seconds=METRIC_ACCUMULATION_SECONDS)
        warnings.append(
            "k6 start timestamp unavailable; using metadata-derived fallback"
        )
    load_duration = PATTERN_LOAD_DURATION_S.get(pattern, 211)
    k6_stop = k6_start + timedelta(seconds=load_duration)

    # 3. events
    rescales, w = load_events(exp_dir / "events.yaml")
    warnings.extend(w)

    # 4. prom
    prom, w = load_prom(exp_dir / "prom.json")
    warnings.extend(w)
    prom_replicas = prom.get("replicas", [])

    # 5. scaling derived (uses prom replicas as authoritative source)
    scaling, w = compute_scaling_metrics(prom_replicas, k6_start, k6_stop, end_time)
    warnings.extend(w)

    # 6. resource derived
    resource = compute_resource_metrics(prom_replicas, k6_stop, end_time)
    controlled = {}
    if metadata.get("protocol_version") == CONTROLLED_PROTOCOL:
        measurement, w = compute_controlled_measurement(exp_dir, metadata, prom_replicas)
        controlled["measurement"] = measurement
        warnings.extend(w)

    # 7. events.yaml is retained as a SECONDARY signal — useful for native_hpa
    # because it provides per-second precision SuccessfulRescale timestamps that
    # can be cross-referenced against our prom-derived events at aggregate time.
    # For phpa, this list is typically empty (PHPA writes scale subresource
    # directly without emitting K8s Events) — or polluted with stale events
    # from previous native_hpa experiments in the same cluster lifetime.
    # See compute_scaling_metrics docstring for why we no longer use this for
    # scaling.events.
    events_yaml_rescales = [
        {
            "timestamp_utc": fmt_utc(r["timestamp_utc"]),
            "new_size": r["new_size"],
            "message": r["message"],
        }
        for r in rescales
    ]

    # 7. PHPA-specific (phpa only)
    phpa_metrics: dict | None = None
    if controller == "phpa":
        decisions, w = load_controller_log(exp_dir / "controller.log")
        warnings.extend(w)
        if not decisions:
            warnings.append("controller.log missing or empty for phpa experiment")
        phpa_metrics = compute_phpa_metrics(decisions)

    return {
        **pilot_identity,
        **controlled,
        "experiment_id": experiment_id,
        "pattern": pattern,
        "controller": controller,
        "repeat": repeat,
        "campaign": campaign,
        "scale_down_stabilization_seconds": scale_down_stabilization_seconds,
        "prediction_variant": prediction_variant,
        "start_time_utc": fmt_utc(start_time),
        "end_time_utc": fmt_utc(end_time),
        "duration_s": duration_s,
        "git_commit": git_commit,
        "k6": k6_metrics,
        "scaling": scaling,
        "phpa": phpa_metrics,
        "resource": resource,
        "events_yaml_rescales": events_yaml_rescales,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract structured metrics from one Phase 3 experiment directory",
    )
    parser.add_argument("experiment_dir", type=Path, help="Path to experiment directory")
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print JSON to stdout instead of writing extract.json",
    )
    args = parser.parse_args()

    exp_dir: Path = args.experiment_dir
    if not exp_dir.is_dir():
        print(f"ERROR: {exp_dir} is not a directory", file=sys.stderr)
        return 1

    result = extract(exp_dir)

    if args.stdout:
        json.dump(result, sys.stdout, indent=2)
        print()
    else:
        out_path = exp_dir / "extract.json"
        with out_path.open("w") as f:
            json.dump(result, f, indent=2)
        # Short progress line to stdout.
        n_events = len(result.get("scaling", {}).get("events", []))
        n_warnings = len(result.get("warnings", []))
        k6 = result.get("k6", {}) or {}
        phpa = result.get("phpa") or {}
        bits = [
            f"controller={result.get('controller')}",
            f"reqs={k6.get('total_requests')}",
            f"fail%={k6.get('failed_rate_pct')}",
            f"events={n_events}",
        ]
        if phpa:
            bits.append(f"stabilized={phpa.get('stabilized_true_count')}")
        if n_warnings:
            bits.append(f"warnings={n_warnings}")
        print(f"extracted {result.get('experiment_id')}: " + " ".join(bits))

    if (result.get("protocol_version") == CONTROLLED_PROTOCOL
            and not result.get("measurement", {}).get("window_valid")):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
