from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from . import PACKAGE_ROOT

WORKER_PATH = PACKAGE_ROOT / "adapters" / "ax_worker" / "worker.py"
HELPER_PATH = PACKAGE_ROOT / "adapters" / "ax_worker" / "ladder_import.py"
RUNTIME_RELEASE_DIGEST = "sha256:" + "a" * 64
SECOND_RUNTIME_RELEASE_DIGEST = "sha256:" + "b" * 64
THIRD_RUNTIME_RELEASE_DIGEST = "sha256:" + "c" * 64
REQUEST_FIELDS = {
    "protocol", "message_type", "correlation_id", "job_id", "operation",
    "created_at", "deadline_at", "environment_digest", "workspace_digest",
    "expected_revision", "authorization_digest", "artifacts",
}
RESULT_FIELDS = {
    "protocol", "message_type", "correlation_id", "job_id", "operation",
    "created_at", "status", "environment_digest", "workspace_digest",
    "request_digest", "before_project_digest", "after_project_digest",
    "revision", "artifacts", "diagnostics",
}


def _load_worker():
    spec = importlib.util.spec_from_file_location("ax_worker_test", WORKER_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load AX worker")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class TextDocument:
    def __init__(self, text: str, on_replace=None) -> None:
        self.text = text
        self.on_replace = on_replace

    def replace(self, *, new_text: str) -> None:
        self.text = new_text
        if self.on_replace is not None:
            self.on_replace()


class Node:
    def __init__(self, name: str, children: list[object] | None = None) -> None:
        self.name = name
        self.type = "SIM_TYPE"
        self.guid = "SIM_" + name
        self.is_folder = bool(children)
        self.is_application = False
        self._children = children or []

    def get_name(self) -> str:
        return self.name

    def get_children(self, _recursive: bool) -> list[object]:
        return list(self._children)


class Source(Node):
    def __init__(
        self,
        name: str,
        declaration: str = "",
        implementation: str = "",
        *,
        project=None,
        is_gvl: bool = False,
    ) -> None:
        super().__init__(name)
        self.project = project
        self.is_global_variable_list = is_gvl
        self.textual_declaration = TextDocument(declaration, self._mark_dirty)
        self.textual_implementation = TextDocument(implementation, self._mark_dirty)

    def _mark_dirty(self) -> None:
        if self.project is not None:
            self.project.dirty = True


class Application(Node):
    def __init__(self, project: "Project") -> None:
        super().__init__("Application")
        self.is_application = True
        self.project = project
        self.build_calls = 0
        self.rebuild_calls = 0
        self.rebuild_error: Exception | None = None
        self.last_create_pou: dict[str, object] | None = None

    def build(self) -> None:
        self.build_calls += 1
        self.project.owner.build_calls += 1
        self.project.owner.events.append(("build",))

    def rebuild(self) -> None:
        self.rebuild_calls += 1
        self.project.owner.rebuild_calls += 1
        self.project.owner.events.append(("rebuild",))
        if self.rebuild_error is not None:
            raise self.rebuild_error

    def _create(self, name: str, *, is_gvl: bool = False) -> Source:
        item = Source(name, project=self.project, is_gvl=is_gvl)
        self._children.append(item)
        self.project.sources.append(item)
        self.project.dirty = True
        return item

    def create_pou(
        self, *, name: str, type: object, language: object,
        return_type: str | None, base_type: str | None, interfaces: str | None,
    ) -> Source:
        self.last_create_pou = {
            "name": name, "type": type, "language": language,
            "return_type": return_type, "base_type": base_type, "interfaces": interfaces,
        }
        self.project.create_pou_calls.append(dict(self.last_create_pou))
        return self._create(name)

    def create_gvl(self, *, name: str) -> Source:
        self.project.create_gvl_calls.append({"name": name})
        self.project.owner.events.append(("create_gvl", name))
        return self._create(name, is_gvl=True)


class Project(Node):
    def __init__(
        self, owner: "Projects", path: Path, store: list[dict[str, object]], create_pou_calls: list[dict[str, object]],
        create_gvl_calls: list[dict[str, object]],
    ) -> None:
        self.owner = owner
        self.path = str(path)
        self.store = store
        self.create_pou_calls = create_pou_calls
        self.create_gvl_calls = create_gvl_calls
        self.dirty = False
        self.closed = False
        self.sources = [
            Source(
                str(x["name"]),
                str(x["declaration"]),
                str(x["implementation"]),
                project=self,
                is_gvl=bool(x.get("is_gvl", False)),
            )
            for x in store
        ]
        self.application = Application(self)
        self.application._children.extend(self.sources)
        self.active_application = self.application
        super().__init__("Scratch", [self.application])

    def find(self, name: str, _recursive: bool) -> list[object]:
        return [source for source in self.sources if source.name == name]

    def save(self) -> None:
        self.store[:] = [
            {
                "name": x.name,
                "declaration": x.textual_declaration.text,
                "implementation": x.textual_implementation.text,
                "is_gvl": x.is_global_variable_list,
            }
            for x in self.sources
        ]
        self.owner.events.append(("save",))
        if self.dirty:
            Path(self.path).write_bytes(Path(self.path).read_bytes() + b"S")
        self.owner.snapshots[Path(self.path).read_bytes()] = [
            dict(item) for item in self.store
        ]
        self.dirty = False

    def save_as(self, path: str) -> None:
        self.path = path
        self.save()

    def close(self) -> None:
        self.closed = True
        self.owner.events.append(("close",))
        self.owner.primary = None

    def export_xml(self, **kwargs: object) -> None:
        Path(str(kwargs["path"])).write_text("<project />", encoding="utf-8")


class Projects:
    def __init__(self, path: Path, *, dirty_on_open: bool = False, rebuild_error: Exception | None = None) -> None:
        self.path = path
        self.primary: Project | None = None
        self.store: list[dict[str, object]] = []
        self.create_pou_calls: list[dict[str, object]] = []
        self.create_gvl_calls: list[dict[str, object]] = []
        self.events: list[tuple[object, ...]] = []
        self.build_calls = 0
        self.rebuild_calls = 0
        self.snapshots = {path.read_bytes(): []}
        self.dirty_on_open = dirty_on_open
        self.rebuild_error = rebuild_error

    def open(self, path: str, primary: bool = True) -> Project:
        assert primary is True
        assert Path(path) == self.path
        self.events.append(("open",))
        raw = self.path.read_bytes()
        if raw in self.snapshots:
            self.store[:] = [dict(item) for item in self.snapshots[raw]]
        self.primary = Project(
            self, self.path, self.store, self.create_pou_calls, self.create_gvl_calls
        )
        self.primary.dirty = self.dirty_on_open
        self.primary.application.rebuild_error = self.rebuild_error
        return self.primary


class Message:
    def __init__(self, severity: str = "Warning", text: str = "simulated warning") -> None:
        self.severity = severity
        self.text = text
        self.source = "Application"
        self.line = 1
        self.column = 2
        self.prefix = "C"
        self.number = 100


class System:
    executable_filename = "DIADesigner-AX.exe"
    version = "1.9.0.9194"

    def __init__(
        self,
        messages: list[Message] | None = None,
        *,
        summary_errors: int | None = None,
        summary_warnings: int | None = None,
    ) -> None:
        self.messages = list(messages or [])
        if summary_errors is None:
            summary_errors = sum(
                "error" in message.severity.lower() for message in self.messages
            )
        if summary_warnings is None:
            summary_warnings = sum(
                "warning" in message.severity.lower() for message in self.messages
            )
        self.messages.append(
            Message(
                "Info",
                "Compile complete -- %d errors, %d warnings"
                % (summary_errors, summary_warnings),
            )
        )

    def get_message_objects(self, _category: object | None = None) -> list[Message]:
        return list(self.messages)

    def get_message_categories(self, _active: bool) -> list[str]:
        return ["Build"]

    def clear_messages(self, _category: object) -> None:
        return None

    def delay(self, milliseconds: int) -> None:
        time.sleep(milliseconds / 1000)


class CompilerFailureSystem(System):
    def __init__(self) -> None:
        super().__init__([])
        self.message_reads = 0

    def get_message_objects(self, _category: object | None = None) -> list[Message]:
        self.message_reads += 1
        if self.message_reads == 1:
            return list(self.messages)
        return [
            Message("Error", "simulated compiler failure"),
            Message("Info", "Compile complete -- 1 error, 0 warnings"),
        ]


class CompilerWarningSystem(CompilerFailureSystem):
    def get_message_objects(self, _category: object | None = None) -> list[Message]:
        self.message_reads += 1
        if self.message_reads == 1:
            return list(self.messages)
        return [
            Message("Warning", "simulated compiler warning"),
            Message("Info", "Compile complete -- 0 errors, 1 warning"),
        ]


class DiagnosticReadFailureSystem(System):
    def __init__(self) -> None:
        super().__init__()
        self.message_reads = 0

    def get_message_objects(self, _category: object | None = None) -> list[Message]:
        self.message_reads += 1
        if self.message_reads == 1:
            return list(self.messages)
        raise RuntimeError("simulated diagnostic read failure")


class MissingBuildSummarySystem(System):
    def __init__(self) -> None:
        super().__init__()
        self.message_reads = 0

    def get_message_objects(self, _category: object | None = None) -> list[Message]:
        self.message_reads += 1
        if self.message_reads == 1:
            return list(self.messages)
        return []


class DiagnosticClearFailureSystem(System):
    def __init__(self) -> None:
        super().__init__()
        self.clear_calls = 0

    def clear_messages(self, _category: object) -> None:
        self.clear_calls += 1
        if self.clear_calls > 1:
            raise RuntimeError("simulated diagnostic clear failure")


class SimulatedHardCrash(BaseException):
    pass


class Languages:
    st = "ST_GUID"


class PouTypes:
    Program = "POU_PROGRAM"
    FunctionBlock = "POU_FUNCTION_BLOCK"
    Function = "POU_FUNCTION"


class WorkerFixture:
    def __init__(self, root: Path, max_requests: int = 50, idle_exit: int = 1) -> None:
        self.root = root
        self.workspace = root / "workspaces" / "scratch.project"
        self.workspace.parent.mkdir(parents=True)
        self.workspace.write_bytes(b"BASELINE")
        self.worker_id = str(uuid.uuid4())
        self.environment = "sha256:" + "1" * 64
        self.config = {
            "protocol": "AXQ-FILE/2", "config_version": "2", "worker_id": self.worker_id,
            "environment_digest": self.environment, "workspace_project": "workspaces/scratch.project",
            "initial_project_sha256": _digest(b"BASELINE"),
            "ide_executable_sha256": "sha256:" + "2" * 64,
            "script_engine_sha256": "sha256:" + "3" * 64,
            "ladder_helper_sha256": _digest(HELPER_PATH.read_bytes()),
            "ide_profile": "DIADesigner-AX 1.9",
            "allowed_operations": [
                "environment.probe", "workspace.create", "workspace.status",
                "project.tree.read", "project.inventory.read", "source.read",
                "source.upsert.st", "project.build", "project.export.native", "artifact.freeze",
            ],
            "heartbeat_seconds": 1, "lease_timeout_seconds": 5, "max_request_seconds": 30,
            "max_runtime_seconds": 60, "max_requests": max_requests, "idle_exit_seconds": idle_exit,
        }
        (root / "worker-config.json").write_bytes(_json_bytes(self.config))
        self.workspace_digest = self.config["initial_project_sha256"]

    def project_digest(self) -> str:
        return _digest(self.workspace.read_bytes())

    def request(
        self, operation: str, *, payload: dict[str, object] | None = None,
        correlation: str | None = None, job_id: str | None = None,
        workspace_digest: str | None = None, expected_revision: int | None = None,
        deadline_offset: int = 20,
    ) -> tuple[str, str]:
        correlation = correlation or str(uuid.uuid4())
        job_id = job_id or str(uuid.uuid4())
        artifacts: list[dict[str, str]] = []
        if payload is not None:
            relative = f"inputs/{job_id}.json"
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            raw = _json_bytes(payload)
            path.write_bytes(raw)
            artifacts.append({"role": "input", "path": relative, "sha256": _digest(raw)})
        created = time.time()
        value = {
            "protocol": "AXQ-FILE/2", "message_type": "request",
            "correlation_id": correlation, "job_id": job_id, "operation": operation,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created)),
            "deadline_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created + deadline_offset)),
            "environment_digest": self.environment, "workspace_digest": workspace_digest,
            "expected_revision": expected_revision, "authorization_digest": None, "artifacts": artifacts,
        }
        self.publish(value)
        return correlation, job_id

    def publish(self, value: dict[str, object]) -> None:
        inbox = self.root / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        correlation = str(value["correlation_id"])
        (inbox / f"{correlation}.request.json").write_bytes(_json_bytes(value))


class AxWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_worker()
        self.module.OPTIONAL_EMPTY_SLOT_BASELINE_COUNT = 21
        self.module.OPTIONAL_EMPTY_SLOT_SECOND_BASELINE_SHA256 = "sha256:" + "9" * 64

    def _make_worker(
        self, root: Path, fixture: WorkerFixture, *, projects: Projects | None = None,
        system: System | None = None,
    ):
        projects = projects or Projects(fixture.workspace)
        worker = self.module.AxWorker(
            str(root), projects_api=projects, system_api=system or System(),
            languages_api=Languages(), pou_types_api=PouTypes(),
        )
        return worker, projects

    def _result(self, root: Path, job_id: str) -> dict[str, object]:
        return json.loads(
            (root / "results" / f"{job_id}.result.json").read_text(encoding="utf-8")
        )

    def test_category_counts_retain_environment_warnings_and_other_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WorkerFixture(root)
            worker, _ = self._make_worker(root, fixture)
            def diagnostic(severity, text, category):
                value = worker._diagnostic(severity, "AX.MESSAGE", text)
                value["category"] = category
                return value
            diagnostics = [
                diagnostic("warning", "Device description for '<Empty>' is missing", "environment"),
                diagnostic("info", "Compile complete -- 0 errors, 0 warnings", "compiler"),
                diagnostic("error", "Additional code check failed", "analysis"),
            ]
            # The compiler summary reconciles, but aggregate severities still
            # reject the build. Reordering categories cannot hide a failure.
            for messages in (diagnostics, list(reversed(diagnostics))):
                self.assertEqual(worker._verified_build_counts(messages), (1, 1))
            self.assertEqual(diagnostics[0]["severity"], "warning")

    def test_category_compiler_mismatch_and_incomplete_context_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WorkerFixture(root)
            worker, _ = self._make_worker(root, fixture)
            summary = worker._diagnostic("info", "C.MESSAGE", "Compile complete -- 0 errors, 0 warnings")
            summary["category"] = "compiler"
            error = worker._diagnostic("error", "C.123", "real compiler error")
            error["category"] = "compiler"
            with self.assertRaises(self.module.VendorCapabilityError):
                worker._verified_build_counts([summary, error])
            del error["category"]
            with self.assertRaises(self.module.VendorCapabilityError):
                worker._verified_build_counts([summary, error])
            with self.assertRaises(self.module.VendorCapabilityError):
                worker._verified_build_counts([summary, dict(summary)])

    def test_preflight_retains_native_category_warning_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WorkerFixture(root)
            categories = {
                "environment": [Message("Warning", "Device description for '<Empty>' is missing") for _ in range(21)],
                "compiler": [Message("Info", "Compile complete -- 0 errors, 0 warnings")],
            }
            system = System()
            system.get_message_categories = lambda _active: list(categories)
            system.get_message_objects = lambda category=None: list(categories[category])
            worker, _ = self._make_worker(root, fixture, system=system)
            _, job = fixture.request("workspace.create")
            worker.process_next()
            result = self._result(root, job)
            self.assertEqual(result["status"], "blocked")
            payload = json.loads((root / result["artifacts"][0]["path"]).read_text(encoding="utf-8"))
            self.assertEqual(len(payload["diagnostics"]), 22)
            self.assertEqual(payload["diagnostics"][0]["category"], "environment")
            self.assertEqual(payload["diagnostics"][0]["severity"], "warning")
            self.assertTrue(any("unresolved <Empty>" in item for item in payload["blockers"]))
            self.assertFalse(any("completion counts" in item for item in payload["blockers"]))

    def test_preflight_preserves_diagnostics_when_compiler_counts_disagree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WorkerFixture(root)
            system = System([Message("Error", "retained compiler error")], summary_errors=0)
            worker, _ = self._make_worker(root, fixture, system=system)
            _, job = fixture.request("workspace.create")
            worker.process_next()
            result = self._result(root, job)
            payload = json.loads((root / result["artifacts"][0]["path"]).read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(payload["diagnostics"][0]["message"], "retained compiler error")
            self.assertTrue(any("completion counts" in item for item in payload["blockers"]))

    def test_optional_category_contract_is_bounded_and_backward_compatible(self) -> None:
        from axmcp_offline.protocol_v2 import validate_diagnostics
        from axmcp_offline.security import ProtocolViolation
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WorkerFixture(root)
            worker, _ = self._make_worker(root, fixture)
            value = worker._diagnostic("warning", "AX.MESSAGE", "retained warning")
            validate_diagnostics([value])
            value["category"] = "compiler"
            validate_diagnostics([value])
            for invalid in ("", "x" * 129, None, 1):
                value["category"] = invalid
                with self.assertRaises(ProtocolViolation):
                    validate_diagnostics([value])

    def test_atomic_publish_is_byte_exact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "result.json"
            raw = self.module._json_bytes({"message": "line one\nline two"})
            self.module._atomic_publish(str(output), raw)
            self.assertEqual(output.read_bytes(), raw)
            self.assertTrue(output.read_bytes().endswith(b"\n"))
            self.assertFalse(output.read_bytes().endswith(b"\r\n"))

    def test_sp18_gvl_identity_uses_exact_type_guid_when_flag_is_absent(self) -> None:
        type_only = type(
            "Sp18GlobalVariableList",
            (),
            {"type": self.module.GLOBAL_VARIABLE_LIST_TYPE_GUID.upper()},
        )()
        self.assertTrue(self.module._is_global_variable_list(type_only))
        wrong_type = type("Sp18OtherObject", (), {"type": "not-a-gvl"})()
        self.assertFalse(self.module._is_global_variable_list(wrong_type))
        explicit_false = type(
            "NewerApiOtherObject",
            (),
            {
                "type": self.module.GLOBAL_VARIABLE_LIST_TYPE_GUID,
                "is_global_variable_list": False,
            },
        )()
        self.assertFalse(self.module._is_global_variable_list(explicit_false))

    def test_wire_fields_and_dispatch_match_frozen_root_contract(self) -> None:
        source = WORKER_PATH.read_text(encoding="utf-8")
        compile(source, str(WORKER_PATH), "exec")
        self.assertNotIn("eval(", source)
        self.assertNotIn("exec(", source)
        self.assertNotIn("subprocess", source)
        self.assertNotIn("project.workspace.open", self.module.SUPPORTED_OPERATIONS)
        self.assertNotIn("source.st.upsert", self.module.SUPPORTED_OPERATIONS)
        self.assertNotIn("create_program", source)
        self.assertNotIn("create_function_block", source)
        self.assertNotIn("create_function(", source)
        self.assertNotIn("save_as(", source)
        self.assertIn("workspace.create", self.module.SUPPORTED_OPERATIONS)
        self.assertIn("source.upsert.st", self.module.SUPPORTED_OPERATIONS)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WorkerFixture(root)
            correlation, job = fixture.request("environment.probe")
            request = json.loads((root / "inbox" / f"{correlation}.request.json").read_text(encoding="utf-8"))
            self.assertEqual(set(request), REQUEST_FIELDS)
            worker, _projects = self._make_worker(root, fixture)
            worker.process_next()
            result = self._result(root, job)
            self.assertEqual(set(result), RESULT_FIELDS)
            self.assertTrue((root / "running" / f"{correlation}.request.json").is_file())
            self.assertFalse((root / "results" / f"{correlation}.result.json").exists())
            self.assertNotIn("sequence", result)
            self.assertNotIn("workspace_id", result)
            self.assertNotIn("real_target_contacted", result)
            for diagnostic in result["diagnostics"]:
                self.assertEqual(set(diagnostic), {"severity", "code", "message", "source", "line", "column"})

    def test_twenty_requests_are_lossless_and_internal_sequence_is_monotonic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WorkerFixture(root, max_requests=20)
            requests = [fixture.request("environment.probe") for _ in range(20)]
            worker, _projects = self._make_worker(root, fixture)
            outcome = worker.run_loop()
            self.assertEqual(outcome["reason"], "max_requests")
            self.assertEqual(outcome["processed"], 20)
            self.assertEqual(len(list((root / "state" / "sequences").glob("*.json"))), 20)
            for correlation, job in requests:
                self.assertEqual(self._result(root, job)["status"], "succeeded")
                self.assertTrue((root / "running" / f"{correlation}.request.json").is_file())

    def test_partial_is_ignored_and_idle_exit_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WorkerFixture(root, idle_exit=1)
            inbox = root / "inbox"
            inbox.mkdir()
            (inbox / "ignored.request.json.partial").write_text("{}", encoding="utf-8")
            worker, _projects = self._make_worker(root, fixture)
            outcome = worker.run_loop()
            self.assertEqual(outcome["reason"], "idle_exit")
            self.assertEqual(outcome["processed"], 0)
            self.assertTrue((inbox / "ignored.request.json.partial").exists())

    def test_st_round_trip_build_and_freeze_with_revision_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WorkerFixture(root)
            worker, projects = self._make_worker(root, fixture)
            create, create_job = fixture.request("workspace.create")
            worker.process_next()
            self.assertEqual(self._result(root, create_job)["revision"], 0)
            create_payload = json.loads(
                (root / self._result(root, create_job)["artifacts"][0]["path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(create_payload["record_type"], "DependencyPreflight")
            self.assertEqual(create_payload["status"], "passed")
            self.assertTrue((root / "state" / "dependency-preflight.json").is_file())
            upsert, upsert_job = fixture.request(
                "source.upsert.st",
                payload={
                    "workspace_id": fixture.worker_id, "expected_revision": 0,
                    "pou_name": "AXMCP_Qualification", "pou_kind": "function",
                    "return_type": "BOOL",
                    "declaration": "FUNCTION AXMCP_Qualification : BOOL",
                    "implementation": "AXMCP_Qualification := TRUE;",
                },
                workspace_digest=fixture.workspace_digest, expected_revision=0,
            )
            worker.process_next()
            upsert_result = self._result(root, upsert_job)
            self.assertEqual(upsert_result["revision"], 1)
            self.assertEqual(upsert_result["workspace_digest"], fixture.workspace_digest)
            self.assertEqual(upsert_result["before_project_digest"], fixture.workspace_digest)
            self.assertEqual(len(projects.create_pou_calls), 1)
            changed_digest = fixture.project_digest()
            read, read_job = fixture.request(
                "source.read",
                payload={
                    "workspace_id": fixture.worker_id,
                    "expected_revision": 1,
                    "target": "AXMCP_Qualification",
                },
                workspace_digest=changed_digest, expected_revision=1,
            )
            worker.process_next()
            self.assertEqual(self._result(root, read_job)["status"], "succeeded")
            first_build_event = len(projects.events)
            build, build_job = fixture.request(
                "project.build",
                payload={
                    "workspace_id": fixture.worker_id, "expected_revision": 1,
                    "mode": "rebuild", "max_attempts": 10,
                    "runtime_release_digest": RUNTIME_RELEASE_DIGEST,
                },
                workspace_digest=changed_digest, expected_revision=1,
            )
            worker.process_next()
            build_result = self._result(root, build_job)
            self.assertEqual(build_result["status"], "succeeded")
            self.assertEqual(build_result["revision"], 1)
            self.assertEqual(build_result["workspace_digest"], changed_digest)
            build_payload = json.loads((root / build_result["artifacts"][0]["path"]).read_text(encoding="utf-8"))
            self.assertEqual(build_payload["record_type"], "BuildResult")
            self.assertEqual(build_payload["status"], "passed")
            self.assertEqual(build_payload["workspace_id"], fixture.worker_id)
            self.assertEqual(
                build_payload["runtime_release_digest"], RUNTIME_RELEASE_DIGEST
            )
            self.assertIsNone(build_payload["artifact_digest"])
            first_release_project_digest = fixture.project_digest()
            self.assertEqual(
                build_result["after_project_digest"], first_release_project_digest
            )
            self.assertEqual(
                build_payload["project_digest"], first_release_project_digest
            )
            self.assertNotEqual(
                build_payload["runtime_release_digest"],
                build_payload["project_digest"],
            )
            marker_declaration = (
                "VAR_GLOBAL CONSTANT\n"
                " ReleaseDigest : STRING(71) := '"
                + RUNTIME_RELEASE_DIGEST
                + "';\n"
                "END_VAR"
            )
            marker = next(
                item for item in projects.store if item["name"] == "AXMCP_RuntimeMarker"
            )
            self.assertEqual(marker["declaration"], marker_declaration)
            self.assertEqual(marker["implementation"], "")
            self.assertIs(marker["is_gvl"], True)
            self.assertNotIn(first_release_project_digest, marker["declaration"])
            self.assertEqual(
                projects.events[first_build_event:],
                [
                    ("create_gvl", "AXMCP_RuntimeMarker"),
                    ("save",),
                    ("close",),
                    ("open",),
                    ("rebuild",),
                ],
            )

            frozen, frozen_job = fixture.request(
                "artifact.freeze",
                workspace_digest=first_release_project_digest,
                expected_revision=1,
            )
            worker.process_next()
            frozen_result = self._result(root, frozen_job)
            self.assertEqual(frozen_result["status"], "succeeded")
            self.assertEqual(frozen_result["revision"], 1)
            self.assertEqual(len(list((root / "outputs").glob("*.project"))), 1)
            self.assertEqual(
                projects.create_gvl_calls, [{"name": "AXMCP_RuntimeMarker"}]
            )
            self.assertEqual(projects.rebuild_calls, 2)

    def test_create_pou_uses_exact_installed_keyword_signature_and_pou_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WorkerFixture(root)
            worker, projects = self._make_worker(root, fixture)
            _create, _create_job = fixture.request("workspace.create")
            worker.process_next()
            upsert, upsert_job = fixture.request(
                "source.upsert.st",
                payload={
                    "workspace_id": fixture.worker_id, "expected_revision": 0,
                    "pou_name": "ExactApi", "pou_kind": "function", "return_type": "BOOL",
                    "declaration": "FUNCTION ExactApi : BOOL",
                    "implementation": "ExactApi := TRUE;",
                },
                workspace_digest=fixture.workspace_digest, expected_revision=0,
            )
            worker.process_next()
            self.assertEqual(self._result(root, upsert_job)["status"], "succeeded")
            self.assertEqual(
                projects.create_pou_calls,
                [{
                    "name": "ExactApi", "type": PouTypes.Function,
                    "language": Languages.st, "return_type": "BOOL",
                    "base_type": None, "interfaces": None,
                }],
            )
            self.assertEqual(projects.store[0]["name"], "ExactApi")
            # The first project was closed after save. Its persisted declaration proves
            # keyword-only create_pou and keyword-only textual replace both executed.
            self.assertEqual(projects.store[0]["implementation"], "ExactApi := TRUE;")

    def test_existing_and_new_programs_may_have_empty_implementation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WorkerFixture(root)
            worker, projects = self._make_worker(root, fixture)
            fixture.request("workspace.create")
            worker.process_next()
            fixture.request(
                "source.upsert.st",
                payload={
                    "workspace_id": fixture.worker_id,
                    "expected_revision": 0,
                    "pou_name": "ExistingProgram",
                    "pou_kind": "program",
                    "return_type": None,
                    "declaration": "PROGRAM ExistingProgram\nVAR\nEND_VAR",
                    "implementation": "ExistingProgram := ExistingProgram;",
                },
                workspace_digest=fixture.project_digest(),
                expected_revision=0,
            )
            worker.process_next()
            changed_digest = fixture.project_digest()
            _restore, restore_job = fixture.request(
                "source.upsert.st",
                payload={
                    "workspace_id": fixture.worker_id,
                    "expected_revision": 1,
                    "pou_name": "ExistingProgram",
                    "pou_kind": "program",
                    "return_type": None,
                    "declaration": "PROGRAM ExistingProgram\nVAR\nEND_VAR",
                    "implementation": "",
                },
                workspace_digest=changed_digest,
                expected_revision=1,
            )
            worker.process_next()
            self.assertEqual(self._result(root, restore_job)["status"], "succeeded")
            self.assertEqual(projects.store[0]["implementation"], "")
            restored_digest = fixture.project_digest()
            _new, new_job = fixture.request(
                "source.upsert.st",
                payload={
                    "workspace_id": fixture.worker_id,
                    "expected_revision": 2,
                    "pou_name": "EmptyNewProgram",
                    "pou_kind": "program",
                    "return_type": None,
                    "declaration": "PROGRAM EmptyNewProgram\nVAR\nEND_VAR",
                    "implementation": "",
                },
                workspace_digest=restored_digest,
                expected_revision=2,
            )
            worker.process_next()
            self.assertEqual(self._result(root, new_job)["status"], "succeeded")
            self.assertTrue(any(item["name"] == "EmptyNewProgram" for item in projects.store))

    def test_vendor_diagnostic_source_object_is_normalized_to_safe_pou_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WorkerFixture(root)
            message = Message("Error", "Expression expected instead of ';'")

            class SourceObject:
                def get_name(self) -> str:
                    return "PLC_PRG"

                def __str__(self) -> str:
                    return "Unsafe ScriptObject(Project=145, Name=PLC_PRG)"

            message.object = SourceObject()
            worker, _projects = self._make_worker(
                root, fixture, system=System([message])
            )

            diagnostics = worker._messages()

            self.assertEqual(diagnostics[0]["source"], "PLC_PRG")

    def test_create_gvl_uses_exact_installed_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WorkerFixture(root)
            worker, projects = self._make_worker(root, fixture)
            fixture.request("workspace.create")
            worker.process_next()
            _correlation, job = fixture.request(
                "source.upsert.st",
                payload={
                    "workspace_id": fixture.worker_id, "expected_revision": 0,
                    "pou_name": "GlobalVars", "pou_kind": "gvl", "return_type": None,
                    "declaration": "VAR_GLOBAL\n    Ready : BOOL;\nEND_VAR",
                    "implementation": "",
                },
                workspace_digest=fixture.project_digest(), expected_revision=0,
            )
            worker.process_next()
            self.assertEqual(self._result(root, job)["status"], "succeeded")
            self.assertEqual(projects.create_gvl_calls, [{"name": "GlobalVars"}])

    def test_project_build_updates_existing_runtime_marker_before_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WorkerFixture(root)
            worker, projects = self._make_worker(root, fixture)
            fixture.request("workspace.create")
            worker.process_next()
            _upsert, upsert_job = fixture.request(
                "source.upsert.st",
                payload={
                    "workspace_id": fixture.worker_id,
                    "expected_revision": 0,
                    "pou_name": "AXMCP_RuntimeMarker",
                    "pou_kind": "gvl",
                    "return_type": None,
                    "declaration": (
                        "VAR_GLOBAL CONSTANT\n"
                        " ReleaseDigest : STRING(71) := '"
                        + RUNTIME_RELEASE_DIGEST
                        + "';\n"
                        "END_VAR"
                    ),
                    "implementation": "",
                },
                workspace_digest=fixture.project_digest(),
                expected_revision=0,
            )
            worker.process_next()
            self.assertEqual(self._result(root, upsert_job)["status"], "succeeded")
            before_build_digest = fixture.project_digest()
            build_event = len(projects.events)
            _build, build_job = fixture.request(
                "project.build",
                payload={
                    "workspace_id": fixture.worker_id,
                    "expected_revision": 1,
                    "mode": "build",
                    "max_attempts": 1,
                    "runtime_release_digest": SECOND_RUNTIME_RELEASE_DIGEST,
                },
                workspace_digest=before_build_digest,
                expected_revision=1,
            )
            worker.process_next()

            result = self._result(root, build_job)
            self.assertEqual(result["status"], "succeeded")
            payload = json.loads(
                (root / result["artifacts"][0]["path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(
                payload["runtime_release_digest"], SECOND_RUNTIME_RELEASE_DIGEST
            )
            marker = next(
                item for item in projects.store if item["name"] == "AXMCP_RuntimeMarker"
            )
            self.assertEqual(
                marker["declaration"],
                "VAR_GLOBAL CONSTANT\n"
                " ReleaseDigest : STRING(71) := '"
                + SECOND_RUNTIME_RELEASE_DIGEST
                + "';\n"
                "END_VAR",
            )
            self.assertEqual(
                projects.create_gvl_calls, [{"name": "AXMCP_RuntimeMarker"}]
            )
            self.assertEqual(
                projects.events[build_event:],
                [("save",), ("close",), ("open",), ("build",)],
            )
            first_build_digest = fixture.project_digest()
            second_build_event = len(projects.events)
            _second, second_job = fixture.request(
                "project.build",
                payload={
                    "workspace_id": fixture.worker_id,
                    "expected_revision": 1,
                    "mode": "rebuild",
                    "max_attempts": 1,
                    "runtime_release_digest": THIRD_RUNTIME_RELEASE_DIGEST,
                },
                workspace_digest=first_build_digest,
                expected_revision=1,
            )
            worker.process_next()
            second_result = self._result(root, second_job)
            self.assertEqual(second_result["status"], "succeeded")
            second_payload = json.loads(
                (root / second_result["artifacts"][0]["path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                second_payload["runtime_release_digest"],
                THIRD_RUNTIME_RELEASE_DIGEST,
            )
            marker = next(
                item for item in projects.store if item["name"] == "AXMCP_RuntimeMarker"
            )
            self.assertIn(THIRD_RUNTIME_RELEASE_DIGEST, marker["declaration"])
            self.assertNotIn(SECOND_RUNTIME_RELEASE_DIGEST, marker["declaration"])
            self.assertEqual(
                projects.events[second_build_event:],
                [("save",), ("close",), ("open",), ("rebuild",)],
            )
            self.assertEqual(
                len(list((root / "state" / "build-passes" / "1").glob("*.json"))),
                2,
            )

    def test_project_build_rejects_missing_or_noncanonical_runtime_release_digest(self) -> None:
        cases = (
            {},
            {"runtime_release_digest": "sha256:" + "A" * 64},
            {
                "runtime_release_digest": RUNTIME_RELEASE_DIGEST,
                "artifact_digest": "sha256:" + "c" * 64,
            },
        )
        for extra in cases:
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                fixture = WorkerFixture(root)
                worker, projects = self._make_worker(root, fixture)
                fixture.request("workspace.create")
                worker.process_next()
                payload = {
                    "workspace_id": fixture.worker_id,
                    "expected_revision": 0,
                    "mode": "rebuild",
                    "max_attempts": 10,
                }
                payload.update(extra)
                _build, build_job = fixture.request(
                    "project.build",
                    payload=payload,
                    workspace_digest=fixture.project_digest(),
                    expected_revision=0,
                )
                worker.process_next()
                result = self._result(root, build_job)
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(projects.create_gvl_calls, [])

    def test_build_transaction_recovers_save_reopen_and_post_build_kill_windows(self) -> None:
        for window in ("after-save", "after-reopen", "after-build"):
            with self.subTest(window=window), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                fixture = WorkerFixture(root)
                system = System()
                worker, projects = self._make_worker(
                    root, fixture, system=system
                )
                fixture.request("workspace.create")
                worker.process_next()
                before_bytes = fixture.workspace.read_bytes()
                before_digest = fixture.project_digest()
                _build, build_job = fixture.request(
                    "project.build",
                    payload={
                        "workspace_id": fixture.worker_id,
                        "expected_revision": 0,
                        "mode": "rebuild",
                        "max_attempts": 1,
                        "runtime_release_digest": RUNTIME_RELEASE_DIGEST,
                    },
                    workspace_digest=before_digest,
                    expected_revision=0,
                )

                if window == "after-save":
                    original = worker._save_project

                    def crash_after_save():
                        original()
                        raise SimulatedHardCrash()

                    worker._save_project = crash_after_save
                elif window == "after-reopen":
                    original = worker._open_project

                    def crash_after_reopen():
                        value = original()
                        raise SimulatedHardCrash()

                    worker._open_project = crash_after_reopen
                else:
                    def crash_after_build(*_args, **_kwargs):
                        raise SimulatedHardCrash()

                    worker._messages = crash_after_build

                with self.assertRaises(SimulatedHardCrash):
                    worker.process_next()

                transaction = (
                    root / "state" / "build-transactions" / build_job
                )
                intent = json.loads(
                    (transaction / "intent.json").read_text(encoding="utf-8")
                )
                self.assertEqual(intent["job_id"], build_job)
                self.assertEqual(intent["snapshot_digest"], before_digest)
                self.assertEqual(
                    (transaction / "snapshot.project").read_bytes(), before_bytes
                )
                self.assertNotEqual(fixture.workspace.read_bytes(), before_bytes)

                recovered, _ = self._make_worker(
                    root, fixture, projects=projects, system=system
                )
                self.assertFalse(recovered.process_next())
                self.assertEqual(fixture.workspace.read_bytes(), before_bytes)
                self.assertIsNone(recovered._find_source("AXMCP_RuntimeMarker"))
                self.assertFalse(transaction.exists())
                archive = (
                    root
                    / "archive"
                    / "build-transactions"
                    / f"{build_job}.recovered"
                )
                self.assertTrue((archive / "intent.json").is_file())
                self.assertEqual(
                    (archive / "snapshot.project").read_bytes(), before_bytes
                )
                self.assertFalse((root / "results" / f"{build_job}.result.json").exists())

    def test_build_transaction_archives_orphan_pass_and_payload_before_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WorkerFixture(root)
            worker, projects = self._make_worker(root, fixture)
            fixture.request("workspace.create")
            worker.process_next()
            before_bytes = fixture.workspace.read_bytes()
            before_digest = fixture.project_digest()
            correlation, build_job = fixture.request(
                "project.build",
                payload={
                    "workspace_id": fixture.worker_id,
                    "expected_revision": 0,
                    "mode": "rebuild",
                    "max_attempts": 1,
                    "runtime_release_digest": RUNTIME_RELEASE_DIGEST,
                },
                workspace_digest=before_digest,
                expected_revision=0,
            )
            original = worker._record_build_pass

            def crash_after_build_pass(request, request_digest):
                original(request, request_digest)
                raise SimulatedHardCrash()

            worker._record_build_pass = crash_after_build_pass
            with self.assertRaises(SimulatedHardCrash):
                worker.process_next()

            payload_path = root / "outputs" / f"{correlation}.payload.json"
            pass_path = root / "state" / "build-passes" / "0" / f"{build_job}.json"
            self.assertTrue(payload_path.is_file())
            self.assertTrue(pass_path.is_file())
            self.assertFalse((root / "results" / f"{build_job}.result.json").exists())

            recovered, _ = self._make_worker(
                root, fixture, projects=projects
            )
            self.assertFalse(recovered.process_next())
            self.assertEqual(fixture.workspace.read_bytes(), before_bytes)
            self.assertIsNone(recovered._find_source("AXMCP_RuntimeMarker"))
            archive = (
                root
                / "archive"
                / "build-transactions"
                / f"{build_job}.recovered"
            )
            self.assertTrue(
                (archive / f"orphan-build-pass-{build_job}.json").is_file()
            )
            self.assertTrue(
                (archive / f"orphan-payload-{correlation}.payload.json").is_file()
            )
            self.assertFalse(payload_path.exists())
            self.assertFalse(pass_path.exists())

    def test_same_job_two_hard_crashes_get_monotonic_transaction_archives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WorkerFixture(root)
            worker, projects = self._make_worker(root, fixture)
            fixture.request("workspace.create")
            worker.process_next()
            before_bytes = fixture.workspace.read_bytes()
            correlation, build_job = fixture.request(
                "project.build",
                payload={
                    "workspace_id": fixture.worker_id,
                    "expected_revision": 0,
                    "mode": "rebuild",
                    "max_attempts": 1,
                    "runtime_release_digest": RUNTIME_RELEASE_DIGEST,
                },
                workspace_digest=fixture.project_digest(),
                expected_revision=0,
            )

            def install_save_crash(target_worker):
                original = target_worker._save_project

                def crash_after_save():
                    original()
                    raise SimulatedHardCrash()

                target_worker._save_project = crash_after_save

            install_save_crash(worker)
            with self.assertRaises(SimulatedHardCrash):
                worker.process_next()
            recovered, _ = self._make_worker(
                root, fixture, projects=projects
            )
            self.assertFalse(recovered.process_next())
            self.assertEqual(fixture.workspace.read_bytes(), before_bytes)

            running = root / "running" / f"{correlation}.request.json"
            inbox = root / "inbox" / f"{correlation}.request.json"
            running.replace(inbox)
            second, _ = self._make_worker(root, fixture, projects=projects)
            install_save_crash(second)
            with self.assertRaises(SimulatedHardCrash):
                second.process_next()
            recovered_again, _ = self._make_worker(
                root, fixture, projects=projects
            )
            self.assertFalse(recovered_again.process_next())
            self.assertEqual(fixture.workspace.read_bytes(), before_bytes)

            archive_root = root / "archive" / "build-transactions"
            self.assertTrue((archive_root / f"{build_job}.recovered").is_dir())
            self.assertTrue((archive_root / f"{build_job}.recovered-001").is_dir())

    def test_cleanup_crash_keeps_orphans_inside_active_transaction_for_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WorkerFixture(root)
            worker, projects = self._make_worker(root, fixture)
            fixture.request("workspace.create")
            worker.process_next()
            before_bytes = fixture.workspace.read_bytes()
            correlation, build_job = fixture.request(
                "project.build",
                payload={
                    "workspace_id": fixture.worker_id,
                    "expected_revision": 0,
                    "mode": "rebuild",
                    "max_attempts": 1,
                    "runtime_release_digest": RUNTIME_RELEASE_DIGEST,
                },
                workspace_digest=fixture.project_digest(),
                expected_revision=0,
            )
            original_record = worker._record_build_pass

            def crash_after_build_pass(request, request_digest):
                original_record(request, request_digest)
                raise SimulatedHardCrash()

            worker._record_build_pass = crash_after_build_pass
            with self.assertRaises(SimulatedHardCrash):
                worker.process_next()

            recovered, _ = self._make_worker(
                root, fixture, projects=projects
            )
            original_move = self.module._atomic_move

            def crash_after_orphans_are_moved(source, target):
                original_move(source, target)
                if os.path.basename(target).startswith("orphan-payload-"):
                    raise SimulatedHardCrash()

            self.module._atomic_move = crash_after_orphans_are_moved
            try:
                with self.assertRaises(SimulatedHardCrash):
                    recovered.process_next()
            finally:
                self.module._atomic_move = original_move

            active = root / "state" / "build-transactions" / build_job
            self.assertTrue(active.is_dir())
            self.assertTrue(
                (active / f"orphan-build-pass-{build_job}.json").is_file()
            )
            self.assertTrue(
                (active / f"orphan-payload-{correlation}.payload.json").is_file()
            )
            self.assertEqual(fixture.workspace.read_bytes(), before_bytes)
            self.assertFalse(
                (root / "state" / "build-passes" / "0" / f"{build_job}.json").exists()
            )
            self.assertFalse(
                (root / "outputs" / f"{correlation}.payload.json").exists()
            )

            recovered_again, _ = self._make_worker(
                root, fixture, projects=projects
            )
            self.assertFalse(recovered_again.process_next())
            archive = (
                root
                / "archive"
                / "build-transactions"
                / f"{build_job}.recovered"
            )
            self.assertTrue(
                (archive / f"orphan-build-pass-{build_job}.json").is_file()
            )
            self.assertTrue(
                (archive / f"orphan-payload-{correlation}.payload.json").is_file()
            )
            self.assertFalse(active.exists())

    def test_build_transaction_keeps_durable_success_during_finalize_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WorkerFixture(root)
            worker, projects = self._make_worker(root, fixture)
            fixture.request("workspace.create")
            worker.process_next()
            before_digest = fixture.project_digest()
            correlation, build_job = fixture.request(
                "project.build",
                payload={
                    "workspace_id": fixture.worker_id,
                    "expected_revision": 0,
                    "mode": "rebuild",
                    "max_attempts": 1,
                    "runtime_release_digest": RUNTIME_RELEASE_DIGEST,
                },
                workspace_digest=before_digest,
                expected_revision=0,
            )
            original = worker._recover_build_transactions
            recovery_calls = [0]

            def crash_before_finalize():
                recovery_calls[0] += 1
                if recovery_calls[0] == 2:
                    raise SimulatedHardCrash()
                return original()

            worker._recover_build_transactions = crash_before_finalize
            with self.assertRaises(SimulatedHardCrash):
                worker.process_next()

            committed_bytes = fixture.workspace.read_bytes()
            self.assertNotEqual(fixture.project_digest(), before_digest)
            result_path = root / "results" / f"{build_job}.result.json"
            payload_path = root / "outputs" / f"{correlation}.payload.json"
            pass_path = root / "state" / "build-passes" / "0" / f"{build_job}.json"
            self.assertTrue(result_path.is_file())
            self.assertTrue(payload_path.is_file())
            self.assertTrue(pass_path.is_file())
            event_count = len(projects.events)

            recovered, _ = self._make_worker(root, fixture, projects=projects)
            self.assertFalse(recovered.process_next())
            self.assertEqual(fixture.workspace.read_bytes(), committed_bytes)
            self.assertEqual(len(projects.events), event_count)
            self.assertIsNotNone(recovered._find_source("AXMCP_RuntimeMarker"))
            self.assertTrue(result_path.is_file())
            self.assertTrue(payload_path.is_file())
            self.assertTrue(pass_path.is_file())
            self.assertTrue(
                (
                    root
                    / "archive"
                    / "build-transactions"
                    / f"{build_job}.committed"
                    / "intent.json"
                ).is_file()
            )

    def test_compiler_error_or_warning_restores_exact_pre_marker_project_bytes(self) -> None:
        cases = (
            ("error", CompilerFailureSystem, 1, 0),
            ("warning", CompilerWarningSystem, 0, 1),
        )
        for label, make_system, error_count, warning_count in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                fixture = WorkerFixture(root)
                worker, projects = self._make_worker(
                    root, fixture, system=make_system()
                )
                fixture.request("workspace.create")
                worker.process_next()
                before_bytes = fixture.workspace.read_bytes()
                before_digest = fixture.project_digest()
                build_event = len(projects.events)
                _build, build_job = fixture.request(
                    "project.build",
                    payload={
                        "workspace_id": fixture.worker_id,
                        "expected_revision": 0,
                        "mode": "rebuild",
                        "max_attempts": 1,
                        "runtime_release_digest": RUNTIME_RELEASE_DIGEST,
                    },
                    workspace_digest=before_digest,
                    expected_revision=0,
                )
                worker.process_next()

                result = self._result(root, build_job)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["before_project_digest"], before_digest)
                self.assertEqual(result["after_project_digest"], before_digest)
                self.assertEqual(fixture.workspace.read_bytes(), before_bytes)
                self.assertEqual(projects.store, [])
                self.assertIsNone(worker._find_source("AXMCP_RuntimeMarker"))
                payload = json.loads(
                    (root / result["artifacts"][0]["path"]).read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(payload["status"], "failed")
                self.assertEqual(payload["error_count"], error_count)
                self.assertEqual(payload["warning_count"], warning_count)
                self.assertEqual(payload["project_digest"], before_digest)
                self.assertEqual(
                    payload["runtime_release_digest"], RUNTIME_RELEASE_DIGEST
                )
                self.assertEqual(
                    projects.events[build_event:],
                    [
                        ("create_gvl", "AXMCP_RuntimeMarker"),
                        ("save",),
                        ("close",),
                        ("open",),
                        ("rebuild",),
                        ("close",),
                        ("open",),
                    ],
                )
                self.assertFalse(
                    (root / "state" / "build-passes" / "0").exists()
                )

    def test_build_fails_closed_when_diagnostics_are_unverifiable(self) -> None:
        cases = (
            ("read-failure", DiagnosticReadFailureSystem),
            ("missing-summary", MissingBuildSummarySystem),
            ("clear-failure", DiagnosticClearFailureSystem),
        )
        for label, make_system in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                fixture = WorkerFixture(root)
                worker, _projects = self._make_worker(
                    root, fixture, system=make_system()
                )
                fixture.request("workspace.create")
                worker.process_next()
                before_digest = fixture.project_digest()
                _build, build_job = fixture.request(
                    "project.build",
                    payload={
                        "workspace_id": fixture.worker_id,
                        "expected_revision": 0,
                        "mode": "rebuild",
                        "max_attempts": 1,
                        "runtime_release_digest": RUNTIME_RELEASE_DIGEST,
                    },
                    workspace_digest=before_digest,
                    expected_revision=0,
                )

                worker.process_next()

                result = self._result(root, build_job)
                self.assertIn(result["status"], {"blocked", "failed"})
                self.assertEqual(result["before_project_digest"], before_digest)
                self.assertEqual(result["after_project_digest"], before_digest)
                self.assertFalse(
                    (root / "state" / "build-passes" / "0" / f"{build_job}.json").exists()
                )

    def test_dependency_preflight_blocks_dirty_missing_dependency_and_internal_error(self) -> None:
        cases = [
            (
                "dirty",
                lambda fixture: Projects(fixture.workspace, dirty_on_open=True),
                System(),
                "scratch project is dirty",
            ),
            (
                "empty-device",
                lambda fixture: Projects(fixture.workspace),
                System([Message("Warning", "Device <Empty> description is unresolved")]),
                "<empty>",
            ),
            (
                "missing-library",
                lambda fixture: Projects(fixture.workspace),
                System([Message("Warning", "Library IoStandard could not open")]),
                "library dependency",
            ),
            (
                "internal-error",
                lambda fixture: Projects(fixture.workspace),
                System([Message("Warning", "Internal error: Object reference is null")]),
                "internal error",
            ),
            (
                "rebuild-exception",
                lambda fixture: Projects(fixture.workspace, rebuild_error=RuntimeError("boom")),
                System([]),
                "baseline rebuild raised",
            ),
        ]
        for label, make_projects, system, blocker_text in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                fixture = WorkerFixture(root)
                projects = make_projects(fixture)
                worker, _ = self._make_worker(root, fixture, projects=projects, system=system)
                correlation, job = fixture.request("workspace.create")
                worker.process_next()
                result = self._result(root, job)
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["diagnostics"][0]["code"], "AX.SP18.STOP_DEPENDENCY")
                payload = json.loads((root / result["artifacts"][0]["path"]).read_text(encoding="utf-8"))
                self.assertEqual(payload["status"], "blocked")
                self.assertIn(blocker_text, " ".join(payload["blockers"]).lower())
                marker = json.loads(
                    (root / "state" / "dependency-preflight.json").read_text(encoding="utf-8")
                )
                self.assertEqual(marker["status"], "blocked")
                self.assertEqual(projects.store, [])

    def test_exact_vendor_optional_empty_slot_warning_is_informational(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WorkerFixture(root)
            self.module.OPTIONAL_EMPTY_SLOT_BASELINE_SHA256 = fixture.config[
                "initial_project_sha256"
            ]
            messages = [
                Message("Warning", "Device description for '<Empty>' is missing")
                for _ in range(self.module.OPTIONAL_EMPTY_SLOT_BASELINE_COUNT)
            ]
            for message in messages:
                message.source = None
                message.line = 0
                message.column = 0
            # Exact installed-project evidence reports these known optional
            # placeholders outside the compiler warning total.
            system = System(messages, summary_warnings=0)
            worker, _projects = self._make_worker(root, fixture, system=system)
            _correlation, create_job = fixture.request("workspace.create")
            worker.process_next()
            create_result = self._result(root, create_job)
            self.assertEqual(create_result["status"], "succeeded")
            preflight = json.loads(
                (root / create_result["artifacts"][0]["path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(preflight["status"], "passed")
            self.assertEqual(preflight["blockers"], [])
            self.assertEqual(
                preflight["diagnostics"][0]["code"],
                "AX.BASELINE.OPTIONAL_EMPTY_SLOT",
            )
            self.assertEqual(preflight["diagnostics"][0]["severity"], "info")

            _correlation, build_job = fixture.request(
                "project.build",
                payload={
                    "workspace_id": fixture.worker_id,
                    "expected_revision": 0,
                    "mode": "rebuild",
                    "max_attempts": 1,
                    "runtime_release_digest": RUNTIME_RELEASE_DIGEST,
                },
                workspace_digest=fixture.project_digest(),
                expected_revision=0,
            )
            worker.process_next()
            build_result = self._result(root, build_job)
            self.assertEqual(build_result["status"], "succeeded")
            build_payload = json.loads(
                (root / build_result["artifacts"][0]["path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(build_payload["error_count"], 0)
            self.assertEqual(build_payload["warning_count"], 0)
            self.assertEqual(
                build_payload["diagnostics"][0]["code"],
                "AX.BASELINE.OPTIONAL_EMPTY_SLOT",
            )

    def test_v44_optional_slots_match_recorded_native_baseline_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WorkerFixture(root)
            messages = [
                Message("Warning", "Device description for '<Empty>' is missing")
                for _ in range(self.module.OPTIONAL_EMPTY_SLOT_BASELINE_COUNT)
            ]
            for message in messages:
                message.source = None
                message.line = 0
                message.column = 0
            worker, _projects = self._make_worker(root, fixture, system=System(messages, summary_warnings=0))
            worker.config["initial_project_sha256"] = self.module.OPTIONAL_EMPTY_SLOT_SECOND_BASELINE_SHA256
            diagnostics = worker._messages(strict=True)
            optional = [item for item in diagnostics if item["code"] == "AX.BASELINE.OPTIONAL_EMPTY_SLOT"]
            self.assertEqual(len(optional), 21)
            self.assertTrue(all(item["severity"] == "info" for item in optional))
            self.assertEqual(worker._verified_build_counts(diagnostics), (0, 0))

    def test_optional_slot_warning_is_not_downgraded_outside_exact_baseline_context(self) -> None:
        cases = ("other-baseline", "wrong-count", "source-context")
        for label in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                fixture = WorkerFixture(root)
                messages = [
                    Message("Warning", "Device description for '<Empty>' is missing")
                    for _ in range(self.module.OPTIONAL_EMPTY_SLOT_BASELINE_COUNT)
                ]
                for message in messages:
                    message.source = None
                    message.line = 0
                    message.column = 0
                if label != "other-baseline":
                    self.module.OPTIONAL_EMPTY_SLOT_BASELINE_SHA256 = fixture.config[
                        "initial_project_sha256"
                    ]
                if label == "wrong-count":
                    messages.pop()
                if label == "source-context":
                    messages[0].source = "RequiredDevice"
                worker, _projects = self._make_worker(
                    root, fixture, system=System(messages)
                )
                _correlation, create_job = fixture.request("workspace.create")
                worker.process_next()
                create_result = self._result(root, create_job)
                self.assertEqual(create_result["status"], "blocked")
                preflight = json.loads(
                    (root / create_result["artifacts"][0]["path"]).read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(preflight["diagnostics"][0]["severity"], "warning")
                self.assertTrue(
                    any("emitted a warning" in item for item in preflight["blockers"])
                )

    def test_error_with_optional_slot_text_is_not_downgraded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WorkerFixture(root)
            system = System(
                [Message("Error", "Device description for '<Empty>' is missing")]
            )
            worker, _projects = self._make_worker(root, fixture, system=system)
            _correlation, create_job = fixture.request("workspace.create")
            worker.process_next()
            create_result = self._result(root, create_job)
            self.assertEqual(create_result["status"], "blocked")
            preflight = json.loads(
                (root / create_result["artifacts"][0]["path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(preflight["diagnostics"][0]["severity"], "error")
            self.assertEqual(preflight["diagnostics"][0]["code"], "C.100")
            self.assertTrue(
                any("emitted an error" in item for item in preflight["blockers"])
            )

    def test_unrecognized_baseline_warning_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WorkerFixture(root)
            system = System([Message("Warning", "Unexpected compiler warning")])
            worker, _projects = self._make_worker(root, fixture, system=system)
            _correlation, create_job = fixture.request("workspace.create")
            worker.process_next()
            create_result = self._result(root, create_job)
            self.assertEqual(create_result["status"], "blocked")
            preflight = json.loads(
                (root / create_result["artifacts"][0]["path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(preflight["diagnostics"][0]["severity"], "warning")
            self.assertTrue(
                any("emitted a warning" in item for item in preflight["blockers"])
            )

    def test_source_mutation_runs_dependency_preflight_when_host_did_not_queue_create(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WorkerFixture(root)
            worker, projects = self._make_worker(root, fixture)
            correlation, job = fixture.request(
                "source.upsert.st",
                payload={
                    "workspace_id": fixture.worker_id, "expected_revision": 0,
                    "pou_name": "Direct", "pou_kind": "program", "return_type": None,
                    "declaration": "PROGRAM Direct", "implementation": "",
                },
                workspace_digest=fixture.workspace_digest, expected_revision=0,
            )
            worker.process_next()
            self.assertEqual(self._result(root, job)["status"], "succeeded")
            self.assertEqual([item["name"] for item in projects.store], ["Direct"])
            marker = json.loads(
                (root / "state" / "dependency-preflight.json").read_text(encoding="utf-8")
            )
            self.assertEqual(marker["status"], "passed")

    def test_source_mutation_preserves_dependency_block_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WorkerFixture(root)
            system = System([Message("Warning", "Device <Empty> description is unresolved")])
            worker, projects = self._make_worker(root, fixture, system=system)
            _correlation, job = fixture.request(
                "source.upsert.st",
                payload={
                    "workspace_id": fixture.worker_id, "expected_revision": 0,
                    "pou_name": "Blocked", "pou_kind": "program", "return_type": None,
                    "declaration": "PROGRAM Blocked", "implementation": "",
                },
                workspace_digest=fixture.workspace_digest, expected_revision=0,
            )
            worker.process_next()
            result = self._result(root, job)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["diagnostics"][0]["code"], "AX.SP18.STOP_DEPENDENCY")
            payload = json.loads(
                (root / result["artifacts"][0]["path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(payload["reason"], "dependency_preflight")
            self.assertNotIn("save_method", payload)
            self.assertNotIn("reopen_verified", payload)
            self.assertEqual(projects.store, [])

    def test_input_is_digest_verified_and_embedded_arguments_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WorkerFixture(root)
            now = time.time()
            value = {
                "protocol": "AXQ-FILE/2", "message_type": "request",
                "correlation_id": str(uuid.uuid4()), "job_id": str(uuid.uuid4()),
                "operation": "source.upsert.st",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                "deadline_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 20)),
                "environment_digest": fixture.environment, "workspace_digest": fixture.workspace_digest,
                "expected_revision": 0, "authorization_digest": None, "artifacts": [],
                "arguments": {"name": "forbidden"},
            }
            fixture.publish(value)
            worker, _projects = self._make_worker(root, fixture)
            worker.process_next()
            self.assertFalse((root / "results" / f"{value['job_id']}.result.json").exists())
            self.assertTrue(any((root / "archive" / "quarantine").glob("*.json")))
            with self.assertRaises(self.module.WorkerError):
                self.module._contained_path(str(root), "../x")

    def test_wrong_revision_duplicate_job_stale_and_unknown_operation_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WorkerFixture(root)
            worker, _projects = self._make_worker(root, fixture)
            _create, job = fixture.request("workspace.create")
            worker.process_next()
            wrong, wrong_job = fixture.request("workspace.status", workspace_digest=fixture.workspace_digest, expected_revision=99)
            worker.process_next()
            self.assertEqual(self._result(root, wrong_job)["status"], "blocked")
            duplicate, _duplicate_job = fixture.request("environment.probe", job_id=job)
            worker.process_next()
            self.assertFalse((root / "running" / f"{duplicate}.request.json").exists())
            self.assertTrue(any((root / "archive" / "rejected").glob(f"*-{duplicate}.request.json")))
            stale, stale_job = fixture.request("environment.probe", deadline_offset=-1)
            worker.process_next()
            self.assertEqual(self._result(root, stale_job)["status"], "blocked")
            unknown, unknown_job = fixture.request("eval")
            worker.process_next()
            self.assertFalse((root / "results" / f"{unknown_job}.result.json").exists())
            self.assertTrue(any((root / "archive" / "quarantine").glob("*.json")))

    def test_project_worker_never_claims_or_quarantines_target_worker_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WorkerFixture(root)
            target_requests = [
                (operation,) + fixture.request(operation)
                for operation in (
                    "deployment.execute",
                    "deployment.reconcile",
                    "target.observe.start",
                    "target.observe.read",
                    "target.observe.stop",
                )
            ]
            project_correlation, project_job = fixture.request("environment.probe")
            worker, _projects = self._make_worker(root, fixture)

            self.assertTrue(worker.process_next())
            self.assertEqual(self._result(root, project_job)["status"], "succeeded")
            for operation, correlation, job_id in target_requests:
                with self.subTest(operation=operation):
                    self.assertTrue(
                        (root / "inbox" / f"{correlation}.request.json").is_file()
                    )
                    self.assertFalse((root / "running" / f"{correlation}.request.json").exists())
                    self.assertFalse((root / "results" / f"{job_id}.result.json").exists())
                    self.assertFalse(
                        any((root / "archive").rglob(f"*{correlation}*.json"))
                    )
            self.assertFalse(worker.process_next())
            self.assertFalse(
                (root / "inbox" / f"{project_correlation}.request.json").exists()
            )

    def test_wrong_initial_digest_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WorkerFixture(root)
            fixture.config["initial_project_sha256"] = "sha256:" + "9" * 64
            (root / "worker-config.json").write_bytes(_json_bytes(fixture.config))
            correlation, job = fixture.request("workspace.create")
            worker, _projects = self._make_worker(root, fixture)
            worker.process_next()
            self.assertEqual(self._result(root, job)["status"], "blocked")

    def test_lease_requires_process_fingerprint_and_supports_adopt_recover_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WorkerFixture(root)
            worker, _projects = self._make_worker(root, fixture)
            self.assertEqual(worker.acquire_lease(), "acquired")
            adopted, _projects = self._make_worker(root, fixture)
            self.assertEqual(adopted.acquire_lease(), "adopted")
            worker.release_lease()
            lease = {
                "protocol": "AXQ-FILE/2", "worker_id": str(uuid.uuid4()), "pid": os.getpid(),
                "process_start_fingerprint": "sha256:" + "4" * 64,
                "started_at": "2026-09-01T00:00:00Z", "heartbeat_at": "2026-09-01T00:00:00Z",
                "environment_digest": fixture.environment, "sequence": -1,
            }
            (root / "state" / "worker.lease.json").write_bytes(_json_bytes(lease))
            blocked, _projects = self._make_worker(root, fixture)
            with self.assertRaises(self.module.WorkerError):
                blocked.acquire_lease()
            (root / "state" / "worker.lease.json").unlink()
            lease["pid"] = 99999999
            (root / "state" / "worker.lease.json").write_bytes(_json_bytes(lease))
            recovered, _projects = self._make_worker(root, fixture)
            self.assertEqual(recovered.acquire_lease(), "acquired")
            recovered.release_lease()
            control = root / "control"
            control.mkdir(exist_ok=True)
            (control / "shutdown.request.json").write_bytes(_json_bytes({
                "protocol": "AXQ-FILE/2", "worker_id": fixture.worker_id,
                "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "reason": "test",
            }))
            graceful, _projects = self._make_worker(root, fixture)
            self.assertEqual(graceful.run_loop()["reason"], "graceful_shutdown")


if __name__ == "__main__":
    unittest.main()
