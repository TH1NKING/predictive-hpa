#!/usr/bin/env python3
"""Prepare and verify a portable, offline PredictiveHPA interview demo (stdlib only)."""

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs/benchmarks/assets/decision-replay-20260911"
EXPECTED = {"runs": 18, "decisions": 607, "computed": 457, "recorded": 150}
MODES = ("Current", "Predictive", "Hybrid")
LIMITS = [
    "Offline policy replay, not a live Kubernetes/Prometheus acceptance run.",
    "Replica inputs are recorded and fixed; this is not a closed-loop simulation.",
    "457 forecasts are recomputed; 150 retain recorded forecasts because initial CPU is unknown.",
    "No new HTTP success, latency, earlier scale-up, or cost benefit is established.",
    "SHA-256 detects bundle changes; an unsigned manifest does not establish publisher authenticity.",
]


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def fingerprint(path):
    return {"bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def command(argv, timeout=30, **kwargs):
    return subprocess.run([str(x) for x in argv], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, **kwargs)


def checked(argv, timeout=30, **kwargs):
    result = command(argv, timeout, **kwargs)
    if result.returncode:
        raise RuntimeError(f"{Path(str(argv[0])).name} exited {result.returncode}: {result.stderr.strip()}")
    return result.stdout


def new_directory(path):
    path = path.resolve()
    # Never merge a fresh result with stale successful receipts.
    path.mkdir(parents=True, exist_ok=False)
    return path


def source_identity():
    paths = sorted({*ROOT.glob("go.*"), *ROOT.glob("cmd/**/*.go"),
                    *ROOT.glob("api/**/*.go"), *ROOT.glob("internal/**/*.go")})
    hashes = {p.relative_to(ROOT).as_posix(): fingerprint(p)["sha256"] for p in paths}
    identity = {"source_files": hashes,
                "source_sha256": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()}
    for key, args in (("git_commit", ["rev-parse", "HEAD"]), ("git_status", ["status", "--porcelain"])):
        identity[key] = checked(["git", *args], cwd=ROOT).strip()
    return identity


def safe_member(root, relative):
    parts = PurePosixPath(relative)
    if not relative or "\\" in relative or ":" in relative or parts.is_absolute() or ".." in parts.parts:
        raise ValueError(f"Unsafe bundle path: {relative!r}")
    path = root.joinpath(*parts.parts)
    if path.resolve() != path.absolute() or not path.is_file():
        raise ValueError(f"Missing file or symlink in bundle: {relative}")
    return path


def prepare(args):
    output = new_directory(Path(args.output))
    receipt = {"passed": False, "started_at": timestamp()}
    try:
        if not shutil.which("go"):
            raise RuntimeError("Go is required only for prepare; use a previously prepared bundle for run")
        source_manifest = read_json(ASSETS / "manifest.json")["files"]
        recordings = sorted(ASSETS.glob("*/replay-input.json"))
        if len(recordings) != EXPECTED["runs"]:
            raise ValueError("Expected all 18 committed recordings")
        # Verify against the repository's original evidence manifest before copying.
        for recording in recordings:
            for path in (recording, recording.with_name("report.json")):
                if fingerprint(path) != source_manifest[path.relative_to(ASSETS).as_posix()]:
                    raise ValueError(f"Historical evidence checksum mismatch: {path.parent.name}/{path.name}")
        source = source_identity()
        binary = "replay.exe" if os.name == "nt" else "replay"
        env = os.environ.copy()
        # Build for this host even when the caller previously used cross-compilation.
        for key in ("GOOS", "GOARCH", "GOARM", "GOAMD64"):
            env.pop(key, None)
        host = json.loads(checked(["go", "env", "-json", "GOHOSTOS", "GOHOSTARCH"], env=env))
        # Explicit values override persisted `go env -w` targets as well as inherited ones.
        env.update(GOOS=host["GOHOSTOS"], GOARCH=host["GOHOSTARCH"], CGO_ENABLED="0")
        if host["GOHOSTARCH"] == "amd64":
            env["GOAMD64"] = "v1"
        if host["GOHOSTARCH"] == "arm":
            env["GOARM"] = "6"
        checked(["go", "build", "-trimpath", "-o", output / binary, "./cmd/replay"],
                timeout=args.build_timeout, cwd=ROOT, env=env)
        after_build = source_identity()
        if any(source[key] != after_build[key] for key in ("git_commit", "source_sha256")):
            raise RuntimeError("Source changed during the build; keep this attempt and prepare a new bundle")
        for recording in recordings:
            destination = output / "recordings" / recording.parent.name
            destination.mkdir(parents=True)
            shutil.copy2(recording, destination / recording.name)
            shutil.copy2(recording.with_name("report.json"), destination / "report.json")
        shutil.copy2(Path(__file__), output / "interview_demo.py")
        files = {p.relative_to(output).as_posix(): fingerprint(p)
                 for p in sorted(output.rglob("*")) if p.is_file()}
        manifest = {"schema_version": 1, "created_at": timestamp(), "platform": sys.platform,
                    "machine": platform.machine().lower(), "binary": binary,
                    "expected": EXPECTED, "recordings": [p.parent.name for p in recordings],
                    "files": files, "source": source,
                    "historical_manifest_sha256": fingerprint(ASSETS / "manifest.json")["sha256"]}
        write_json(output / "manifest.json", manifest)
        # A successful build alone is not a ready-to-demo bundle.
        report = verify_bundle(output, output / "self-check", args.timeout)
        receipt.update(passed=True, totals=report["totals"])
        print(f"READY: {output}\nVerified 18 runs / 607 decisions; copy the entire directory to the same OS/architecture.")
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        receipt["error"] = str(error) or type(error).__name__
        raise
    finally:
        receipt["finished_at"] = timestamp()
        write_json(output / "prepare.json", receipt)


def load_bundle(bundle):
    manifest = read_json(bundle / "manifest.json")
    if not isinstance(manifest, dict):
        raise ValueError("Bundle manifest must be a JSON object")
    if manifest.get("schema_version") != 1 or manifest.get("expected") != EXPECTED:
        raise ValueError("Unsupported bundle schema or incomplete campaign counts")
    if (manifest.get("platform"), manifest.get("machine")) != (sys.platform, platform.machine().lower()):
        raise ValueError("Bundle OS/architecture differs from this host; prepare on the target host")
    binary = "replay.exe" if os.name == "nt" else "replay"
    if manifest.get("binary") != binary:
        raise ValueError("Unexpected replay executable name")
    names = manifest["recordings"]
    if len(names) != EXPECTED["runs"] or len(set(names)) != len(names):
        raise ValueError("Expected 18 unique recordings")
    for name in names:
        if not isinstance(name, str) or not name or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in name):
            raise ValueError("Invalid recording name")
    wanted = {binary, "interview_demo.py"}
    wanted.update(f"recordings/{name}/{file}" for name in names for file in ("replay-input.json", "report.json"))
    if set(manifest["files"]) != wanted:
        raise ValueError("Bundle file inventory does not match the complete campaign")
    for relative, expected in manifest["files"].items():
        if fingerprint(safe_member(bundle, relative)) != expected:
            raise ValueError(f"Bundle checksum mismatch: {relative}")
    return manifest, bundle / binary


def negative_checks(binary, recording, timeout, receipts=None):
    cases = []
    malformed = deepcopy(recording)
    malformed["config"]["horizonSeconds"] = -1
    cases.append(("negative-horizon", malformed, 2))
    tampered = deepcopy(recording)
    tampered["cycles"][0]["expected"]["finalDesired"] += 1
    cases.append(("altered-expected-decision", tampered, 1))
    unknown = deepcopy(recording)
    unknown["unexpectedField"] = True
    cases.append(("unknown-field", unknown, 2))
    if receipts is None:
        receipts = []
    for name, payload, expected in cases:
        result = command([binary], timeout, input=json.dumps(payload))
        passed = result.returncode == expected and not result.stdout.strip() and bool(result.stderr.strip())
        receipt = {"case": name, "expected_exit": expected, "actual_exit": result.returncode,
                   "diagnostic": result.stderr.strip(), "passed": passed}
        if not passed:
            receipt["stdout"] = result.stdout
        receipts.append(receipt)
        if not passed:
            raise ValueError(f"{name}: expected rejection exit {expected}, no stdout and a diagnostic; got exit {result.returncode}")
    return receipts


def report_markdown(report):
    lines = ["# PredictiveHPA interview replay", "", f"Result: {'PASS' if report['passed'] else 'FAIL'}", ""]
    if "error" in report:
        lines.extend([f"Error: {report['error']}", ""])
    if "totals" in report:
        lines.extend([f"Verified totals: `{json.dumps(report['totals'])}`", ""])
    lines.extend(["## First decision after load onset (recorded replica inputs)", "",
                  "Mode columns show policy candidates and skip reasons. A nonempty skip reason prevents a Scale write, even when the candidate exceeds requested replicas.", "",
                  "| Run | Observed / requested | CPU % | Raw forecast % | Current candidate | Predictive candidate | Hybrid candidate |",
                  "|---|---|---:|---:|---|---|---|"])
    for example in report.get("examples", []):
        cells = [example["run"], f"{example['observed_replicas']} / {example['requested_replicas']}",
                 f"{example['current_cpu']:.2f}", f"{example['raw_prediction']:.2f}"]
        for mode in MODES:
            outcome = example["modes"][mode]
            cells.append(f"{outcome['finalDesired']} ({outcome['skipReason'] or 'write candidate'})")
        lines.append("| " + " | ".join(cells) + " |")
    lines.extend(["", "## Negative checks", ""])
    for check in report.get("negative_checks", []):
        outcome = "PASS" if check["passed"] else "FAIL"
        lines.append(f"- {check['case']}: {outcome}, exit {check['actual_exit']} (expected {check['expected_exit']})")
    lines.extend(["", "## Evidence boundaries", "", *["- " + limit for limit in LIMITS], ""])
    return "\n".join(lines)


def verify_bundle(bundle, output, timeout):
    output = new_directory(output)
    report = {"schema_version": 1, "started_at": timestamp(), "passed": False,
              "runs": [], "examples": [], "limitations": LIMITS}
    try:
        manifest, binary = load_bundle(bundle)
        report["bundle_manifest_sha256"] = fingerprint(bundle / "manifest.json")["sha256"]
        report["source"] = manifest["source"]
        report["replay_binary_sha256"] = fingerprint(binary)["sha256"]
        totals = Counter(runs=0, decisions=0, computed=0, recorded=0)
        matrix = Counter()
        first = None
        for name in manifest["recordings"]:
            input_path = bundle / "recordings" / name / "replay-input.json"
            recording = read_json(input_path)
            result = json.loads(checked([binary, "-input", input_path], timeout))
            if result.get("verified") is not True or len(result["cycles"]) != len(recording["cycles"]):
                raise ValueError(f"{name}: incomplete or unverified replay")
            if first is None:
                first = recording
            sources = Counter(c["predictionSource"] for c in result["cycles"])
            if set(sources) - {"computed-history", "recorded-forecast"}:
                raise ValueError(f"{name}: unknown prediction source")
            totals.update(runs=1, decisions=len(result["cycles"]), computed=sources["computed-history"],
                          recorded=sources["recorded-forecast"])
            pattern = "step" if "_step_" in name else "ramp" if "_ramp_" in name else "unknown"
            matrix[(pattern, recording["cycles"][0]["actualMode"])] += 1
            write_json(output / f"{name}.json", result)
            report["runs"].append({"name": name, "decisions": len(result["cycles"]), "verified": True})
            historical = read_json(input_path.with_name("report.json"))
            onset = next((c for c in historical["cycles"] if c.get("replay_index") is not None
                          and c.get("decision_seconds") is not None and c["decision_seconds"] >= 0), None)
            if onset is None:
                raise ValueError(f"{name}: missing first decision after load onset")
            index = onset["replay_index"]
            cycle = result["cycles"][index]
            report["examples"].append({"run": name, "at": cycle["at"],
                                       "observed_replicas": recording["cycles"][index]["observedReplicas"],
                                       "requested_replicas": recording["cycles"][index]["requestedReplicas"],
                                       "current_cpu": recording["cycles"][index]["currentCPU"],
                                       "raw_prediction": cycle["rawPrediction"], "modes": cycle["modes"]})
            print(f"PASS {name}: {len(result['cycles'])} decisions")
        if dict(totals) != EXPECTED or matrix != Counter({(pattern, mode): 3 for pattern in ("step", "ramp") for mode in MODES}):
            raise ValueError(f"Campaign inventory mismatch: {dict(totals)}, {dict(matrix)}")
        report["totals"] = dict(totals)
        report["negative_checks"] = []
        negative_checks(binary, first, timeout, report["negative_checks"])
        report["passed"] = True
        return report
    except (OSError, ValueError, KeyError, IndexError, TypeError, RuntimeError, subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        report["error"] = str(error) or type(error).__name__
        raise
    finally:
        report["finished_at"] = timestamp()
        write_json(output / "report.json", report)
        (output / "report.md").write_text(report_markdown(report), encoding="utf-8")


def doctor(args):
    checks = [{"name": "Python >= 3.10", "passed": sys.version_info >= (3, 10), "detail": sys.version.split()[0]}]
    checks.append({"name": "offline recordings", "passed": len(list(ASSETS.glob('*/replay-input.json'))) == 18,
                   "detail": "Required for prepare only; a prepared bundle is self-contained"})
    commands = [("Go (prepare only)", ["go", "version"]), ("Git (prepare only)", ["git", "--version"])]
    if args.live:
        checks.append({"name": "Linux live acceptance host", "passed": sys.platform == "linux",
                       "detail": "Use the documented Linux host for the full Kind acceptance"})
        commands += [("Docker daemon", ["docker", "version", "--format", "{{.Server.Version}}"]),
                     ("Kind", ["kind", "version"]), ("Helm", ["helm", "version", "--short"]),
                     ("kubectl client", ["kubectl", "version", "--client=true", "-o", "json"])]
    for name, argv in commands:
        try:
            result = command(argv, timeout=10)
            checks.append({"name": name, "passed": result.returncode == 0,
                           "detail": (result.stdout or result.stderr).strip()[:1500]})
        except (OSError, subprocess.TimeoutExpired) as error:
            checks.append({"name": name, "passed": False, "detail": str(error)})
    result = {"passed": all(c["passed"] for c in checks), "checks": checks,
              "limitations": "Read-only prerequisite check; does not certify a live cluster or download dependencies"}
    for check in checks:
        print(f"{'PASS' if check['passed'] else 'FAIL'} {check['name']}: {check['detail']}")
    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, indent=2)
            stream.write("\n")
    return 0 if result["passed"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    prep = sub.add_parser("prepare", help="Build, checksum and rehearse a portable bundle before the interview")
    prep.add_argument("--output", required=True, help="New bundle directory (existing paths are refused)")
    prep.add_argument("--build-timeout", type=int, default=300)
    prep.add_argument("--timeout", type=int, default=30)
    run = sub.add_parser("run", help="Offline replay, no Go, Docker, kubeconfig or network required")
    run.add_argument("--bundle", required=True)
    run.add_argument("--output", required=True, help="New report directory")
    run.add_argument("--timeout", type=int, default=30)
    check = sub.add_parser("doctor", help="Read-only preflight (no cluster access)")
    check.add_argument("--live", action="store_true")
    check.add_argument("--output")
    args = parser.parse_args(argv)
    for name in ("timeout", "build_timeout"):
        if hasattr(args, name) and getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    try:
        if args.action == "prepare":
            prepare(args)
        elif args.action == "run":
            report = verify_bundle(Path(args.bundle).resolve(), Path(args.output), args.timeout)
            print(f"PASS {report['totals']['decisions']} decisions / 3 rejection checks; report: {Path(args.output).resolve() / 'report.md'}")
        else:
            return doctor(args)
        return 0
    except (OSError, ValueError, KeyError, IndexError, TypeError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("FAIL: interrupted; any created output directory retains the failed receipt", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
