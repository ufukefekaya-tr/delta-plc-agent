from __future__ import annotations

import calendar
import importlib.util
import json
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = ROOT / "backends" / "ax" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from axmcp_offline.jobs import FileJobQueue


WORKER_PATH = (
    ROOT
    / "backends" / "ax"
    / "adapters"
    / "ax_deployment_worker"
    / "worker.py"
)
RESULT_FIELDS = {
    "protocol",
    "message_type",
    "correlation_id",
    "job_id",
    "operation",
    "created_at",
    "status",
    "environment_digest",
    "workspace_digest",
    "request_digest",
    "before_project_digest",
    "after_project_digest",
    "revision",
    "artifacts",
    "diagnostics",
}
ENVIRONMENT = "sha256:" + "1" * 64
TARGET = "sha256:" + "2" * 64
ISOLATION = "sha256:" + "3" * 64
ROUTE = "sha256:" + "4" * 64


def _load_worker():
    spec = importlib.util.spec_from_file_location("ax_deployment_worker", WORKER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Clock:
    def __init__(self) -> None:
        self.value = float(calendar.timegm(time.strptime("2026-09-01T20:00:00Z", "%Y-%m-%dT%H:%M:%SZ")))

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds

    def stamp(self, offset: float = 0) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.value + offset))


class ChangeOptions:
    Keep = "KEEP"
    Never = "NEVER"


class ApplicationStates:
    run = "run"
    stop = "stop"


class VersionUpdateFlags:
    NoUpdates = "NO_UPDATES"


class CredentialSources:
    none = 0


class SimulatedWorkerCrash(BaseException):
    pass


class Application:
    def __init__(self, label: str) -> None:
        self.label = label


class Project:
    def __init__(self, label: str) -> None:
        self.label = label
        self.active_application = Application(label)
        self.closed = False

    def close(self) -> None:
        self.closed = True


class Projects:
    def __init__(self, candidate_digest: str, rollback_digest: str, events: list[tuple]) -> None:
        self.candidate_digest = candidate_digest
        self.rollback_digest = rollback_digest
        self.events = events
        self.projects: list[Project] = []

    def open(self, **kwargs):
        path = str(kwargs["path"])
        self.events.append(("project.open", Path(path).name, kwargs["update_flags"], kwargs["allow_readonly"]))
        if self.candidate_digest[7:] in path:
            label = "candidate"
        elif self.rollback_digest[7:] in path:
            label = "rollback"
        else:  # pragma: no cover - the worker path gate should make this unreachable
            raise AssertionError("unexpected project path")
        project = Project(label)
        self.projects.append(project)
        return project


class OnlineApplication:
    def __init__(self, label: str, behavior: dict, events: list[tuple]) -> None:
        self.label = label
        self.behavior = behavior
        self.events = events
        self.timeout = None
        self.is_logged_in = False
        self.application_state = "stop"
        self.disposed = False

    def login(self, option, delete_foreign_apps) -> None:
        self.events.append((self.label, "login", option, delete_foreign_apps))
        if self.label == "candidate" and option == ChangeOptions.Never and self.behavior.get("fail_candidate_download"):
            raise ConnectionError("connection lost during candidate full download")
        if self.label == "candidate" and option == ChangeOptions.Never and self.behavior.get("crash_candidate_download"):
            raise SimulatedWorkerCrash("worker terminated during candidate full download")
        if self.label == "rollback" and option == ChangeOptions.Never and self.behavior.get("fail_rollback_download"):
            raise ConnectionError("connection lost during rollback full download")
        self.is_logged_in = True

    def logout(self) -> None:
        self.events.append((self.label, "logout"))
        self.is_logged_in = False

    def Dispose(self) -> None:
        self.events.append((self.label, "dispose"))
        self.disposed = True

    def get_forced_expressions(self):
        self.events.append((self.label, "forced.read"))
        return tuple(self.behavior.get("forced", ()))

    def start(self) -> None:
        self.events.append((self.label, "start"))
        self.application_state = "run"

    def stop(self) -> None:
        self.events.append((self.label, "stop"))
        self.application_state = "stop"

    def read_values(self, expressions):
        self.events.append((self.label, "read_values", tuple(expressions)))
        samples = self.behavior.setdefault("samples", {})
        source = samples.get(self.label) or {
            "PLC_PRG.FaultCode": "0",
            "PLC_PRG.RestartRequired": "FALSE",
        }
        if isinstance(source, list):
            values = source.pop(0) if len(source) > 1 else source[0]
        else:
            values = source
        return [values.get(expression, "0") for expression in expressions]


class Online:
    def __init__(self, behavior: dict, events: list[tuple]) -> None:
        self.behavior = behavior
        self.events = events
        self.auth_fallback_modes = None
        self.instances: list[OnlineApplication] = []

    def create_online_application(self, application):
        self.events.append((application.label, "online.create"))
        item = OnlineApplication(application.label, self.behavior, self.events)
        self.instances.append(item)
        return item


class Target:
    def __init__(self, enrollment: dict, events: list[tuple], behavior: dict) -> None:
        self.enrollment = dict(enrollment)
        self.events = events
        self.behavior = behavior
        self.reconciliation = {
            "target_fingerprint": enrollment["fingerprint"],
            "runtime_release_digest": "sha256:" + "0" * 64,
            "application_state": "unknown",
            "forced_values_present": False,
            "physical_outputs_energized": False,
        }

    def probe(self, application):
        self.events.append((application.label, "target.probe"))
        return dict(self.enrollment)

    def current_application(self):
        self.events.append(("current", "application"))
        return Application("current")

    def verify_running_artifact(self, online_application, runtime_release_digest):
        self.events.append(
            (online_application.label, "artifact.verify", runtime_release_digest)
        )
        return not self.behavior.get("artifact_mismatch", False)

    def reconcile(self, candidate_runtime_release_digest, rollback_runtime_release_digest):
        self.events.append(
            (
                "target",
                "reconcile",
                candidate_runtime_release_digest,
                rollback_runtime_release_digest,
            )
        )
        return dict(self.reconciliation)


class Fixture:
    def __init__(
        self,
        root: Path,
        module,
        *,
        behavior: dict | None = None,
        worker_id: str | None = None,
        side_effect_callback=None,
    ) -> None:
        self.root = root
        self.module = module
        self.clock = Clock()
        self.events: list[tuple] = []
        self.behavior = behavior or {}
        self.enrollment = {
            "schema_version": "1",
            "record_type": "TargetEnrollment",
            "status": "enrolled",
            "captured_at": self.clock.stamp(-60),
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
        self.target = Target(self.enrollment, self.events, self.behavior)
        self.candidate_raw = b"candidate-project\n"
        self.rollback_raw = b"rollback-project\n"
        self.candidate_digest = self.module._sha256_bytes(self.candidate_raw)
        self.rollback_digest = self.module._sha256_bytes(self.rollback_raw)
        self.candidate_runtime_release_digest = "sha256:" + "5" * 64
        self.rollback_runtime_release_digest = "sha256:" + "6" * 64
        self.target.reconciliation["runtime_release_digest"] = (
            self.candidate_runtime_release_digest
        )
        self.projects = Projects(self.candidate_digest, self.rollback_digest, self.events)
        self.online = Online(self.behavior, self.events)
        for name in ("inputs", "plans", "artifacts", "workspaces", "results", "evidence", "observations"):
            (root / name).mkdir(parents=True, exist_ok=True)
        self._register_artifact(self.candidate_digest, self.candidate_raw, "candidate")
        self._register_artifact(self.rollback_digest, self.rollback_raw, "rollback")
        self.worker = module.AxDeploymentWorker(
            str(root),
            ENVIRONMENT,
            self.enrollment,
            ["PLC_PRG.FaultCode", "PLC_PRG.RestartRequired"],
            self.projects,
            self.online,
            self.target,
            ChangeOptions,
            ApplicationStates,
            VersionUpdateFlags,
            CredentialSources,
            worker_id=worker_id,
            side_effect_callback=side_effect_callback,
            now=self.clock.now,
            sleeper=self.clock.sleep,
        )

    def _write_json(self, relative: str, value: dict) -> tuple[Path, bytes]:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = self.module._json_bytes(value)
        path.write_bytes(raw)
        return path, raw

    def _descriptor(self, role: str, relative: str, raw: bytes) -> dict:
        return {"role": role, "path": relative, "sha256": self.module._sha256_bytes(raw)}

    def archive_request(self, request: dict) -> Path:
        path = self.root / "archive" / f"{request['correlation_id']}.request.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.module._json_bytes(request))
        return path

    def _register_artifact(self, digest: str, raw: bytes, label: str) -> None:
        relative = f"artifacts/{digest[7:]}.project"
        (self.root / relative).write_bytes(raw)
        workspace = self.root / "workspaces" / str(uuid.uuid4())
        workspace.mkdir()
        manifest = {
            "schema_version": "1",
            "frozen_artifacts": [
                {
                    "artifact_digest": digest,
                    "environment_digest": ENVIRONMENT,
                    "build_digest": "sha256:" + ("8" if label == "candidate" else "7") * 64,
                    "runtime_release_digest": (
                        self.candidate_runtime_release_digest
                        if label == "candidate"
                        else self.rollback_runtime_release_digest
                    ),
                    "path": relative,
                    "status": "READY_FOR_DEPLOY",
                    "label": label,
                }
            ],
        }
        (workspace / "manifest.json").write_bytes(self.module._json_bytes(manifest))

    def profile(self) -> dict:
        return {
            "schema_version": "1",
            "record_type": "ObservationProfile",
            "profile_id": "universal-plc.lab-v1",
            "expressions": ["PLC_PRG.FaultCode", "PLC_PRG.RestartRequired"],
            "interval_ms": 100,
            "duration_seconds": 1,
            "fault_invariants": [
                {
                    "expression": "PLC_PRG.FaultCode",
                    "operator": "eq",
                    "expected": 0,
                    "action": "stop_and_rollback",
                },
                {
                    "expression": "PLC_PRG.RestartRequired",
                    "operator": "eq",
                    "expected": False,
                    "action": "stop_and_rollback",
                },
            ],
        }

    def deployment_request(
        self,
        *,
        target: str = TARGET,
        plan_created_offset: int = -10,
        valid_for_seconds: int = 600,
        issued_offset: int = 0,
    ) -> dict:
        plan_id = str(uuid.uuid4())
        challenge = str(uuid.uuid4())
        created = self.clock.stamp(plan_created_offset)
        expires = self.clock.stamp(plan_created_offset + valid_for_seconds)
        profile = self.profile()
        profile_raw = self.module._json_bytes(profile)
        observation_digest = self.module._sha256_bytes(profile_raw)
        plan = {
            "schema_version": "1",
            "record_type": "DeploymentPlan",
            "plan_id": plan_id,
            "created_at": created,
            "expires_at": expires,
            "valid_for_seconds": valid_for_seconds,
            "artifact_digest": self.candidate_digest,
            "build_digest": "sha256:" + "8" * 64,
            "runtime_release_digest": self.candidate_runtime_release_digest,
            "target_fingerprint": target,
            "rollback_digest": self.rollback_digest,
            "rollback_runtime_release_digest": self.rollback_runtime_release_digest,
            "observation_digest": observation_digest,
            "challenge_nonce": challenge,
            "authority_digest": None,
            "one_shot": True,
            "state": "prepared",
            "reconciliation_required": False,
        }
        plan_path, plan_raw = self._write_json(f"plans/{plan_id}.plan.json", plan)
        del plan_path
        profile_path, profile_raw = self._write_json(f"plans/{plan_id}.observation.json", profile)
        del profile_path
        issued = self.clock.stamp(issued_offset)
        binding = {
            "plan_id": plan_id,
            "artifact_digest": self.candidate_digest,
            "target_fingerprint": target,
            "rollback_digest": self.rollback_digest,
            "runtime_release_digest": self.candidate_runtime_release_digest,
            "rollback_runtime_release_digest": self.rollback_runtime_release_digest,
            "observation_digest": observation_digest,
            "challenge_nonce": challenge,
            "phrase_digest": self.module.PHRASE_DIGEST,
            "issued_at": issued,
            "expires_at": expires,
            "single_use": True,
        }
        authority_digest = self.module._sha256_bytes(self.module._json_bytes(binding))
        authority = dict(binding)
        authority.update(
            {
                "schema_version": "1",
                "record_type": "DeploymentAuthority",
                "status": "consumed",
                "consumed_at": issued,
                "authority_digest": authority_digest,
            }
        )
        authority_path, authority_raw = self._write_json(f"plans/{plan_id}.consumed.json", authority)
        del authority_path
        job_id = str(uuid.uuid4())
        execution = {
            "schema_version": "1",
            "record_type": "DeploymentExecuteInput",
            "plan_id": plan_id,
            "plan_digest": self.module._sha256_bytes(plan_raw),
            "artifact_digest": self.candidate_digest,
            "runtime_release_digest": self.candidate_runtime_release_digest,
            "target_fingerprint": target,
            "rollback_digest": self.rollback_digest,
            "rollback_runtime_release_digest": self.rollback_runtime_release_digest,
            "expires_at": expires,
            "challenge_nonce": challenge,
            "observation_digest": observation_digest,
            "authority_digest": authority_digest,
        }
        execution_path, execution_raw = self._write_json(f"inputs/{job_id}.deployment.json", execution)
        del execution_path
        return {
            "protocol": "AXQ-FILE/2",
            "message_type": "request",
            "correlation_id": str(uuid.uuid4()),
            "job_id": job_id,
            "operation": "deployment.execute",
            "created_at": issued,
            "deadline_at": self.clock.stamp(issued_offset + 330),
            "environment_digest": ENVIRONMENT,
            "workspace_digest": None,
            "expected_revision": None,
            "authorization_digest": authority_digest,
            "artifacts": [
                self._descriptor("input", f"inputs/{job_id}.deployment.json", execution_raw),
                self._descriptor("input", f"plans/{plan_id}.plan.json", plan_raw),
                self._descriptor("observation", f"plans/{plan_id}.observation.json", profile_raw),
                self._descriptor("authorization", f"plans/{plan_id}.consumed.json", authority_raw),
            ],
        }

    def observation_request(self, operation: str, payload: dict, *, job_id: str | None = None) -> dict:
        job_id = job_id or str(uuid.uuid4())
        relative = f"inputs/{job_id}.{operation.replace('.', '-')}.json"
        _path, raw = self._write_json(relative, payload)
        return {
            "protocol": "AXQ-FILE/2",
            "message_type": "request",
            "correlation_id": str(uuid.uuid4()),
            "job_id": job_id,
            "operation": operation,
            "created_at": self.clock.stamp(),
            "deadline_at": self.clock.stamp(300 if operation == "target.observe.start" else 30),
            "environment_digest": ENVIRONMENT,
            "workspace_digest": None,
            "expected_revision": None,
            "authorization_digest": None,
            "artifacts": [self._descriptor("observation", relative, raw)],
        }

    def reconciliation_request(self, original_job_id: str) -> dict:
        job_id = str(uuid.uuid4())
        relative = f"inputs/{job_id}.deployment-reconcile.json"
        payload = {
            "schema_version": "1",
            "record_type": "DeploymentReconcileInput",
            "original_job_id": original_job_id,
            "target_fingerprint": TARGET,
            "artifact_digest": self.candidate_digest,
            "rollback_digest": self.rollback_digest,
        }
        _path, raw = self._write_json(relative, payload)
        return {
            "protocol": "AXQ-FILE/2",
            "message_type": "request",
            "correlation_id": str(uuid.uuid4()),
            "job_id": job_id,
            "operation": "deployment.reconcile",
            "created_at": self.clock.stamp(),
            "deadline_at": self.clock.stamp(60),
            "environment_digest": ENVIRONMENT,
            "workspace_digest": None,
            "expected_revision": None,
            "authorization_digest": None,
            "artifacts": [self._descriptor("input", relative, raw)],
        }


class AxDeploymentWorkerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_worker()

    def test_atomic_publish_and_append_are_byte_exact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            immutable = root / "result.json"
            raw = self.module._json_bytes({"message": "line one\nline two"})
            self.module._atomic_publish(str(immutable), raw)
            self.assertEqual(immutable.read_bytes(), raw)
            self.assertFalse(immutable.read_bytes().endswith(b"\r\n"))

            journal = root / "observe.jsonl"
            self.module._append_json_line(str(journal), {"sample": 1})
            expected = self.module._json_bytes({"sample": 1})
            self.assertEqual(journal.read_bytes(), expected)
            self.assertFalse(journal.read_bytes().endswith(b"\r\n"))

    def test_source_exposes_only_bounded_installed_scriptengine_calls(self) -> None:
        source = WORKER_PATH.read_text(encoding="utf-8")
        compile(source, str(WORKER_PATH), "exec")
        self.assertEqual(
            self.module.SUPPORTED_OPERATIONS,
            (
                "deployment.execute",
                "deployment.reconcile",
                "target.observe.start",
                "target.observe.read",
                "target.observe.stop",
            ),
        )
        for forbidden in (
            "eval(",
            "exec(",
            "subprocess",
            "perform_network_scan(",
            "set_prepared_value(",
            "write_prepared_values(",
            "force_prepared_values(",
            "reset_origin(",
        ):
            self.assertNotIn(forbidden, source)
        self.assertIn("TEXT_TYPES = (basestring,)", source)
        self.assertIn("INTEGER_TYPES = (int, long)", source)
        self.assertNotIn("isinstance(value, str)", source)

    def test_loaded_script_engine_identity_is_bound_to_exact_assembly_path(self) -> None:
        class Assembly:
            def __init__(self, location: str) -> None:
                self.Location = location

        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "ScriptEngine3.dll"
            other = Path(tmp) / "Other.dll"
            engine.write_bytes(b"SCRIPT-ENGINE\n")
            other.write_bytes(b"OTHER\n")
            digest = self.module._sha256_file(str(engine.resolve()))
            self.assertEqual(
                self.module._verify_loaded_script_engine(
                    str(engine.resolve()), digest, [Assembly(str(engine.resolve()))]
                ),
                str(engine.resolve()),
            )
            with self.assertRaisesRegex(self.module.WorkerError, "not loaded"):
                self.module._verify_loaded_script_engine(
                    str(engine.resolve()), digest, [Assembly(str(other.resolve()))]
                )

    def test_observation_invariant_expected_is_strict_json_scalar(self) -> None:
        for invalid in (None, [], {}, float("nan"), float("inf")):
            with self.subTest(expected=repr(invalid)):
                profile = {
                    "schema_version": "1",
                    "record_type": "ObservationProfile",
                    "profile_id": "strict.scalar-v1",
                    "expressions": ["PLC_PRG.FaultCode"],
                    "interval_ms": 100,
                    "duration_seconds": 1,
                    "fault_invariants": [
                        {
                            "expression": "PLC_PRG.FaultCode",
                            "operator": "eq",
                            "expected": invalid,
                            "action": "record",
                        }
                    ],
                }
                with self.assertRaises(self.module.WorkerError):
                    self.module._validate_profile(profile)

    def test_exact_authority_target_lkg_download_start_observe_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), self.module)
            request = fixture.deployment_request()
            result = fixture.worker.process_request(request)
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(set(result), RESULT_FIELDS)
            self.assertEqual(result["workspace_digest"], None)
            self.assertEqual(result["revision"], None)
            logins = [event for event in fixture.events if len(event) > 2 and event[1] == "login"]
            self.assertEqual(
                [(item[0], item[2], item[3]) for item in logins],
                [("candidate", ChangeOptions.Keep, False), ("candidate", ChangeOptions.Never, False)],
            )
            self.assertIn(("candidate", "start"), fixture.events)
            self.assertNotIn(("candidate", "stop"), fixture.events)
            marker = json.loads(
                (
                    Path(tmp)
                    / "state"
                    / "side-effects"
                    / f"{request['job_id']}.started.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(marker["job_id"], request["job_id"])
            self.assertEqual(marker["operation"], "deployment.execute")
            self.assertEqual(marker["environment_digest"], ENVIRONMENT)
            evidence = json.loads((Path(tmp) / result["artifacts"][0]["path"]).read_text(encoding="utf-8"))
            self.assertEqual(evidence["outcome"], "succeeded")
            self.assertFalse(evidence["reconciliation_required"])
            self.assertGreaterEqual(evidence["sample_count"], 9)
            self.assertEqual(len(list((Path(tmp) / "state" / "deployment-authorities").glob("*.json"))), 1)

    def test_300_second_observation_fits_only_the_330_second_execution_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), self.module)
            original_profile = fixture.profile

            def long_profile():
                profile = original_profile()
                profile["interval_ms"] = 500
                profile["duration_seconds"] = 300
                return profile

            fixture.profile = long_profile
            started = fixture.clock.value
            result = fixture.worker.process_request(fixture.deployment_request())
            self.assertEqual(result["status"], "succeeded")
            self.assertGreaterEqual(fixture.clock.value - started, 300)

        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), self.module)
            request = fixture.deployment_request()
            request["deadline_at"] = fixture.clock.stamp(331)
            with self.assertRaisesRegex(self.module.WorkerError, "deadline window"):
                fixture.worker.process_request(request)
            self.assertFalse(fixture.events)

        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), self.module)
            original_profile = fixture.profile

            def long_profile_with_spent_budget():
                profile = original_profile()
                profile["interval_ms"] = 500
                profile["duration_seconds"] = 300
                return profile

            fixture.profile = long_profile_with_spent_budget
            request = fixture.deployment_request()
            fixture.clock.value += 11
            result = fixture.worker.process_request(request)
            self.assertEqual(result["status"], "blocked")
            self.assertFalse(
                any(
                    len(event) > 2
                    and event[1] == "login"
                    and event[2] == ChangeOptions.Never
                    for event in fixture.events
                )
            )

        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), self.module)
            request = fixture.observation_request(
                "target.observe.start",
                {
                    "target_fingerprint": TARGET,
                    "observation_profile": fixture.profile(),
                },
            )
            request["deadline_at"] = fixture.clock.stamp(301)
            with self.assertRaisesRegex(self.module.WorkerError, "deadline window"):
                fixture.worker.process_request(request)

    def test_wrong_target_missing_lkg_and_authority_mismatch_block_before_login(self) -> None:
        cases = ("wrong-target", "missing-lkg", "authority-mismatch")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                fixture = Fixture(Path(tmp), self.module)
                request = fixture.deployment_request(target=("sha256:" + "9" * 64) if case == "wrong-target" else TARGET)
                if case == "missing-lkg":
                    (Path(tmp) / "artifacts" / f"{fixture.rollback_digest[7:]}.project").unlink()
                elif case == "authority-mismatch":
                    request["authorization_digest"] = "sha256:" + "a" * 64
                result = fixture.worker.process_request(request)
                self.assertEqual(result["status"], "blocked")
                self.assertFalse(any(len(event) > 1 and event[1] == "login" for event in fixture.events))

    def test_forced_value_blocks_after_keep_and_before_full_download(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), self.module, behavior={"forced": ["PLC_PRG.Test"]})
            result = fixture.worker.process_request(fixture.deployment_request())
            self.assertEqual(result["status"], "blocked")
            logins = [event for event in fixture.events if len(event) > 2 and event[1] == "login"]
            self.assertEqual([item[2] for item in logins], [ChangeOptions.Keep])
            self.assertNotIn(("candidate", "start"), fixture.events)

    def test_candidate_build_digest_mismatch_blocks_before_target_contact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), self.module)
            request = fixture.deployment_request()
            plan_descriptor = next(
                item
                for item in request["artifacts"]
                if item["role"] == "input" and item["path"].endswith(".plan.json")
            )
            plan_path = Path(tmp) / plan_descriptor["path"]
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["build_digest"] = "sha256:" + "9" * 64
            plan_raw = self.module._json_bytes(plan)
            plan_path.write_bytes(plan_raw)
            plan_descriptor["sha256"] = self.module._sha256_bytes(plan_raw)
            execution_descriptor = next(
                item
                for item in request["artifacts"]
                if item["role"] == "input" and item["path"].endswith(".deployment.json")
            )
            execution_path = Path(tmp) / execution_descriptor["path"]
            execution = json.loads(execution_path.read_text(encoding="utf-8"))
            execution["plan_digest"] = self.module._sha256_bytes(plan_raw)
            execution_raw = self.module._json_bytes(execution)
            execution_path.write_bytes(execution_raw)
            execution_descriptor["sha256"] = self.module._sha256_bytes(execution_raw)
            result = fixture.worker.process_request(request)
            self.assertEqual(result["status"], "blocked")
            self.assertFalse(fixture.events)

    def test_expired_consumed_authority_blocks_without_target_contact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), self.module)
            request = fixture.deployment_request(
                plan_created_offset=-11,
                valid_for_seconds=10,
                issued_offset=-2,
            )
            result = fixture.worker.process_request(request)
            self.assertEqual(result["status"], "blocked")
            self.assertFalse(fixture.events)

    def test_post_download_target_identity_uncertainty_is_unknown_not_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), self.module)
            calls = {"count": 0}

            def changing_probe(application):
                fixture.events.append((application.label, "target.probe"))
                calls["count"] += 1
                live = dict(fixture.enrollment)
                if calls["count"] >= 2:
                    live["device_identity"] = "UNEXPECTED"
                return live

            fixture.target.probe = changing_probe
            result = fixture.worker.process_request(fixture.deployment_request())
            self.assertEqual(result["status"], "unknown_outcome")

    def test_running_artifact_mismatch_after_download_is_unknown_and_never_started(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), self.module, behavior={"artifact_mismatch": True})
            result = fixture.worker.process_request(fixture.deployment_request())
            self.assertEqual(result["status"], "unknown_outcome")
            self.assertFalse(
                any(event[:2] == ("candidate", "start") for event in fixture.events)
            )
            evidence = json.loads(
                (Path(tmp) / result["artifacts"][0]["path"]).read_text(encoding="utf-8")
            )
            self.assertTrue(evidence["reconciliation_required"])
            evidence = json.loads(
                (Path(tmp) / result["artifacts"][0]["path"]).read_text(encoding="utf-8")
            )
            self.assertTrue(evidence["reconciliation_required"])

    def test_fault_stops_candidate_and_verifies_last_known_good_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            behavior = {
                "samples": {
                    "candidate": {
                        "PLC_PRG.FaultCode": "7",
                        "PLC_PRG.RestartRequired": "FALSE",
                    }
                }
            }
            fixture = Fixture(Path(tmp), self.module, behavior=behavior)
            result = fixture.worker.process_request(fixture.deployment_request())
            self.assertEqual(result["status"], "failed")
            self.assertIn(("candidate", "stop"), fixture.events)
            self.assertIn(("rollback", "start"), fixture.events)
            rollback_logins = [
                item for item in fixture.events if len(item) > 2 and item[0] == "rollback" and item[1] == "login"
            ]
            self.assertEqual(
                [item[2:] for item in rollback_logins],
                [(ChangeOptions.Keep, False), (ChangeOptions.Never, False)],
            )
            evidence = json.loads((Path(tmp) / result["artifacts"][0]["path"]).read_text(encoding="utf-8"))
            self.assertEqual(evidence["outcome"], "rolled_back")
            self.assertFalse(evidence["reconciliation_required"])

    def test_connection_loss_after_mutation_is_unknown_no_retry_and_read_only_reconciles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), self.module, behavior={"fail_candidate_download": True})
            request = fixture.deployment_request()
            result = fixture.worker.process_request(request)
            self.assertEqual(result["status"], "unknown_outcome")
            fixture.archive_request(request)
            evidence = json.loads((Path(tmp) / result["artifacts"][0]["path"]).read_text(encoding="utf-8"))
            self.assertTrue(evidence["reconciliation_required"])
            never_before = sum(
                1 for event in fixture.events if len(event) > 2 and event[1] == "login" and event[2] == ChangeOptions.Never
            )
            replay = fixture.deployment_request()
            replay["artifacts"] = request["artifacts"]
            replay["authorization_digest"] = request["authorization_digest"]
            replay_result = fixture.worker.process_request(replay)
            self.assertEqual(replay_result["status"], "blocked")
            never_after = sum(
                1 for event in fixture.events if len(event) > 2 and event[1] == "login" and event[2] == ChangeOptions.Never
            )
            self.assertEqual(never_after, never_before)
            fixture.target.reconciliation.update(
                {
                    "runtime_release_digest": fixture.candidate_runtime_release_digest,
                    "application_state": "run",
                }
            )
            reconcile_request = fixture.reconciliation_request(request["job_id"])
            reconciled = fixture.worker.process_request(reconcile_request)
            self.assertEqual(reconciled["status"], "succeeded")
            records = list(
                (Path(tmp) / "state" / "reconciliations").glob(
                    f"{request['job_id']}.*.json"
                )
            )
            self.assertEqual(len(records), 1)
            record = json.loads(records[0].read_text(encoding="utf-8"))
            self.assertEqual(record["outcome"], "succeeded")
            self.assertFalse(record["reconciliation_required"])

    def test_side_effect_callback_failure_blocks_before_never_login(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            def fail_marker(job_id, worker_id, environment_digest):
                del job_id, worker_id, environment_digest
                raise OSError("durable marker unavailable")

            fixture = Fixture(
                Path(tmp),
                self.module,
                worker_id=str(uuid.uuid4()),
                side_effect_callback=fail_marker,
            )
            request = fixture.deployment_request()
            result = fixture.worker.process_request(request)
            self.assertEqual(result["status"], "failed")
            self.assertFalse(
                any(
                    len(event) > 2
                    and event[1] == "login"
                    and event[2] == ChangeOptions.Never
                    for event in fixture.events
                )
            )
            self.assertFalse(
                (
                    Path(tmp)
                    / "state"
                    / "side-effects"
                    / f"{request['job_id']}.started.json"
                ).exists()
            )

    def test_real_adapter_marker_drives_crash_recovery_to_unknown_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = FileJobQueue(root)
            worker_id = str(uuid.uuid4())
            fixture = Fixture(
                root,
                self.module,
                behavior={"crash_candidate_download": True},
                worker_id=worker_id,
                side_effect_callback=queue.mark_side_effect_started,
            )
            fixture.clock.value = time.time()
            request = fixture.deployment_request()
            queued = queue.submit(request)
            claimed = queue.claim_next()
            self.assertIsNotNone(claimed)
            claimed_request, raw = claimed
            with self.assertRaises(SimulatedWorkerCrash):
                fixture.worker.process_request(claimed_request, raw)

            marker_path = (
                root / "state" / "side-effects" / f"{queued['job_id']}.started.json"
            )
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            self.assertEqual(marker["worker_id"], worker_id)
            self.assertEqual(marker["operation"], "deployment.execute")
            self.assertFalse((root / "results" / f"{queued['job_id']}.result.json").exists())

            recovered = queue.recover()
            self.assertEqual(recovered["unknown_outcome"], 1)
            self.assertEqual(recovered["requeued"], 0)
            self.assertEqual(queue.status(queued["job_id"])["state"], "unknown_outcome")

    def test_standalone_fallback_marker_drives_crash_recovery_to_unknown_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = FileJobQueue(root)
            worker_id = str(uuid.uuid4())
            fixture = Fixture(
                root,
                self.module,
                behavior={"crash_candidate_download": True},
                worker_id=worker_id,
                side_effect_callback=None,
            )
            fixture.clock.value = time.time()
            request = fixture.deployment_request()
            queued = queue.submit(request)
            claimed = queue.claim_next()
            self.assertIsNotNone(claimed)
            claimed_request, raw = claimed
            with self.assertRaises(SimulatedWorkerCrash):
                fixture.worker.process_request(claimed_request, raw)

            marker_path = (
                root / "state" / "side-effects" / f"{queued['job_id']}.started.json"
            )
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            self.assertEqual(marker["worker_id"], worker_id)
            self.assertEqual(marker["operation"], "deployment.execute")
            self.assertFalse((root / "results" / f"{queued['job_id']}.result.json").exists())

            recovered = queue.recover()
            self.assertEqual(recovered["unknown_outcome"], 1)
            self.assertEqual(recovered["requeued"], 0)
            self.assertEqual(queue.status(queued["job_id"])["state"], "unknown_outcome")

    def test_recovered_deployment_crash_has_strict_evidence_and_can_reconcile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = FileJobQueue(root)
            worker_id = str(uuid.uuid4())
            fixture = Fixture(
                root,
                self.module,
                behavior={"crash_candidate_download": True},
                worker_id=worker_id,
                side_effect_callback=queue.mark_side_effect_started,
            )
            fixture.clock.value = time.time()
            queued = queue.submit(fixture.deployment_request())
            claimed = queue.claim_next()
            self.assertIsNotNone(claimed)
            claimed_request, raw = claimed
            with self.assertRaises(SimulatedWorkerCrash):
                fixture.worker.process_request(claimed_request, raw)

            recovered = queue.recover()
            self.assertEqual(recovered["unknown_outcome"], 1)
            result_path = root / "results" / f"{queued['job_id']}.result.json"
            recovered_result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(len(recovered_result["artifacts"]), 1)
            descriptor = recovered_result["artifacts"][0]
            evidence = json.loads((root / descriptor["path"]).read_text(encoding="utf-8"))
            self.assertEqual(evidence["record_type"], "AxDeploymentWorkerEvidence")
            self.assertEqual(evidence["candidate_digest"], fixture.candidate_digest)
            self.assertEqual(evidence["rollback_digest"], fixture.rollback_digest)
            self.assertTrue(evidence["reconciliation_required"])

            fixture.behavior.pop("crash_candidate_download")
            fixture.target.reconciliation.update(
                {
                    "runtime_release_digest": fixture.candidate_runtime_release_digest,
                    "application_state": "run",
                }
            )
            reconciliation_request = fixture.reconciliation_request(queued["job_id"])
            reconciliation_result = fixture.worker.process_request(reconciliation_request)
            self.assertEqual(reconciliation_result["status"], "succeeded")
            fixture.archive_request(reconciliation_request)
            runtime = self.module.AxDeploymentRuntime(
                str(root),
                fixture.worker,
                self._runtime_config(worker_id),
                "sha256:" + ("b" * 64),
            )
            self.assertTrue(
                runtime._terminal_reconciliation_exists(
                    queued["job_id"], str(result_path)
                )
            )

    def test_host_launch_uncertainty_recovery_reconciles_without_deployment_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = FileJobQueue(root)
            worker_id = str(uuid.uuid4())
            fixture = Fixture(root, self.module, worker_id=worker_id)
            fixture.clock.value = time.time()
            request = fixture.deployment_request()
            queued = queue.submit(request)
            request_raw = self.module._json_bytes(request)
            provenance_path = (
                root
                / "state"
                / "deployment-launch-uncertainty"
                / f"{queued['job_id']}.json"
            )
            provenance_path.parent.mkdir(parents=True, exist_ok=True)
            provenance_path.write_bytes(
                self.module._json_bytes(
                    {
                        "schema_version": "1",
                        "record_type": "DeploymentLaunchUncertainty",
                        "created_at": fixture.clock.stamp(),
                        "correlation_id": request["correlation_id"],
                        "job_id": request["job_id"],
                        "operation": "deployment.execute",
                        "environment_digest": ENVIRONMENT,
                        "target_fingerprint": TARGET,
                        "request_digest": self.module._sha256_bytes(request_raw),
                        "launch_state": "unknown",
                    }
                )
            )
            self.assertIsNotNone(queue.claim_next())
            recovered = queue.recover()
            self.assertEqual(recovered["unknown_outcome"], 1)
            self.assertEqual(recovered["requeued"], 0)
            self.assertFalse((root / "state" / "deployments" / request["job_id"]).exists())

            fixture.target.reconciliation.update(
                {
                    "runtime_release_digest": fixture.candidate_runtime_release_digest,
                    "application_state": "run",
                }
            )
            reconciliation_request = fixture.reconciliation_request(request["job_id"])
            reconciliation_result = fixture.worker.process_request(reconciliation_request)
            self.assertEqual(reconciliation_result["status"], "succeeded")
            fixture.archive_request(reconciliation_request)
            record_path = next((root / "state" / "reconciliations").glob("*.json"))
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(record["provenance_head_kind"], "launch_uncertainty")
            self.assertEqual(
                record["journal_head_digest"], self.module._sha256_file(str(provenance_path))
            )
            self.assertEqual(
                [event[0:2] for event in fixture.events], [("target", "reconcile")]
            )
            runtime = self.module.AxDeploymentRuntime(
                str(root),
                fixture.worker,
                self._runtime_config(worker_id),
                "sha256:" + ("b" * 64),
            )
            self.assertTrue(
                runtime._terminal_reconciliation_exists(
                    request["job_id"],
                    str(root / "results" / f"{request['job_id']}.result.json"),
                )
            )
            valid_provenance = provenance_path.read_bytes()
            tampered = json.loads(valid_provenance.decode("utf-8"))
            tampered["launch_state"] = "started"
            provenance_path.write_bytes(self.module._json_bytes(tampered))
            with self.assertRaisesRegex(
                self.module.WorkerError, "unsupported DeploymentLaunchUncertainty"
            ):
                runtime._terminal_reconciliation_exists(
                    request["job_id"],
                    str(root / "results" / f"{request['job_id']}.result.json"),
                )

    def test_pre_intent_journal_and_launch_uncertainty_form_one_composite_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = FileJobQueue(root)
            worker_id = str(uuid.uuid4())
            fixture = Fixture(root, self.module, worker_id=worker_id)
            fixture.clock.value = time.time()
            request = fixture.deployment_request()
            queued = queue.submit(request)
            request_raw = self.module._json_bytes(request)
            provenance_path = (
                root
                / "state"
                / "deployment-launch-uncertainty"
                / f"{queued['job_id']}.json"
            )
            provenance_path.parent.mkdir(parents=True, exist_ok=True)
            provenance_path.write_bytes(
                self.module._json_bytes(
                    {
                        "schema_version": "1",
                        "record_type": "DeploymentLaunchUncertainty",
                        "created_at": fixture.clock.stamp(),
                        "correlation_id": request["correlation_id"],
                        "job_id": request["job_id"],
                        "operation": "deployment.execute",
                        "environment_digest": ENVIRONMENT,
                        "target_fingerprint": TARGET,
                        "request_digest": self.module._sha256_bytes(request_raw),
                        "launch_state": "unknown",
                    }
                )
            )
            fixture.worker._journal(
                request,
                "authority-claimed",
                {
                    "authority_digest": request["authorization_digest"],
                    "reconciliation_required": False,
                },
            )
            journal_path = next(
                (root / "state" / "deployments" / request["job_id"]).glob("*.json")
            )
            self.assertIsNotNone(queue.claim_next())
            self.assertEqual(queue.recover()["unknown_outcome"], 1)

            fixture.target.reconciliation.update(
                {
                    "runtime_release_digest": fixture.candidate_runtime_release_digest,
                    "application_state": "run",
                }
            )
            reconciliation_request = fixture.reconciliation_request(request["job_id"])
            result = fixture.worker.process_request(reconciliation_request)
            self.assertEqual(result["status"], "succeeded")
            fixture.archive_request(reconciliation_request)
            record = json.loads(
                next((root / "state" / "reconciliations").glob("*.json")).read_text(
                    encoding="utf-8"
                )
            )
            expected = self.module._sha256_bytes(
                self.module._json_bytes(
                    {
                        "journal_head_digest": self.module._sha256_file(str(journal_path)),
                        "launch_uncertainty_digest": self.module._sha256_file(
                            str(provenance_path)
                        ),
                    }
                )
            )
            self.assertEqual(record["provenance_head_kind"], "pre_intent_journal")
            self.assertEqual(record["journal_head_digest"], expected)
            runtime = self.module.AxDeploymentRuntime(
                str(root),
                fixture.worker,
                self._runtime_config(worker_id),
                "sha256:" + ("b" * 64),
            )
            self.assertTrue(
                runtime._terminal_reconciliation_exists(
                    request["job_id"],
                    str(root / "results" / f"{request['job_id']}.result.json"),
                )
            )

    def test_preflight_prefix_is_composite_and_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = FileJobQueue(root)
            worker_id = str(uuid.uuid4())
            fixture = Fixture(root, self.module, worker_id=worker_id)
            fixture.clock.value = time.time()
            request = fixture.deployment_request()
            queued = queue.submit(request)
            request_raw = self.module._json_bytes(request)
            provenance_path = (
                root
                / "state"
                / "deployment-launch-uncertainty"
                / f"{queued['job_id']}.json"
            )
            provenance_path.parent.mkdir(parents=True, exist_ok=True)
            provenance_path.write_bytes(
                self.module._json_bytes(
                    {
                        "schema_version": "1",
                        "record_type": "DeploymentLaunchUncertainty",
                        "created_at": fixture.clock.stamp(),
                        "correlation_id": request["correlation_id"],
                        "job_id": request["job_id"],
                        "operation": "deployment.execute",
                        "environment_digest": ENVIRONMENT,
                        "target_fingerprint": TARGET,
                        "request_digest": self.module._sha256_bytes(request_raw),
                        "launch_state": "unknown",
                    }
                )
            )
            fixture.worker._journal(
                request,
                "authority-claimed",
                {
                    "authority_digest": request["authorization_digest"],
                    "reconciliation_required": False,
                },
            )
            fixture.worker._journal(
                request,
                "target-preflight-passed",
                {"forced_values_present": False},
            )
            journal_paths = sorted(
                (root / "state" / "deployments" / request["job_id"]).glob("*.json")
            )
            self.assertIsNotNone(queue.claim_next())
            self.assertEqual(queue.recover()["unknown_outcome"], 1)

            fixture.target.reconciliation.update(
                {
                    "runtime_release_digest": fixture.candidate_runtime_release_digest,
                    "application_state": "run",
                }
            )
            reconciliation_request = fixture.reconciliation_request(request["job_id"])
            result = fixture.worker.process_request(reconciliation_request)
            self.assertEqual(result["status"], "succeeded")
            fixture.archive_request(reconciliation_request)
            record = json.loads(
                next((root / "state" / "reconciliations").glob("*.json")).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(record["provenance_head_kind"], "pre_intent_journal")
            expected_journal_digest = self.module._validate_deployment_journal(
                [str(path) for path in journal_paths], request["job_id"]
            )
            expected = self.module._sha256_bytes(
                self.module._json_bytes(
                    {
                        "journal_head_digest": expected_journal_digest,
                        "launch_uncertainty_digest": self.module._sha256_file(
                            str(provenance_path)
                        ),
                    }
                )
            )
            self.assertEqual(record["journal_head_digest"], expected)

            second = json.loads(journal_paths[1].read_text(encoding="utf-8"))
            second["details"]["forced_values_present"] = True
            journal_paths[1].write_bytes(self.module._json_bytes(second))
            runtime = self.module.AxDeploymentRuntime(
                str(root),
                fixture.worker,
                self._runtime_config(worker_id),
                "sha256:" + ("b" * 64),
            )
            with self.assertRaisesRegex(
                self.module.WorkerError, "pre-intent journal binding mismatch"
            ):
                runtime._terminal_reconciliation_exists(
                    request["job_id"],
                    str(root / "results" / f"{request['job_id']}.result.json"),
                )

    def test_marker_to_started_journal_crash_remains_reconcilable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = FileJobQueue(root)
            worker_id = str(uuid.uuid4())
            fixture = Fixture(
                root,
                self.module,
                worker_id=worker_id,
                side_effect_callback=queue.mark_side_effect_started,
            )
            fixture.clock.value = time.time()
            original_journal = fixture.worker._journal

            def crash_before_started_event(request, event, details=None):
                if event == "candidate-full-download-started":
                    raise SimulatedWorkerCrash("process died after durable marker")
                return original_journal(request, event, details)

            fixture.worker._journal = crash_before_started_event
            queued = queue.submit(fixture.deployment_request())
            claimed = queue.claim_next()
            self.assertIsNotNone(claimed)
            with self.assertRaises(SimulatedWorkerCrash):
                fixture.worker.process_request(*claimed)
            journal_paths = list(
                (root / "state" / "deployments" / queued["job_id"]).glob("*.json")
            )
            self.assertTrue(
                any("candidate-full-download-intent" in path.name for path in journal_paths)
            )
            self.assertFalse(
                any(
                    "full-download-started" in path.name
                    for path in journal_paths
                )
            )
            self.assertEqual(queue.recover()["unknown_outcome"], 1)

            fixture.worker._journal = original_journal
            fixture.target.reconciliation.update(
                {
                    "runtime_release_digest": fixture.candidate_runtime_release_digest,
                    "application_state": "run",
                }
            )
            reconciliation_request = fixture.reconciliation_request(queued["job_id"])
            self.assertEqual(
                fixture.worker.process_request(reconciliation_request)["status"],
                "succeeded",
            )

    def test_reconciliation_result_publish_crash_is_finalized_without_second_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = FileJobQueue(root)
            worker_id = str(uuid.uuid4())
            fixture = Fixture(
                root,
                self.module,
                behavior={"fail_candidate_download": True},
                worker_id=worker_id,
            )
            fixture.clock.value = time.time()
            original_request = fixture.deployment_request()
            original_result = fixture.worker.process_request(original_request)
            self.assertEqual(original_result["status"], "unknown_outcome")
            fixture.archive_request(original_request)
            fixture.target.reconciliation.update(
                {
                    "runtime_release_digest": fixture.candidate_runtime_release_digest,
                    "application_state": "run",
                }
            )
            reconciliation_request = fixture.reconciliation_request(
                original_request["job_id"]
            )
            queued = queue.submit(reconciliation_request)
            claimed = queue.claim_next()
            self.assertIsNotNone(claimed)
            original_complete = fixture.worker._complete_reconciliation_transaction

            def crash_before_terminal(*args, **kwargs):
                raise SimulatedWorkerCrash("process died after durable result")

            fixture.worker._complete_reconciliation_transaction = crash_before_terminal
            with self.assertRaises(SimulatedWorkerCrash):
                fixture.worker.process_request(*claimed)
            result_path = root / "results" / f"{queued['job_id']}.result.json"
            self.assertTrue(result_path.is_file())
            self.assertEqual(
                len(list((root / "state" / "reconciliation-prepared").glob("*.json"))),
                1,
            )
            self.assertFalse((root / "state" / "reconciliations").exists())
            target_probe_count = sum(
                event[0:2] == ("target", "reconcile") for event in fixture.events
            )

            fixture.worker._complete_reconciliation_transaction = original_complete
            recovered = queue.recover()
            self.assertEqual(recovered["requeued"], 1)
            runtime = self.module.AxDeploymentRuntime(
                str(root),
                fixture.worker,
                self._runtime_config(worker_id),
                "sha256:" + ("b" * 64),
            )
            self.assertTrue(runtime.process_next())
            self.assertEqual(
                sum(event[0:2] == ("target", "reconcile") for event in fixture.events),
                target_probe_count,
            )
            self.assertEqual(
                len(list((root / "state" / "reconciliations").glob("*.json"))),
                1,
            )
            self.assertIsNotNone(queue._matching_request(queued["job_id"], "archive"))

    def test_missing_runtime_marker_is_durable_unknown_not_failed_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = Fixture(
                root,
                self.module,
                behavior={"fail_candidate_download": True},
                worker_id=str(uuid.uuid4()),
            )
            original_request = fixture.deployment_request()
            original_result = fixture.worker.process_request(original_request)
            self.assertEqual(original_result["status"], "unknown_outcome")
            fixture.archive_request(original_request)
            fixture.target.reconciliation.update(
                {
                    "target_fingerprint": None,
                    "runtime_release_digest": None,
                    "application_state": "unknown",
                    "forced_values_present": True,
                    "physical_outputs_energized": True,
                }
            )
            reconciliation_request = fixture.reconciliation_request(
                original_request["job_id"]
            )
            result = fixture.worker.process_request(reconciliation_request)
            self.assertEqual(result["status"], "unknown_outcome")
            evidence = json.loads(
                (root / result["artifacts"][0]["path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(evidence["outcome"], "unknown_outcome")
            self.assertTrue(evidence["reconciliation_required"])
            prepared = list(
                (root / "state" / "reconciliation-prepared").glob("*.json")
            )
            self.assertEqual(len(prepared), 1)
            record = json.loads(prepared[0].read_text(encoding="utf-8"))
            self.assertIsNone(record["observed"]["target_fingerprint"])
            self.assertIsNone(record["observed"]["runtime_release_digest"])
            self.assertFalse((root / "state" / "reconciliations").exists())

    def _runtime_config(self, worker_id: str) -> dict:
        return {
            "schema_version": "1",
            "worker_id": worker_id,
            "ide_executable": str(WORKER_PATH),
            "ide_executable_sha256": "sha256:" + ("5" * 64),
            "script_engine": str(WORKER_PATH),
            "script_engine_sha256": "sha256:" + ("6" * 64),
            "ide_profile": "DIADesigner-AX 1.9",
            "worker_script": str(WORKER_PATH),
            "worker_script_sha256": "sha256:" + ("7" * 64),
            "target_adapter": str(WORKER_PATH),
            "target_adapter_sha256": "sha256:" + ("8" * 64),
            "autonomous_config": str(WORKER_PATH),
            "autonomous_config_sha256": "sha256:" + ("9" * 64),
            "target_enrollment": str(WORKER_PATH),
            "target_enrollment_sha256": "sha256:" + ("a" * 64),
            "environment_digest": ENVIRONMENT,
            "target_fingerprint": TARGET,
            "observation_expression_allowlist": [
                "PLC_PRG.FaultCode",
                "PLC_PRG.RestartRequired",
            ],
            "heartbeat_seconds": 1,
            "lease_timeout_seconds": 10,
            "max_runtime_seconds": 330,
            "max_requests": 20,
            "idle_exit_seconds": 5,
            "startup_timeout_seconds": 30,
        }

    def test_runtime_claims_only_target_jobs_and_leaves_project_jobs_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker_id = str(uuid.uuid4())
            fixture = Fixture(root, self.module, worker_id=worker_id)
            fixture.clock.value = time.time()
            queue = FileJobQueue(root)
            target_request = fixture.deployment_request()
            queued = queue.submit(target_request)
            project_correlation = str(uuid.uuid4())
            project_path = root / "inbox" / f"{project_correlation}.request.json"
            project_path.write_text(
                json.dumps(
                    {
                        "protocol": "AXQ-FILE/2",
                        "message_type": "request",
                        "correlation_id": project_correlation,
                        "job_id": str(uuid.uuid4()),
                        "operation": "project.build",
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            runtime = self.module.AxDeploymentRuntime(
                str(root),
                fixture.worker,
                self._runtime_config(worker_id),
                "sha256:" + ("b" * 64),
                now=fixture.clock.now,
                sleeper=fixture.clock.sleep,
            )

            self.assertTrue(runtime.process_next())
            self.assertTrue(project_path.is_file())
            self.assertTrue((root / "results" / f"{queued['job_id']}.result.json").is_file())
            self.assertIsNotNone(queue._matching_request(queued["job_id"], "archive"))
            self.assertFalse(runtime.process_next())

    def test_runtime_rejects_observation_it_cannot_outlive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker_id = str(uuid.uuid4())
            fixture = Fixture(root, self.module, worker_id=worker_id)
            fixture.clock.value = time.time()
            profile = fixture.profile()
            profile["duration_seconds"] = 300
            for invariant in profile["fault_invariants"]:
                invariant["action"] = "record"
            request = fixture.observation_request(
                "target.observe.start",
                {"target_fingerprint": TARGET, "observation_profile": profile},
            )
            queue = FileJobQueue(root)
            queue.submit(request)
            runtime = self.module.AxDeploymentRuntime(
                str(root),
                fixture.worker,
                self._runtime_config(worker_id),
                "sha256:" + ("b" * 64),
                now=fixture.clock.now,
                sleeper=fixture.clock.sleep,
            )
            runtime.started = fixture.clock.now() - 20
            with self.assertRaisesRegex(
                self.module.WorkerError, "cannot cover observation duration"
            ):
                runtime.process_next()
            self.assertEqual(queue.status(request["job_id"])["state"], "queued")
            self.assertFalse(fixture.events)
            fresh = self.module.AxDeploymentRuntime(
                str(root),
                fixture.worker,
                self._runtime_config(worker_id),
                "sha256:" + ("b" * 64),
                now=fixture.clock.now,
                sleeper=fixture.clock.sleep,
            )
            self.assertTrue(fresh.process_next())
            self.assertEqual(queue.status(request["job_id"])["state"], "succeeded")
            lifecycle = sorted(
                (root / "state" / "observations" / request["job_id"]).glob("*.json")
            )
            self.assertEqual([path.name for path in lifecycle], ["00000001-running.json"])

    def test_runtime_crash_after_marker_leaves_running_for_unknown_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker_id = str(uuid.uuid4())
            fixture = Fixture(
                root,
                self.module,
                behavior={"crash_candidate_download": True},
                worker_id=worker_id,
                side_effect_callback=None,
            )
            fixture.clock.value = time.time()
            queue = FileJobQueue(root)
            queued = queue.submit(fixture.deployment_request())
            runtime = self.module.AxDeploymentRuntime(
                str(root),
                fixture.worker,
                self._runtime_config(worker_id),
                "sha256:" + ("b" * 64),
                now=fixture.clock.now,
                sleeper=fixture.clock.sleep,
            )

            with self.assertRaises(SimulatedWorkerCrash):
                runtime.process_next()
            self.assertIsNotNone(queue._matching_request(queued["job_id"], "running"))
            recovered = queue.recover()
            self.assertEqual(recovered["unknown_outcome"], 1)
            self.assertEqual(recovered["requeued"], 0)

    def test_runtime_blocks_prequeued_second_deployment_after_unknown_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker_id = str(uuid.uuid4())
            fixture = Fixture(
                root,
                self.module,
                behavior={"fail_candidate_download": True},
                worker_id=worker_id,
                side_effect_callback=None,
            )
            fixture.clock.value = time.time()
            queue = FileJobQueue(root)
            first = queue.submit(fixture.deployment_request())
            second = queue.submit(fixture.deployment_request())
            runtime = self.module.AxDeploymentRuntime(
                str(root),
                fixture.worker,
                self._runtime_config(worker_id),
                "sha256:" + ("b" * 64),
                now=fixture.clock.now,
                sleeper=fixture.clock.sleep,
            )

            self.assertTrue(runtime.process_next())
            self.assertEqual(queue.status(first["job_id"])["state"], "unknown_outcome")
            with self.assertRaisesRegex(self.module.WorkerError, "unresolved"):
                runtime.process_next()
            self.assertEqual(queue.status(second["job_id"])["state"], "queued")
            self.assertIsNone(queue._matching_request(second["job_id"], "running"))

    def test_runtime_rejects_partial_terminal_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker_id = str(uuid.uuid4())
            fixture = Fixture(
                root,
                self.module,
                behavior={"fail_candidate_download": True},
                worker_id=worker_id,
            )
            original_request = fixture.deployment_request()
            original_result = fixture.worker.process_request(original_request)
            self.assertEqual(original_result["status"], "unknown_outcome")
            fixture.archive_request(original_request)
            result_path = root / "results" / f"{original_request['job_id']}.result.json"
            fixture.target.reconciliation.update(
                {
                    "runtime_release_digest": fixture.candidate_runtime_release_digest,
                    "application_state": "run",
                }
            )
            reconciliation_request = fixture.reconciliation_request(
                original_request["job_id"]
            )
            reconciliation_result = fixture.worker.process_request(
                reconciliation_request
            )
            self.assertEqual(reconciliation_result["status"], "succeeded")
            fixture.archive_request(reconciliation_request)
            reconciliation_path = next(
                (root / "state" / "reconciliations").glob(
                    f"{original_request['job_id']}.*.json"
                )
            )
            record = json.loads(reconciliation_path.read_text(encoding="utf-8"))
            valid_record_raw = reconciliation_path.read_bytes()
            record["observed"] = {"application_state": "run"}
            reconciliation_path.write_bytes(self.module._json_bytes(record))
            runtime = self.module.AxDeploymentRuntime(
                str(root),
                fixture.worker,
                self._runtime_config(worker_id),
                "sha256:" + ("b" * 64),
            )
            with self.assertRaisesRegex(self.module.WorkerError, "semantic mismatch"):
                runtime._terminal_reconciliation_exists(
                    original_request["job_id"], str(result_path)
                )

            reconciliation_path.write_bytes(valid_record_raw)
            reconciliation_result_path = (
                root / "results" / f"{reconciliation_request['job_id']}.result.json"
            )
            valid_result_raw = reconciliation_result_path.read_bytes()
            reconciliation_result_path.write_bytes(
                self.module._json_bytes(
                    {
                        "job_id": reconciliation_request["job_id"],
                        "operation": "deployment.reconcile",
                        "status": "succeeded",
                        "environment_digest": ENVIRONMENT,
                    }
                )
            )
            with self.assertRaisesRegex(self.module.WorkerError, "result shape"):
                runtime._terminal_reconciliation_exists(
                    original_request["job_id"], str(result_path)
                )
            reconciliation_result_path.write_bytes(valid_result_raw)
            self.assertTrue(
                runtime._terminal_reconciliation_exists(
                    original_request["job_id"], str(result_path)
                )
            )
            journal_entries = sorted(
                (root / "state" / "deployments" / original_request["job_id"]).glob(
                    "*.json"
                )
            )
            first_event = json.loads(journal_entries[0].read_text(encoding="utf-8"))
            first_event["details"]["tampered_but_schema_valid"] = True
            journal_entries[0].write_bytes(self.module._json_bytes(first_event))
            with self.assertRaisesRegex(self.module.WorkerError, "semantic mismatch"):
                runtime._terminal_reconciliation_exists(
                    original_request["job_id"], str(result_path)
                )

    def test_runtime_lease_is_separate_from_project_worker_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker_id = str(uuid.uuid4())
            fixture = Fixture(root, self.module, worker_id=worker_id)
            config_digest = "sha256:" + ("b" * 64)
            runtime = self.module.AxDeploymentRuntime(
                str(root),
                fixture.worker,
                self._runtime_config(worker_id),
                config_digest,
                process_alive=lambda _pid: False,
                now=fixture.clock.now,
                sleeper=fixture.clock.sleep,
            )
            self.assertEqual(runtime.acquire_lease(), "acquired")
            self.assertTrue((root / "state" / "deployment-worker.lease.json").is_file())
            self.assertFalse((root / "state" / "worker.lease.json").exists())
            runtime.release_lease()
            self.assertFalse((root / "state" / "deployment-worker.lease.json").exists())

    def test_runtime_restart_uses_collision_free_immutable_heartbeats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker_id = str(uuid.uuid4())
            fixture = Fixture(root, self.module, worker_id=worker_id)
            config = self._runtime_config(worker_id)
            digest = "sha256:" + ("b" * 64)
            first = self.module.AxDeploymentRuntime(
                str(root), fixture.worker, config, digest, process_alive=lambda _pid: False
            )
            self.assertEqual(first.acquire_lease(), "acquired")
            first.release_lease()
            second = self.module.AxDeploymentRuntime(
                str(root), fixture.worker, config, digest, process_alive=lambda _pid: False
            )
            self.assertEqual(second.acquire_lease(), "acquired")
            second.release_lease()
            heartbeats = list(
                (root / "state" / "deployment-heartbeats" / worker_id).glob("*.json")
            )
            self.assertEqual(len(heartbeats), 2)
            self.assertNotEqual(heartbeats[0].name, heartbeats[1].name)

    def test_runtime_never_steals_unknown_owner_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker_id = str(uuid.uuid4())
            fixture = Fixture(root, self.module, worker_id=worker_id)
            config = self._runtime_config(worker_id)
            digest = "sha256:" + ("b" * 64)
            runtime = self.module.AxDeploymentRuntime(
                str(root), fixture.worker, config, digest, process_alive=lambda _pid: None
            )
            lease = {
                "protocol": "AXQ-FILE/2",
                "worker_id": worker_id,
                "pid": 9999,
                "process_start_fingerprint": "sha256:" + ("c" * 64),
                "started_at": "2026-01-01T00:00:00Z",
                "heartbeat_at": "2026-01-01T00:00:00Z",
                "environment_digest": ENVIRONMENT,
                "target_fingerprint": TARGET,
                "config_digest": digest,
                "sequence": 0,
            }
            lease_path = root / "state" / "deployment-worker.lease.json"
            lease_path.write_text(json.dumps(lease, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(self.module.WorkerError, "owner state is unknown"):
                runtime.acquire_lease()
            self.assertTrue(lease_path.is_file())

    def test_runtime_factory_executes_only_after_lease_is_acquired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker_id = str(uuid.uuid4())
            fixture = Fixture(root, self.module, worker_id=worker_id)
            fixture.clock.value = time.time()
            config = self._runtime_config(worker_id)
            config["idle_exit_seconds"] = 0.05
            observed = []

            def factory():
                observed.append((root / "state" / "deployment-worker.lease.json").is_file())
                return fixture.worker

            runtime = self.module.AxDeploymentRuntime(
                str(root),
                None,
                config,
                "sha256:" + ("b" * 64),
                worker_factory=factory,
                process_alive=lambda _pid: False,
                now=fixture.clock.now,
                sleeper=fixture.clock.sleep,
            )
            result = runtime.run_loop()
            self.assertEqual(observed, [True])
            self.assertEqual(result["reason"], "idle_exit")
            self.assertFalse((root / "state" / "deployment-worker.lease.json").exists())

    def test_heartbeat_pump_runs_while_request_thread_can_be_busy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker_id = str(uuid.uuid4())
            fixture = Fixture(root, self.module, worker_id=worker_id)
            config = self._runtime_config(worker_id)
            config["heartbeat_seconds"] = 0.01
            runtime = self.module.AxDeploymentRuntime(
                str(root),
                fixture.worker,
                config,
                "sha256:" + ("b" * 64),
                process_alive=lambda _pid: False,
            )
            runtime.acquire_lease()
            runtime._start_heartbeat_pump()
            time.sleep(0.05)
            runtime._stop_heartbeat_pump()
            runtime.release_lease()
            heartbeats = list(
                (root / "state" / "deployment-heartbeats" / worker_id).glob("*.json")
            )
            self.assertGreaterEqual(len(heartbeats), 3)
            records = [json.loads(path.read_text(encoding="utf-8")) for path in heartbeats]
            self.assertTrue(
                all(item["process_start_fingerprint"] for item in records)
            )

    def test_observe_start_read_stop_is_read_only_and_stop_never_stops_plc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), self.module)
            profile = fixture.profile()
            for invariant in profile["fault_invariants"]:
                invariant["action"] = "record"
            start_request = fixture.observation_request(
                "target.observe.start",
                {"target_fingerprint": TARGET, "observation_profile": profile},
            )
            start = fixture.worker.process_request(start_request)
            self.assertEqual(start["status"], "succeeded")
            observed = fixture.worker.read_observation(start_request["job_id"])
            self.assertEqual(observed["state"], "running")
            self.assertEqual(observed["sample_count"], 1)
            stop_request = fixture.observation_request(
                "target.observe.stop", {"observation_id": start_request["job_id"]}
            )
            stopped = fixture.worker.process_request(stop_request)
            self.assertEqual(stopped["status"], "succeeded")
            self.assertEqual(fixture.worker.read_observation(start_request["job_id"])["state"], "stopped")
            lifecycle = sorted(
                (fixture.root / "state" / "observations" / start_request["job_id"]).glob("*.json")
            )
            self.assertEqual(
                [path.name for path in lifecycle],
                ["00000001-running.json", "00000002-stopped.json"],
            )
            fixture.worker._sessions = {}
            self.assertEqual(
                fixture.worker.read_observation(start_request["job_id"])["state"],
                "stopped",
            )
            self.assertFalse(any(len(event) > 1 and event[1] == "stop" for event in fixture.events))
            observation_logins = [event for event in fixture.events if len(event) > 2 and event[1] == "login"]
            self.assertEqual([item[2] for item in observation_logins], [ChangeOptions.Keep])

    def test_active_observation_is_durably_marked_worker_lost_on_worker_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), self.module, worker_id=str(uuid.uuid4()))
            profile = fixture.profile()
            for invariant in profile["fault_invariants"]:
                invariant["action"] = "record"
            request = fixture.observation_request(
                "target.observe.start",
                {"target_fingerprint": TARGET, "observation_profile": profile},
            )
            self.assertEqual(fixture.worker.process_request(request)["status"], "succeeded")
            fixture.worker.close_active_observations()
            fixture.worker._sessions = {}
            observed = fixture.worker.read_observation(request["job_id"])
            self.assertEqual(observed["state"], "worker_lost")
            lifecycle = sorted(
                (fixture.root / "state" / "observations" / request["job_id"]).glob("*.json")
            )
            self.assertEqual(
                [path.name for path in lifecycle],
                ["00000001-running.json", "00000002-worker_lost.json"],
            )
            self.assertFalse(
                any(len(event) > 1 and event[1] == "stop" for event in fixture.events)
            )

    def test_standalone_stop_and_rollback_profile_is_rejected_before_target_contact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(
                Path(tmp),
                self.module,
                behavior={
                    "samples": {
                        "current": {
                            "PLC_PRG.FaultCode": "1",
                            "PLC_PRG.RestartRequired": "FALSE",
                        }
                    }
                },
            )
            request = fixture.observation_request(
                "target.observe.start",
                {"target_fingerprint": TARGET, "observation_profile": fixture.profile()},
            )
            result = fixture.worker.process_request(request)
            self.assertEqual(result["status"], "blocked")
            self.assertIn("stop_and_rollback", result["diagnostics"][0]["message"])
            self.assertFalse(any(len(event) > 1 and event[1] == "stop" for event in fixture.events))
            self.assertFalse(any(event[0] == "rollback" for event in fixture.events))

    def test_non_allowlisted_observation_expression_blocks_before_target_contact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), self.module)
            profile = fixture.profile()
            profile["expressions"].append("PLC_PRG.SecretValue")
            request = fixture.observation_request(
                "target.observe.start",
                {"target_fingerprint": TARGET, "observation_profile": profile},
            )
            result = fixture.worker.process_request(request)
            self.assertEqual(result["status"], "blocked")
            self.assertFalse(fixture.events)

    def test_malformed_or_traversal_artifact_fails_without_result_or_target_contact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), self.module)
            request = fixture.deployment_request()
            request["artifacts"][0]["path"] = "../escape.json"
            with self.assertRaises(self.module.WorkerError):
                fixture.worker.process_request(request)
            self.assertFalse((Path(tmp) / "results" / f"{request['job_id']}.result.json").exists())
            self.assertFalse(fixture.events)


if __name__ == "__main__":
    unittest.main()
