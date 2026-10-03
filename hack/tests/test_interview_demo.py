"""Fail-closed checks at the portable demo's filesystem/process boundary."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from hack import interview_demo as demo


class InterviewDemoTests(unittest.TestCase):
    def bundle(self, root):
        root.mkdir()
        binary = "replay.exe" if sys.platform == "win32" else "replay"
        names = [f"run_{i}" for i in range(18)]
        paths = [binary, "interview_demo.py"]
        paths += [f"recordings/{name}/{file}" for name in names for file in ("replay-input.json", "report.json")]
        for relative in paths:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
        manifest = {"schema_version": 1, "expected": demo.EXPECTED,
                    "platform": sys.platform, "machine": platform.machine().lower(),
                    "binary": binary, "recordings": names,
                    "source": {}, "files": {name: demo.fingerprint(root / name) for name in paths}}
        demo.write_json(root / "manifest.json", manifest)
        return manifest

    def run_cli(self, bundle, output):
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            return demo.main(["run", "--bundle", str(bundle), "--output", str(output)])

    def test_changed_binary_is_rejected_before_execution_and_leaves_failed_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = self.bundle(root / "bundle")
            (root / "bundle" / manifest["binary"]).write_text("modified", encoding="utf-8")
            with patch.object(demo, "command") as execute:
                self.assertEqual(1, self.run_cli(root / "bundle", root / "report"))
                execute.assert_not_called()
            report = demo.read_json(root / "report/report.json")
            self.assertFalse(report["passed"])
            self.assertIn("checksum", report["error"])

    def test_partial_manifest_cannot_silently_drop_a_recording(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = self.bundle(root / "bundle")
            manifest["recordings"].pop()
            demo.write_json(root / "bundle/manifest.json", manifest)
            with patch.object(demo, "command") as execute:
                self.assertEqual(1, self.run_cli(root / "bundle", root / "report"))
                execute.assert_not_called()

    def test_os_mismatch_is_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = self.bundle(root / "bundle")
            manifest["platform"] = "another-os"
            demo.write_json(root / "bundle/manifest.json", manifest)
            with patch.object(demo, "command") as execute:
                self.assertEqual(1, self.run_cli(root / "bundle", root / "report"))
                execute.assert_not_called()

    def test_timeout_preserves_failure_instead_of_stale_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.bundle(root / "bundle")
            with patch.object(demo, "command", side_effect=subprocess.TimeoutExpired("replay", 1)):
                self.assertEqual(1, self.run_cli(root / "bundle", root / "report"))
            report = demo.read_json(root / "report/report.json")
            self.assertFalse(report["passed"])
            self.assertIn("timed out", report["error"])
            before = (root / "report/report.json").read_bytes()
            self.assertEqual(1, self.run_cli(root / "bundle", root / "report"))
            self.assertEqual(before, (root / "report/report.json").read_bytes())

    def test_rejection_must_be_expected_exit_and_have_no_success_json(self):
        recording = {"config": {}, "cycles": [{"expected": {"finalDesired": 1}}]}
        for result in (subprocess.CompletedProcess([], 0, "", ""),
                       subprocess.CompletedProcess([], 2, '{"verified":true}', "invalid"),
                       subprocess.CompletedProcess([], 2, "", "")):
            with self.subTest(result=result), patch.object(demo, "command", return_value=result):
                with self.assertRaises(ValueError):
                    demo.negative_checks(Path("replay"), recording, 1)

    def test_bundle_paths_cannot_escape_root(self):
        with tempfile.TemporaryDirectory() as temp:
            for relative in ("../outside", "/absolute", "C:/absolute", "recordings\\outside"):
                with self.subTest(relative=relative), self.assertRaises(ValueError):
                    demo.safe_member(Path(temp), relative)

    def test_doctor_creates_parent_directory_but_preserves_existing_report(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "new-directory/doctor.json"
            with patch.object(demo, "command", return_value=subprocess.CompletedProcess([], 0, "version", "")), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(0, demo.main(["doctor", "--output", str(output)]))
                before = output.read_bytes()
                self.assertEqual(1, demo.main(["doctor", "--output", str(output)]))
            self.assertEqual(before, output.read_bytes())

    def test_prepare_without_go_retains_failure_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "bundle"
            with patch.object(demo.shutil, "which", return_value=None), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(1, demo.main(["prepare", "--output", str(output)]))
            report = demo.read_json(output / "prepare.json")
            self.assertFalse(report["passed"])
            self.assertIn("Go is required", report["error"])

    def test_prepare_overrides_persisted_and_inherited_go_build_targets(self):
        host_os = "windows" if sys.platform == "win32" else "darwin" if sys.platform == "darwin" else "linux"
        identity = {"git_commit": "test", "source_sha256": "test"}
        build_environments = []

        def checked(argv, **kwargs):
            if argv[1] == "env":
                return json.dumps({"GOHOSTOS": host_os, "GOHOSTARCH": "amd64"})
            self.assertEqual("build", argv[1])
            build_environments.append(kwargs["env"])
            Path(argv[argv.index("-o") + 1]).write_text("test binary", encoding="utf-8")
            return ""

        with tempfile.TemporaryDirectory() as temp, \
                patch.object(demo.shutil, "which", return_value="go"), \
                patch.object(demo, "source_identity", return_value=identity), \
                patch.object(demo, "checked", side_effect=checked), \
                patch.object(demo, "verify_bundle", return_value={"totals": demo.EXPECTED}), \
                patch.dict(os.environ, {"GOOS": "plan9", "GOARCH": "mips", "GOAMD64": "v4", "CGO_ENABLED": "1"}), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(0, demo.main(["prepare", "--output", str(Path(temp) / "bundle")]))
        self.assertEqual(1, len(build_environments))
        self.assertEqual(host_os, build_environments[0]["GOOS"])
        self.assertEqual("amd64", build_environments[0]["GOARCH"])
        self.assertEqual("v1", build_environments[0]["GOAMD64"])
        self.assertEqual("0", build_environments[0]["CGO_ENABLED"])

    def test_malformed_manifest_preserves_diagnostic_without_executing_binary(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.bundle(root / "bundle")
            demo.write_json(root / "bundle/manifest.json", [])
            with patch.object(demo, "command") as execute:
                self.assertEqual(1, self.run_cli(root / "bundle", root / "report"))
                execute.assert_not_called()
            self.assertIn("JSON object", demo.read_json(root / "report/report.json")["error"])

    def test_failed_negative_check_retains_actual_response(self):
        recording = {"config": {}, "cycles": [{"expected": {"finalDesired": 1}}]}
        receipts = []
        with patch.object(demo, "command", return_value=subprocess.CompletedProcess([], 0, "wrong success", "")):
            with self.assertRaises(ValueError):
                demo.negative_checks(Path("replay"), recording, 1, receipts)
        self.assertEqual(1, len(receipts))
        self.assertFalse(receipts[0]["passed"])
        self.assertEqual("wrong success", receipts[0]["stdout"])

    def test_interruption_retains_failed_replay_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.bundle(root / "bundle")
            with patch.object(demo, "command", side_effect=KeyboardInterrupt):
                self.assertEqual(130, self.run_cli(root / "bundle", root / "report"))
            report = demo.read_json(root / "report/report.json")
            self.assertFalse(report["passed"])
            self.assertEqual("KeyboardInterrupt", report["error"])


if __name__ == "__main__":
    unittest.main()
