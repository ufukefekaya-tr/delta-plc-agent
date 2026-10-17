from __future__ import annotations

from datetime import datetime, timezone
import importlib.resources
import importlib.metadata
import inspect
import os
from pathlib import Path
import shutil
import threading
from typing import Any
from uuid import uuid4

from .adapters import AdapterManager
from .dvp_controls import MACHINE_COMMAND_OPERATIONS
from .models import PLCError, TargetProfile
from .registry import Registry
from .storage import canonical_digest, digest, inside, read_json, validate_id, write_json


NATIVE_OPERATIONS = {"project.create", "project.build", "project.simulate", "project.upload", "deployment.execute",
                     "deployment.reconcile", "target.run", "target.stop", "target.observe.start",
                     "target.observe.read", "target.observe.stop"}
LIVE_OPERATIONS = {"project.upload", "deployment.execute", "deployment.reconcile", "target.run", "target.stop",
                   "target.observe.start", "target.observe.read", "target.observe.stop", "program.contract.qualify"}

LIVE_OPERATIONS.update(MACHINE_COMMAND_OPERATIONS)

class Service:
    def __init__(self, root: Path, *, read_roots: list[Path] | None = None,
                 registry: Registry | None = None, adapters: AdapterManager | None = None):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.read_roots = [p.resolve() for p in (read_roots or [])]
        self.registry = registry or Registry()
        self.adapters = adapters or AdapterManager(os.environ.get("UNIVERSAL_PLC_AX_CONFIG"))
        self.lock = threading.RLock()

    @classmethod
    def from_environment(cls) -> Service:
        roots = [Path(p) for p in os.environ.get("UNIVERSAL_PLC_READ_ROOTS", "").split(os.pathsep) if p]
        return cls(Path(os.environ.get("UNIVERSAL_PLC_ROOT", ".agent-state/universal-runtime")), read_roots=roots)

    def resolve(self, target: dict[str, Any], operation: str | None = None,
                project_target: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.registry.resolve(TargetProfile.from_dict(target), operation, project_target)

    def capabilities(self, target: dict[str, Any]) -> dict[str, Any]:
        resolved = self.resolve(target)
        results = {}
        for adapter_id in resolved["candidates"]:
            try:
                results[adapter_id] = self.adapters.get(adapter_id).capabilities()
            except (ImportError, ValueError, OSError) as exc:
                results[adapter_id] = {"available": False, "reason": str(exc)}
        return {"routing": resolved, "adapters": results, "execution_authorized": False,
                "note": "Capability presence does not authorize a session or prove a native build."}

    def workspace_create(self, target: dict[str, Any], baseline_path: str | None = None,
                         expected_sha256: str | None = None,
                         project_target: dict[str, Any] | None = None,
                         task_id: str | None = None, revision: str | None = None) -> dict[str, Any]:
        for identifier in (task_id, revision):
            if identifier is not None:
                validate_id(identifier)
        resolved = self.resolve(target, project_target=project_target)
        if resolved["requires_selection"]:
            raise PLCError("Select toolchain/project_format from target.resolve candidates")
        source = None
        if baseline_path:
            source = Path(baseline_path).resolve()
            if not any(source.is_relative_to(p) for p in self.read_roots):
                raise PLCError("Baseline must be inside a configured UNIVERSAL_PLC_READ_ROOTS directory")
            if not source.is_file() or source.stat().st_size > 256 * 1024 * 1024:
                raise PLCError("Baseline missing or larger than 256 MiB")
            if not expected_sha256 or digest(source) != expected_sha256.removeprefix("sha256:"):
                raise PLCError("Baseline SHA256 mismatch")
            profile_format = resolved["target"].get("project_format")
            if profile_format and source.suffix.lower() != profile_format:
                raise PLCError("Baseline extension conflicts with TargetProfile")
        elif expected_sha256:
            raise PLCError("expected_sha256 requires baseline_path")
        workspace_id = "ws_" + uuid4().hex
        directory = self.root / "workspaces" / workspace_id
        directory.mkdir(parents=True)
        files_dir = directory / "files"
        files_dir.mkdir()
        copied = None
        if source:
            copied = files_dir / ("baseline" + source.suffix.lower())
            shutil.copyfile(source, copied)
            if digest(copied) != expected_sha256.removeprefix("sha256:"):
                copied.unlink()
                raise PLCError("Baseline changed during copy")
        manifest = {"schema_version": 1, "workspace_id": workspace_id,
                    "task_id": task_id or workspace_id, "revision": revision or "initial",
                    "authority": "file_workspace_only; native/live authority is validated by the adapter",
                    "target": resolved["target"], "adapter_id": resolved["adapter_id"],
                    "baseline_sha256": digest(copied) if copied else None,
                    "baseline_file": copied.name if copied else None,
                    "project_identity_source": "provided metadata" if project_target else "user declared; native inspection pending",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "native_project_created": False,
                    "note": "An empty workspace is not a vendor project. Use a qualified project.create operation."}
        manifest["context_digest"] = canonical_digest(manifest)
        write_json(directory / "manifest.json", manifest)
        return manifest

    def workspace(self, workspace_id: str) -> tuple[Path, dict[str, Any]]:
        directory = self.root / "workspaces" / validate_id(workspace_id)
        manifest = read_json(directory / "manifest.json")
        claimed = manifest.pop("context_digest")
        if canonical_digest(manifest) != claimed:
            raise PLCError("WORKSPACE_CONTEXT_DRIFT")
        manifest["context_digest"] = claimed
        if manifest.get("baseline_file"):
            if digest(inside(directory / "files", manifest["baseline_file"])) != manifest["baseline_sha256"]:
                raise PLCError("BASELINE_DRIFT: original baseline must remain immutable")
        return directory, manifest

    def import_file(self, workspace_id: str, source_path: str, destination: str, expected_sha256: str) -> dict[str, Any]:
        with self.lock:
            directory, _ = self.workspace(workspace_id)
            source = Path(source_path).resolve()
            if not any(source.is_relative_to(p) for p in self.read_roots):
                raise PLCError("Import source outside configured read roots")
            expected = expected_sha256.removeprefix("sha256:")
            if not source.is_file() or source.stat().st_size > 256 * 1024 * 1024 or digest(source) != expected:
                raise PLCError("Import source hash/size mismatch")
            output = inside(directory / "files", destination)
            output.parent.mkdir(parents=True, exist_ok=True)
            try:
                with output.open("xb") as sink, source.open("rb") as stream:
                    shutil.copyfileobj(stream, sink)
            except FileExistsError as exc:
                raise PLCError("Destination already exists") from exc
            if digest(output) != expected:
                output.unlink()
                raise PLCError("Import source changed during copy")
            return {"path": destination, "sha256": expected, "evidence_kind": "FILE_COPY"}

    def _arguments(self, directory: Path, arguments: dict[str, Any]) -> dict[str, Any]:
        # File adapter path fields are always explicitly named; nested arbitrary paths are not accepted.
        result = dict(arguments)
        for key, value in arguments.items():
            if key.endswith(("_path", "_dir")):
                if not isinstance(value, str):
                    raise PLCError(f"{key} must be a workspace-relative path")
                result[key] = str(inside(directory / "files", value))
        return result

    def invoke(self, workspace_id: str, operation: str, arguments: dict[str, Any],
               expected_context_digest: str) -> dict[str, Any]:
        with self.lock:
            directory, manifest = self.workspace(workspace_id)
            if manifest["context_digest"] != expected_context_digest:
                raise PLCError("STALE_EXECUTION_CONTEXT")
            adapter_id = manifest["adapter_id"]
            adapter = self.adapters.get(adapter_id)
            capabilities = adapter.capabilities()
            cap = next((c for c in capabilities if c["operation"] == operation), None)
            if cap is None or cap.get("implementation") != "available":
                raise PLCError(f"OPERATION_NOT_IMPLEMENTED: {operation}")
            if adapter_id == "delta.ax":
                # AX retains its own digest-locked workspace, typed authority, live enrollment and worker gates.
                # Bind the universal target explicitly so the backend cannot default to another controller.
                prepared = dict(arguments)
                prepared["universal_target"] = manifest["target"]
            else:
                # Machine recovery journals belong to the explicit shared target state,
                # not workspace files. Its backend validates the exact target boundary.
                prepared = (dict(arguments) if adapter_id == "delta.dvpserial" and operation == "machine.reconcile"
                            else self._arguments(directory, arguments))
                prepared["universal_target"] = manifest["target"]
                if adapter_id == "delta.dvpserial":
                    prepared["_execution_context"] = {
                        "workspace_id": manifest["workspace_id"], "task_id": manifest["task_id"],
                        "revision": manifest["revision"], "context_digest": manifest["context_digest"],
                        "workspace_root": str(directory), "baseline_sha256": manifest.get("baseline_sha256")}
            evidence_id = "ev_" + uuid4().hex
            adapter_source = Path(inspect.getfile(type(adapter)))
            module_root = type(adapter).__module__.split('.', 1)[0]
            distributions = importlib.metadata.packages_distributions().get(module_root, [])
            package_name = distributions[0] if len(distributions) == 1 else None
            try:
                package_version = importlib.metadata.version(package_name) if package_name else "unidentified_distribution"
            except importlib.metadata.PackageNotFoundError:
                package_version = "source_checkout_uninstalled"
            start = {"schema_version": 1, "evidence_id": evidence_id, "workspace_id": workspace_id,
                     "operation": operation, "adapter_id": adapter_id,
                     "target": manifest["target"],
                     "implementation_identity": {"package": package_name, "version": package_version,
                         "adapter_module": type(adapter).__module__, "adapter_module_sha256": digest(adapter_source)},
                     "context_digest": expected_context_digest, "arguments_digest": canonical_digest(arguments),
                     "started_at": datetime.now(timezone.utc).isoformat(), "status": "started"}
            if adapter_id == "delta.dvpserial":
                start["implementation_identity"]["module_dependencies"] = {
                    name: digest(adapter_source.with_name(name + ".py"))
                    for name in ("dvp_native", "dvp_transfer", "dvp_controls")}
            write_json(directory / "evidence" / (evidence_id + ".intent.json"), start)
            try:
                result = adapter.invoke(operation, prepared)
            except Exception as exc:
                failure = {**start, "status": "unknown_outcome" if operation in LIVE_OPERATIONS else "failed",
                           "error": str(exc), "retry_allowed": operation not in LIVE_OPERATIONS}
                write_json(directory / "evidence" / (evidence_id + ".json"), failure)
                raise PLCError(f"{failure['status']}: {exc}; evidence={evidence_id}") from exc
            record = {**start, "status": "returned", "result": result,
                      "finished_at": datetime.now(timezone.utc).isoformat()}
            write_json(directory / "evidence" / (evidence_id + ".json"), record)
            return {"evidence_id": evidence_id, "result": result,
                    "note": "Backend evidence kind and status determine qualification; returned is not native PASS."}

    def evidence_read(self, workspace_id: str, evidence_id: str) -> dict[str, Any]:
        directory, _ = self.workspace(workspace_id)
        name = validate_id(evidence_id)
        final = directory / "evidence" / (name + ".json")
        if final.exists():
            return read_json(final)
        intent = read_json(directory / "evidence" / (name + ".intent.json"))
        return {**intent, "status": "unknown_outcome", "retry_allowed": False,
                "note": "No terminal record; reconcile before any retry."}

    def workflow_start(self, workspace_id: str, workflow: str) -> dict[str, Any]:
        directory, manifest = self.workspace(workspace_id)
        definitions = __import__("json").loads(importlib.resources.files("universal_plc").joinpath("data/workflows.json").read_text("utf-8"))
        if workflow not in definitions:
            raise PLCError("Unknown workflow")
        run_id = "run_" + uuid4().hex
        value = {"run_id": run_id, "workspace_id": workspace_id, "workflow": workflow,
                 "context_digest": manifest["context_digest"], "status": "planned",
                 "definition": definitions[workflow], "completed": [], "next_step": "supply explicit operation steps"}
        write_json(directory / "runs" / (run_id + ".json"), value)
        return value

    def workflow_status(self, workspace_id: str, run_id: str) -> dict[str, Any]:
        directory, _ = self.workspace(workspace_id)
        value = read_json(directory / "runs" / (validate_id(run_id) + ".json"))
        if value["status"] == "running":
            value = {**value, "status": "unknown_outcome", "next_step": "read operation evidence; reconcile, never auto-replay"}
        return value

    def workflow_run(self, workspace_id: str, run_id: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
        with self.lock:
            directory, manifest = self.workspace(workspace_id)
            path = directory / "runs" / (validate_id(run_id) + ".json")
            value = read_json(path)
            if value["status"] != "planned":
                return self.workflow_status(workspace_id, run_id)
            if value["context_digest"] != manifest["context_digest"]:
                raise PLCError("WORKFLOW_CONTEXT_DRIFT")
            if not isinstance(steps, list) or not 1 <= len(steps) <= 32:
                raise PLCError("Provide 1..32 explicit typed steps")
            allowed = value["definition"]["operations"]
            for step in steps:
                if set(step) != {"operation", "arguments"} or step["operation"] not in allowed or not isinstance(step["arguments"], dict):
                    raise PLCError("Step not allowed in this workflow")
            # Exclusive durable admission prevents two host processes replaying the same run.
            admission = path.with_suffix(".admitted.json")
            try:
                write_json(admission, {"steps_digest": canonical_digest(steps), "context_digest": value["context_digest"]})
            except FileExistsError:
                return {**value, "status": "unknown_outcome", "next_step": "another process admitted this run; inspect evidence"}
            value.update(status="running", steps=steps)
            write_json(path, value, replace=True)
            for index, step in enumerate(steps):
                value["active_step"] = index
                write_json(path, value, replace=True)
                try:
                    result = self.invoke(workspace_id, step["operation"], step["arguments"], manifest["context_digest"])
                except Exception as exc:
                    value.update(status="unknown_outcome" if step["operation"] in LIVE_OPERATIONS else "failed", error=str(exc), next_step="inspect evidence and correct the cause; this run is not replayable")
                    break
                value["completed"].append(result)
                backend = result["result"]
                backend_state = backend.get("status", backend.get("state"))
                if not backend_state and isinstance(backend.get("job"), dict):
                    backend_state = backend["job"].get("state", backend["job"].get("status"))
                if backend_state in {"queued", "running", "cancel_requested"}:
                    value.update(status="waiting_backend", next_step="query backend job/evidence before scheduling dependent work")
                    break
                if backend.get("ok") is False or backend_state in {"failed", "blocked", "cancelled", "fault", "error"}:
                    value.update(status="failed", next_step="backend did not succeed; inspect evidence before new work")
                    break
                success = backend_state in {"succeeded", "created", "complete", "completed", "frozen", "prepared", "ok", "success"}
                if not success and not (backend_state is None and backend.get("ok") is True):
                    value.update(status="unknown_outcome", next_step="backend has no recognized successful terminal state; reconcile before dependent work")
                    break
            else:
                value.update(status="steps_returned", next_step="review evidence; required native/hardware acceptance remains separate")
            write_json(path, value, replace=True)
            return value
