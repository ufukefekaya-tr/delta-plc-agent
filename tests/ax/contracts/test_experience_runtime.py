"""Offline regression controls for slow transfers and evidence-only progress."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from tests.ax.contracts.test_ax_deployment_worker import (
    ChangeOptions,
    ENVIRONMENT,
    Fixture,
    OperatingStates,
    _load_worker,
)
from axmcp_offline.jobs import FileJobQueue
from axmcp_offline.job_progress import read_job_progress
from axmcp_offline.mcp_server import _SERVER_INSTRUCTIONS
from axmcp_offline.protocol_v2 import make_request_v2, validate_public_operation
from axmcp_offline.security import ProtocolViolation, canonical_json_bytes


class ExperienceRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_worker()

    def _execute(self, behavior: dict, *, profile: str = "lan"):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        fixture = Fixture(Path(temp.name), self.module, behavior=behavior)
        request = fixture.deployment_request(transport_profile=profile)
        return fixture, request, fixture.worker.process_request(request)

    def _assert_retained(self, fixture: Fixture) -> None:
        self.assertTrue(fixture.worker._transfer_held)
        self.assertFalse(any(event[1] == "start" for event in fixture.events))
        self.assertFalse(fixture.online.instances[-1].disposed)
        self.assertFalse(fixture.projects.projects[0].closed)

    def test_150_and_330_second_unresolved_transfer_keep_session(self) -> None:
        # Old behavior disposed the online object and closed the project on timeout.
        for seconds in (150, 330):
            with self.subTest(seconds=seconds):
                fixture, _, result = self._execute(
                    {"download_return_after": seconds, "fail_candidate_download": True}
                )
                self.assertEqual(result["status"], "unknown_outcome")
                self._assert_retained(fixture)
                self.assertEqual(sum(event[1] == "login" and event[2] == ChangeOptions.Never for event in fixture.events if len(event) > 2), 1)

    def test_350_second_late_success_records_completion_but_never_runs(self) -> None:
        fixture, request, result = self._execute({"download_return_after": 350})
        self.assertEqual(result["status"], "unknown_outcome")
        self.assertNotIn(("candidate", "start"), fixture.events)
        journal = Path(fixture.root) / "state" / "deployments" / request["job_id"]
        events = [json.loads(path.read_text(encoding="utf-8"))["event"] for path in sorted(journal.glob("*.json"))]
        self.assertIn("candidate-download-initialization-complete", events)
        self.assertNotIn("candidate-run-verified", events)

    def test_600_second_diacloud_timeout_never_runs_or_retries(self) -> None:
        fixture, _, result = self._execute(
            {"download_return_after": 600, "fail_candidate_download": True,
             "workflow_deadline_capability": True}, profile="diacloud"
        )
        self.assertEqual(result["status"], "unknown_outcome")
        self._assert_retained(fixture)

    def test_incomplete_initialization_blocks_run_and_cleanup(self) -> None:
        fixture, _, result = self._execute({"incomplete_initialization": True})
        self.assertEqual(result["status"], "unknown_outcome")
        self._assert_retained(fixture)

    def test_late_state_change_is_evidence_not_run_permission(self) -> None:
        fixture, request, result = self._execute({"incomplete_initialization": True})
        self.assertEqual(result["status"], "unknown_outcome")
        fixture.online.instances[-1].operation_state = OperatingStates.program_loaded
        self.assertTrue(fixture.worker.observe_held_transfer())
        self.assertFalse(fixture.worker.observe_held_transfer())
        self.assertFalse(fixture.worker._transfer_held)
        self.assertTrue(fixture.online.instances[-1].disposed)
        self.assertTrue(fixture.projects.projects[0].closed)
        self.assertNotIn(("candidate", "start"), fixture.events)
        journal = fixture.root / "state" / "deployments" / request["job_id"]
        records = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(journal.glob("*.json"))]
        self.assertEqual(records[-2]["event"], "candidate-late-transfer-state-observed")
        self.assertTrue(records[-2]["details"]["terminal_transfer_observed"])
        self.assertEqual(records[-1]["event"], "candidate-transfer-owner-released")
        self.assertTrue(records[-1]["details"]["reconciliation_required"])

    def test_stale_loaded_after_exception_cannot_release_owner(self) -> None:
        fixture, _, result = self._execute({"fail_candidate_download": True})
        self.assertEqual(result["status"], "unknown_outcome")
        self.assertTrue(fixture.worker.observe_held_transfer())
        self.assertFalse(fixture.worker.observe_held_transfer())
        self._assert_retained(fixture)

    def test_held_state_probe_is_throttled_to_30_seconds(self) -> None:
        fixture, _, result = self._execute({"incomplete_initialization": True})
        self.assertEqual(result["status"], "unknown_outcome")
        runtime = self.module.AxDeploymentRuntime(
            str(fixture.root), fixture.worker, {"worker_id": str(uuid.uuid4())},
            "sha256:" + "b" * 64, now=fixture.clock.now, sleeper=fixture.clock.sleep,
        )
        calls = []
        original = fixture.worker.observe_held_transfer
        def counted():
            calls.append(fixture.clock.now())
            return original()
        fixture.worker.observe_held_transfer = counted
        self.assertFalse(runtime._poll_held_transfer())
        fixture.clock.sleep(29)
        fixture.online.instances[-1].operation_state = OperatingStates.program_loaded
        self.assertFalse(runtime._poll_held_transfer())
        self.assertTrue(fixture.worker._transfer_held)
        fixture.clock.sleep(1)
        self.assertTrue(runtime._poll_held_transfer())
        self.assertFalse(fixture.worker._transfer_held)
        self.assertEqual(len(calls), 2)

    def test_terminal_proof_allows_queued_readonly_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), self.module, behavior={"incomplete_initialization": True})
            fixture.clock.value = time.time()
            queue = FileJobQueue(fixture.root)
            original = fixture.deployment_request()
            queue.submit(original)
            runtime = self.module.AxDeploymentRuntime(
                str(fixture.root), fixture.worker,
                {"worker_id": str(uuid.uuid4()), "max_runtime_seconds": 330},
                "sha256:" + "b" * 64, now=fixture.clock.now, sleeper=fixture.clock.sleep,
            )
            self.assertTrue(runtime.process_next())
            self.assertEqual(queue.status(original["job_id"])["state"], "unknown_outcome")
            reconcile = fixture.reconciliation_request(original["job_id"])
            queue.submit(reconcile)
            self.assertFalse(runtime.process_next())
            fixture.online.instances[-1].operation_state = OperatingStates.program_loaded
            self.assertTrue(runtime._poll_held_transfer())
            self.assertTrue(runtime.process_next())
            self.assertEqual(queue.status(reconcile["job_id"])["state"], "unknown_outcome")
            self.assertNotIn(("candidate", "start"), fixture.events)

    def test_unicode_diagnostic_does_not_mask_transfer_ownership(self) -> None:
        fixture, _, result = self._execute(
            {"fail_candidate_download": True,
             "fail_candidate_download_message": "bağlantı koptu — aktarım sürüyor"}
        )
        self.assertEqual(result["status"], "unknown_outcome")
        self._assert_retained(fixture)

    def test_legacy_worker_rejects_diacloud_before_target_contact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), self.module)
            request = fixture.deployment_request(transport_profile="diacloud")
            with self.assertRaisesRegex(self.module.WorkerError, "legacy deployment worker"):
                fixture.worker.process_request(request)
        self.assertEqual(fixture.events, [])

    def test_retained_transfer_prevents_second_queue_claim(self) -> None:
        fixture, _, _ = self._execute({"fail_candidate_download": True})
        config = {"worker_id": str(uuid.uuid4())}
        runtime = self.module.AxDeploymentRuntime(
            str(fixture.root), fixture.worker, config, "sha256:" + "b" * 64,
            now=fixture.clock.now, sleeper=fixture.clock.sleep,
        )
        self.assertFalse(runtime.process_next())
        self.assertEqual(runtime.processed, 0)

    def test_shared_absolute_deadline_is_not_refreshed(self) -> None:
        start = datetime.now(UTC).replace(microsecond=0)
        deadline = (start + timedelta(seconds=600)).strftime("%Y-%m-%dT%H:%M:%SZ")
        def request(created: datetime):
            return make_request_v2(
                correlation_id=str(uuid.uuid4()), job_id=str(uuid.uuid4()),
                operation="project.build", created_at=created.strftime("%Y-%m-%dT%H:%M:%SZ"),
                timeout_seconds=600, environment_digest=ENVIRONMENT,
                workspace_digest="sha256:" + "a" * 64, expected_revision=0,
                authorization_digest=None, artifacts=[], transport_profile="diacloud",
                workflow_deadline_at=deadline,
            )
        self.assertEqual(request(start)["deadline_at"], deadline)
        self.assertEqual(request(start + timedelta(seconds=480))["deadline_at"], deadline)
        with self.assertRaises(ProtocolViolation):
            request(start + timedelta(seconds=601))

    def test_job_progress_is_read_only_and_claims_no_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = FileJobQueue(root)
            now = datetime.now(UTC).replace(microsecond=0)
            request = make_request_v2(
                correlation_id=str(uuid.uuid4()), job_id=str(uuid.uuid4()),
                operation="project.build",
                created_at=(now - timedelta(seconds=480)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                timeout_seconds=600, environment_digest=ENVIRONMENT,
                workspace_digest="sha256:" + "a" * 64, expected_revision=0,
                authorization_digest=None, artifacts=[], transport_profile="diacloud",
                workflow_deadline_at=(now + timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            queue.submit(request)
            before = sorted(str(path.relative_to(root)) for path in root.rglob("*"))
            progress = read_job_progress(queue, request["job_id"])
            after = sorted(str(path.relative_to(root)) for path in root.rglob("*"))
            self.assertEqual(before, after)
            self.assertEqual(progress["stage"], "queued")
            self.assertEqual(progress["source"], "queue")
            self.assertIsNone(progress["last_real_progress"])
            self.assertTrue(progress["warning"])
            self.assertEqual(progress["status_interval_seconds"], 30)
            for field in ("bytes_transferred", "bytes_total", "percent"):
                self.assertIsNone(progress[field])
            schema = json.loads((Path(__file__).resolve().parents[3] / "backends" / "ax" / "contracts" / "v2" / "job_progress.schema.json").read_text(encoding="utf-8"))
            self.assertEqual(list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(progress)), [])
            with self.assertRaises(ProtocolViolation):
                validate_public_operation("job.progress")

    def test_unknown_transfer_progress_is_intent_not_percent(self) -> None:
        fixture, request, result = self._execute({"fail_candidate_download": True})
        self.assertEqual(result["status"], "unknown_outcome")
        queue = FileJobQueue(fixture.root)
        (fixture.root / "running" / f"{request['correlation_id']}.request.json").write_bytes(canonical_json_bytes(request))
        progress = read_job_progress(queue, request["job_id"])
        self.assertTrue(progress["uncertainty"])
        self.assertEqual(progress["source"], "deployment_journal")
        self.assertIsNone(progress["percent"])
        self.assertIsNotNone(progress["last_real_progress"])

    def test_initialize_instructions_frontload_offline_and_transfer_boundaries(self) -> None:
        leading = _SERVER_INSTRUCTIONS[:512]
        for phrase in ("recorded_replay", "PLC source changes", "named revision", "no kill", "no blind retry", "job.progress"):
            self.assertIn(phrase, leading)
        self.assertIn("workflow_deadline_at", _SERVER_INSTRUCTIONS)
        self.assertIn("initialization complete before RUN", _SERVER_INSTRUCTIONS)


if __name__ == "__main__":
    unittest.main()
