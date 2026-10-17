from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from axmcp_offline.constants import ALLOWED_OPERATIONS
from axmcp_offline.security import sha256_file

from . import PACKAGE_ROOT, SRC_ROOT


def _run(*args: str, env_overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_ROOT)
    env["AXMCP_MODE"] = "recorded_replay"
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "axmcp_offline", *args],
        cwd=PACKAGE_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )


class CliTests(unittest.TestCase):
    def test_doctor_and_smoke(self) -> None:
        doctor = _run("doctor")
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        doctor_payload = json.loads(doctor.stdout)
        self.assertTrue(doctor_payload["ready"])
        self.assertFalse(doctor_payload["live_ax_verified"])

        smoke = _run("smoke")
        self.assertEqual(smoke.returncode, 0, smoke.stderr)
        smoke_payload = json.loads(smoke.stdout)
        self.assertEqual(tuple(smoke_payload["operations"]), ALLOWED_OPERATIONS)
        self.assertFalse(smoke_payload["live_ax_verified"])

    def test_print_config_is_valid_and_uses_stdio(self) -> None:
        result = _run("print-config")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        server = payload["mcpServers"]["universal-plc-axmcp-offline"]
        self.assertEqual(server["args"], ["-m", "axmcp_offline", "serve"])
        self.assertEqual(server["env"], {"AXMCP_MODE": "recorded_replay"})

    def test_init_scratch_is_idempotent_and_placeholder_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = _run("init-scratch", tmp)
            second = _run("init-scratch", tmp)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            config = json.loads((Path(tmp) / "probe-config.json").read_text(encoding="utf-8"))
            self.assertEqual(config["environment_digest"], "sha256:" + ("0" * 64))

    def test_file_bridge_smoke_fails_when_every_operation_times_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            initialized = _run("init-scratch", tmp)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            config_path = Path(tmp) / "probe-config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["environment_digest"] = "sha256:" + ("1" * 64)
            config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            smoke = _run(
                "smoke",
                env_overrides={
                    "AXMCP_MODE": "file_bridge",
                    "AXMCP_SCRATCH_ROOT": tmp,
                    "AXMCP_TIMEOUT_SECONDS": "1",
                },
            )
            self.assertEqual(smoke.returncode, 2)
            self.assertIn("did not receive all five validated results", smoke.stderr)

    def test_setup_autonomous_is_one_command_digest_locked_and_doctor_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "private-root"
            baseline = base / "mini.project"
            executable = base / "DIADesigner-AX.exe"
            engine = base / "ScriptEngine3.dll"
            baseline.write_bytes(b"MINIMAL-AX-PROJECT\n")
            executable.write_bytes(b"FAKE-IDE-IDENTITY\n")
            engine.write_bytes(b"FAKE-SCRIPT-ENGINE-IDENTITY\n")
            environment_digest = "sha256:" + ("3" * 64)

            setup = _run(
                "setup-autonomous",
                str(root),
                "--environment-digest",
                environment_digest,
                "--baseline",
                str(baseline),
                "--baseline-sha256",
                sha256_file(baseline),
                "--ide-executable",
                str(executable),
                "--script-engine",
                str(engine),
            )
            self.assertEqual(setup.returncode, 0, setup.stderr)
            payload = json.loads(setup.stdout)
            self.assertFalse(payload["ide_or_target_contacted"])
            autonomous = json.loads(
                (root / "autonomous-config.json").read_text(encoding="utf-8")
            )
            probe = json.loads((root / "probe-config.json").read_text(encoding="utf-8"))
            self.assertEqual(autonomous["environment_digest"], environment_digest)
            self.assertEqual(probe["environment_digest"], environment_digest)
            enrolled = root / "v2" / payload["baseline"]["baseline_path"]
            self.assertEqual(sha256_file(enrolled), sha256_file(baseline))
            launcher = json.loads(
                (root / "ax-launcher-config.json").read_text(encoding="utf-8")
            )
            self.assertEqual(sha256_file(Path(launcher["worker_script"])), launcher["worker_script_sha256"])
            self.assertEqual(sha256_file(Path(launcher["ladder_helper"])), launcher["ladder_helper_sha256"])
            self.assertEqual(
                Path(launcher["ladder_helper"]),
                Path(launcher["worker_script"]).with_name("ladder_import.py"),
            )
            codex = tomllib.loads(
                (root / "codex-mcp.toml").read_text(encoding="utf-8")
            )["mcp_servers"]["universal-plc-axmcp"]
            self.assertEqual(codex["command"], str(Path(sys.executable).resolve()))
            self.assertEqual(codex["env"]["AXMCP_SCRATCH_ROOT"], str(root.resolve()))
            self.assertEqual(codex["env"]["AXMCP_AUTOSTART_WORKER"], "1")
            self.assertEqual(
                codex["env"]["AXMCP_AUTOSTART_DEPLOYMENT_WORKER"], "0"
            )

            autonomous_env = {
                "AXMCP_MODE": "autonomous_file_bridge",
                "AXMCP_SCRATCH_ROOT": str(root),
                "AXMCP_AUTOSTART_WORKER": "1",
            }
            doctor = _run("doctor", env_overrides=autonomous_env)
            self.assertEqual(doctor.returncode, 0, doctor.stderr)
            self.assertTrue(json.loads(doctor.stdout)["ready"])
            smoke = _run("autonomous-smoke", env_overrides=autonomous_env)
            self.assertEqual(smoke.returncode, 0, smoke.stderr)
            self.assertTrue(json.loads(smoke.stdout)["ready"])

            autonomous["target_allowlist"] = [
                {
                    "label": "isolated-ax308",
                    "fingerprint": "sha256:" + ("2" * 64),
                }
            ]
            (root / "autonomous-config.json").write_text(
                json.dumps(autonomous, sort_keys=True) + "\n", encoding="utf-8"
            )
            target_doctor = _run("doctor", env_overrides=autonomous_env)
            self.assertEqual(target_doctor.returncode, 2)
            target_payload = json.loads(target_doctor.stdout)
            self.assertFalse(target_payload["ready"])
            deployment_check = next(
                item
                for item in target_payload["checks"]
                if item["check"] == "deployment_worker_launcher"
            )
            self.assertEqual(deployment_check["status"], "fail")
            target_smoke = _run("autonomous-smoke", env_overrides=autonomous_env)
            self.assertEqual(target_smoke.returncode, 2)
            self.assertFalse(json.loads(target_smoke.stdout)["ready"])

            autonomous["target_allowlist"] = []
            (root / "autonomous-config.json").write_text(
                json.dumps(autonomous, sort_keys=True) + "\n", encoding="utf-8"
            )
            probe["environment_digest"] = "sha256:" + ("4" * 64)
            (root / "probe-config.json").write_text(
                json.dumps(probe, sort_keys=True) + "\n", encoding="utf-8"
            )
            mismatch = _run("doctor", env_overrides=autonomous_env)
            self.assertEqual(mismatch.returncode, 2)
            mismatch_payload = json.loads(mismatch.stdout)
            self.assertFalse(mismatch_payload["ready"])
            self.assertIn(
                "V1/V2 environment digests do not match",
                next(
                    item["diagnostic"]
                    for item in mismatch_payload["checks"]
                    if item["status"] == "fail"
                ),
            )

    def test_setup_autonomous_preflight_failure_does_not_create_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "must-not-exist"
            baseline = base / "mini.project"
            executable = base / "DIADesigner-AX.exe"
            engine = base / "ScriptEngine3.dll"
            baseline.write_bytes(b"MINIMAL-AX-PROJECT\n")
            executable.write_bytes(b"FAKE-IDE-IDENTITY\n")
            engine.write_bytes(b"FAKE-SCRIPT-ENGINE-IDENTITY\n")
            result = _run(
                "setup-autonomous",
                str(root),
                "--environment-digest",
                "sha256:" + ("3" * 64),
                "--baseline",
                str(baseline),
                "--baseline-sha256",
                "sha256:" + ("9" * 64),
                "--ide-executable",
                str(executable),
                "--script-engine",
                str(engine),
            )
            self.assertEqual(result.returncode, 2)
            self.assertFalse(root.exists())
            self.assertEqual(baseline.read_bytes(), b"MINIMAL-AX-PROJECT\n")

    def test_configure_deployment_worker_rejects_unapproved_live_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "private-root"
            baseline = base / "mini.project"
            executable = base / "DIADesigner-AX.exe"
            engine = base / "ScriptEngine3.dll"
            adapter = base / "ax308_target_adapter.py"
            enrollment_path = root / "v2" / "target-enrollment.json"
            baseline.write_bytes(b"MINIMAL-AX-PROJECT\n")
            executable.write_bytes(b"FAKE-IDE-IDENTITY\n")
            engine.write_bytes(b"FAKE-SCRIPT-ENGINE-IDENTITY\n")
            adapter.write_text(
                "ADAPTER_KIND = 'DIADESIGNER_AX308_SP18_TARGET/1'\n"
                "def create_target_api(context):\n"
                "    return context['target']\n",
                encoding="utf-8",
            )
            setup = _run(
                "setup-autonomous",
                str(root),
                "--environment-digest",
                "sha256:" + ("3" * 64),
                "--baseline",
                str(baseline),
                "--baseline-sha256",
                sha256_file(baseline),
                "--ide-executable",
                str(executable),
                "--script-engine",
                str(engine),
            )
            self.assertEqual(setup.returncode, 0, setup.stderr)
            target = "sha256:" + ("2" * 64)
            autonomous_path = root / "autonomous-config.json"
            autonomous = json.loads(autonomous_path.read_text(encoding="utf-8"))
            autonomous["target_allowlist"] = [
                {"label": "isolated-ax308", "fingerprint": target}
            ]
            autonomous["observation_expression_allowlist"] = [
                "Supervisor.State",
                "Supervisor.FaultCode",
            ]
            autonomous_path.write_text(
                json.dumps(autonomous, sort_keys=True) + "\n", encoding="utf-8"
            )
            enrollment = {
                "schema_version": "1",
                "record_type": "TargetEnrollment",
                "status": "enrolled",
                "captured_at": "2026-09-02T10:00:00Z",
                "model": "AX-308",
                "device_identity": "AX308-LAB-001",
                "runtime_version": "3.5.18.0",
                "firmware_version": "1.0.9.2",
                "gateway_route_digest": "sha256:" + ("4" * 64),
                "application_identity": "Application",
                "forced_values_present": False,
                "physical_outputs_energized": False,
                "isolation_evidence_digest": "sha256:" + ("5" * 64),
                "fingerprint": target,
            }
            enrollment_path.write_text(
                json.dumps(enrollment, sort_keys=True) + "\n", encoding="utf-8"
            )

            configured = _run(
                "configure-ax-deployment-worker",
                str(root),
                "--target-adapter",
                str(adapter),
                "--target-adapter-sha256",
                sha256_file(adapter),
                "--target-enrollment",
                str(enrollment_path),
                "--target-enrollment-sha256",
                sha256_file(enrollment_path),
            )
            self.assertEqual(configured.returncode, 2)
            self.assertIn("not approved", configured.stderr)
            self.assertFalse((root / "deployment-launcher-config.json").exists())
            codex = tomllib.loads((root / "codex-mcp.toml").read_text(encoding="utf-8"))
            env = codex["mcp_servers"]["universal-plc-axmcp"]["env"]
            self.assertEqual(env["AXMCP_AUTOSTART_DEPLOYMENT_WORKER"], "0")


if __name__ == "__main__":
    unittest.main()
