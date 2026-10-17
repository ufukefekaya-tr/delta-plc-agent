from __future__ import annotations
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backends/ax/src"))
from axmcp_offline import project_creation as jobs
from axmcp_offline.security import ProtocolViolation, canonical_json_bytes, sha256_file


class ProjectCreationJobTests(unittest.TestCase):
    def setup_config(self, root):
        exe = root / "vendor.exe"; exe.write_bytes(b"synthetic-executable")
        engine = root / "engine.dll"; engine.write_bytes(b"synthetic-engine")
        path = root / "adapter.json"
        value = {"mode": "recorded_replay", "target": {"model": "AX-TEST"}, "project_factory": {
            "ide_executable": str(exe), "ide_executable_sha256": sha256_file(exe), "script_engine": str(engine), "script_engine_sha256": sha256_file(engine),
            "ide_profile": "Test Profile", "output_root": "scratch", "timeout_seconds": 20,
            "device_profile": {"profile_id": "test-v1", "model": "AX-TEST", "device_type": 4102, "device_id": "0000 0001", "device_version": "1.0"}}}
        path.write_bytes(canonical_json_bytes(value))
        args = {"project_id": "PortableProject", "controller_name": "Controller", "pou_name": "Program", "task_name": "Task", "interval_ms": 10, "declaration": "PROGRAM Program\nVAR\nEND_VAR", "implementation": "// no output"}
        return path, args

    def test_queue_is_confined_and_result_bound_to_request_and_project_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); config, args = self.setup_config(root)
            with patch.object(jobs.subprocess, "Popen") as launch:
                queued = jobs.submit(config, args)
            self.assertEqual(launch.call_count, 1)
            self.assertEqual(queued["status"], "queued")
            job = root / "scratch" / queued["job_id"]
            request = json.loads((job / "request.json").read_text(encoding="utf-8"))
            output = Path(request["create"]["output_path"])
            self.assertEqual(output.parent, job)
            output.write_bytes(b"synthetic-native")
            result = {"job_id": queued["job_id"], "operation": "project.create", "status": "created", "request_digest": sha256_file(job / "request.json"), "project_sha256": sha256_file(output), "live_verified": False}
            (job / "result.json").write_bytes(canonical_json_bytes(result))
            self.assertEqual(jobs.status(config, queued["job_id"])["status"], "created")
            output.write_bytes(b"drift")
            with self.assertRaisesRegex(ProtocolViolation, "drifted"):
                jobs.status(config, queued["job_id"])

    def test_no_arbitrary_path_model_or_executable_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); config, args = self.setup_config(root)
            with patch.object(jobs.subprocess, "Popen") as launch:
                for mutation in ({"output_path": "elsewhere.project"}, {"project_id": "../escape"}, {"interval_ms": True}):
                    with self.assertRaises(ProtocolViolation): jobs.submit(config, dict(args, **mutation))
                (root / "vendor.exe").write_bytes(b"changed")
                with self.assertRaisesRegex(ProtocolViolation, "identity changed"): jobs.submit(config, args)
                launch.assert_not_called()

    def test_cancel_is_a_durable_owned_job_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); config, args = self.setup_config(root)
            with patch.object(jobs.subprocess, "Popen"):
                queued = jobs.submit(config, args)
            self.assertEqual(jobs.cancel(config, queued["job_id"])["status"], "cancel_requested")
            self.assertTrue((root / "scratch" / queued["job_id"] / "cancel.json").is_file())
            with self.assertRaises(ProtocolViolation): jobs.cancel(config, "create_../../bad")

    def test_supervisor_revalidates_pins_before_any_ide_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); config, args = self.setup_config(root)
            with patch.object(jobs.subprocess, "Popen"):
                queued = jobs.submit(config, args)
            job = root / "scratch" / queued["job_id"]
            request = job / "request.json"
            (root / "engine.dll").write_bytes(b"changed")
            with patch.object(jobs.subprocess, "Popen") as launch:
                jobs.supervise(request, sha256_file(request))
                launch.assert_not_called()
            result = json.loads((job / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "failed")
            self.assertIn("identity drift", result["error"])


if __name__ == "__main__": unittest.main()
