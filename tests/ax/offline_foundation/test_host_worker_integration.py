from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from axmcp_offline.autonomous import AutonomousHost, initialize_autonomous_root
from axmcp_offline.jobs import FileJobQueue
from axmcp_offline.protocol_v2 import make_request_v2
from axmcp_offline.security import (
    canonical_json_bytes,
    sha256_bytes,
    sha256_file,
    utc_now,
)

from . import PACKAGE_ROOT


ENVIRONMENT_DIGEST = "sha256:" + ("1" * 64)
WORKER_PATH = PACKAGE_ROOT / "adapters" / "ax_worker" / "worker.py"
HELPER_PATH = PACKAGE_ROOT / "adapters" / "ax_worker" / "ladder_import.py"


def _load_worker_module():
    spec = importlib.util.spec_from_file_location("ax_worker_transport_e2e", WORKER_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to import the real AX worker")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _TextDocument:
    def __init__(self, text: str = "") -> None:
        self.text = text

    def replace(self, *, new_text: str) -> None:
        self.text = new_text


class _Source:
    def __init__(self, name: str, declaration: str = "", implementation: str = "") -> None:
        self.name = name
        self.is_global_variable_list = name == "AXMCP_RuntimeMarker"
        self.textual_declaration = _TextDocument(declaration)
        self.textual_implementation = _TextDocument(implementation)

    def get_name(self) -> str:
        return self.name

    def get_children(self, _recursive: bool) -> list[object]:
        return []


class _Application:
    name = "Application"
    is_application = True

    def __init__(self, project: "_Project") -> None:
        self.project = project
        self._children: list[_Source] = list(project.sources)
        self.rebuild_count = 0

    def get_name(self) -> str:
        return self.name

    def get_children(self, _recursive: bool) -> list[object]:
        return list(self._children)

    def create_pou(
        self,
        *,
        name: str,
        type: object,
        language: object,
        return_type: str | None,
        base_type: str | None,
        interfaces: str | None,
    ) -> _Source:
        if type is None or language is None:
            raise AssertionError("installed create_pou identity was not supplied")
        if return_type is not None or base_type is not None or interfaces is not None:
            raise AssertionError("program POU received unsupported optional fields")
        source = _Source(name)
        self._children.append(source)
        self.project.sources.append(source)
        self.project.dirty = True
        return source

    def create_gvl(self, *, name: str) -> _Source:
        source = _Source(name)
        self._children.append(source)
        self.project.sources.append(source)
        self.project.dirty = True
        return source

    def rebuild(self) -> None:
        self.rebuild_count += 1

    def build(self) -> None:
        self.rebuild_count += 1


class _Project:
    name = "Scratch"

    def __init__(self, path: Path, store: list[dict[str, str]], owner: "_Projects") -> None:
        self.path = str(path)
        self.store = store
        self.owner = owner
        self.dirty = False
        self.sources = [
            _Source(item["name"], item["declaration"], item["implementation"])
            for item in store
        ]
        self.active_application = _Application(self)

    def get_name(self) -> str:
        return self.name

    def get_children(self, _recursive: bool) -> list[object]:
        return [self.active_application]

    def find(self, name: str, _recursive: bool) -> list[object]:
        return [source for source in self.sources if source.name == name]

    def save(self) -> None:
        self.store[:] = [
            {
                "name": source.name,
                "declaration": source.textual_declaration.text,
                "implementation": source.textual_implementation.text,
            }
            for source in self.sources
        ]
        project_path = Path(self.path)
        project_path.write_bytes(project_path.read_bytes() + b"\nSAVED")
        self.dirty = False

    def save_as(self, path: str) -> None:
        self.path = path
        self.save()

    def close(self) -> None:
        self.owner.primary = None


class _Projects:
    def __init__(self, project_path: Path) -> None:
        self.project_path = project_path
        self.primary: _Project | None = None
        self.store: list[dict[str, str]] = []

    def open(self, path: str, primary: bool = True) -> _Project:
        if not primary or Path(path) != self.project_path:
            raise AssertionError("worker attempted to open a non-scratch project")
        self.primary = _Project(self.project_path, self.store, self)
        return self.primary


class _System:
    executable_filename = "DIADesigner-AX.exe"
    version = "1.9.0.9194"

    def get_message_categories(self, _active: bool) -> list[str]:
        return ["Build"]

    def clear_messages(self, _category: object) -> None:
        return None

    def get_message_objects(self, _category: object | None = None) -> list[object]:
        return [_BuildMessage()]


class _BuildMessage:
    severity = "Info"
    text = "Compile complete -- 0 errors, 0 warnings"
    source = "Application"
    line = 0
    column = 0


class _Languages:
    st = "ST_GUID"


class _PouTypes:
    Program = "POU_PROGRAM"
    FunctionBlock = "POU_FUNCTION_BLOCK"
    Function = "POU_FUNCTION"


def _activate_host(root: Path) -> AutonomousHost:
    initialize_autonomous_root(root)
    config_path = root / "autonomous-config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["environment_digest"] = ENVIRONMENT_DIGEST
    config_path.write_bytes(canonical_json_bytes(config))
    return AutonomousHost(root)


def _configure_worker(v2: Path, workspace_id: str, project_digest: str) -> None:
    config = {
        "protocol": "AXQ-FILE/2",
        "config_version": "2",
        "worker_id": workspace_id,
        "environment_digest": ENVIRONMENT_DIGEST,
        "workspace_project": f"workspaces/{workspace_id}/project.project",
        "initial_project_sha256": project_digest,
        "ide_executable_sha256": "sha256:" + ("2" * 64),
        "script_engine_sha256": "sha256:" + ("3" * 64),
        "ladder_helper_sha256": sha256_file(HELPER_PATH),
        "ide_profile": "DIADesigner-AX 1.9",
        "allowed_operations": [
            "workspace.create",
            "workspace.status",
            "source.upsert.st",
            "project.build",
        ],
        "heartbeat_seconds": 1,
        "lease_timeout_seconds": 5,
        "max_request_seconds": 300,
        "max_runtime_seconds": 300,
        "max_requests": 10,
        "idle_exit_seconds": 1,
    }
    (v2 / "worker-config.json").write_bytes(canonical_json_bytes(config))


class HostWorkerTransportIntegrationTests(unittest.TestCase):
    def test_host_request_worker_result_and_replay_align_end_to_end(self) -> None:
        worker_module = _load_worker_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = _activate_host(root)
            self.assertIsInstance(host.queue, FileJobQueue)
            baseline = host.v2 / "baselines" / "minimal.project"
            baseline.write_bytes(b"MINIMAL-AX-PROJECT")
            baseline_digest = sha256_file(baseline)
            workspace = host.workspace_create(
                "baselines/minimal.project",
                baseline_digest,
                ENVIRONMENT_DIGEST,
            )
            workspace_id = workspace["workspace_id"]
            project_path = host.v2 / "workspaces" / workspace_id / "project.project"
            self.assertEqual(workspace["revision"], 0)
            self.assertEqual(workspace["current_project_digest"], baseline_digest)
            _configure_worker(host.v2, workspace_id, baseline_digest)

            projects = _Projects(project_path)
            worker = worker_module.AxWorker(
                str(host.v2),
                projects_api=projects,
                system_api=_System(),
                languages_api=_Languages(),
                pou_types_api=_PouTypes(),
            )

            preflight_request = make_request_v2(
                correlation_id=str(uuid.uuid4()),
                job_id=str(uuid.uuid4()),
                operation="workspace.create",
                created_at=utc_now(),
                timeout_seconds=60,
                environment_digest=ENVIRONMENT_DIGEST,
                workspace_digest=None,
                expected_revision=None,
                authorization_digest=None,
                artifacts=[],
                scratch_root=host.v2,
            )
            preflight = host.queue.submit(preflight_request)
            preflight_request_path = (
                host.v2
                / "inbox"
                / f"{preflight_request['correlation_id']}.request.json"
            )
            self.assertTrue(preflight_request_path.is_file())
            self.assertTrue(worker.process_next())
            preflight_result_path = (
                host.v2 / "results" / f"{preflight_request['job_id']}.result.json"
            )
            preflight_result = json.loads(preflight_result_path.read_bytes())
            self.assertEqual(preflight_result["status"], "succeeded")
            self.assertEqual(preflight_result["correlation_id"], preflight["correlation_id"])
            self.assertEqual(preflight_result["job_id"], preflight["job_id"])
            host.ingest_worker_result(preflight_result)
            self.assertEqual(host.job_status(preflight["job_id"])["status"], "succeeded")
            self.assertTrue(
                (host.v2 / "archive" / preflight_request_path.name).is_file()
            )

            queued = host.source_upsert_st(
                workspace_id,
                0,
                "TransportProbe",
                "program",
                "PROGRAM TransportProbe\nVAR\n    Counter : DINT;\nEND_VAR",
                "Counter := Counter + 1;",
            )
            request_path = host.v2 / "inbox" / f"{queued['correlation_id']}.request.json"
            self.assertTrue(request_path.is_file())
            request_raw = request_path.read_bytes()
            request = json.loads(request_raw)
            self.assertEqual(request["job_id"], queued["job_id"])
            self.assertEqual(request["correlation_id"], queued["correlation_id"])
            self.assertEqual(request["expected_revision"], 0)
            self.assertEqual(request["workspace_digest"], baseline_digest)
            input_descriptor = request["artifacts"][0]
            self.assertEqual(input_descriptor["role"], "input")
            input_path = host.v2 / input_descriptor["path"]
            self.assertEqual(input_descriptor["sha256"], sha256_file(input_path))
            self.assertEqual(
                set(json.loads(input_path.read_bytes())),
                {
                    "workspace_id",
                    "expected_revision",
                    "pou_name",
                    "pou_kind",
                    "return_type",
                    "declaration",
                    "implementation",
                },
            )

            self.assertTrue(worker.process_next())
            result_path = host.v2 / "results" / f"{queued['job_id']}.result.json"
            self.assertTrue(result_path.is_file())
            result_raw = result_path.read_bytes()
            result = json.loads(result_raw)
            self.assertEqual(result["correlation_id"], request["correlation_id"])
            self.assertEqual(result["job_id"], request["job_id"])
            self.assertEqual(result["request_digest"], sha256_bytes(request_raw))
            self.assertEqual(result["status"], "succeeded", result["diagnostics"])
            self.assertEqual(result["revision"], 1)
            self.assertEqual(result["workspace_digest"], request["workspace_digest"])
            output_descriptor = next(
                artifact for artifact in result["artifacts"] if artifact["role"] == "output"
            )
            self.assertEqual(
                output_descriptor["sha256"], sha256_file(host.v2 / output_descriptor["path"])
            )
            host.ingest_worker_result(result)
            self.assertEqual(host.job_status(queued["job_id"])["status"], "succeeded")
            self.assertEqual(host.workspace_status(workspace_id)["revision"], 1)
            archived_request = host.v2 / "archive" / request_path.name
            self.assertTrue(archived_request.is_file())

            preserved_result_digest = sha256_bytes(result_raw)
            replay_path = host.v2 / "inbox" / request_path.name
            replay_path.write_bytes(request_raw)
            self.assertTrue(worker.process_next())
            self.assertEqual(sha256_file(result_path), preserved_result_digest)
            self.assertTrue(archived_request.is_file())
            self.assertFalse(replay_path.exists())
            self.assertFalse((host.v2 / "running" / request_path.name).exists())

            build = host.project_build(workspace_id, 1, mode="rebuild", max_attempts=2)
            build_request_path = (
                host.v2 / "inbox" / f"{build['correlation_id']}.request.json"
            )
            build_request_raw = build_request_path.read_bytes()
            build_request = json.loads(build_request_raw)
            self.assertTrue(worker.process_next())
            build_result_path = (
                host.v2 / "results" / f"{build['job_id']}.result.json"
            )
            build_result = json.loads(build_result_path.read_bytes())
            self.assertEqual(build_result["job_id"], build_request["job_id"])
            self.assertEqual(build_result["correlation_id"], build_request["correlation_id"])
            self.assertEqual(build_result["request_digest"], sha256_bytes(build_request_raw))
            self.assertEqual(build_result["revision"], 1)
            host.ingest_worker_result(build_result)
            self.assertEqual(host.job_status(build["job_id"])["status"], "succeeded")
            self.assertEqual(host.workspace_status(workspace_id)["revision"], 1)


if __name__ == "__main__":
    unittest.main()
