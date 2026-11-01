"""Explicit Universal PLC entry point; no environment edits or arbitrary dispatch."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .constants import PUBLIC_OPERATIONS, ALLOWED_OPERATIONS, REPLAY_MODE, FILE_BRIDGE_MODE, AUTONOMOUS_MODE
from .security import ProtocolViolation, strict_json_loads_v2, validate_digest
from .service import OfflineService
from .operator_evidence import profile, check_evidence


class AxAdapter:
    """Construct lazily. Merely inspecting capabilities never opens an IDE or PLC.

    A config file accepts mode, scratch_root, timeout_seconds, replay_bundle,
    autostart_worker and autostart_deployment_worker. Without it, the legacy
    AXMCP environment configuration is used (default: explicitly simulated replay).
    """

    def __init__(self, config_path: str | Path | None = None) -> None:
        self.config_path = Path(config_path) if config_path is not None else None
        self._service: OfflineService | None = None

    def capabilities(self) -> list[dict[str, Any]]:
        return [
            {
                "operation": operation,
                "implementation": "available",
                "qualification": "offline_regression",
                "description": "Preserved AX operation: " + operation,
                "live_verified": False,
                "requires": (
                    "explicitly simulated replay or configured AX file bridge"
                    if operation in ALLOWED_OPERATIONS else "configured autonomous AX file bridge"
                ),
                "limitation": (
                    "Live target adapter release approval remains empty; no deployment readiness claim."
                    if operation.startswith(("deployment.", "target.")) else
                    "Neutral-project IDE qualification is separate from inherited source evidence."
                ),
            }
            for operation in PUBLIC_OPERATIONS
        ] + [{
            "operation": "project.create", "implementation": "available",
            "description": "Create a new controller/application/called ST program/task in a bounded offline IDE job.",
            "qualification": "vendor_api_documented", "live_verified": False,
            "requires": "offline ScriptEngine project factory and explicit device profile",
            "limitation": "Requires pinned project_factory config; native creation evidence is separate from compile/live acceptance.",
        }]

    def _backend(self) -> OfflineService:
        if self._service is not None:
            return self._service
        if self.config_path is None:
            self._service = OfflineService.from_environment()
            return self._service
        config = strict_json_loads_v2(self.config_path.read_bytes())
        allowed = {"mode", "target", "project_factory", "scratch_root", "timeout_seconds", "replay_bundle", "autostart_worker", "autostart_deployment_worker"}
        if set(config) - allowed or "mode" not in config:
            raise ProtocolViolation("AX adapter config has missing or unknown fields")
        mode = config["mode"]
        timeout = config.get("timeout_seconds", 3)
        if type(timeout) is not int or not 1 <= timeout <= 300:
            raise ProtocolViolation("timeout_seconds must be a bounded integer")
        for flag in ("autostart_worker", "autostart_deployment_worker"):
            if flag in config and type(config[flag]) is not bool:
                raise ProtocolViolation(flag + " must be boolean")
        def resolve(value: Any) -> Path:
            if not isinstance(value, str) or not value:
                raise ProtocolViolation("configured path must be nonempty text")
            path = Path(value)
            return path if path.is_absolute() else self.config_path.parent / path
        if mode == REPLAY_MODE:
            from .replay import ReplayBackend
            backend = ReplayBackend.from_path(resolve(config["replay_bundle"])) if "replay_bundle" in config else ReplayBackend.from_default()
        elif mode in (FILE_BRIDGE_MODE, AUTONOMOUS_MODE):
            root = resolve(config.get("scratch_root"))
            if mode == FILE_BRIDGE_MODE:
                from .bridge import FileBridgeBackend
                backend = FileBridgeBackend(root, timeout_seconds=timeout)
            else:
                from .autonomous import AutonomousHost
                from .ax_supervisor import AxWorkerSupervisor
                from .ax_deployment_supervisor import AxDeploymentSupervisor
                backend = AutonomousHost(
                    root, legacy_timeout_seconds=timeout,
                    supervisor=AxWorkerSupervisor(root) if config.get("autostart_worker", False) else None,
                    deployment_supervisor=AxDeploymentSupervisor(root) if config.get("autostart_deployment_worker", False) else None,
                )
        else:
            raise ProtocolViolation("unknown AX adapter mode")
        self._service = OfflineService(backend)
        return self._service

    def invoke(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise ProtocolViolation("AX arguments must be an object")
        arguments = dict(arguments)
        target = arguments.pop("universal_target", None)
        if target is not None:
            self._validate_universal_target(target)
        if operation == "project.create":
            if self.config_path is None or target is None:
                raise ProtocolViolation("project.create requires explicit adapter config and universal_target")
            from .project_creation import submit
            return submit(self.config_path.resolve(), arguments)
        if operation == "job.status" and str(arguments.get("job_id", "")).startswith("create_"):
            if self.config_path is None or set(arguments) != {"job_id"}:
                raise ProtocolViolation("project creation job.status requires explicit config and job_id")
            from .project_creation import status
            return status(self.config_path.resolve(), arguments["job_id"])
        if operation == "job.cancel" and str(arguments.get("job_id", "")).startswith("create_"):
            if self.config_path is None or set(arguments) != {"job_id"}:
                raise ProtocolViolation("project creation job.cancel requires explicit config and job_id")
            from .project_creation import cancel
            return cancel(self.config_path.resolve(), arguments["job_id"])
        if operation not in PUBLIC_OPERATIONS:
            raise ProtocolViolation("AX operation is not in the explicit registry")
        backend = self._backend()
        if operation in ALLOWED_OPERATIONS and not (operation == "source.read" and backend.supports_autonomous):
            if arguments:
                raise ProtocolViolation("recorded/file-bridge read operations accept no arguments")
            return backend.execute(operation)
        host = backend.autonomous
        # These bound methods are an explicit allowlist, never getattr on user input.
        calls = {
            "source.read": host.source_read,
            "workspace.status": host.workspace_status,
            "source.upsert.st": host.source_upsert_st,
            "ladder.networks.apply": host.ladder_networks_apply,
            "project.build": host.project_build,
            "artifact.freeze": host.artifact_freeze,
            "deployment.prepare": host.deployment_prepare,
            "deployment.execute": host.deployment_execute,
            "deployment.reconcile": host.deployment_reconcile,
            "target.observe.start": host.target_observe_start,
            "target.observe.read": host.target_observe_read,
            "target.observe.stop": host.target_observe_stop,
            "job.status": host.job_status,
            "job.progress": host.job_progress,
            "revision.apply": host.revision_apply,
            "candidate.snapshot": host.candidate_snapshot,
            "operator.profile": profile,
            "operator.evidence.check": check_evidence,
            "job.cancel": host.job_cancel,
        }
        if operation == "workspace.create":
            args = dict(arguments)
            digest = args.get("baseline_sha256")
            validate_digest(digest)
            derived = "baselines/" + digest.removeprefix("sha256:") + ".project"
            if args.get("baseline_path", derived) != derived:
                raise ProtocolViolation("baseline_path must equal deterministic digest-derived path")
            args["baseline_path"] = derived
            call = host.workspace_create
        else:
            args, call = arguments, calls[operation]
        try:
            return call(**args)
        except TypeError as exc:
            raise ProtocolViolation("invalid arguments for " + operation + ": " + str(exc)) from exc

    def _validate_universal_target(self, target: dict[str, Any]) -> None:
        if not isinstance(target, dict) or str(target.get("vendor", "")).lower() != "delta" or str(target.get("family", "")).upper() != "AX":
            raise ProtocolViolation("AX adapter requires an explicit Delta AX target")
        if self.config_path is None:
            raise ProtocolViolation("Universal AX calls require an explicit adapter config with a bound target profile")
        config = strict_json_loads_v2(self.config_path.read_bytes())
        expected = config.get("target")
        if not isinstance(expected, dict):
            raise ProtocolViolation("AX adapter config has no bound target profile")
        # Normalize cosmetic model separators only, never infer a model family.
        def normalized(value: Any) -> str:
            return str(value).replace("-", "").replace(" ", "").lower()
        for field in ("vendor", "family", "model", "toolchain", "ide_version"):
            if not expected.get(field) or not target.get(field) or normalized(expected[field]) != normalized(target[field]):
                raise ProtocolViolation("Universal target differs from configured AX profile: " + field)
        for field in ("firmware", "project_format"):
            if expected.get(field) and normalized(expected[field]) != normalized(target.get(field, "")):
                raise ProtocolViolation("Universal target differs from configured AX profile: " + field)
