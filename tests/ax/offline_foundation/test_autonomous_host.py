from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from mcp import Client
from jsonschema import Draft202012Validator, FormatChecker

from axmcp_offline.autonomous import (
    AutonomousHost,
    configure_autonomous_environment,
    enroll_baseline,
    initialize_autonomous_root,
)
from axmcp_offline.constants import PACKAGE_VERSION, PUBLIC_OPERATIONS
from axmcp_offline.jobs import FileJobQueue, WorkerLease
from axmcp_offline.mcp_server import create_server
from axmcp_offline.protocol_v2 import (
    load_v2,
    make_request_v2,
    request_v2_bytes,
    validate_arguments,
    validate_request_v2,
)
from axmcp_offline.security import (
    ProtocolViolation,
    canonical_json_bytes,
    sha256_bytes,
    sha256_file,
    strict_json_loads_v2,
    utc_now,
)
from axmcp_offline.service import OfflineService

from . import REPO_ROOT

try:
    import axmcp_offline.ladder  # noqa: F401

    HAS_CANONICAL_LADDER = True
except ModuleNotFoundError:
    HAS_CANONICAL_LADDER = False


ENVIRONMENT_DIGEST = "sha256:" + ("1" * 64)
TARGET_FINGERPRINT = "sha256:" + ("2" * 64)


class _DeploymentSupervisor:
    def __init__(self, launch_state: str = "started") -> None:
        self.launch_state = launch_state
        self.raise_error = False
        self.echo_target_fingerprint: str | None = None
        self.on_ensure = None
        self.on_recover = None
        self.calls: list[tuple[str, str, str]] = []

    def ensure_running(
        self, job_id: str, operation: str, target_fingerprint: str
    ) -> dict[str, object]:
        self.calls.append((job_id, operation, target_fingerprint))
        if self.on_ensure is not None:
            self.on_ensure()
        if self.raise_error:
            raise ProtocolViolation("synthetic deployment launcher failure")
        return {
            "launch_state": self.launch_state,
            "job_id": job_id,
            "operation": operation,
            "target_fingerprint": self.echo_target_fingerprint or target_fingerprint,
        }

    def recover_inactive(self) -> dict[str, int]:
        if self.on_recover is not None:
            return self.on_recover()
        return {
            "partial_rejected": 0,
            "corrupt_rejected": 0,
            "stale_rejected": 0,
            "requeued": 0,
            "blocked_for_reconciliation": 0,
            "unknown_outcome": 0,
        }


class _WorkspaceRecoverySupervisor:
    def __init__(self, actual_project_digest: str) -> None:
        self.actual_project_digest = actual_project_digest
        self.calls: list[tuple[str, str, str]] = []

    def recover_workspace_job(
        self, workspace_id: str, job_id: str, expected_project_digest: str
    ) -> dict[str, str]:
        self.calls.append((workspace_id, job_id, expected_project_digest))
        return {
            "status": "unknown_outcome",
            "job_id": job_id,
            "actual_project_digest": self.actual_project_digest,
        }

def _timestamp(offset_seconds: int = 0) -> str:
    return (datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=offset_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _activate_root(
    root: Path,
    with_target: bool = True,
    with_deployment_supervisor: bool = True,
) -> AutonomousHost:
    initialize_autonomous_root(root)
    config_path = root / "autonomous-config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["environment_digest"] = ENVIRONMENT_DIGEST
    config["target_allowlist"] = (
        [{"label": "isolated-ax308", "fingerprint": TARGET_FINGERPRINT}]
        if with_target
        else []
    )
    config["observation_expression_allowlist"] = [
        "Supervisor.State",
        "Supervisor.FaultCode",
    ]
    config_path.write_bytes(canonical_json_bytes(config))

    probe_path = root / "probe-config.json"
    probe = json.loads(probe_path.read_text(encoding="utf-8"))
    probe["environment_digest"] = ENVIRONMENT_DIGEST
    probe_path.write_text(json.dumps(probe, indent=2) + "\n", encoding="utf-8")
    deployment_supervisor = (
        _DeploymentSupervisor() if with_deployment_supervisor else None
    )
    return AutonomousHost(
        root,
        legacy_timeout_seconds=1,
        deployment_supervisor=deployment_supervisor,
    )


def _request(
    root: Path,
    *,
    operation: str = "environment.probe",
    job_id: str | None = None,
    correlation_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, object]:
    return make_request_v2(
        correlation_id=correlation_id or str(uuid.uuid4()),
        job_id=job_id or str(uuid.uuid4()),
        operation=operation,
        created_at=created_at or utc_now(),
        timeout_seconds=30,
        environment_digest=ENVIRONMENT_DIGEST,
        workspace_digest=None,
        expected_revision=None,
        authorization_digest=None,
        artifacts=[],
        scratch_root=root,
    )


def _result(
    request: dict[str, object],
    raw: bytes,
    *,
    status: str = "succeeded",
    revision: int | None = None,
    before_digest: str | None = None,
    after_digest: str | None = None,
    artifacts: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    return {
        "protocol": "AXQ-FILE/2",
        "message_type": "result",
        "correlation_id": request["correlation_id"],
        "job_id": request["job_id"],
        "operation": request["operation"],
        "created_at": utc_now(),
        "status": status,
        "environment_digest": request["environment_digest"],
        "workspace_digest": request["workspace_digest"],
        "revision": request["expected_revision"] if revision is None else revision,
        "before_project_digest": before_digest,
        "after_project_digest": after_digest,
        "request_digest": sha256_bytes(raw),
        "artifacts": artifacts or [],
        "diagnostics": [],
    }


def _worker_evidence_descriptor(
    root: Path,
    request: dict[str, object],
    *,
    outcome: str,
    target_fingerprint: str,
    candidate_digest: str,
    rollback_digest: str,
    reconciliation_required: bool,
) -> dict[str, str]:
    evidence = {
        "schema_version": "1",
        "record_type": "AxDeploymentWorkerEvidence",
        "created_at": utc_now(),
        "correlation_id": request["correlation_id"],
        "job_id": request["job_id"],
        "operation": request["operation"],
        "outcome": outcome,
        "target_fingerprint": target_fingerprint,
        "authority_digest": request["authorization_digest"],
        "candidate_digest": candidate_digest,
        "rollback_digest": rollback_digest,
        "reconciliation_required": reconciliation_required,
        "sample_count": 0,
        "diagnostics": [],
    }
    relative = f"evidence/{request['job_id']}.ax-deployment.json"
    raw = canonical_json_bytes(evidence)
    (root / relative).write_bytes(raw)
    return {"role": "evidence", "path": relative, "sha256": sha256_bytes(raw)}


def _profile() -> dict[str, object]:
    return {
        "schema_version": "1",
        "record_type": "ObservationProfile",
        "profile_id": "isolated.ax308.default",
        "expressions": ["Supervisor.State", "Supervisor.FaultCode"],
        "interval_ms": 500,
        "duration_seconds": 60,
        "fault_invariants": [
            {
                "expression": "Supervisor.FaultCode",
                "operator": "eq",
                "expected": 0,
                "action": "record",
            }
        ],
    }


def _validate_if_integrated(instance: dict[str, object], schema_name: str) -> None:
    schema_path = (
        REPO_ROOT
        / "backends" / "ax"
        / "contracts"
        / "v2"
        / schema_name
    )
    if schema_path.is_file():
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(instance)


class ProtocolAndQueueTests(unittest.TestCase):
    def test_order_only_submit_crash_is_idempotently_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = FileJobQueue(root)
            request = _request(root)
            raw = request_v2_bytes(request, root)
            order = {
                "schema_version": "1",
                "sequence": 1,
                "correlation_id": request["correlation_id"],
                "job_id": request["job_id"],
                "request_digest": sha256_bytes(raw),
            }
            order_path = (
                root
                / "state"
                / "order"
                / f"{1:020d}_{request['correlation_id']}.order.json"
            )
            order_path.write_bytes(canonical_json_bytes(order))
            with self.assertRaises(ProtocolViolation):
                queue.status(request["job_id"])
            resumed = queue.submit(request)
            self.assertEqual(resumed["job_id"], request["job_id"])
            self.assertEqual(resumed["queue_sequence"], 1)
            self.assertEqual(queue.status(request["job_id"])["state"], "queued")
            self.assertEqual(len(list((root / "state" / "order").glob("*.json"))), 1)

    def test_v2_rejects_duplicate_float_unknown_traversal_and_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = FileJobQueue(root)
            valid = _request(root)
            validate_request_v2(valid, root)
            with self.assertRaises(ProtocolViolation):
                strict_json_loads_v2(b'{"a":1,"a":2}')
            self.assertEqual(strict_json_loads_v2(b'{"a":1.5}'), {"a": 1.5})
            with self.assertRaises(ProtocolViolation):
                strict_json_loads_v2(b'{"a":NaN}')

            unknown = dict(valid)
            unknown["unknown"] = "x"
            with self.assertRaises(ProtocolViolation):
                validate_request_v2(unknown, root)

            overlong = dict(valid)
            created = datetime.strptime(
                valid["created_at"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=UTC)
            overlong["deadline_at"] = (created + timedelta(seconds=301)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            with self.assertRaisesRegex(ProtocolViolation, "300-second"):
                validate_request_v2(overlong, root)

            traversal = make_request_v2(
                correlation_id=str(uuid.uuid4()),
                job_id=str(uuid.uuid4()),
                operation="workspace.create",
                created_at=utc_now(),
                timeout_seconds=30,
                environment_digest=ENVIRONMENT_DIGEST,
                workspace_digest=None,
                expected_revision=None,
                authorization_digest=None,
                artifacts=[],
                scratch_root=None,
            )
            traversal["artifacts"] = [
                {
                    "role": "input",
                    "path": "inputs/../escape.json",
                    "sha256": ENVIRONMENT_DIGEST,
                }
            ]
            with self.assertRaises(ProtocolViolation):
                validate_request_v2(traversal, root)

            stale = _request(root, created_at="2026-01-01T00:00:00Z")
            with self.assertRaises(ProtocolViolation):
                queue.submit(stale)

    def test_twenty_jobs_are_lossless_ordered_and_correlated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = FileJobQueue(root)
            submitted = [queue.submit(_request(root)) for _ in range(20)]
            self.assertEqual([item["queue_sequence"] for item in submitted], list(range(1, 21)))
            claimed: list[str] = []
            while True:
                item = queue.claim_next()
                if item is None:
                    break
                request, raw = item
                claimed.append(request["job_id"])
                queue.finish(_result(request, raw))
            self.assertEqual(claimed, [item["job_id"] for item in submitted])
            self.assertTrue(all(queue.status(job_id)["state"] == "succeeded" for job_id in claimed))

    def test_duplicate_partial_corrupt_and_hung_recovery_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = FileJobQueue(root)
            request = _request(root)
            queue.submit(request)
            with self.assertRaises(ProtocolViolation):
                queue.submit(request)
            duplicate_correlation = _request(root, correlation_id=request["correlation_id"])
            with self.assertRaises(ProtocolViolation):
                queue.submit(duplicate_correlation)

            partial = root / "inbox" / "dangling.request.json.partial"
            partial.write_text("{", encoding="utf-8")
            corrupt = root / "inbox" / f"99999999999999999998_{uuid.uuid4()}.request.json"
            corrupt.write_text("{not-json", encoding="utf-8")
            running = queue.claim_next()
            self.assertIsNotNone(running)
            recovery = queue.recover()
            self.assertEqual(recovery["partial_rejected"], 1)
            self.assertEqual(recovery["corrupt_rejected"], 1)
            self.assertEqual(recovery["requeued"], 1)
            self.assertEqual(queue.status(request["job_id"])["state"], "queued")

    def test_cancel_queued_and_running_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = FileJobQueue(root)
            queued = _request(root)
            queue.submit(queued)
            self.assertEqual(queue.cancel(queued["job_id"])["state"], "cancelled")

            running = _request(root)
            queue.submit(running)
            queue.claim_next()
            status = queue.cancel(running["job_id"])
            self.assertEqual(status["state"], "running")
            self.assertTrue(status["cancel_requested"])

    def test_cancel_intent_crash_windows_never_requeue_the_request(self) -> None:
        for crash_after_rename in (False, True):
            with self.subTest(crash_after_rename=crash_after_rename):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    queue = FileJobQueue(root)
                    request = _request(root)
                    request_raw = request_v2_bytes(request, root)
                    queue.submit(request)
                    if crash_after_rename:
                        with patch.object(
                            queue,
                            "finish",
                            side_effect=RuntimeError("synthetic result publish power loss"),
                        ):
                            with self.assertRaisesRegex(RuntimeError, "power loss"):
                                queue.cancel(request["job_id"])
                    else:
                        original_replace = os.replace

                        def crash_before_claim(source, destination):
                            if Path(source).parent.name == "inbox":
                                raise RuntimeError("synthetic pre-claim power loss")
                            return original_replace(source, destination)

                        with patch(
                            "axmcp_offline.jobs.os.replace",
                            side_effect=crash_before_claim,
                        ):
                            with self.assertRaisesRegex(RuntimeError, "power loss"):
                                queue.cancel(request["job_id"])
                    recovered = queue.recover()
                    self.assertEqual(recovered["cancelled"], 1)
                    self.assertEqual(recovered["requeued"], 0)
                    result = queue.status(request["job_id"])["result"]
                    self.assertEqual(result["status"], "cancelled")
                    self.assertEqual(result["request_digest"], sha256_bytes(request_raw))
                    self.assertIsNone(queue.claim_next())

    def test_worker_lease_rejects_race_and_adopts_only_stale_dead_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = str(uuid.uuid4())
            second = str(uuid.uuid4())
            process_a = "sha256:" + ("a" * 64)
            process_b = "sha256:" + ("b" * 64)
            lease = WorkerLease(root, stale_seconds=5, pid_alive=lambda _pid: False)
            lease.acquire(first, 101, process_a, ENVIRONMENT_DIGEST, "2026-09-01T00:00:00Z")
            with self.assertRaises(ProtocolViolation):
                lease.acquire(
                    second, 202, process_b, ENVIRONMENT_DIGEST, "2026-09-01T00:00:04Z"
                )
            adopted = lease.acquire(
                second, 202, process_b, ENVIRONMENT_DIGEST, "2026-09-01T00:00:06Z"
            )
            self.assertEqual(adopted["worker_id"], second)
            lease.heartbeat(second, process_b, ENVIRONMENT_DIGEST, "2026-09-01T00:00:07Z")
            lease.release(second)
            self.assertFalse(lease.path.exists())

    def test_started_deployment_recovers_as_unknown_outcome_without_requeue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = FileJobQueue(root)
            authority_dir = root / "authorization"
            authority_dir.mkdir()
            authority = authority_dir / "one-shot.json"
            authority.write_bytes(b'{"authority":"fixture"}\n')
            authority_digest = sha256_file(authority)
            plan_id = str(uuid.uuid4())
            execution_input = {
                "schema_version": "1",
                "record_type": "DeploymentExecuteInput",
                "plan_id": plan_id,
                "plan_digest": "sha256:" + ("3" * 64),
                "artifact_digest": "sha256:" + ("4" * 64),
                "runtime_release_digest": "sha256:" + ("5" * 64),
                "target_fingerprint": TARGET_FINGERPRINT,
                "rollback_digest": "sha256:" + ("6" * 64),
                "rollback_runtime_release_digest": "sha256:" + ("7" * 64),
                "expires_at": _timestamp(300),
                "challenge_nonce": str(uuid.uuid4()),
                "observation_digest": "sha256:" + ("8" * 64),
                "authority_digest": authority_digest,
            }
            input_dir = root / "inputs"
            input_dir.mkdir()
            execution_path = input_dir / f"{plan_id}.deployment.json"
            execution_path.write_bytes(canonical_json_bytes(execution_input))
            request = make_request_v2(
                correlation_id=str(uuid.uuid4()),
                job_id=str(uuid.uuid4()),
                operation="deployment.execute",
                created_at=utc_now(),
                timeout_seconds=30,
                environment_digest=ENVIRONMENT_DIGEST,
                workspace_digest=None,
                expected_revision=None,
                authorization_digest=authority_digest,
                artifacts=[
                    {
                        "role": "input",
                        "path": f"inputs/{plan_id}.deployment.json",
                        "sha256": sha256_file(execution_path),
                    },
                    {
                        "role": "authorization",
                        "path": "authorization/one-shot.json",
                        "sha256": sha256_file(authority),
                    }
                ],
                scratch_root=root,
            )
            queue.submit(request)
            queue.claim_next()
            queue.mark_side_effect_started(
                request["job_id"], str(uuid.uuid4()), ENVIRONMENT_DIGEST
            )
            recovery = queue.recover()
            self.assertEqual(recovery["unknown_outcome"], 1)
            self.assertEqual(queue.status(request["job_id"])["state"], "unknown_outcome")
            self.assertIsNone(queue.claim_next())


class AutonomousSetupTests(unittest.TestCase):
    def test_environment_configs_are_synchronized_and_placeholder_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            initialize_autonomous_root(root)
            configured = configure_autonomous_environment(root, ENVIRONMENT_DIGEST)
            autonomous = strict_json_loads_v2(
                (root / "autonomous-config.json").read_bytes()
            )
            probe = strict_json_loads_v2((root / "probe-config.json").read_bytes())
            self.assertEqual(configured["environment_digest"], ENVIRONMENT_DIGEST)
            self.assertEqual(autonomous["environment_digest"], ENVIRONMENT_DIGEST)
            self.assertEqual(probe["environment_digest"], ENVIRONMENT_DIGEST)
            with self.assertRaises(ProtocolViolation):
                configure_autonomous_environment(root, "sha256:" + ("0" * 64))

    def test_baseline_enrollment_is_digest_locked_idempotent_and_non_destructive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"
            source = base / "SyntheticProject.project"
            source.write_bytes(b"AXMCP-BASELINE\n")
            digest = sha256_file(source)
            initialize_autonomous_root(root)

            first = enroll_baseline(root, source, digest)
            second = enroll_baseline(root, source, digest)
            enrolled = root / "v2" / first["baseline_path"]
            self.assertEqual(first, second)
            self.assertEqual(sha256_file(enrolled), digest)
            self.assertEqual(source.read_bytes(), b"AXMCP-BASELINE\n")

            source.write_bytes(b"CHANGED\n")
            with self.assertRaises(ProtocolViolation):
                enroll_baseline(root, source, digest)
            source.write_bytes(b"AXMCP-BASELINE\n")
            enrolled.write_bytes(b"TAMPERED\n")
            with self.assertRaises(ProtocolViolation):
                enroll_baseline(root, source, digest)

    def test_baseline_symlink_is_rejected_when_platform_supports_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"
            source = base / "source.project"
            link = base / "linked.project"
            source.write_bytes(b"AXMCP-BASELINE\n")
            initialize_autonomous_root(root)
            try:
                link.symlink_to(source)
            except OSError:
                self.skipTest("symlink creation is unavailable on this Windows host")
            with self.assertRaises(ProtocolViolation):
                enroll_baseline(root, link, sha256_file(source))


class AutonomousHostTests(unittest.TestCase):
    def test_gvl_requires_empty_implementation_and_program_roundtrip_may_be_empty(self) -> None:
        base = {
            "workspace_id": str(uuid.uuid4()),
            "expected_revision": 0,
            "pou_name": "CompileProbe",
            "pou_kind": "gvl",
            "return_type": None,
            "declaration": "VAR_GLOBAL\n    Counter : DINT;\nEND_VAR",
            "implementation": "",
        }
        validate_arguments("source.upsert.st", base, Path("."))
        with self.assertRaisesRegex(ProtocolViolation, "GVL implementation must be empty"):
            validate_arguments(
                "source.upsert.st", {**base, "implementation": "Counter := 1;"}, Path(".")
            )
        validate_arguments(
            "source.upsert.st", {**base, "pou_kind": "program"}, Path(".")
        )

    def _workspace(self, host: AutonomousHost) -> tuple[str, str]:
        baseline = host.v2 / "baselines" / "mini.project"
        baseline.write_bytes(b"AX-MINI-BASELINE\n")
        digest = sha256_file(baseline)
        created = host.workspace_create("baselines/mini.project", digest, ENVIRONMENT_DIGEST)
        return created["workspace_id"], digest

    def _complete_workspace_job(
        self,
        host: AutonomousHost,
        *,
        new_bytes: bytes | None = None,
        build_counts: tuple[int, int] | None = None,
        status: str = "succeeded",
    ) -> dict[str, object]:
        claimed = host.queue.claim_next()
        self.assertIsNotNone(claimed)
        request, raw = claimed
        input_descriptor = next(
            artifact for artifact in request["artifacts"] if artifact["path"].startswith("inputs/")
        )
        operation_input = strict_json_loads_v2(
            (host.v2 / input_descriptor["path"]).read_bytes()
        )
        workspace_id = operation_input["workspace_id"]
        before = request["workspace_digest"]
        project = host.v2 / "workspaces" / workspace_id / "project.project"
        if new_bytes is not None:
            project.write_bytes(new_bytes)
        after = sha256_file(project)
        revision = request["expected_revision"]
        if status == "succeeded" and request["operation"] in {
            "source.upsert.st",
            "ladder.networks.apply",
        }:
            revision += 1
        artifacts: list[dict[str, str]] = []
        if request["operation"] == "project.build" and status == "succeeded":
            error_count, warning_count = build_counts or (0, 0)
            build_result = {
                "schema_version": "1",
                "record_type": "BuildResult",
                "build_id": request["job_id"],
                "created_at": utc_now(),
                "workspace_id": workspace_id,
                "revision": revision,
                "kind": operation_input["mode"],
                "status": "passed" if error_count == 0 else "failed",
                "error_count": error_count,
                "warning_count": warning_count,
                "diagnostics": [],
                "project_digest": after,
                "artifact_digest": None,
                "runtime_release_digest": operation_input["runtime_release_digest"],
                "environment_digest": ENVIRONMENT_DIGEST,
            }
            output = host.v2 / "outputs" / f"{request['job_id']}.build-result.json"
            output.write_bytes(canonical_json_bytes(build_result))
            artifacts.append(
                {
                    "role": "output",
                    "path": f"outputs/{output.name}",
                    "sha256": sha256_file(output),
                }
            )
        result = _result(
            request,
            raw,
            status=status,
            revision=revision,
            before_digest=before,
            after_digest=after if status == "succeeded" else None,
            artifacts=artifacts,
        )
        host.ingest_worker_result(result)
        return result

    def _qualified_artifact(self, host: AutonomousHost) -> tuple[str, dict[str, object]]:
        workspace_id, _ = self._workspace(host)
        queued = host.source_upsert_st(
            workspace_id,
            0,
            "McpTestPou",
            "program",
            "PROGRAM McpTestPou",
            "McpCounter := McpCounter + 1;",
        )
        self.assertEqual(queued["state"], "queued")
        self._complete_workspace_job(host, new_bytes=b"AX-MINI-WITH-ST\n")
        build = host.project_build(workspace_id, 1, "rebuild", 10)
        self.assertEqual(build["revision"], 1)
        self._complete_workspace_job(host, build_counts=(0, 0))
        artifact = host.artifact_freeze(workspace_id, 1)
        return workspace_id, artifact

    def test_workspace_digest_revision_mutation_build_and_freeze(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host = _activate_root(Path(tmp))
            workspace_id, artifact = self._qualified_artifact(host)
            status = host.workspace_status(workspace_id)
            self.assertEqual(status["revision"], 1)
            self.assertFalse(status["dirty"])
            self.assertEqual(artifact["status"], "READY_FOR_DEPLOY")
            self.assertEqual(sha256_file(host.v2 / artifact["path"]), artifact["artifact_digest"])
            with self.assertRaises(ProtocolViolation):
                host.source_upsert_st(
                    workspace_id,
                    0,
                    "WrongRevision",
                    "program",
                    "PROGRAM WrongRevision",
                    "x := TRUE;",
                )

    def test_workspace_source_read_returns_verified_output_without_revision_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host = _activate_root(Path(tmp))
            workspace_id, _ = self._workspace(host)
            queued = host.source_read(workspace_id, 0, "PLC_PRG")
            claimed = host.queue.claim_next()
            self.assertIsNotNone(claimed)
            request, raw = claimed
            payload = {
                "name": "PLC_PRG",
                "declaration": "PROGRAM PLC_PRG\nVAR\n    Counter : DINT;\nEND_VAR",
                "implementation": "Counter := Counter + 1;",
            }
            payload["source_digest"] = sha256_bytes(canonical_json_bytes(payload))
            payload["real_target_contacted"] = False
            output = host.v2 / "outputs" / f"{request['correlation_id']}.payload.json"
            output.write_bytes(canonical_json_bytes(payload))
            project = host.v2 / "workspaces" / workspace_id / "project.project"
            result = _result(
                request,
                raw,
                status="succeeded",
                revision=0,
                before_digest=sha256_file(project),
                after_digest=sha256_file(project),
                artifacts=[
                    {
                        "role": "output",
                        "path": f"outputs/{output.name}",
                        "sha256": sha256_file(output),
                    }
                ],
            )
            host.ingest_worker_result(result)

            status = host.job_status(queued["job_id"])

            self.assertEqual(status["status"], "succeeded")
            self.assertEqual(status["output"], payload)
            self.assertEqual(host.workspace_status(workspace_id)["revision"], 0)

    def test_dead_workspace_worker_with_changed_project_terminalizes_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host = _activate_root(Path(tmp))
            workspace_id, _ = self._workspace(host)
            queued = host.source_upsert_st(
                workspace_id,
                0,
                "CrashProbe",
                "program",
                "PROGRAM CrashProbe",
                "CrashProbe := CrashProbe;",
            )
            self.assertIsNotNone(host.queue.claim_next())
            project = host.v2 / "workspaces" / workspace_id / "project.project"
            project.write_bytes(b"CHANGED-BEFORE-WORKER-DEATH")
            supervisor = _WorkspaceRecoverySupervisor(sha256_file(project))
            host.supervisor = supervisor  # type: ignore[assignment]

            status = host.job_status(queued["job_id"])

            self.assertEqual(status["status"], "unknown_outcome")
            self.assertTrue(status["unknown_outcome"])
            self.assertIn("automatic replay was refused", status["diagnostics"][0])
            self.assertIsNone(host._load_workspace(workspace_id)["pending_job_id"])
            self.assertEqual(len(supervisor.calls), 1)

    def test_exhausted_workspace_launch_terminalizes_after_inbox_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host = _activate_root(Path(tmp))
            workspace_id, _ = self._workspace(host)
            queued = host.source_upsert_st(
                workspace_id,
                0,
                "LaunchFailureProbe",
                "program",
                "PROGRAM LaunchFailureProbe",
                "LaunchFailureProbe := LaunchFailureProbe;",
            )
            project = host.v2 / "workspaces" / workspace_id / "project.project"

            class ExhaustedLaunchSupervisor:
                def recover_workspace_job(
                    self,
                    _workspace_id: str,
                    job_id: str,
                    _expected_project_digest: str,
                ) -> dict[str, str]:
                    inbox = host.queue._matching_request(job_id, "inbox")
                    assert inbox is not None
                    os.replace(inbox, host.v2 / "running" / inbox.name)
                    return {
                        "status": "blocked",
                        "job_id": job_id,
                        "actual_project_digest": sha256_file(project),
                        "diagnostic": "AX worker failed to start in two bounded attempts",
                    }

            host.supervisor = ExhaustedLaunchSupervisor()  # type: ignore[assignment]

            status = host.job_status(queued["job_id"])

            self.assertEqual(status["status"], "blocked")
            self.assertFalse(status["unknown_outcome"])
            self.assertIn("failed to start in two bounded attempts", status["diagnostics"][0])
            self.assertTrue(
                (host.v2 / "results" / f"{queued['job_id']}.result.json").is_file()
            )

    def test_job_request_load_re_resolves_atomic_inbox_to_running_move(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host = _activate_root(Path(tmp))
            workspace_id, _digest = self._workspace(host)
            queued = host.source_upsert_st(
                workspace_id,
                0,
                "AtomicMoveProbe",
                "program",
                "PROGRAM AtomicMoveProbe",
                "Counter := Counter + 1;",
            )
            original = host.queue._matching_request
            moved = False

            def race(job_id: str, directory: str | None = None) -> Path | None:
                nonlocal moved
                path = original(job_id, directory)
                if not moved and directory is None and path is not None:
                    moved = True
                    os.replace(path, host.v2 / "running" / path.name)
                    return path
                return original(job_id, directory)

            host.queue._matching_request = race  # type: ignore[method-assign]
            request = host._load_job_request_after_atomic_move(queued["job_id"])
            self.assertIsNotNone(request)
            self.assertEqual(request["job_id"], queued["job_id"])
            self.assertEqual(request["operation"], "source.upsert.st")

    def test_workspace_path_pending_cancel_and_out_of_protocol_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host = _activate_root(Path(tmp))
            baseline = host.v2 / "baselines" / "mini.project"
            baseline.write_bytes(b"BASE")
            digest = sha256_file(baseline)
            with self.assertRaises(ProtocolViolation):
                host.workspace_create("../mini.project", digest, ENVIRONMENT_DIGEST)
            workspace_id, _ = self._workspace(host)
            queued = host.source_upsert_st(
                workspace_id,
                0,
                "PendingPou",
                "program",
                "PROGRAM PendingPou",
                "x := TRUE;",
            )
            with self.assertRaises(ProtocolViolation):
                host.project_build(workspace_id, 0)
            cancelled = host.job_cancel(queued["job_id"])
            self.assertEqual(cancelled["status"], "cancelled")
            self.assertIsNone(host._load_workspace(workspace_id)["pending_job_id"])
            project = host.v2 / "workspaces" / workspace_id / "project.project"
            project.write_bytes(b"OUT-OF-PROTOCOL")
            with self.assertRaises(ProtocolViolation):
                host.project_build(workspace_id, 0)

    @unittest.skipUnless(HAS_CANONICAL_LADDER, "parallel canonical ladder commit not integrated")
    def test_ladder_recipe_is_strict_and_project_specific(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host = _activate_root(Path(tmp))
            workspace_id, _ = self._workspace(host)
            recipe = {
                "schema_version": "2",
                "record_type": "LadderRecipe",
                "output_allowlist": ["RestartRequired"],
                "project": "SyntheticProject",
                "pou": "MachineSupervisor",
                "variables": [
                    {
                        "name": "CmdStart",
                        "type": "BOOL",
                        "direction": "input",
                        "comment": "Momentary operator start request.",
                    },
                    {
                        "name": "SafetyPermit",
                        "type": "BOOL",
                        "direction": "input",
                        "comment": "True while hardwired safety feedback permits motion.",
                    },
                    {
                        "name": "PhaseHealthy",
                        "type": "BOOL",
                        "direction": "input",
                        "comment": "True while monitored power is healthy.",
                    },
                    {
                        "name": "RestartRequired",
                        "type": "BOOL",
                        "direction": "output",
                        "comment": "Blocks automatic restart after a permit loss.",
                    },
                ],
                "networks": [
                    {
                        "id": "MSUP-RESTART",
                        "order": 1,
                        "title": "Restart latch",
                        "comment": "Require a deliberate restart unless start and permits are present.",
                        "elements": [
                            {"kind": "contact", "variable": "CmdStart", "polarity": "NC"},
                            {
                                "kind": "branch",
                                "paths": [
                                    [
                                        {
                                            "kind": "contact",
                                            "variable": "SafetyPermit",
                                            "polarity": "NC",
                                        }
                                    ],
                                    [
                                        {
                                            "kind": "contact",
                                            "variable": "PhaseHealthy",
                                            "polarity": "NC",
                                        }
                                    ],
                                ],
                            },
                        ],
                        "coil": {"variable": "RestartRequired", "mode": "normal"},
                    }
                ],
            }
            queued = host.ladder_networks_apply(workspace_id, 0, recipe)
            self.assertEqual(queued["operation"], "ladder.networks.apply")
            bad = dict(recipe)
            bad["unknown"] = True
            with self.assertRaises(ProtocolViolation):
                host.ladder_networks_apply(workspace_id, 0, bad)

    def test_deployment_is_fully_bound_single_use_and_unknown_outcome_has_no_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host = _activate_root(Path(tmp))
            _, artifact = self._qualified_artifact(host)
            digest = artifact["artifact_digest"]
            plan = host.deployment_prepare(digest, TARGET_FINGERPRINT, digest, _profile())
            stored_plan = strict_json_loads_v2(
                (host.v2 / "plans" / f"{plan['plan_id']}.plan.json").read_bytes()
            )
            _validate_if_integrated(stored_plan, "deployment_plan.schema.json")
            execute_args = (
                plan["plan_id"],
                plan["artifact_digest"],
                plan["target_fingerprint"],
                plan["rollback_digest"],
                plan["expires_at"],
                plan["challenge_nonce"],
                "yükle",
            )
            wrong = list(execute_args)
            wrong[5] = str(uuid.uuid4())
            with self.assertRaises(ProtocolViolation):
                host.deployment_execute(*wrong)
            wrong = list(execute_args)
            wrong[6] = "deploy"
            with self.assertRaises(ProtocolViolation):
                host.deployment_execute(*wrong)

            queued = host.deployment_execute(*execute_args)
            self.assertTrue(queued["authorization_consumed"])
            self.assertFalse(queued["blind_retry_allowed"])
            self.assertEqual(
                host.deployment_supervisor.calls,
                [(queued["job_id"], "deployment.execute", TARGET_FINGERPRINT)],
            )
            duplicate = host.deployment_execute(*execute_args)
            self.assertEqual(duplicate["job_id"], queued["job_id"])
            self.assertTrue(duplicate["duplicate_suppressed"])
            self.assertEqual(duplicate["worker_launch"]["launch_state"], "started")

            claimed = host.queue.claim_next()
            self.assertIsNotNone(claimed)
            request, raw = claimed
            authority_artifacts = [
                item for item in request["artifacts"] if item["role"] == "authorization"
            ]
            self.assertEqual(len(authority_artifacts), 1)
            authority = strict_json_loads_v2(
                (host.v2 / authority_artifacts[0]["path"]).read_bytes()
            )
            _validate_if_integrated(authority, "deployment_authority.schema.json")
            _validate_if_integrated(request, "axq_request.schema.json")
            self.assertEqual(authority["authority_digest"], request["authorization_digest"])
            self.assertEqual(
                authority["phrase_digest"],
                "sha256:d254948f417a488968d144e12a6b87dfdeafb5582dbe86fa807dac952341088c",
            )
            evidence_descriptor = _worker_evidence_descriptor(
                host.v2,
                request,
                outcome="unknown_outcome",
                target_fingerprint=TARGET_FINGERPRINT,
                candidate_digest=digest,
                rollback_digest=digest,
                reconciliation_required=True,
            )
            unknown = _result(
                request,
                raw,
                status="unknown_outcome",
                artifacts=[evidence_descriptor],
            )
            _validate_if_integrated(unknown, "axq_result.schema.json")
            host.ingest_worker_result(unknown)
            self.assertEqual(host.job_status(queued["job_id"])["status"], "unknown_outcome")
            with self.assertRaises(ProtocolViolation):
                host.deployment_execute(*execute_args)

            side_effect_path = (
                host.v2 / "state" / "side-effects" / f"{queued['job_id']}.started.json"
            )
            side_effect_path.parent.mkdir(parents=True, exist_ok=True)
            side_effect_path.write_bytes(
                canonical_json_bytes(
                    {
                        "schema_version": "1",
                        "job_id": queued["job_id"],
                        "worker_id": str(uuid.uuid4()),
                        "operation": "deployment.execute",
                        "environment_digest": ENVIRONMENT_DIGEST,
                        "started_at": utc_now(),
                    }
                )
            )
            journal_dir = host.v2 / "state" / "deployments" / queued["job_id"]
            journal_dir.mkdir(parents=True, exist_ok=True)
            journal_path = journal_dir / "0001-candidate-full-download-intent.json"
            journal_path.write_bytes(
                canonical_json_bytes(
                    {
                        "schema_version": "1",
                        "record_type": "DeploymentJournalEvent",
                        "sequence": 1,
                        "job_id": queued["job_id"],
                        "event": "candidate-full-download-intent",
                        "created_at": utc_now(),
                        "details": {"reconciliation_required": True},
                    }
                )
            )

            with patch.object(
                host,
                "_publish_operation_input_idempotent",
                side_effect=ProtocolViolation("synthetic publish failure"),
            ):
                with self.assertRaisesRegex(ProtocolViolation, "synthetic publish failure"):
                    host.deployment_reconcile(
                        queued["job_id"], TARGET_FINGERPRINT, digest, digest
                    )
            self.assertFalse(
                any(
                    call[1] == "deployment.reconcile"
                    for call in host.deployment_supervisor.calls
                )
            )

            peer_supervisor = _DeploymentSupervisor()
            peer = AutonomousHost(
                host.root,
                legacy_timeout_seconds=1,
                deployment_supervisor=peer_supervisor,
            )
            barrier = threading.Barrier(3)
            concurrent_results: list[dict[str, object]] = []
            concurrent_errors: list[BaseException] = []

            def reconcile_from(instance: AutonomousHost) -> None:
                try:
                    barrier.wait()
                    concurrent_results.append(
                        instance.deployment_reconcile(
                            queued["job_id"], TARGET_FINGERPRINT, digest, digest
                        )
                    )
                except BaseException as exc:  # pragma: no cover - assertion reports it
                    concurrent_errors.append(exc)

            threads = [
                threading.Thread(target=reconcile_from, args=(instance,))
                for instance in (host, peer)
            ]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join()
            self.assertEqual(concurrent_errors, [])
            self.assertEqual(len(concurrent_results), 2)
            self.assertEqual(
                {item["job_id"] for item in concurrent_results},
                {concurrent_results[0]["job_id"]},
            )
            reconciliation = next(
                item for item in concurrent_results if not item.get("duplicate_suppressed")
            )
            duplicate = next(
                item for item in concurrent_results if item.get("duplicate_suppressed")
            )
            self.assertEqual(reconciliation["operation"], "deployment.reconcile")
            self.assertFalse(reconciliation["blind_retry_allowed"])
            self.assertEqual(duplicate["job_id"], reconciliation["job_id"])
            self.assertTrue(duplicate["duplicate_suppressed"])
            reconciliation_launches = [
                call
                for supervisor in (host.deployment_supervisor, peer_supervisor)
                for call in supervisor.calls
                if call[1] == "deployment.reconcile"
            ]
            self.assertTrue(reconciliation_launches)
            self.assertTrue(
                all(
                    call
                    == (
                        reconciliation["job_id"],
                        "deployment.reconcile",
                        TARGET_FINGERPRINT,
                    )
                    for call in reconciliation_launches
                )
            )
            claimed_reconciliation = host.queue.claim_next()
            self.assertIsNotNone(claimed_reconciliation)
            reconciliation_request, reconciliation_raw = claimed_reconciliation
            self.assertEqual(reconciliation_request["job_id"], reconciliation["job_id"])
            unresolved_reconciliation_evidence = _worker_evidence_descriptor(
                host.v2,
                reconciliation_request,
                outcome="unknown_outcome",
                target_fingerprint=TARGET_FINGERPRINT,
                candidate_digest=digest,
                rollback_digest=digest,
                reconciliation_required=True,
            )
            host.ingest_worker_result(
                _result(
                    reconciliation_request,
                    reconciliation_raw,
                    status="unknown_outcome",
                    artifacts=[unresolved_reconciliation_evidence],
                )
            )
            retry = host.deployment_reconcile(
                queued["job_id"], TARGET_FINGERPRINT, digest, digest
            )
            self.assertNotEqual(retry["job_id"], reconciliation["job_id"])
            reconciliation = retry
            claimed_reconciliation = host.queue.claim_next()
            self.assertIsNotNone(claimed_reconciliation)
            reconciliation_request, reconciliation_raw = claimed_reconciliation
            reconciliation_evidence = _worker_evidence_descriptor(
                host.v2,
                reconciliation_request,
                outcome="succeeded",
                target_fingerprint=TARGET_FINGERPRINT,
                candidate_digest=digest,
                rollback_digest=digest,
                reconciliation_required=False,
            )
            host.ingest_worker_result(
                _result(
                    reconciliation_request,
                    reconciliation_raw,
                    status="succeeded",
                    artifacts=[reconciliation_evidence],
                )
            )
            result_path = host.v2 / "results" / f"{queued['job_id']}.result.json"
            reconciliation_record = {
                "schema_version": "1",
                "record_type": "DeploymentReconciliation",
                "original_job_id": queued["job_id"],
                "reconciliation_job_id": reconciliation["job_id"],
                "created_at": utc_now(),
                "target_fingerprint": TARGET_FINGERPRINT,
                "artifact_digest": digest,
                "rollback_digest": digest,
                "runtime_release_digest": artifact["runtime_release_digest"],
                "rollback_runtime_release_digest": artifact["runtime_release_digest"],
                "original_result_digest": sha256_file(result_path),
                        "side_effect_marker_digest": sha256_file(side_effect_path),
                        "provenance_head_kind": "journal",
                        "journal_head_digest": sha256_file(journal_path),
                "outcome": "succeeded",
                "reconciliation_required": False,
                "observed": {
                    "target_fingerprint": TARGET_FINGERPRINT,
                    "runtime_release_digest": artifact["runtime_release_digest"],
                    "application_state": "run",
                    "forced_values_present": False,
                    "physical_outputs_energized": False,
                },
            }
            record_path = (
                host.v2
                / "state"
                / "reconciliations"
                / f"{queued['job_id']}.{reconciliation['job_id']}.json"
            )
            invalid_record = dict(reconciliation_record)
            invalid_record["observed"] = {"application_state": "run"}
            record_path.write_bytes(canonical_json_bytes(invalid_record))
            with self.assertRaisesRegex(
                ProtocolViolation, "corrupt reconciliation evidence"
            ):
                host.deployment_reconcile(
                    queued["job_id"], TARGET_FINGERPRINT, digest, digest
                )
            record_path.write_bytes(canonical_json_bytes(reconciliation_record))
            reconciliation_result_path = (
                host.v2
                / "results"
                / f"{reconciliation['job_id']}.result.json"
            )
            valid_reconciliation_result = reconciliation_result_path.read_bytes()
            reconciliation_result_path.write_bytes(
                canonical_json_bytes(
                    {
                        "job_id": reconciliation["job_id"],
                        "operation": "deployment.reconcile",
                        "status": "succeeded",
                        "environment_digest": ENVIRONMENT_DIGEST,
                    }
                )
            )
            with self.assertRaisesRegex(
                ProtocolViolation, "corrupt reconciliation evidence"
            ):
                host.deployment_reconcile(
                    queued["job_id"], TARGET_FINGERPRINT, digest, digest
                )
            reconciliation_result_path.write_bytes(valid_reconciliation_result)
            repeated = host.deployment_reconcile(
                queued["job_id"], TARGET_FINGERPRINT, digest, digest
            )
            self.assertTrue(repeated["already_resolved"])
            next_plan = host.deployment_prepare(
                digest, TARGET_FINGERPRINT, digest, _profile()
            )
            self.assertEqual(next_plan["state"], "prepared")

    def test_public_reconcile_restarts_same_job_to_finalize_prepublished_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host = _activate_root(Path(tmp))
            _, artifact = self._qualified_artifact(host)
            digest = artifact["artifact_digest"]
            plan = host.deployment_prepare(
                digest, TARGET_FINGERPRINT, digest, _profile()
            )
            queued = host.deployment_execute(
                plan["plan_id"],
                digest,
                TARGET_FINGERPRINT,
                digest,
                plan["expires_at"],
                plan["challenge_nonce"],
                "yükle",
            )
            original_claim = host.queue.claim_next()
            self.assertIsNotNone(original_claim)
            original_request, original_raw = original_claim
            original_evidence = _worker_evidence_descriptor(
                host.v2,
                original_request,
                outcome="unknown_outcome",
                target_fingerprint=TARGET_FINGERPRINT,
                candidate_digest=digest,
                rollback_digest=digest,
                reconciliation_required=True,
            )
            host.ingest_worker_result(
                _result(
                    original_request,
                    original_raw,
                    status="unknown_outcome",
                    artifacts=[original_evidence],
                )
            )
            marker_path = (
                host.v2 / "state" / "side-effects" / f"{queued['job_id']}.started.json"
            )
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            marker_path.write_bytes(
                canonical_json_bytes(
                    {
                        "schema_version": "1",
                        "job_id": queued["job_id"],
                        "worker_id": str(uuid.uuid4()),
                        "operation": "deployment.execute",
                        "environment_digest": ENVIRONMENT_DIGEST,
                        "started_at": utc_now(),
                    }
                )
            )
            journal_path = (
                host.v2
                / "state"
                / "deployments"
                / queued["job_id"]
                / "0001-candidate-full-download-intent.json"
            )
            journal_path.parent.mkdir(parents=True, exist_ok=True)
            journal_path.write_bytes(
                canonical_json_bytes(
                    {
                        "schema_version": "1",
                        "record_type": "DeploymentJournalEvent",
                        "sequence": 1,
                        "job_id": queued["job_id"],
                        "event": "candidate-full-download-intent",
                        "created_at": utc_now(),
                        "details": {"reconciliation_required": True},
                    }
                )
            )

            reconciliation = host.deployment_reconcile(
                queued["job_id"], TARGET_FINGERPRINT, digest, digest
            )
            reconciliation_claim = host.queue.claim_next()
            self.assertIsNotNone(reconciliation_claim)
            reconciliation_request, reconciliation_raw = reconciliation_claim
            reconciliation_evidence = _worker_evidence_descriptor(
                host.v2,
                reconciliation_request,
                outcome="succeeded",
                target_fingerprint=TARGET_FINGERPRINT,
                candidate_digest=digest,
                rollback_digest=digest,
                reconciliation_required=False,
            )
            reconciliation_result = _result(
                reconciliation_request,
                reconciliation_raw,
                status="succeeded",
                artifacts=[reconciliation_evidence],
            )
            result_path = (
                host.v2 / "results" / f"{reconciliation['job_id']}.result.json"
            )
            result_path.write_bytes(canonical_json_bytes(reconciliation_result))
            original_result_path = (
                host.v2 / "results" / f"{queued['job_id']}.result.json"
            )
            record = {
                "schema_version": "1",
                "record_type": "DeploymentReconciliation",
                "original_job_id": queued["job_id"],
                "reconciliation_job_id": reconciliation["job_id"],
                "created_at": utc_now(),
                "target_fingerprint": TARGET_FINGERPRINT,
                "artifact_digest": digest,
                "rollback_digest": digest,
                "runtime_release_digest": artifact["runtime_release_digest"],
                "rollback_runtime_release_digest": artifact[
                    "runtime_release_digest"
                ],
                "original_result_digest": sha256_file(original_result_path),
                "side_effect_marker_digest": sha256_file(marker_path),
                "provenance_head_kind": "journal",
                "journal_head_digest": sha256_file(journal_path),
                "outcome": "succeeded",
                "reconciliation_required": False,
                "observed": {
                    "target_fingerprint": TARGET_FINGERPRINT,
                    "runtime_release_digest": artifact["runtime_release_digest"],
                    "application_state": "run",
                    "forced_values_present": False,
                    "physical_outputs_energized": False,
                },
            }
            prepared_path = (
                host.v2
                / "state"
                / "reconciliation-prepared"
                / f"{queued['job_id']}.{reconciliation['job_id']}.prepared.json"
            )
            prepared_path.parent.mkdir(parents=True, exist_ok=True)
            prepared_path.write_bytes(canonical_json_bytes(record))
            host.deployment_supervisor.on_recover = lambda: host.queue.recover(
                operations={
                    "deployment.execute",
                    "deployment.reconcile",
                    "target.observe.start",
                    "target.observe.read",
                    "target.observe.stop",
                }
            )

            restarted = host.deployment_reconcile(
                queued["job_id"], TARGET_FINGERPRINT, digest, digest
            )
            self.assertEqual(restarted["job_id"], reconciliation["job_id"])
            self.assertTrue(restarted["result_pending_terminal"])
            self.assertEqual(
                host.deployment_supervisor.calls[-1],
                (
                    reconciliation["job_id"],
                    "deployment.reconcile",
                    TARGET_FINGERPRINT,
                ),
            )
            self.assertIsNotNone(host.queue.claim_next())
            terminal_path = (
                host.v2
                / "state"
                / "reconciliations"
                / f"{queued['job_id']}.{reconciliation['job_id']}.json"
            )
            terminal_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(prepared_path, terminal_path)
            host.queue.finish(reconciliation_result)
            resolved = host.deployment_reconcile(
                queued["job_id"], TARGET_FINGERPRINT, digest, digest
            )
            self.assertTrue(resolved["already_resolved"])

    def test_expired_wrong_target_and_no_target_config_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host = _activate_root(Path(tmp))
            _, artifact = self._qualified_artifact(host)
            digest = artifact["artifact_digest"]
            with self.assertRaises(ProtocolViolation):
                host.deployment_prepare(digest, "sha256:" + ("9" * 64), digest, _profile())
            plan = host.deployment_prepare(digest, TARGET_FINGERPRINT, digest, _profile())
            plan_path = host.v2 / "plans" / f"{plan['plan_id']}.plan.json"
            plan["expires_at"] = "2026-01-01T00:00:00Z"
            plan_path.write_bytes(canonical_json_bytes(plan))
            with self.assertRaises(ProtocolViolation):
                host.deployment_execute(
                    plan["plan_id"],
                    plan["artifact_digest"],
                    plan["target_fingerprint"],
                    plan["rollback_digest"],
                    plan["expires_at"],
                    plan["challenge_nonce"],
                    "yükle",
                )

        with tempfile.TemporaryDirectory() as tmp:
            host = _activate_root(Path(tmp), with_target=False)
            baseline = host.v2 / "artifacts" / f"{'a' * 64}.project"
            baseline.write_bytes(b"not-matching")
            with self.assertRaises(ProtocolViolation):
                host.deployment_prepare(
                    "sha256:" + ("a" * 64), TARGET_FINGERPRINT, "sha256:" + ("a" * 64), _profile()
                )

    def test_observation_is_allowlisted_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host = _activate_root(Path(tmp))
            queued = host.target_observe_start(TARGET_FINGERPRINT, _profile())
            self.assertEqual(queued["state"], "queued")
            self.assertEqual(queued["observation_id"], queued["job_id"])
            self.assertEqual(
                host.deployment_supervisor.calls,
                [(queued["job_id"], "target.observe.start", TARGET_FINGERPRINT)],
            )
            bad = _profile()
            bad["expressions"] = ["Unapproved.Value"]
            with self.assertRaises(ProtocolViolation):
                host.target_observe_start(TARGET_FINGERPRINT, bad)
            too_long = _profile()
            too_long["duration_seconds"] = 301
            with self.assertRaises(ProtocolViolation):
                host.target_observe_start(TARGET_FINGERPRINT, too_long)

    def test_observation_manifest_precedes_submit_and_lifecycle_is_restart_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host = _activate_root(Path(tmp))
            profile = _profile()
            for invariant in profile["fault_invariants"]:
                invariant["action"] = "record"
            with patch.object(
                host.queue, "submit", side_effect=RuntimeError("submit crash")
            ):
                with self.assertRaisesRegex(RuntimeError, "submit crash"):
                    host.target_observe_start(TARGET_FINGERPRINT, profile)
            manifests = list((host.v2 / "observations").glob("*.manifest.json"))
            self.assertEqual(len(manifests), 1)
            manifest_before = manifests[0].read_bytes()

            queued = host.target_observe_start(TARGET_FINGERPRINT, profile)
            self.assertEqual(queued["state"], "queued")
            self.assertEqual(manifests[0].read_bytes(), manifest_before)
            self.assertEqual(
                host.target_observe_start(TARGET_FINGERPRINT, profile)["job_id"],
                queued["job_id"],
            )
            lifecycle_path = (
                host.v2
                / "state"
                / "observations"
                / queued["job_id"]
                / "00000001-running.json"
            )
            lifecycle_path.parent.mkdir(parents=True, exist_ok=True)
            lifecycle_path.write_bytes(
                canonical_json_bytes(
                    {
                        "schema_version": "1",
                        "record_type": "ObservationLifecycle",
                        "sequence": 1,
                        "observation_id": queued["job_id"],
                        "state": "running",
                        "created_at": _timestamp(),
                        "worker_id": str(uuid.uuid4()),
                        "target_fingerprint": TARGET_FINGERPRINT,
                        "sample_count": 0,
                    }
                )
            )
            observed = host.target_observe_read(queued["job_id"])
            self.assertEqual(observed["session_state"], "running")
            first_stop = host.target_observe_stop(queued["job_id"])
            second_stop = host.target_observe_stop(queued["job_id"])
            self.assertEqual(first_stop["job_id"], second_stop["job_id"])
            self.assertEqual(first_stop["state"], "queued")

    def test_terminal_observation_allocates_next_durable_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host = _activate_root(Path(tmp))
            profile = _profile()
            for invariant in profile["fault_invariants"]:
                invariant["action"] = "record"

            first = host.target_observe_start(TARGET_FINGERPRINT, profile)
            self.assertEqual(
                host.target_observe_start(TARGET_FINGERPRINT, profile)["job_id"],
                first["job_id"],
            )
            lifecycle = host.v2 / "state" / "observations" / first["job_id"]
            lifecycle.mkdir(parents=True, exist_ok=True)
            worker_id = str(uuid.uuid4())
            records = (
                (1, "running", 0),
                (2, "completed", 4),
            )
            for sequence, state, sample_count in records:
                (lifecycle / f"{sequence:08d}-{state}.json").write_bytes(
                    canonical_json_bytes(
                        {
                            "schema_version": "1",
                            "record_type": "ObservationLifecycle",
                            "sequence": sequence,
                            "observation_id": first["job_id"],
                            "state": state,
                            "created_at": _timestamp(),
                            "worker_id": worker_id,
                            "target_fingerprint": TARGET_FINGERPRINT,
                            "sample_count": sample_count,
                        }
                    )
                )

            second = host.target_observe_start(TARGET_FINGERPRINT, profile)
            self.assertNotEqual(second["job_id"], first["job_id"])
            self.assertEqual(second["state"], "queued")
            self.assertEqual(
                host.target_observe_start(TARGET_FINGERPRINT, profile)["job_id"],
                second["job_id"],
            )
            self.assertTrue(
                (host.v2 / "observations" / f"{second['job_id']}.manifest.json").is_file()
            )

    def test_target_operations_fail_closed_without_deployment_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host = _activate_root(
                Path(tmp), with_target=True, with_deployment_supervisor=False
            )
            _, artifact = self._qualified_artifact(host)
            digest = artifact["artifact_digest"]
            plan = host.deployment_prepare(
                digest, TARGET_FINGERPRINT, digest, _profile()
            )
            execute_args = (
                plan["plan_id"],
                plan["artifact_digest"],
                plan["target_fingerprint"],
                plan["rollback_digest"],
                plan["expires_at"],
                plan["challenge_nonce"],
                "yükle",
            )
            with self.assertRaisesRegex(ProtocolViolation, "launcher is disabled"):
                host.deployment_execute(*execute_args)
            self.assertFalse(
                (host.v2 / "plans" / f"{plan['plan_id']}.consumed.json").exists()
            )
            self.assertEqual(list((host.v2 / "inbox").glob("*.request.json")), [])

            with self.assertRaisesRegex(ProtocolViolation, "launcher is disabled"):
                host.target_observe_start(TARGET_FINGERPRINT, _profile())
            self.assertEqual(list((host.v2 / "inbox").glob("*.request.json")), [])

    def test_deployment_launcher_failure_consumes_authority_and_cancels_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host = _activate_root(Path(tmp))
            host.deployment_supervisor.launch_state = "not_started"
            _, artifact = self._qualified_artifact(host)
            digest = artifact["artifact_digest"]
            plan = host.deployment_prepare(
                digest, TARGET_FINGERPRINT, digest, _profile()
            )
            with self.assertRaisesRegex(ProtocolViolation, "unclaimed request was cancelled"):
                host.deployment_execute(
                    plan["plan_id"],
                    plan["artifact_digest"],
                    plan["target_fingerprint"],
                    plan["rollback_digest"],
                    plan["expires_at"],
                    plan["challenge_nonce"],
                    "yükle",
                )
            self.assertTrue(
                (host.v2 / "plans" / f"{plan['plan_id']}.consumed.json").is_file()
            )
            cancelled = [
                path
                for path in (host.v2 / "results").glob("*.result.json")
                if strict_json_loads_v2(path.read_bytes())["operation"]
                == "deployment.execute"
            ]
            self.assertEqual(len(cancelled), 1)
            self.assertEqual(
                strict_json_loads_v2(cancelled[0].read_bytes())["status"], "cancelled"
            )

    def test_deployment_launcher_identity_mismatch_cancels_unclaimed_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host = _activate_root(Path(tmp))
            host.deployment_supervisor.echo_target_fingerprint = "sha256:" + ("9" * 64)
            _, artifact = self._qualified_artifact(host)
            digest = artifact["artifact_digest"]
            plan = host.deployment_prepare(
                digest, TARGET_FINGERPRINT, digest, _profile()
            )
            with self.assertRaisesRegex(ProtocolViolation, "unclaimed request was cancelled"):
                host.deployment_execute(
                    plan["plan_id"],
                    plan["artifact_digest"],
                    plan["target_fingerprint"],
                    plan["rollback_digest"],
                    plan["expires_at"],
                    plan["challenge_nonce"],
                    "yükle",
                )
            cancelled = [
                path
                for path in (host.v2 / "results").glob("*.result.json")
                if strict_json_loads_v2(path.read_bytes())["operation"]
                == "deployment.execute"
            ]
            self.assertEqual(len(cancelled), 1)
            self.assertEqual(
                strict_json_loads_v2(cancelled[0].read_bytes())["status"], "cancelled"
            )

    def test_unknown_post_claim_launch_is_reconciliation_not_false_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host = _activate_root(Path(tmp))
            _, artifact = self._qualified_artifact(host)
            digest = artifact["artifact_digest"]
            plan = host.deployment_prepare(
                digest, TARGET_FINGERPRINT, digest, _profile()
            )
            host.deployment_supervisor.launch_state = "unknown"
            host.deployment_supervisor.on_ensure = host.queue.claim_next
            with self.assertRaisesRegex(ProtocolViolation, "reconciliation is required"):
                host.deployment_execute(
                    plan["plan_id"],
                    plan["artifact_digest"],
                    plan["target_fingerprint"],
                    plan["rollback_digest"],
                    plan["expires_at"],
                    plan["challenge_nonce"],
                    "yükle",
                )
            running = list((host.v2 / "running").glob("*.request.json"))
            self.assertEqual(len(running), 1)
            request, _ = load_v2(running[0])
            job_id = request["job_id"]
            state = host.queue.status(job_id)
            self.assertEqual(state["state"], "running")
            self.assertTrue(state["cancel_requested"])
            self.assertTrue(
                (host.v2 / "state" / "side-effects" / f"{job_id}.started.json").is_file()
            )
            self.assertFalse((host.v2 / "results" / f"{job_id}.result.json").exists())

            recovery = host.queue.recover()
            self.assertEqual(recovery["unknown_outcome"], 1)
            self.assertEqual(recovery["requeued"], 0)
            self.assertEqual(host.job_status(job_id)["status"], "unknown_outcome")

    def test_launch_uncertainty_recovers_and_public_reconcile_resolves_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host = _activate_root(Path(tmp))
            _, artifact = self._qualified_artifact(host)
            digest = artifact["artifact_digest"]
            plan = host.deployment_prepare(
                digest, TARGET_FINGERPRINT, digest, _profile()
            )
            host.deployment_supervisor.launch_state = "unknown"
            host.deployment_supervisor.on_ensure = host.queue.claim_next
            with patch.object(
                host.queue,
                "mark_side_effect_started",
                side_effect=RuntimeError("synthetic host power loss before marker"),
            ):
                with self.assertRaisesRegex(RuntimeError, "power loss"):
                    host.deployment_execute(
                        plan["plan_id"],
                        digest,
                        TARGET_FINGERPRINT,
                        digest,
                        plan["expires_at"],
                        plan["challenge_nonce"],
                        "yükle",
                    )
            running = next((host.v2 / "running").glob("*.request.json"))
            original_request, _original_raw = load_v2(running)
            original_job_id = original_request["job_id"]
            provenance_path = (
                host.v2
                / "state"
                / "deployment-launch-uncertainty"
                / f"{original_job_id}.json"
            )
            self.assertTrue(provenance_path.is_file())
            self.assertFalse(
                (host.v2 / "state" / "side-effects" / f"{original_job_id}.started.json").exists()
            )
            recovered = host.queue.recover()
            self.assertEqual(recovered["unknown_outcome"], 1)
            self.assertEqual(recovered["requeued"], 0)

            host.deployment_supervisor.launch_state = "started"
            host.deployment_supervisor.on_ensure = None
            reconciliation = host.deployment_reconcile(
                original_job_id, TARGET_FINGERPRINT, digest, digest
            )
            claim = host.queue.claim_next()
            self.assertIsNotNone(claim)
            reconciliation_request, reconciliation_raw = claim
            descriptor = _worker_evidence_descriptor(
                host.v2,
                reconciliation_request,
                outcome="succeeded",
                target_fingerprint=TARGET_FINGERPRINT,
                candidate_digest=digest,
                rollback_digest=digest,
                reconciliation_required=False,
            )
            host.ingest_worker_result(
                _result(
                    reconciliation_request,
                    reconciliation_raw,
                    status="succeeded",
                    artifacts=[descriptor],
                )
            )
            result_path = host.v2 / "results" / f"{original_job_id}.result.json"
            marker_path = (
                host.v2 / "state" / "side-effects" / f"{original_job_id}.started.json"
            )
            record_path = (
                host.v2
                / "state"
                / "reconciliations"
                / f"{original_job_id}.{reconciliation['job_id']}.json"
            )
            record_path.write_bytes(
                canonical_json_bytes(
                    {
                        "schema_version": "1",
                        "record_type": "DeploymentReconciliation",
                        "original_job_id": original_job_id,
                        "reconciliation_job_id": reconciliation["job_id"],
                        "created_at": utc_now(),
                        "target_fingerprint": TARGET_FINGERPRINT,
                        "artifact_digest": digest,
                        "rollback_digest": digest,
                        "runtime_release_digest": artifact["runtime_release_digest"],
                        "rollback_runtime_release_digest": artifact["runtime_release_digest"],
                        "original_result_digest": sha256_file(result_path),
                        "side_effect_marker_digest": sha256_file(marker_path),
                        "provenance_head_kind": "launch_uncertainty",
                        "journal_head_digest": sha256_file(provenance_path),
                        "outcome": "succeeded",
                        "reconciliation_required": False,
                        "observed": {
                            "target_fingerprint": TARGET_FINGERPRINT,
                            "runtime_release_digest": artifact["runtime_release_digest"],
                            "application_state": "run",
                            "forced_values_present": False,
                            "physical_outputs_energized": False,
                        },
                    }
                )
            )
            repeated = host.deployment_reconcile(
                original_job_id, TARGET_FINGERPRINT, digest, digest
            )
            self.assertTrue(repeated["already_resolved"])
            self.assertEqual(repeated["outcome"], "succeeded")
            self.assertFalse(
                (host.v2 / "state" / "deployments" / original_job_id).exists()
            )

    def test_execute_submit_launch_crash_retries_same_consumed_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            host = _activate_root(root)
            _, artifact = self._qualified_artifact(host)
            digest = artifact["artifact_digest"]
            plan = host.deployment_prepare(
                digest, TARGET_FINGERPRINT, digest, _profile()
            )
            arguments = (
                plan["plan_id"],
                digest,
                TARGET_FINGERPRINT,
                digest,
                plan["expires_at"],
                plan["challenge_nonce"],
                "yükle",
            )
            with patch.object(
                host,
                "_start_target_worker",
                side_effect=RuntimeError("synthetic submit-to-launch power loss"),
            ):
                with self.assertRaisesRegex(RuntimeError, "power loss"):
                    host.deployment_execute(*arguments)
            queued_path = next((host.v2 / "inbox").glob("*.request.json"))
            queued_request, _queued_raw = load_v2(queued_path)

            restarted_supervisor = _DeploymentSupervisor()
            restarted = AutonomousHost(
                root,
                legacy_timeout_seconds=1,
                deployment_supervisor=restarted_supervisor,
            )
            with self.assertRaisesRegex(ProtocolViolation, "earlier deployment request"):
                restarted.target_observe_start(TARGET_FINGERPRINT, _profile())
            self.assertEqual(restarted_supervisor.calls, [])
            resumed = restarted.deployment_execute(*arguments)
            self.assertEqual(resumed["job_id"], queued_request["job_id"])
            self.assertTrue(resumed["duplicate_suppressed"])
            self.assertEqual(
                restarted_supervisor.calls,
                [(queued_request["job_id"], "deployment.execute", TARGET_FINGERPRINT)],
            )
            self.assertEqual(
                len(list((host.v2 / "inbox").glob("*.request.json"))), 1
            )

    def test_execute_input_publish_crash_reconstructs_exact_request_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            host = _activate_root(root)
            _, artifact = self._qualified_artifact(host)
            digest = artifact["artifact_digest"]
            plan = host.deployment_prepare(
                digest, TARGET_FINGERPRINT, digest, _profile()
            )
            arguments = (
                plan["plan_id"],
                digest,
                TARGET_FINGERPRINT,
                digest,
                plan["expires_at"],
                plan["challenge_nonce"],
                "yükle",
            )
            order_count_before = len(
                list((host.v2 / "state" / "order").glob("*.json"))
            )
            with patch.object(
                host.queue,
                "submit",
                side_effect=RuntimeError("synthetic input-to-order power loss"),
            ):
                with self.assertRaisesRegex(RuntimeError, "power loss"):
                    host.deployment_execute(*arguments)
            inputs = list((host.v2 / "inputs").glob("*.input.json"))
            self.assertEqual(len(inputs), 1)
            expected_job_id = inputs[0].name.removesuffix(".input.json")
            self.assertFalse(any((host.v2 / "inbox").glob("*.request.json")))
            self.assertEqual(
                len(list((host.v2 / "state" / "order").glob("*.json"))),
                order_count_before,
            )

            supervisor = _DeploymentSupervisor()
            restarted = AutonomousHost(
                root,
                legacy_timeout_seconds=1,
                deployment_supervisor=supervisor,
            )
            resumed = restarted.deployment_execute(*arguments)
            self.assertEqual(resumed["job_id"], expected_job_id)
            self.assertTrue(resumed["duplicate_suppressed"])
            request_path = next((host.v2 / "inbox").glob("*.request.json"))
            request, request_raw = load_v2(request_path)
            provenance = strict_json_loads_v2(
                (
                    host.v2
                    / "state"
                    / "deployment-launch-uncertainty"
                    / f"{expected_job_id}.json"
                ).read_bytes()
            )
            self.assertEqual(provenance["request_digest"], sha256_bytes(request_raw))
            self.assertEqual(request["job_id"], expected_job_id)

    def test_reconcile_order_and_submit_launch_crashes_resume_same_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host = _activate_root(Path(tmp))
            _, artifact = self._qualified_artifact(host)
            digest = artifact["artifact_digest"]
            plan = host.deployment_prepare(
                digest, TARGET_FINGERPRINT, digest, _profile()
            )
            host.deployment_supervisor.launch_state = "unknown"
            host.deployment_supervisor.on_ensure = host.queue.claim_next
            with self.assertRaisesRegex(ProtocolViolation, "reconciliation is required"):
                host.deployment_execute(
                    plan["plan_id"],
                    digest,
                    TARGET_FINGERPRINT,
                    digest,
                    plan["expires_at"],
                    plan["challenge_nonce"],
                    "yükle",
                )
            original_job_id = next(
                strict_json_loads_v2(path.read_bytes())["job_id"]
                for path in (host.v2 / "running").glob("*.request.json")
            )
            self.assertEqual(host.queue.recover()["unknown_outcome"], 1)
            host.deployment_supervisor.launch_state = "started"
            host.deployment_supervisor.on_ensure = None
            original_submit = host.queue.submit

            def crash_after_order(request):
                raw = request_v2_bytes(request, host.v2)
                sequence = host.queue._next_sequence()
                order = {
                    "schema_version": "1",
                    "sequence": sequence,
                    "correlation_id": request["correlation_id"],
                    "job_id": request["job_id"],
                    "request_digest": sha256_bytes(raw),
                }
                path = (
                    host.v2
                    / "state"
                    / "order"
                    / f"{sequence:020d}_{request['correlation_id']}.order.json"
                )
                path.write_bytes(canonical_json_bytes(order))
                raise RuntimeError("synthetic order-to-inbox power loss")

            with patch.object(host.queue, "submit", side_effect=crash_after_order):
                with self.assertRaisesRegex(RuntimeError, "power loss"):
                    host.deployment_reconcile(
                        original_job_id, TARGET_FINGERPRINT, digest, digest
                    )
            arguments = {
                "original_job_id": original_job_id,
                "target_fingerprint": TARGET_FINGERPRINT,
                "artifact_digest": digest,
                "rollback_digest": digest,
            }
            missing = host._reconciliation_claim_for(arguments)
            self.assertIsNotNone(missing)
            self.assertEqual(missing["state"], "missing")
            reconciliation_job_id = missing["job_id"]

            host.queue.submit = original_submit
            with patch.object(
                host,
                "_start_target_worker",
                side_effect=RuntimeError("synthetic submit-to-launch power loss"),
            ):
                with self.assertRaisesRegex(RuntimeError, "power loss"):
                    host.deployment_reconcile(
                        original_job_id, TARGET_FINGERPRINT, digest, digest
                    )
            self.assertEqual(
                host.queue.status(reconciliation_job_id)["state"], "queued"
            )
            resumed = host.deployment_reconcile(
                original_job_id, TARGET_FINGERPRINT, digest, digest
            )
            self.assertEqual(resumed["job_id"], reconciliation_job_id)
            self.assertTrue(resumed["duplicate_suppressed"])
            self.assertEqual(resumed["worker_launch"]["launch_state"], "started")

    def test_crashed_deployment_recovers_to_unknown_and_blocks_next_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host = _activate_root(Path(tmp))
            _, artifact = self._qualified_artifact(host)
            digest = artifact["artifact_digest"]
            plan = host.deployment_prepare(
                digest, TARGET_FINGERPRINT, digest, _profile()
            )

            def claim_and_mark() -> None:
                claimed = host.queue.claim_next()
                self.assertIsNotNone(claimed)
                request, _raw = claimed
                host.queue.mark_side_effect_started(
                    request["job_id"], request["job_id"], ENVIRONMENT_DIGEST
                )
                journal_dir = (
                    host.v2 / "state" / "deployments" / request["job_id"]
                )
                journal_dir.mkdir(parents=True, exist_ok=True)
                (journal_dir / "0001-candidate-full-download-intent.json").write_bytes(
                    canonical_json_bytes(
                        {
                            "schema_version": "1",
                            "record_type": "DeploymentJournalEvent",
                            "sequence": 1,
                            "job_id": request["job_id"],
                            "event": "candidate-full-download-intent",
                            "created_at": utc_now(),
                            "details": {
                                "artifact_digest": digest,
                                "reconciliation_required": True,
                            },
                        }
                    )
                )

            host.deployment_supervisor.on_ensure = claim_and_mark
            queued = host.deployment_execute(
                plan["plan_id"],
                plan["artifact_digest"],
                plan["target_fingerprint"],
                plan["rollback_digest"],
                plan["expires_at"],
                plan["challenge_nonce"],
                "yükle",
            )
            host.deployment_supervisor.on_recover = lambda: host.queue.recover(
                operations={
                    "deployment.execute",
                    "target.observe.start",
                    "target.observe.read",
                    "target.observe.stop",
                }
            )
            status = host.job_status(queued["job_id"])
            self.assertEqual(status["status"], "unknown_outcome")
            self.assertTrue(status["unknown_outcome"])
            with self.assertRaisesRegex(
                ProtocolViolation, "outcome is unresolved"
            ):
                host.deployment_prepare(
                    digest, TARGET_FINGERPRINT, digest, _profile()
                )

            host.deployment_supervisor.on_ensure = None
            reconciliation = host.deployment_reconcile(
                queued["job_id"], TARGET_FINGERPRINT, digest, digest
            )
            self.assertEqual(reconciliation["operation"], "deployment.reconcile")
            claimed_reconciliation = host.queue.claim_next()
            self.assertIsNotNone(claimed_reconciliation)
            reconciliation_request, reconciliation_raw = claimed_reconciliation
            reconciliation_evidence = _worker_evidence_descriptor(
                host.v2,
                reconciliation_request,
                outcome="succeeded",
                target_fingerprint=TARGET_FINGERPRINT,
                candidate_digest=digest,
                rollback_digest=digest,
                reconciliation_required=False,
            )
            host.ingest_worker_result(
                _result(
                    reconciliation_request,
                    reconciliation_raw,
                    status="succeeded",
                    artifacts=[reconciliation_evidence],
                )
            )
            original_result_path = (
                host.v2 / "results" / f"{queued['job_id']}.result.json"
            )
            marker_path = (
                host.v2
                / "state"
                / "side-effects"
                / f"{queued['job_id']}.started.json"
            )
            journal_path = (
                host.v2
                / "state"
                / "deployments"
                / queued["job_id"]
                / "0001-candidate-full-download-intent.json"
            )
            record_path = (
                host.v2
                / "state"
                / "reconciliations"
                / f"{queued['job_id']}.{reconciliation['job_id']}.json"
            )
            record_path.parent.mkdir(parents=True, exist_ok=True)
            record_path.write_bytes(
                canonical_json_bytes(
                    {
                        "schema_version": "1",
                        "record_type": "DeploymentReconciliation",
                        "original_job_id": queued["job_id"],
                        "reconciliation_job_id": reconciliation["job_id"],
                        "created_at": utc_now(),
                        "target_fingerprint": TARGET_FINGERPRINT,
                        "artifact_digest": digest,
                        "rollback_digest": digest,
                        "runtime_release_digest": artifact["runtime_release_digest"],
                        "rollback_runtime_release_digest": artifact[
                            "runtime_release_digest"
                        ],
                        "original_result_digest": sha256_file(original_result_path),
                        "side_effect_marker_digest": sha256_file(marker_path),
                        "provenance_head_kind": "journal",
                        "journal_head_digest": sha256_file(journal_path),
                        "outcome": "succeeded",
                        "reconciliation_required": False,
                        "observed": {
                            "target_fingerprint": TARGET_FINGERPRINT,
                            "runtime_release_digest": artifact[
                                "runtime_release_digest"
                            ],
                            "application_state": "run",
                            "forced_values_present": False,
                            "physical_outputs_energized": False,
                        },
                    }
                )
            )
            repeated = host.deployment_reconcile(
                queued["job_id"], TARGET_FINGERPRINT, digest, digest
            )
            self.assertTrue(repeated["already_resolved"])
            self.assertEqual(
                host.deployment_prepare(
                    digest, TARGET_FINGERPRINT, digest, _profile()
                )["state"],
                "prepared",
            )

    def test_autonomous_mcp_lists_all_public_tools_with_safe_annotations(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                host = _activate_root(Path(tmp))
                server = create_server(OfflineService(host))
                async with Client(server) as client:
                    listed = await client.list_tools()
                    names = tuple(tool.name for tool in listed.tools)
                    self.assertEqual(names, PUBLIC_OPERATIONS)
                    workspace_tool = next(
                        tool for tool in listed.tools if tool.name == "workspace.create"
                    )
                    self.assertNotIn(
                        "baseline_path", workspace_tool.input_schema.get("required", [])
                    )
                    self.assertIn("deterministically derived", workspace_tool.description)
                    source_tool = next(
                        tool for tool in listed.tools if tool.name == "source.upsert.st"
                    )
                    self.assertEqual(
                        source_tool.input_schema["properties"]["pou_kind"]["enum"],
                        ["program", "function_block", "function", "gvl"],
                    )
                    readonly = {
                        "environment.probe",
                        "project.tree.read",
                        "project.inventory.read",
                        "source.read",
                        "project.export.native",
                        "workspace.status",
                        "deployment.reconcile",
                        "target.observe.read",
                        "job.status",
                        "job.progress",
                        "operator.profile",
                        "operator.evidence.check",
                    }
                    for tool in listed.tools:
                        self.assertFalse(tool.annotations.open_world_hint)
                        self.assertEqual(tool.annotations.read_only_hint, tool.name in readonly)
                        self.assertEqual(tool.annotations.destructive_hint, tool.name not in readonly)
                    baseline_raw = b"DETERMINISTIC-MCP-BASELINE"
                    baseline_digest = sha256_bytes(baseline_raw)
                    baseline_path = (
                        host.v2
                        / "baselines"
                        / f"{baseline_digest.removeprefix('sha256:')}.project"
                    )
                    baseline_path.write_bytes(baseline_raw)
                    created = await client.call_tool(
                        "workspace.create",
                        {
                            "baseline_sha256": baseline_digest,
                            "environment_digest": ENVIRONMENT_DIGEST,
                        },
                    )
                    self.assertFalse(created.is_error)
                    self.assertEqual(
                        created.structured_content["baseline_project_digest"],
                        baseline_digest,
                    )
                    rollback_profile = _profile()
                    rollback_profile["fault_invariants"][0][
                        "action"
                    ] = "stop_and_rollback"
                    rejected = await client.call_tool(
                        "target.observe.start",
                        {
                            "target_fingerprint": TARGET_FINGERPRINT,
                            "observation_profile": rollback_profile,
                        },
                    )
                    self.assertTrue(rejected.is_error)
                    self.assertIn(
                        "stop_and_rollback",
                        " ".join(getattr(item, "text", "") for item in rejected.content),
                    )

        asyncio.run(scenario())

    def test_version_is_0_4_0(self) -> None:
        self.assertEqual(PACKAGE_VERSION, "0.4.0")


if __name__ == "__main__":
    unittest.main()
