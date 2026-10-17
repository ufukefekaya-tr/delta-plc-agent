from __future__ import annotations

import json
import hashlib
import os
import tempfile
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from axmcp_offline.autonomous import initialize_autonomous_root
import axmcp_offline.ax_deployment_supervisor as deployment_supervisor_module
from axmcp_offline.ax_deployment_supervisor import (
    AxDeploymentSupervisor,
    write_deployment_launcher_config,
)
from axmcp_offline.security import (
    ProtocolViolation,
    canonical_json_bytes,
    sha256_bytes,
    sha256_file,
)
from axmcp_offline.service import OfflineService


ENVIRONMENT = "sha256:" + ("1" * 64)
TARGET = "sha256:" + ("2" * 64)
ROUTE = "sha256:" + ("3" * 64)
ISOLATION = "sha256:" + ("4" * 64)
ADAPTER_BYTES = (
    b"ADAPTER_KIND = 'DIADESIGNER_AX308_SP18_TARGET/1'\n"
    b"def create_target_api(context):\n"
    b"    return context['target']\n"
)
ADAPTER_DIGEST = "sha256:" + hashlib.sha256(ADAPTER_BYTES).hexdigest()


class _Process:
    pid = 4242

    @staticmethod
    def poll():
        return None


class _PopenRecorder:
    def __init__(self, on_start=None) -> None:
        self.calls: list[tuple[str | list[str], dict[str, object]]] = []
        self.on_start = on_start

    def __call__(self, command, **kwargs):
        self.calls.append((command, dict(kwargs)))
        process = _Process()
        if self.on_start is not None:
            self.on_start(process)
        return process


def _stamp(offset_seconds: int = 0) -> str:
    value = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=offset_seconds)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _archived_request(
    v2: Path,
    job_id: str,
    operation: str,
    artifacts: list[dict[str, str]],
    authorization_digest: str | None = None,
) -> tuple[dict, bytes]:
    request = {
        "protocol": "AXQ-FILE/2",
        "message_type": "request",
        "correlation_id": str(uuid.uuid4()),
        "job_id": job_id,
        "operation": operation,
        "created_at": _stamp(-10),
        "deadline_at": _stamp(60),
        "environment_digest": ENVIRONMENT,
        "workspace_digest": None,
        "expected_revision": None,
        "authorization_digest": authorization_digest,
        "artifacts": artifacts,
    }
    raw = canonical_json_bytes(request)
    path = v2 / "archive" / f"{request['correlation_id']}.request.json"
    path.write_bytes(raw)
    return request, raw


def _terminal_result(
    request: dict,
    request_raw: bytes,
    status: str,
    artifacts: list[dict[str, str]],
) -> dict:
    return {
        "protocol": "AXQ-FILE/2",
        "message_type": "result",
        "correlation_id": request["correlation_id"],
        "job_id": request["job_id"],
        "operation": request["operation"],
        "created_at": _stamp(),
        "status": status,
        "environment_digest": ENVIRONMENT,
        "workspace_digest": None,
        "revision": None,
        "before_project_digest": None,
        "after_project_digest": None,
        "request_digest": sha256_bytes(request_raw),
        "artifacts": artifacts,
        "diagnostics": [],
    }


def _worker_evidence(
    request: dict,
    outcome: str,
    artifact_digest: str,
    reconciliation_required: bool,
) -> dict:
    return {
        "schema_version": "1",
        "record_type": "AxDeploymentWorkerEvidence",
        "created_at": _stamp(),
        "correlation_id": request["correlation_id"],
        "job_id": request["job_id"],
        "operation": request["operation"],
        "outcome": outcome,
        "target_fingerprint": TARGET,
        "authority_digest": request["authorization_digest"],
        "candidate_digest": artifact_digest,
        "rollback_digest": artifact_digest,
        "reconciliation_required": reconciliation_required,
        "sample_count": 0,
        "diagnostics": [],
    }


class AxDeploymentSupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.approval = patch.object(
            deployment_supervisor_module,
            "APPROVED_TARGET_ADAPTER_SHA256",
            frozenset({ADAPTER_DIGEST}),
        )
        self.approval.start()

    def tearDown(self) -> None:
        self.approval.stop()

    def _fixture(self, base: Path) -> tuple[Path, dict[str, Path], Path]:
        root = base / "runtime"
        initialize_autonomous_root(root)
        autonomous_path = root / "autonomous-config.json"
        autonomous = json.loads(autonomous_path.read_text(encoding="utf-8"))
        autonomous["environment_digest"] = ENVIRONMENT
        autonomous["target_allowlist"] = [
            {"label": "isolated-ax308", "fingerprint": TARGET}
        ]
        autonomous["observation_expression_allowlist"] = [
            "Supervisor.State",
            "Supervisor.FaultCode",
        ]
        autonomous_path.write_bytes(canonical_json_bytes(autonomous))

        files = {
            "ide": base / "DIADesigner-AX.exe",
            "engine": base / "ScriptEngine3.dll",
            "worker": base / "worker.py",
            "adapter": base / "ax308_target_adapter.py",
            "enrollment": root / "v2" / "target-enrollment.json",
        }
        files["ide"].write_bytes(b"IDE\n")
        files["engine"].write_bytes(b"ENGINE\n")
        files["worker"].write_bytes(b"WORKER\n")
        files["adapter"].write_bytes(ADAPTER_BYTES)
        enrollment = {
            "schema_version": "1",
            "record_type": "TargetEnrollment",
            "status": "enrolled",
            "captured_at": _stamp(-60),
            "model": "AX-308",
            "device_identity": "AX308-LAB-001",
            "runtime_version": "3.5.18.0",
            "firmware_version": "1.0.9.2",
            "gateway_route_digest": ROUTE,
            "application_identity": "Application",
            "forced_values_present": False,
            "physical_outputs_energized": False,
            "isolation_evidence_digest": ISOLATION,
            "fingerprint": TARGET,
        }
        files["enrollment"].write_bytes(canonical_json_bytes(enrollment))
        config_path = write_deployment_launcher_config(
            root,
            ide_executable=files["ide"],
            ide_executable_sha256=sha256_file(files["ide"]),
            script_engine=files["engine"],
            script_engine_sha256=sha256_file(files["engine"]),
            ide_profile="DIADesigner-AX 1.9",
            worker_script=files["worker"],
            worker_script_sha256=sha256_file(files["worker"]),
            target_adapter=files["adapter"],
            target_adapter_sha256=sha256_file(files["adapter"]),
            target_enrollment=files["enrollment"],
            target_enrollment_sha256=sha256_file(files["enrollment"]),
        )
        return root, files, config_path

    @staticmethod
    def _queue_request(root: Path, job_id: str, operation: str) -> None:
        request = {
            "protocol": "AXQ-FILE/2",
            "message_type": "request",
            "correlation_id": str(uuid.uuid4()),
            "job_id": job_id,
            "operation": operation,
        }
        path = root / "v2" / "inbox" / f"{request['correlation_id']}.request.json"
        path.write_bytes(canonical_json_bytes(request))

    @staticmethod
    def _lease(root: Path, config: dict, config_digest: str, **changes) -> Path:
        lease = {
            "protocol": "AXQ-FILE/2",
            "worker_id": config["worker_id"],
            "pid": 9001,
            "process_start_fingerprint": "sha256:" + ("5" * 64),
            "started_at": _stamp(-1),
            "heartbeat_at": _stamp(-1),
            "environment_digest": ENVIRONMENT,
            "target_fingerprint": TARGET,
            "config_digest": config_digest,
            "sequence": 0,
        }
        lease.update(changes)
        path = root / "v2" / "state" / "deployment-worker.lease.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical_json_bytes(lease))
        return path

    def _ready_worker(
        self, root: Path, config: dict, config_digest: str, pid: int
    ) -> None:
        lease_path = self._lease(root, config, config_digest, pid=pid)
        lease = json.loads(lease_path.read_text(encoding="utf-8"))
        heartbeat = {
            "protocol": "AXQ-FILE/2",
            "worker_id": config["worker_id"],
            "pid": pid,
            "process_start_fingerprint": lease["process_start_fingerprint"],
            "created_at": _stamp(),
            "state": "started",
            "processed": 0,
            "target_fingerprint": TARGET,
            "config_digest": config_digest,
        }
        heartbeat_path = (
            root
            / "v2"
            / "state"
            / "deployment-heartbeats"
            / config["worker_id"]
            / "ready-00000001.json"
        )
        heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        heartbeat_path.write_bytes(canonical_json_bytes(heartbeat))

    @staticmethod
    def _launch_record(
        root: Path,
        config: dict,
        config_digest: str,
        job_id: str,
        operation: str,
        pid: int,
    ) -> Path:
        path = root / "v2" / "state" / "launches" / f"{job_id}.deployment.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            canonical_json_bytes(
                {
                    "schema_version": "1",
                    "record_type": "AxDeploymentWorkerLaunch",
                    "job_id": job_id,
                    "operation": operation,
                    "target_fingerprint": TARGET,
                    "worker_id": config["worker_id"],
                    "pid": pid,
                    "launched_at": _stamp(-1),
                    "config_digest": config_digest,
                    "headless": True,
                }
            )
        )
        return path

    def test_exact_start_is_shell_free_and_echoes_all_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, files, config_path = self._fixture(Path(tmp))
            job_id = str(uuid.uuid4())
            self._queue_request(root, job_id, "deployment.execute")
            config = json.loads(config_path.read_text(encoding="utf-8"))
            popen = _PopenRecorder(
                lambda process: self._ready_worker(
                    root,
                    config,
                    sha256_file(config_path),
                    process.pid,
                )
            )
            supervisor = AxDeploymentSupervisor(
                root, popen=popen, process_alive=lambda _pid: True
            )

            launch = supervisor.ensure_running(job_id, "deployment.execute", TARGET)

            self.assertEqual(launch["launch_state"], "started")
            self.assertEqual(launch["job_id"], job_id)
            self.assertEqual(launch["operation"], "deployment.execute")
            self.assertEqual(launch["target_fingerprint"], TARGET)
            self.assertEqual(launch["config_digest"], sha256_file(config_path))
            command, kwargs = popen.calls[0]
            if os.name == "nt":
                self.assertIsInstance(command, str)
                self.assertTrue(command.startswith(f'"{files["ide"].resolve()}" '))
                self.assertIn('--profile="DIADesigner-AX 1.9"', command)
                self.assertIn(f'--runscript="{files["worker"].resolve()}"', command)
                self.assertTrue(command.endswith("--culture=en --noUI --textPrompts"))
            else:
                self.assertEqual(command[0], str(files["ide"].resolve()))
                self.assertIn('--profile="DIADesigner-AX 1.9"', command)
                self.assertIn(f'--runscript="{files["worker"].resolve()}"', command)
                self.assertIn("--culture=en", command)
                self.assertIn("--noUI", command)
            self.assertFalse(kwargs["shell"])
            self.assertEqual(
                kwargs["env"]["AXMCP_DEPLOYMENT_CONFIG"], str(config_path)
            )
            self.assertEqual(
                kwargs["env"]["AXMCP_DEPLOYMENT_CONFIG_SHA256"], sha256_file(config_path)
            )
            self.assertTrue(
                (root / "v2" / "state" / "launches" / f"{job_id}.deployment.json").is_file()
            )

    def test_wrong_operation_target_or_missing_request_is_rejected_prelaunch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _files, _config = self._fixture(Path(tmp))
            popen = _PopenRecorder()
            supervisor = AxDeploymentSupervisor(root, popen=popen)
            job_id = str(uuid.uuid4())
            with self.assertRaises(ProtocolViolation):
                supervisor.ensure_running(job_id, "project.build", TARGET)
            with self.assertRaises(ProtocolViolation):
                supervisor.ensure_running(
                    job_id, "deployment.execute", "sha256:" + ("9" * 64)
                )
            with self.assertRaisesRegex(ProtocolViolation, "not queued"):
                supervisor.ensure_running(job_id, "deployment.execute", TARGET)
            self.assertEqual(popen.calls, [])

    def test_pinned_file_or_host_config_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, files, _config = self._fixture(Path(tmp))
            files["adapter"].write_bytes(b"DRIFT\n")
            with self.assertRaisesRegex(ProtocolViolation, "target_adapter bytes changed"):
                AxDeploymentSupervisor(root)

        with tempfile.TemporaryDirectory() as tmp:
            root, _files, _config = self._fixture(Path(tmp))
            path = root / "autonomous-config.json"
            config = json.loads(path.read_text(encoding="utf-8"))
            config["target_allowlist"] = []
            path.write_bytes(canonical_json_bytes(config))
            with self.assertRaisesRegex(ProtocolViolation, "bytes changed"):
                AxDeploymentSupervisor(root)

    def test_live_exact_lease_is_adopted_and_foreign_lease_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _files, config_path = self._fixture(Path(tmp))
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self._lease(root, config, sha256_file(config_path))
            job_id = str(uuid.uuid4())
            self._queue_request(root, job_id, "target.observe.start")
            popen = _PopenRecorder()
            supervisor = AxDeploymentSupervisor(
                root, popen=popen, process_alive=lambda _pid: True
            )
            launch = supervisor.ensure_running(job_id, "target.observe.start", TARGET)
            self.assertEqual(launch["launch_state"], "adopted")
            self.assertEqual(launch["pid"], 9001)
            self.assertEqual(popen.calls, [])

        with tempfile.TemporaryDirectory() as tmp:
            root, _files, config_path = self._fixture(Path(tmp))
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self._lease(
                root,
                config,
                sha256_file(config_path),
                target_fingerprint="sha256:" + ("9" * 64),
            )
            job_id = str(uuid.uuid4())
            self._queue_request(root, job_id, "deployment.execute")
            supervisor = AxDeploymentSupervisor(root, process_alive=lambda _pid: True)
            with self.assertRaisesRegex(ProtocolViolation, "foreign"):
                supervisor.ensure_running(job_id, "deployment.execute", TARGET)

    def test_launch_record_ready_gap_adopts_or_retries_only_after_proven_dead(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _files, config_path = self._fixture(Path(tmp))
            config = json.loads(config_path.read_text(encoding="utf-8"))
            digest = sha256_file(config_path)
            job_id = str(uuid.uuid4())
            self._queue_request(root, job_id, "target.observe.start")
            self._launch_record(
                root, config, digest, job_id, "target.observe.start", _Process.pid
            )
            self._ready_worker(root, config, digest, _Process.pid)
            popen = _PopenRecorder()
            supervisor = AxDeploymentSupervisor(
                root, popen=popen, process_alive=lambda pid: pid == _Process.pid
            )
            launch = supervisor.ensure_running(
                job_id, "target.observe.start", TARGET
            )
            self.assertEqual(launch["launch_state"], "adopted")
            self.assertEqual(launch["pid"], _Process.pid)
            self.assertEqual(popen.calls, [])

        with tempfile.TemporaryDirectory() as tmp:
            root, _files, config_path = self._fixture(Path(tmp))
            config = json.loads(config_path.read_text(encoding="utf-8"))
            digest = sha256_file(config_path)
            job_id = str(uuid.uuid4())
            self._queue_request(root, job_id, "target.observe.start")
            self._launch_record(
                root, config, digest, job_id, "target.observe.start", 31337
            )
            popen = _PopenRecorder(
                lambda process: self._ready_worker(
                    root, config, digest, process.pid
                )
            )
            supervisor = AxDeploymentSupervisor(
                root,
                popen=popen,
                process_alive=lambda pid: pid == _Process.pid,
            )
            launch = supervisor.ensure_running(
                job_id, "target.observe.start", TARGET
            )
            self.assertEqual(launch["launch_state"], "started")
            self.assertEqual(len(popen.calls), 1)
            self.assertEqual(
                len(list((root / "v2" / "archive" / "launches").glob("*.json"))),
                1,
            )

    def test_launch_record_unknown_pid_is_fail_closed_without_second_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _files, config_path = self._fixture(Path(tmp))
            config = json.loads(config_path.read_text(encoding="utf-8"))
            digest = sha256_file(config_path)
            job_id = str(uuid.uuid4())
            self._queue_request(root, job_id, "deployment.execute")
            record = self._launch_record(
                root, config, digest, job_id, "deployment.execute", 31337
            )
            popen = _PopenRecorder()
            supervisor = AxDeploymentSupervisor(
                root, popen=popen, process_alive=lambda _pid: None
            )
            with self.assertRaisesRegex(ProtocolViolation, "liveness is unknown"):
                supervisor.ensure_running(job_id, "deployment.execute", TARGET)
            self.assertTrue(record.is_file())
            self.assertEqual(popen.calls, [])

    def test_dead_lease_is_archived_before_new_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _files, config_path = self._fixture(Path(tmp))
            config = json.loads(config_path.read_text(encoding="utf-8"))
            lease = self._lease(root, config, sha256_file(config_path))
            job_id = str(uuid.uuid4())
            self._queue_request(root, job_id, "deployment.execute")
            popen = _PopenRecorder(
                lambda process: self._ready_worker(
                    root,
                    config,
                    sha256_file(config_path),
                    process.pid,
                )
            )
            supervisor = AxDeploymentSupervisor(
                root,
                popen=popen,
                process_alive=lambda pid: pid == _Process.pid,
            )
            launch = supervisor.ensure_running(job_id, "deployment.execute", TARGET)
            self.assertEqual(launch["launch_state"], "started")
            self.assertEqual(
                json.loads(lease.read_text(encoding="utf-8"))["pid"], _Process.pid
            )
            self.assertEqual(
                len(list((root / "v2" / "archive" / "leases").glob("*-deployment-dead.json"))),
                1,
            )

    def test_live_pid_with_stale_heartbeat_is_never_stolen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _files, config_path = self._fixture(Path(tmp))
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self._lease(
                root,
                config,
                sha256_file(config_path),
                heartbeat_at=_stamp(-30),
            )
            job_id = str(uuid.uuid4())
            self._queue_request(root, job_id, "deployment.execute")
            supervisor = AxDeploymentSupervisor(root, process_alive=lambda _pid: True)
            with self.assertRaisesRegex(ProtocolViolation, "heartbeat is stale"):
                supervisor.ensure_running(job_id, "deployment.execute", TARGET)

    def test_unknown_pid_state_is_never_stolen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _files, config_path = self._fixture(Path(tmp))
            config = json.loads(config_path.read_text(encoding="utf-8"))
            lease = self._lease(
                root,
                config,
                sha256_file(config_path),
                heartbeat_at=_stamp(-30),
            )
            job_id = str(uuid.uuid4())
            self._queue_request(root, job_id, "deployment.execute")
            supervisor = AxDeploymentSupervisor(root, process_alive=lambda _pid: None)
            with self.assertRaisesRegex(ProtocolViolation, "liveness is unknown"):
                supervisor.ensure_running(job_id, "deployment.execute", TARGET)
            self.assertTrue(lease.is_file())

    def test_start_without_exact_readiness_lease_returns_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _files, _config_path = self._fixture(Path(tmp))
            job_id = str(uuid.uuid4())
            self._queue_request(root, job_id, "target.observe.start")
            clock = [0.0]
            popen = _PopenRecorder()
            supervisor = AxDeploymentSupervisor(
                root,
                popen=popen,
                process_alive=lambda _pid: True,
                monotonic=lambda: clock[0],
                sleeper=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
            )
            launch = supervisor.ensure_running(job_id, "target.observe.start", TARGET)
            self.assertEqual(launch["launch_state"], "unknown")
            self.assertIn("readiness lease", launch["diagnostic"])

    def test_unapproved_adapter_digest_is_rejected_before_config_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(
                deployment_supervisor_module,
                "APPROVED_TARGET_ADAPTER_SHA256",
                frozenset(),
            ):
                with self.assertRaisesRegex(ProtocolViolation, "not approved"):
                    self._fixture(Path(tmp))

    def test_service_wires_deployment_supervisor_only_with_explicit_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _files, _config = self._fixture(Path(tmp))
            environment = {
                "AXMCP_MODE": "autonomous_file_bridge",
                "AXMCP_SCRATCH_ROOT": str(root),
                "AXMCP_AUTOSTART_WORKER": "0",
                "AXMCP_AUTOSTART_DEPLOYMENT_WORKER": "1",
            }
            with patch.dict(os.environ, environment, clear=True):
                service = OfflineService.from_environment()
            self.assertIsInstance(
                service.autonomous.deployment_supervisor, AxDeploymentSupervisor
            )
            self.assertIsNone(service.autonomous.supervisor)

            environment["AXMCP_AUTOSTART_DEPLOYMENT_WORKER"] = "maybe"
            with patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(
                    ProtocolViolation, "AXMCP_AUTOSTART_DEPLOYMENT_WORKER"
                ):
                    OfflineService.from_environment()

    def test_partial_reconciliation_record_cannot_clear_unknown_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _files, _config = self._fixture(Path(tmp))
            v2 = root / "v2"
            artifact_path = v2 / "artifacts" / ("a" * 64 + ".project")
            artifact_path.write_bytes(b"DEPLOYABLE\n")
            artifact_digest = sha256_file(artifact_path)
            canonical_artifact_path = v2 / "artifacts" / f"{artifact_digest[7:]}.project"
            artifact_path.replace(canonical_artifact_path)
            runtime_release = "sha256:" + ("6" * 64)
            workspace = v2 / "workspaces" / str(uuid.uuid4())
            workspace.mkdir()
            (workspace / "manifest.json").write_bytes(
                canonical_json_bytes(
                    {
                        "schema_version": "1",
                        "frozen_artifacts": [
                            {
                                "artifact_digest": artifact_digest,
                                "environment_digest": ENVIRONMENT,
                                "build_digest": "sha256:" + ("7" * 64),
                                "runtime_release_digest": runtime_release,
                                "path": f"artifacts/{artifact_digest[7:]}.project",
                                "status": "READY_FOR_DEPLOY",
                            }
                        ],
                    }
                )
            )
            original_job_id = str(uuid.uuid4())
            reconciliation_job_id = str(uuid.uuid4())
            authority_path = v2 / "plans" / f"{original_job_id}.authority.json"
            authority_raw = canonical_json_bytes({"authority": "bound"})
            authority_path.write_bytes(authority_raw)
            authority_digest = sha256_bytes(authority_raw)
            original_request, original_request_raw = _archived_request(
                v2,
                original_job_id,
                "deployment.execute",
                [
                    {
                        "role": "authorization",
                        "path": f"plans/{authority_path.name}",
                        "sha256": authority_digest,
                    }
                ],
                authority_digest,
            )
            evidence_path = v2 / "evidence" / f"{original_job_id}.deployment.json"
            evidence_path.write_bytes(
                canonical_json_bytes(
                    _worker_evidence(
                        original_request,
                        "unknown_outcome",
                        artifact_digest,
                        True,
                    )
                )
            )
            result_path = v2 / "results" / f"{original_job_id}.result.json"
            result_path.write_bytes(
                canonical_json_bytes(
                    _terminal_result(
                        original_request,
                        original_request_raw,
                        "unknown_outcome",
                        [
                            {
                                "role": "evidence",
                                "path": f"evidence/{evidence_path.name}",
                                "sha256": sha256_file(evidence_path),
                            }
                        ],
                    )
                )
            )
            marker_path = v2 / "state" / "side-effects" / f"{original_job_id}.started.json"
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            marker_path.write_bytes(
                canonical_json_bytes(
                    {
                        "schema_version": "1",
                        "job_id": original_job_id,
                        "worker_id": str(uuid.uuid4()),
                        "operation": "deployment.execute",
                        "environment_digest": ENVIRONMENT,
                        "started_at": _stamp(),
                    }
                )
            )
            journal_dir = v2 / "state" / "deployments" / original_job_id
            journal_dir.mkdir(parents=True)
            journal_path = journal_dir / "0001-candidate-full-download-intent.json"
            journal_path.write_bytes(
                canonical_json_bytes(
                    {
                        "schema_version": "1",
                        "record_type": "DeploymentJournalEvent",
                        "sequence": 1,
                        "job_id": original_job_id,
                        "event": "candidate-full-download-intent",
                        "created_at": _stamp(),
                        "details": {"reconciliation_required": True},
                    }
                )
            )
            reconciliation_result_path = (
                v2 / "results" / f"{reconciliation_job_id}.result.json"
            )
            reconciliation_input_path = (
                v2 / "inputs" / f"{reconciliation_job_id}.input.json"
            )
            reconciliation_input_raw = canonical_json_bytes(
                {
                    "schema_version": "1",
                    "record_type": "DeploymentReconcileInput",
                    "original_job_id": original_job_id,
                    "target_fingerprint": TARGET,
                    "artifact_digest": artifact_digest,
                    "rollback_digest": artifact_digest,
                }
            )
            reconciliation_input_path.write_bytes(reconciliation_input_raw)
            reconciliation_request, reconciliation_request_raw = _archived_request(
                v2,
                reconciliation_job_id,
                "deployment.reconcile",
                [
                    {
                        "role": "input",
                        "path": f"inputs/{reconciliation_input_path.name}",
                        "sha256": sha256_bytes(reconciliation_input_raw),
                    }
                ],
            )
            reconciliation_evidence_path = (
                v2 / "evidence" / f"{reconciliation_job_id}.deployment.json"
            )
            reconciliation_evidence_path.write_bytes(
                canonical_json_bytes(
                    _worker_evidence(
                        reconciliation_request,
                        "succeeded",
                        artifact_digest,
                        False,
                    )
                )
            )
            valid_reconciliation_result = _terminal_result(
                reconciliation_request,
                reconciliation_request_raw,
                "succeeded",
                [
                    {
                        "role": "evidence",
                        "path": f"evidence/{reconciliation_evidence_path.name}",
                        "sha256": sha256_file(reconciliation_evidence_path),
                    }
                ],
            )
            reconciliation_result_path.write_bytes(
                canonical_json_bytes(
                    {
                        "job_id": reconciliation_job_id,
                        "operation": "deployment.reconcile",
                        "status": "succeeded",
                        "environment_digest": ENVIRONMENT,
                    }
                )
            )
            record = {
                "schema_version": "1",
                "record_type": "DeploymentReconciliation",
                "original_job_id": original_job_id,
                "reconciliation_job_id": reconciliation_job_id,
                "created_at": _stamp(),
                "target_fingerprint": TARGET,
                "artifact_digest": artifact_digest,
                "rollback_digest": artifact_digest,
                "runtime_release_digest": runtime_release,
                "rollback_runtime_release_digest": runtime_release,
                "original_result_digest": sha256_file(result_path),
                "side_effect_marker_digest": sha256_file(marker_path),
                "provenance_head_kind": "journal",
                "journal_head_digest": sha256_file(journal_path),
                "outcome": "succeeded",
                "reconciliation_required": False,
                "observed": {"application_state": "run"},
            }
            record_path = (
                v2
                / "state"
                / "reconciliations"
                / f"{original_job_id}.{reconciliation_job_id}.json"
            )
            record_path.write_bytes(canonical_json_bytes(record))
            supervisor = AxDeploymentSupervisor(root)
            with self.assertRaisesRegex(ProtocolViolation, "semantic mismatch"):
                supervisor._terminal_reconciliation_exists(original_job_id, result_path)

            record["observed"] = {
                "target_fingerprint": TARGET,
                "runtime_release_digest": runtime_release,
                "application_state": "run",
                "forced_values_present": False,
                "physical_outputs_energized": False,
            }
            record_path.write_bytes(canonical_json_bytes(record))
            with self.assertRaisesRegex(ProtocolViolation, "result chain"):
                supervisor._terminal_reconciliation_exists(original_job_id, result_path)
            reconciliation_result_path.write_bytes(
                canonical_json_bytes(valid_reconciliation_result)
            )
            self.assertTrue(
                supervisor._terminal_reconciliation_exists(original_job_id, result_path)
            )
            record["provenance_head_kind"] = "launch_uncertainty"
            record_path.write_bytes(canonical_json_bytes(record))
            with self.assertRaisesRegex(ProtocolViolation, "semantic mismatch"):
                supervisor._terminal_reconciliation_exists(original_job_id, result_path)

            pre_intent_path = journal_path.with_name("0001-authority-claimed.json")
            journal_path.replace(pre_intent_path)
            pre_intent_path.write_bytes(
                canonical_json_bytes(
                    {
                        "schema_version": "1",
                        "record_type": "DeploymentJournalEvent",
                        "sequence": 1,
                        "job_id": original_job_id,
                        "event": "authority-claimed",
                        "created_at": _stamp(),
                        "details": {
                            "authority_digest": authority_digest,
                            "reconciliation_required": False,
                        },
                    }
                )
            )
            launch_uncertainty_path = (
                v2
                / "state"
                / "deployment-launch-uncertainty"
                / f"{original_job_id}.json"
            )
            launch_uncertainty_path.parent.mkdir(parents=True, exist_ok=True)
            launch_uncertainty_path.write_bytes(
                canonical_json_bytes(
                    {
                        "schema_version": "1",
                        "record_type": "DeploymentLaunchUncertainty",
                        "created_at": _stamp(),
                        "correlation_id": original_request["correlation_id"],
                        "job_id": original_job_id,
                        "operation": "deployment.execute",
                        "environment_digest": ENVIRONMENT,
                        "target_fingerprint": TARGET,
                        "request_digest": sha256_bytes(original_request_raw),
                        "launch_state": "unknown",
                    }
                )
            )
            record["provenance_head_kind"] = "pre_intent_journal"
            record["journal_head_digest"] = sha256_bytes(
                canonical_json_bytes(
                    {
                        "journal_head_digest": sha256_file(pre_intent_path),
                        "launch_uncertainty_digest": sha256_file(
                            launch_uncertainty_path
                        ),
                    }
                )
            )
            record_path.write_bytes(canonical_json_bytes(record))
            self.assertTrue(
                supervisor._terminal_reconciliation_exists(original_job_id, result_path)
            )


if __name__ == "__main__":
    unittest.main()
