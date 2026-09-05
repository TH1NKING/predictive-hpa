#!/usr/bin/env python3
"""Summarize fixed-replica routing evidence without declaring capacity validated."""
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

from extract import load_k6


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def pod_identities(snapshot: dict) -> dict[str, tuple]:
    result = {}
    for pod in snapshot["items"]:
        ready = any(c["type"] == "Ready" and c["status"] == "True"
                    for c in pod.get("status", {}).get("conditions", []))
        metadata = pod["metadata"]
        if metadata.get("deletionTimestamp") or not ready:
            raise ValueError("Pod was terminating or not Ready")
        result[metadata["name"]] = (
            metadata["uid"],
            sum(c.get("restartCount", 0) for c in pod["status"].get("containerStatuses", [])),
        )
    return result


def ready_endpoint_uids(snapshot: dict) -> set[str]:
    return {
        endpoint["targetRef"]["uid"]
        for item in snapshot["items"] for endpoint in item.get("endpoints", [])
        if endpoint.get("conditions", {}).get("ready") is True
        and not endpoint.get("conditions", {}).get("terminating", False)
        and endpoint.get("targetRef", {}).get("kind") == "Pod"
    }


def summarize_probe(directory: Path) -> dict:
    probe = read_json(directory / "probe.json")
    problems = []
    if probe["status"] != "success":
        problems.append("Probe did not complete collection")
    before = pod_identities(read_json(directory / "pods-before.json"))
    after = pod_identities(read_json(directory / "pods-after.json"))
    if before != after or len(before) != probe["replicas"]:
        problems.append("Pod identities, restart counts, or replica count changed")
    expected_uids = {identity[0] for identity in before.values()}
    for phase in ("before", "after"):
        if ready_endpoint_uids(read_json(directory / f"endpoints-{phase}.json")) != expected_uids:
            problems.append(f"Ready Service endpoints did not match Pods ({phase})")

    metrics, warnings = load_k6(directory / "k6.json")
    problems.extend(warnings)
    if not metrics.get("total_requests"):
        problems.append("No completed k6 requests")
    start = int((directory / "k6-start-time-unix").read_text().strip())
    end = int((directory / "k6-end-time-unix").read_text().strip())
    if end <= start:
        raise ValueError("Invalid k6 execution interval")
    cpu = read_json(directory / "cpu-by-pod.json")
    if cpu.get("status") != "success":
        problems.append("Prometheus query failed")
    cpu_by_pod: dict[str, list[float]] = {}
    for series in cpu.get("data", {}).get("result", []):
        values = []
        for timestamp, value in series.get("values", []):
            value = float(value)
            if start + 30 <= float(timestamp) <= end and math.isfinite(value) and value >= 0:
                values.append(value)
        cpu_by_pod.setdefault(series["metric"]["pod"], []).extend(values)

    rows = []
    for name in sorted(before):
        # A probe-specific User-Agent excludes unrelated requests and previous
        # probes. Missing/truncated access logs must not become a routing pass.
        lines = (directory / f"{name}.log").read_text(encoding="utf-8", errors="replace").splitlines()
        marker = f'"phpa-routing/{probe["token"]}"'
        count = sum(marker in line and re.search(r'"GET / HTTP/[0-9.]+" \d{3} ', line) is not None
                    for line in lines)
        values = cpu_by_pod.get(name, [])
        if not count:
            problems.append(f"No identified probe requests for {name}")
        if len(values) < 2:
            problems.append(f"Insufficient per-Pod CPU samples for {name}")
        rows.append({"pod": name, "requests": count,
                     "mean_cpu_cores": sum(values) / len(values) if values else None})
    total_logged = sum(row["requests"] for row in rows)
    for row in rows:
        row["request_share_pct"] = 100 * row["requests"] / total_logged if total_logged else None
    completed = metrics.get("total_requests", 0)
    successful = completed - metrics.get("failed_count", 0)
    return {
        "directory": directory.name, "replicas": probe["replicas"], "offered_rps": probe["rps"],
        "duration_seconds": probe["duration_seconds"], "execution_seconds": end - start,
        "metrics": metrics, "completed_rps": completed / (end - start),
        "successful_rps": successful / (end - start), "pods": rows,
        "routing_observed": not problems, "problems": problems,
        "capacity_validated": False,
    }


def summarize_run(root: Path) -> dict:
    probes = []
    for metadata in sorted(root.glob("replicas-*/probe.json")):
        try:
            probes.append(summarize_probe(metadata.parent))
        except (OSError, ValueError, KeyError, TypeError) as error:
            probes.append({"directory": metadata.parent.name, "routing_observed": False,
                           "capacity_validated": False, "problems": [str(error)]})
    return {"schema_version": 1, "probes": probes, "capacity_validated": False,
            "note": "Routing observations do not establish capacity. Review all Pod requests/CPU, generator headroom, throughput and SLA across replica counts."}


def write_report(root: Path, summary: dict) -> None:
    (root / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    lines = ["# Service routing calibration observations", "", summary["note"], "",
             "Throughput uses the measured k6 execution interval, including request drain.", "",
             "| Probe | Replicas | Offered RPS | Successful RPS | Failed % | p95 ms | Dropped | All Pods observed |",
             "|---|---:|---:|---:|---:|---:|---:|---|"]
    for probe in summary["probes"]:
        metrics = probe.get("metrics", {})
        values = [probe["directory"], probe.get("replicas", "—"), probe.get("offered_rps", "—"),
                  round(probe["successful_rps"], 3) if "successful_rps" in probe else "—",
                  metrics.get("failed_rate_pct", "—"), metrics.get("duration_p95_ms", "—"),
                  metrics.get("dropped_iterations", "—"), "yes" if probe["routing_observed"] else "unverified"]
        lines.append("| " + " | ".join(map(str, values)) + " |")
    for probe in summary["probes"]:
        lines.extend(["", f"## {probe['directory']}", ""])
        for problem in probe["problems"]:
            lines.append(f"- {problem}")
        if probe.get("pods"):
            lines.extend(["", "| Pod | Probe requests | Share % | Mean CPU cores |", "|---|---:|---:|---:|"])
            for pod in probe["pods"]:
                lines.append(f"| {pod['pod']} | {pod['requests']} | {pod['request_share_pct']} | {pod['mean_cpu_cores']} |")
    (root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    summary = summarize_run(args.directory)
    write_report(args.directory, summary)
    print(f"Wrote {args.directory / 'summary.json'} and report.md; capacity is not automatically validated")
    raise SystemExit(0 if summary["probes"] and all(p["routing_observed"] for p in summary["probes"]) else 1)
