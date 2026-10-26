from __future__ import annotations

import json
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .ax_supervisor import AxWorkerSupervisor

from .bridge import FileBridgeBackend, initialize_scratch
from .constants import AUTONOMOUS_MODE
from .jobs import FileJobQueue
from .ladder import LadderRecipeError, validate_revision_subset
from .job_progress import read_job_progress
from .protocol_v2 import (
    load_archived_result_chain_v2,
    load_v2,
    make_request_v2,
    validate_artifacts_v2,
    validate_arguments,
    validate_deployment_launch_uncertainty_v1,
    validate_deployment_reconciliation_head_v1,
    validate_deployment_side_effect_marker_v1,
    validate_deployment_worker_evidence_v1,
    validate_observation_profile,
    validate_request_v2,
    validate_result_v2,
    validate_workspace_id,
)
from .security import (
    ProtocolViolation,
    atomic_write_exclusive,
    atomic_write_replace,
    canonical_json_bytes,
    parse_utc,
    sha256_bytes,
    sha256_file,
    strict_json_loads_v2,
    utc_now,
    validate_artifact_path,
    validate_correlation_id,
    validate_digest,
    require_single_workspace,
)

_CONFIG_KEYS = {
    "config_version",
    "environment_digest",
    "target_allowlist",
    "deployment_authority_phrase",
    "deployment_ttl_seconds",
    "observation_expression_allowlist",
}
_WORKSPACE_KEYS = {
    "schema_version",
    "workspace_id",
    "created_at",
    "baseline_path",
    "baseline_digest",
    "environment_digest",
    "revision",
    "project_digest",
    "pending_job_id",
    "last_successful_build",
    "frozen_artifacts",
    "last_native_save",
    "native_candidates",
}
_LEGACY_WORKSPACE_KEYS = _WORKSPACE_KEYS - {"last_native_save", "native_candidates"}
_PHRASE_DIGEST = "sha256:d254948f417a488968d144e12a6b87dfdeafb5582dbe86fa807dac952341088c"
_RECONCILIATION_RECORD_KEYS = {
    "schema_version",
    "record_type",
    "original_job_id",
    "reconciliation_job_id",
    "created_at",
    "target_fingerprint",
    "artifact_digest",
    "rollback_digest",
    "runtime_release_digest",
    "rollback_runtime_release_digest",
    "original_result_digest",
    "side_effect_marker_digest",
    "provenance_head_kind",
    "journal_head_digest",
    "outcome",
    "reconciliation_required",
    "observed",
}
_RECONCILIATION_PROCESS_LOCK = threading.Lock()
_DEPLOYMENT_EXECUTE_INPUT_KEYS = {
    "schema_version",
    "record_type",
    "plan_id",
    "plan_digest",
    "artifact_digest",
    "runtime_release_digest",
    "target_fingerprint",
    "rollback_digest",
    "rollback_runtime_release_digest",
    "expires_at",
    "challenge_nonce",
    "observation_digest",
    "authority_digest",
}


def initialize_autonomous_root(root: Path) -> Path:
    root = root.resolve()
    initialize_scratch(root)
    v2 = root / "v2"
    FileJobQueue(v2)
    for name in (
        "baselines",
        "workspaces",
        "inputs",
        "outputs",
        "evidence",
        "artifacts",
        "native_candidates",
        "plans",
        "observations",
    ):
        (v2 / name).mkdir(exist_ok=True)
    (v2 / "state" / "reconciliations").mkdir(parents=True, exist_ok=True)
    (v2 / "state" / "deployment-launch-uncertainty").mkdir(
        parents=True, exist_ok=True
    )
    config_path = root / "autonomous-config.json"
    if not config_path.exists():
        config = {
            "config_version": "2",
            "environment_digest": "sha256:" + ("0" * 64),
            "target_allowlist": [],
            "deployment_authority_phrase": "yükle",
            "deployment_ttl_seconds": 600,
            "observation_expression_allowlist": [],
        }
        atomic_write_exclusive(config_path, canonical_json_bytes(config))
    return config_path


def configure_autonomous_environment(root: Path, environment_digest: str) -> dict[str, str]:
    """Synchronize the V1 compatibility and V2 host environment identities.

    Each file is published atomically.  If the second publication or final
    invariant check fails, both prior byte sequences are restored before the
    error escapes.
    """

    root = root.resolve()
    validate_digest(environment_digest)
    if environment_digest == "sha256:" + ("0" * 64):
        raise ProtocolViolation("environment digest cannot remain the placeholder")
    autonomous_path = root / "autonomous-config.json"
    probe_path = root / "probe-config.json"
    try:
        autonomous_raw = autonomous_path.read_bytes()
        probe_raw = probe_path.read_bytes()
        autonomous = strict_json_loads_v2(autonomous_raw)
        probe = strict_json_loads_v2(probe_raw)
    except (OSError, ProtocolViolation) as exc:
        raise ProtocolViolation("autonomous root configs are unavailable or invalid") from exc
    if set(autonomous) != _CONFIG_KEYS or autonomous.get("config_version") != "2":
        raise ProtocolViolation("autonomous config shape or version mismatch")
    if set(probe) != {"config_version", "environment_digest", "source_target", "export_path"}:
        raise ProtocolViolation("probe config shape mismatch")
    if probe.get("config_version") != "1":
        raise ProtocolViolation("probe config version mismatch")
    autonomous["environment_digest"] = environment_digest
    probe["environment_digest"] = environment_digest
    try:
        atomic_write_replace(autonomous_path, canonical_json_bytes(autonomous))
        atomic_write_replace(probe_path, canonical_json_bytes(probe))
        synchronized_autonomous = strict_json_loads_v2(autonomous_path.read_bytes())
        synchronized_probe = strict_json_loads_v2(probe_path.read_bytes())
        if (
            synchronized_autonomous.get("environment_digest") != environment_digest
            or synchronized_probe.get("environment_digest") != environment_digest
        ):
            raise ProtocolViolation("environment config synchronization failed")
    except Exception as exc:
        atomic_write_replace(autonomous_path, autonomous_raw)
        atomic_write_replace(probe_path, probe_raw)
        if isinstance(exc, ProtocolViolation):
            raise
        raise ProtocolViolation("environment config synchronization failed") from exc
    return {
        "autonomous_config": str(autonomous_path),
        "probe_config": str(probe_path),
        "environment_digest": environment_digest,
    }


def enroll_baseline(
    root: Path, source_project: Path, expected_sha256: str
) -> dict[str, Any]:
    """Copy one digest-locked regular project into the private V2 baseline store."""

    root = root.resolve()
    validate_digest(expected_sha256)
    if expected_sha256 == "sha256:" + ("0" * 64):
        raise ProtocolViolation("baseline digest cannot be a placeholder")
    if source_project.is_symlink():
        raise ProtocolViolation("baseline source cannot be a symlink")
    try:
        source = source_project.resolve(strict=True)
    except OSError as exc:
        raise ProtocolViolation("baseline source is unavailable") from exc
    if not source.is_file():
        raise ProtocolViolation("baseline source must be a regular file")
    size = source.stat().st_size
    if size <= 0 or size > 128 * 1024 * 1024:
        raise ProtocolViolation("baseline source size is outside the bounded range")
    before = sha256_file(source)
    if before != expected_sha256:
        raise ProtocolViolation("baseline source digest mismatch")
    baseline_directory = root / "v2" / "baselines"
    if not baseline_directory.is_dir():
        raise ProtocolViolation("autonomous root is not initialized")
    destination = baseline_directory / f"{expected_sha256[7:]}.project"
    if source == destination:
        raise ProtocolViolation("baseline source cannot alias the enrollment destination")
    raw = source.read_bytes()
    if sha256_bytes(raw) != expected_sha256:
        raise ProtocolViolation("baseline source changed while being read")
    if destination.exists():
        if destination.is_symlink() or not destination.is_file():
            raise ProtocolViolation("baseline destination is not a regular file")
        if sha256_file(destination) != expected_sha256:
            raise ProtocolViolation("baseline destination already exists with different bytes")
    else:
        atomic_write_exclusive(destination, raw)
    if sha256_file(destination) != expected_sha256 or sha256_file(source) != before:
        raise ProtocolViolation("baseline enrollment integrity check failed")
    return {
        "baseline_path": f"baselines/{destination.name}",
        "baseline_sha256": expected_sha256,
        "source_sha256_after": before,
    }


class AutonomousHost:
    def __init__(
        self,
        root: Path,
        legacy_timeout_seconds: int = 3,
        supervisor: AxWorkerSupervisor | None = None,
        deployment_supervisor: Any | None = None,
    ) -> None:
        self.root = root.resolve()
        if not self.root.is_dir():
            raise ProtocolViolation("autonomous scratch root does not exist")
        self.v2 = self.root / "v2"
        self.queue = FileJobQueue(self.v2)
        for name in (
            "baselines",
            "workspaces",
            "inputs",
            "outputs",
            "evidence",
            "artifacts",
            "native_candidates",
            "plans",
            "observations",
        ):
            (self.v2 / name).mkdir(exist_ok=True)
        (self.v2 / "state" / "reconciliations").mkdir(parents=True, exist_ok=True)
        (self.v2 / "state" / "reconciliation-claims").mkdir(parents=True, exist_ok=True)
        (self.v2 / "state" / "deployment-launch-uncertainty").mkdir(
            parents=True, exist_ok=True
        )
        self.config = self._load_config()
        self.legacy = FileBridgeBackend(self.root, timeout_seconds=legacy_timeout_seconds)
        self.mode = AUTONOMOUS_MODE
        self.supervisor = supervisor
        self.deployment_supervisor = deployment_supervisor

    @contextmanager
    def _workspace_fence(self, workspace_id: str):
        """Exclude another host's enqueue while a native candidate is published."""
        validate_workspace_id(workspace_id)
        directory = self.v2 / "state" / "workspace-fences"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{workspace_id}.lock"
        try:
            with path.open("xb") as handle:
                handle.write(canonical_json_bytes({"workspace_id": workspace_id, "pid": os.getpid()}))
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as exc:
            raise ProtocolViolation("workspace publication fence is held or requires reconciliation") from exc
        try:
            yield
        finally:
            path.unlink()

    def _revision_path_budget(self, workspace_id: str, job_id: str,
                              correlation_id: str, identity_path: Path) -> None:
        """Reject legacy Windows MAX_PATH hazards before creating durable intent."""
        if os.name != "nt":
            return
        transaction = self.v2 / "state" / "revision-transactions" / job_id
        workspace = self.v2 / "workspaces" / workspace_id
        paths = [
            identity_path.with_name(identity_path.name + ".partial"),
            self.v2 / "inputs" / f"{job_id}.arguments.json.partial",
            self.v2 / "state" / "order" / f"{'0' * 20}_{correlation_id}.order.json.partial",
            self.v2 / "inbox" / f"{correlation_id}.request.json.partial",
            self.v2 / "running" / f"{correlation_id}.request.json",
            self.v2 / "results" / f"{job_id}.result.json.partial",
            self.v2 / "outputs" / f"{job_id}.output.json.partial",
            self.v2 / "evidence" / f"{job_id}.window-guard.json.partial",
            transaction / "project.project.partial",
            transaction / "intent.json.partial",
            transaction / "receipt.json.partial",
            transaction / "before.xml",
            transaction / "after.xml",
            transaction / "ladder-15.xml",
            workspace / "project.project.restore.partial",
            self.v2 / "native_candidates" / f"{'0' * 64}.project.partial",
        ]
        if any(len(str(path.resolve())) >= 240 for path in paths):
            raise ProtocolViolation("revision scratch path exceeds bounded Windows path budget")

    def _require_deployment_supervisor(self) -> Any:
        if self.deployment_supervisor is None:
            raise ProtocolViolation(
                "target operation is unavailable because the deployment worker launcher is disabled"
            )
        return self.deployment_supervisor

    def _recover_target_queue_if_inactive(self) -> None:
        recover = getattr(self.deployment_supervisor, "recover_inactive", None)
        if callable(recover):
            recover()

    def _terminal_reconciliation_for(
        self, original_job_id: str, original_result_digest: str
    ) -> dict[str, Any] | None:
        validate_correlation_id(original_job_id)
        validate_digest(original_result_digest)
        original_result_path = self.v2 / "results" / f"{original_job_id}.result.json"
        try:
            original_request, original_result = load_archived_result_chain_v2(
                original_result_path, self.v2
            )
        except (OSError, ProtocolViolation) as exc:
            raise ProtocolViolation("original deployment result is missing or corrupt") from exc
        if (
            sha256_file(original_result_path) != original_result_digest
            or original_result.get("job_id") != original_job_id
            or original_result.get("operation") != "deployment.execute"
            or original_result.get("status") != "unknown_outcome"
            or original_result.get("environment_digest") != self.environment_digest
        ):
            raise ProtocolViolation("original deployment result binding mismatch")
        evidence_descriptors = [
            item
            for item in original_result.get("artifacts", [])
            if isinstance(item, dict) and item.get("role") == "evidence"
        ]
        if len(evidence_descriptors) != 1:
            raise ProtocolViolation("original deployment evidence descriptor is missing")
        evidence_path = validate_artifact_path(evidence_descriptors[0]["path"], self.v2)
        if (
            not evidence_path.is_file()
            or sha256_file(evidence_path) != evidence_descriptors[0]["sha256"]
        ):
            raise ProtocolViolation("original deployment evidence bytes changed")
        evidence = validate_deployment_worker_evidence_v1(
            strict_json_loads_v2(evidence_path.read_bytes()), original_result
        )
        if (
            evidence["reconciliation_required"] is not True
            or evidence["outcome"] != "unknown_outcome"
        ):
            raise ProtocolViolation("original deployment evidence binding mismatch")
        candidate_record = self._frozen_artifact_record(evidence.get("candidate_digest"))
        rollback_record = self._frozen_artifact_record(evidence.get("rollback_digest"))
        marker_path = self.v2 / "state" / "side-effects" / f"{original_job_id}.started.json"
        try:
            marker = strict_json_loads_v2(marker_path.read_bytes())
        except (OSError, ProtocolViolation) as exc:
            raise ProtocolViolation("original deployment side-effect marker is missing") from exc
        validate_deployment_side_effect_marker_v1(
            marker,
            job_id=original_job_id,
            environment_digest=self.environment_digest,
        )
        provenance_head = validate_deployment_reconciliation_head_v1(
            self.v2,
            request=original_request,
            request_raw=canonical_json_bytes(original_request),
            target_fingerprint=evidence["target_fingerprint"],
        )
        journal_head_digest = provenance_head["digest"]
        directory = self.v2 / "state" / "reconciliations"
        terminal: list[dict[str, Any]] = []
        for path in sorted(directory.glob(f"{original_job_id}.*.json")):
            try:
                value = strict_json_loads_v2(path.read_bytes())
                if not isinstance(value, dict) or set(value) != _RECONCILIATION_RECORD_KEYS:
                    raise ProtocolViolation("reconciliation record shape mismatch")
                if (
                    value["schema_version"] != "1"
                    or value["record_type"] != "DeploymentReconciliation"
                    or value["original_job_id"] != original_job_id
                    or value["original_result_digest"] != original_result_digest
                    or value["outcome"] not in {"succeeded", "rolled_back"}
                    or value["reconciliation_required"] is not False
                ):
                    continue
                validate_correlation_id(value["reconciliation_job_id"])
                parse_utc(value["created_at"])
                for field in (
                    "target_fingerprint",
                    "artifact_digest",
                    "rollback_digest",
                    "runtime_release_digest",
                    "rollback_runtime_release_digest",
                    "side_effect_marker_digest",
                    "journal_head_digest",
                ):
                    validate_digest(value[field])
                observed = value["observed"]
                if (
                    value["target_fingerprint"] != evidence.get("target_fingerprint")
                    or value["artifact_digest"] != evidence.get("candidate_digest")
                    or value["rollback_digest"] != evidence.get("rollback_digest")
                    or value["runtime_release_digest"]
                    != candidate_record["runtime_release_digest"]
                    or value["rollback_runtime_release_digest"]
                    != rollback_record["runtime_release_digest"]
                    or not self._target_allowed(value["target_fingerprint"])
                    or value["side_effect_marker_digest"] != sha256_file(marker_path)
                    or value["provenance_head_kind"] != provenance_head["kind"]
                    or value["journal_head_digest"] != journal_head_digest
                    or not isinstance(observed, dict)
                    or set(observed)
                    != {
                        "target_fingerprint",
                        "runtime_release_digest",
                        "application_state",
                        "forced_values_present",
                        "physical_outputs_energized",
                    }
                    or observed["target_fingerprint"] != value["target_fingerprint"]
                    or observed["application_state"] != "run"
                    or observed["forced_values_present"] is not False
                    or observed["physical_outputs_energized"] is not False
                ):
                    raise ProtocolViolation("reconciliation semantic binding mismatch")
                expected_release = (
                    value["runtime_release_digest"]
                    if value["outcome"] == "succeeded"
                    else value["rollback_runtime_release_digest"]
                )
                if observed["runtime_release_digest"] != expected_release:
                    raise ProtocolViolation("reconciliation runtime release mismatch")
                result_path = self.v2 / "results" / (
                    value["reconciliation_job_id"] + ".result.json"
                )
                try:
                    reconciliation_request, reconciliation_result = (
                        load_archived_result_chain_v2(result_path, self.v2)
                    )
                except (OSError, ProtocolViolation) as exc:
                    raise ProtocolViolation("reconciliation result is missing or corrupt") from exc
                if (
                    reconciliation_result.get("job_id")
                    != value["reconciliation_job_id"]
                    or reconciliation_result.get("operation") != "deployment.reconcile"
                    or reconciliation_result.get("status") != "succeeded"
                    or reconciliation_result.get("environment_digest")
                    != self.environment_digest
                ):
                    raise ProtocolViolation("reconciliation result binding mismatch")
                input_descriptors = [
                    item
                    for item in reconciliation_request["artifacts"]
                    if item["role"] == "input"
                ]
                if len(reconciliation_request["artifacts"]) != 1 or len(input_descriptors) != 1:
                    raise ProtocolViolation("reconciliation request input binding mismatch")
                reconciliation_input = strict_json_loads_v2(
                    validate_artifact_path(input_descriptors[0]["path"], self.v2).read_bytes()
                )
                if reconciliation_input != {
                    "schema_version": "1",
                    "record_type": "DeploymentReconcileInput",
                    "original_job_id": original_job_id,
                    "target_fingerprint": value["target_fingerprint"],
                    "artifact_digest": value["artifact_digest"],
                    "rollback_digest": value["rollback_digest"],
                }:
                    raise ProtocolViolation("reconciliation request payload binding mismatch")
                reconciliation_evidence_descriptors = [
                    item
                    for item in reconciliation_result["artifacts"]
                    if item["role"] == "evidence"
                ]
                if (
                    len(reconciliation_result["artifacts"]) != 1
                    or len(reconciliation_evidence_descriptors) != 1
                ):
                    raise ProtocolViolation("reconciliation evidence descriptor binding mismatch")
                reconciliation_evidence_path = validate_artifact_path(
                    reconciliation_evidence_descriptors[0]["path"], self.v2
                )
                reconciliation_evidence = validate_deployment_worker_evidence_v1(
                    strict_json_loads_v2(reconciliation_evidence_path.read_bytes()),
                    reconciliation_result,
                )
                if (
                    reconciliation_evidence["target_fingerprint"]
                    != value["target_fingerprint"]
                    or reconciliation_evidence["candidate_digest"]
                    != value["artifact_digest"]
                    or reconciliation_evidence["rollback_digest"]
                    != value["rollback_digest"]
                    or reconciliation_evidence["outcome"] != value["outcome"]
                    or reconciliation_evidence["reconciliation_required"] is not False
                ):
                    raise ProtocolViolation("reconciliation evidence semantic binding mismatch")
            except (OSError, ProtocolViolation) as exc:
                raise ProtocolViolation("deployment gate found corrupt reconciliation evidence") from exc
            terminal.append(value)
        if len(terminal) > 1:
            first = canonical_json_bytes(terminal[0])
            if any(canonical_json_bytes(item) != first for item in terminal[1:]):
                raise ProtocolViolation("conflicting terminal reconciliation evidence")
        return terminal[0] if terminal else None

    def _require_no_unresolved_deployment(self) -> None:
        self._recover_target_queue_if_inactive()
        for path in (self.v2 / "results").glob("*.result.json"):
            try:
                result, _ = load_v2(path)
            except (OSError, ProtocolViolation) as exc:
                raise ProtocolViolation(
                    "deployment gate found corrupt result evidence"
                ) from exc
            if (
                result.get("operation") == "deployment.execute"
                and result.get("status") == "unknown_outcome"
            ):
                digest = sha256_file(path)
                if self._terminal_reconciliation_for(result["job_id"], digest) is not None:
                    continue
                raise ProtocolViolation(
                    "an earlier deployment outcome is unresolved; reconciliation is required"
                )
        for path in list((self.v2 / "inbox").glob("*.request.json")) + list(
            (self.v2 / "running").glob("*.request.json")
        ):
            try:
                request, _ = load_v2(path)
            except (OSError, ProtocolViolation) as exc:
                raise ProtocolViolation(
                    "deployment gate found corrupt running request evidence"
                ) from exc
            if request["operation"] == "deployment.execute":
                raise ProtocolViolation(
                    "an earlier deployment request is unresolved; resume its exact job before any target action"
                )

    def _start_target_worker(
        self, job_id: str, operation: str, target_fingerprint: str
    ) -> dict[str, Any]:
        if operation == "deployment.execute":
            self._record_deployment_launch_uncertainty(job_id, target_fingerprint)
        supervisor = self._require_deployment_supervisor()
        try:
            launch = supervisor.ensure_running(job_id, operation, target_fingerprint)
        except Exception as exc:
            return self._abort_target_launch(
                job_id,
                operation,
                "unknown",
                f"launcher raised {type(exc).__name__}",
            )
        if not isinstance(launch, dict):
            return self._abort_target_launch(
                job_id, operation, "unknown", "launcher returned a non-object status"
            )
        launch_state = launch.get("launch_state")
        if launch_state in {"started", "adopted"}:
            if (
                launch.get("job_id") != job_id
                or launch.get("operation") != operation
                or launch.get("target_fingerprint") != target_fingerprint
            ):
                return self._abort_target_launch(
                    job_id,
                    operation,
                    "unknown",
                    "launcher identity echo mismatch",
                )
            return launch
        if launch_state not in {"not_started", "unknown"}:
            launch_state = "unknown"
        return self._abort_target_launch(
            job_id,
            operation,
            launch_state,
            str(launch.get("diagnostic") or "launcher did not prove a live worker"),
        )

    def _record_deployment_launch_uncertainty(
        self, job_id: str, target_fingerprint: str
    ) -> dict[str, Any]:
        """Publish immutable provenance before a launcher can claim the request."""

        request_path = self.queue._matching_request(job_id)
        if request_path is None or request_path.parent.name not in {"inbox", "running"}:
            raise ProtocolViolation("deployment launch request is not durable")
        request, request_raw = load_v2(request_path)
        record = {
            "schema_version": "1",
            "record_type": "DeploymentLaunchUncertainty",
            "created_at": utc_now(),
            "correlation_id": request["correlation_id"],
            "job_id": job_id,
            "operation": "deployment.execute",
            "environment_digest": self.environment_digest,
            "target_fingerprint": target_fingerprint,
            "request_digest": sha256_bytes(request_raw),
            "launch_state": "unknown",
        }
        path = (
            self.v2
            / "state"
            / "deployment-launch-uncertainty"
            / f"{job_id}.json"
        )
        if path.exists():
            existing = strict_json_loads_v2(path.read_bytes())
            validate_deployment_launch_uncertainty_v1(
                existing,
                request=request,
                request_raw=request_raw,
                target_fingerprint=target_fingerprint,
            )
            return existing
        validate_deployment_launch_uncertainty_v1(
            record,
            request=request,
            request_raw=request_raw,
            target_fingerprint=target_fingerprint,
        )
        atomic_write_exclusive(path, canonical_json_bytes(record))
        return record

    def _durable_target_fingerprint(self, request: dict[str, Any]) -> str:
        """Validate a queued/running target request before launch or adoption."""

        validate_request_v2(request, self.v2)
        validate_artifacts_v2(
            request["artifacts"], self.v2, request=True, verify_files=True
        )
        if request["environment_digest"] != self.environment_digest:
            raise ProtocolViolation("durable target request environment mismatch")
        values = [
            (
                descriptor,
                strict_json_loads_v2(
                    validate_artifact_path(descriptor["path"], self.v2).read_bytes()
                ),
            )
            for descriptor in request["artifacts"]
        ]
        if request["operation"] in {
            "target.observe.start",
            "target.observe.read",
            "target.observe.stop",
        }:
            if len(values) != 1 or values[0][0]["role"] != "observation":
                raise ProtocolViolation("durable observation request shape mismatch")
            payload = values[0][1]
            if request["operation"] == "target.observe.start":
                if (
                    not isinstance(payload, dict)
                    or set(payload)
                    != {"target_fingerprint", "observation_profile"}
                ):
                    raise ProtocolViolation("durable observation start input mismatch")
                validate_arguments(request["operation"], payload, self.v2)
                manifest = self._observation_manifest(request["job_id"])
                if (
                    manifest["target_fingerprint"] != payload["target_fingerprint"]
                    or manifest["profile"] != payload["observation_profile"]
                    or manifest["created_at"] != request["created_at"]
                ):
                    raise ProtocolViolation("durable observation manifest binding mismatch")
                return payload["target_fingerprint"]
            if (
                not isinstance(payload, dict)
                or set(payload) != {"observation_id"}
            ):
                raise ProtocolViolation("durable observation command input mismatch")
            validate_arguments(request["operation"], payload, self.v2)
            return self._observation_manifest(payload["observation_id"])[
                "target_fingerprint"
            ]
        if request["operation"] == "deployment.reconcile":
            if len(values) != 1 or values[0][0]["role"] != "input":
                raise ProtocolViolation("durable reconciliation request shape mismatch")
            payload = values[0][1]
            if (
                not isinstance(payload, dict)
                or set(payload)
                != {
                    "schema_version",
                    "record_type",
                    "original_job_id",
                    "target_fingerprint",
                    "artifact_digest",
                    "rollback_digest",
                }
                or payload["schema_version"] != "1"
                or payload["record_type"] != "DeploymentReconcileInput"
            ):
                raise ProtocolViolation("durable reconciliation input mismatch")
            validate_digest(payload["target_fingerprint"])
            return payload["target_fingerprint"]
        if request["operation"] != "deployment.execute" or len(values) != 4:
            raise ProtocolViolation("durable deployment request shape mismatch")
        executions = [
            value
            for descriptor, value in values
            if descriptor["role"] == "input"
            and isinstance(value, dict)
            and value.get("record_type") == "DeploymentExecuteInput"
        ]
        authorities = [
            value for descriptor, value in values if descriptor["role"] == "authorization"
        ]
        if len(executions) != 1 or len(authorities) != 1:
            raise ProtocolViolation("durable deployment authority chain is ambiguous")
        execution = executions[0]
        authority = authorities[0]
        if (
            set(execution) != _DEPLOYMENT_EXECUTE_INPUT_KEYS
            or execution["schema_version"] != "1"
            or execution["record_type"] != "DeploymentExecuteInput"
        ):
            raise ProtocolViolation("durable deployment execution input mismatch")
        plan = self._load_plan(execution["plan_id"])
        self._validate_authority(authority, plan)
        for field in (
            "artifact_digest",
            "runtime_release_digest",
            "target_fingerprint",
            "rollback_digest",
            "rollback_runtime_release_digest",
            "expires_at",
            "challenge_nonce",
            "observation_digest",
            "authority_digest",
        ):
            if execution[field] != authority[field]:
                raise ProtocolViolation(
                    f"durable deployment {field} authority mismatch"
                )
        if request["authorization_digest"] != authority["authority_digest"]:
            raise ProtocolViolation("durable deployment request authority mismatch")
        return execution["target_fingerprint"]

    def _resume_durable_target_request(
        self, request: dict[str, Any]
    ) -> dict[str, Any]:
        target_fingerprint = self._durable_target_fingerprint(request)
        if not self._target_allowed(target_fingerprint):
            raise ProtocolViolation("durable target request is no longer allowlisted")
        state = self.queue.status(request["job_id"])
        launch = None
        if state["state"] in {"queued", "running"}:
            launch = self._start_target_worker(
                request["job_id"], request["operation"], target_fingerprint
            )
            state = self.queue.status(request["job_id"])
        result = {
            "job_id": request["job_id"],
            "correlation_id": request["correlation_id"],
            "operation": request["operation"],
            "state": state["state"],
            "target_fingerprint": target_fingerprint,
            "blind_retry_allowed": False,
        }
        if launch is not None:
            result["worker_launch"] = launch
        return result

    def _abort_target_launch(
        self, job_id: str, operation: str, launch_state: str, diagnostic: str
    ) -> dict[str, Any]:
        """Cancel only when the queue proves the request was never claimed.

        If a worker may already own the request, publish the same conservative
        side-effect marker used by crash recovery and request cancellation.
        This preserves the job id for reconciliation and never reports a
        terminal cancellation while target mutation could still be in flight.
        """
        try:
            cancellation = self.queue.cancel(job_id)
        except (OSError, ProtocolViolation) as exc:
            raise ProtocolViolation(
                f"deployment worker launch outcome is {launch_state}; job_id={job_id}; "
                "queue state is uncertain and reconciliation is required"
            ) from exc
        queue_state = cancellation.get("state")
        if queue_state == "cancelled":
            raise ProtocolViolation(
                f"deployment worker was not started for {operation}; job_id={job_id}; "
                "the unclaimed request was cancelled"
            )
        if queue_state == "running":
            marker = self.v2 / "state" / "side-effects" / f"{job_id}.started.json"
            if not marker.exists():
                try:
                    self.queue.mark_side_effect_started(
                        job_id, job_id, self.environment_digest
                    )
                except (OSError, ProtocolViolation):
                    pass
            raise ProtocolViolation(
                f"deployment worker launch outcome is {launch_state}; job_id={job_id}; "
                f"request may be running ({diagnostic}); reconciliation is required"
            )
        raise ProtocolViolation(
            f"deployment worker launch ended in queue state {queue_state}; job_id={job_id}; "
            "inspect job.status before any further target action"
        )

    def _load_config(self) -> dict[str, Any]:
        path = self.root / "autonomous-config.json"
        try:
            config = strict_json_loads_v2(path.read_bytes())
        except (OSError, ProtocolViolation) as exc:
            raise ProtocolViolation("invalid autonomous-config.json") from exc
        if set(config) != _CONFIG_KEYS or config["config_version"] != "2":
            raise ProtocolViolation("autonomous config has unknown, missing or unsupported fields")
        validate_digest(config["environment_digest"])
        if config["environment_digest"] == "sha256:" + ("0" * 64):
            raise ProtocolViolation("autonomous environment digest is still the placeholder")
        targets = config["target_allowlist"]
        if not isinstance(targets, list):
            raise ProtocolViolation("target_allowlist must be a list")
        seen_targets: set[str] = set()
        for target in targets:
            if not isinstance(target, dict) or set(target) != {"label", "fingerprint"}:
                raise ProtocolViolation("target allowlist entries require exact label and fingerprint")
            if not isinstance(target["label"], str) or not target["label"]:
                raise ProtocolViolation("target label must be non-empty")
            fingerprint = validate_digest(target["fingerprint"])
            if fingerprint in seen_targets:
                raise ProtocolViolation("target fingerprints must be unique")
            seen_targets.add(fingerprint)
        if config["deployment_authority_phrase"] != "yükle":
            raise ProtocolViolation("deployment authority phrase must remain exact 'yükle'")
        ttl = config["deployment_ttl_seconds"]
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not 60 <= ttl <= 600:
            raise ProtocolViolation("deployment TTL must be between 60 and 600 seconds")
        expressions = config["observation_expression_allowlist"]
        if not isinstance(expressions, list) or not all(
            isinstance(item, str) and item for item in expressions
        ):
            raise ProtocolViolation("observation expression allowlist is invalid")
        if len(expressions) != len(set(expressions)):
            raise ProtocolViolation("observation expression allowlist contains duplicates")
        return config

    @property
    def environment_digest(self) -> str:
        return self.config["environment_digest"]

    def execute_legacy(self, operation: str) -> dict[str, Any]:
        return self.legacy.execute(operation)

    def workspace_create(
        self,
        baseline_path: str,
        baseline_sha256: str,
        environment_digest: str,
        *,
        _fenced: bool = False,
    ) -> dict[str, Any]:
        if not _fenced:
            # Reserved identity serializes first ownership across host processes.
            with self._workspace_fence("00000000-0000-4000-8000-000000000000"):
                return self.workspace_create(
                    baseline_path, baseline_sha256, environment_digest, _fenced=True
                )
        if any((self.v2 / "workspaces").iterdir()):
            raise ProtocolViolation(
                "AX root supports one workspace only; continue its workspace_id "
                "or use a fresh isolated root"
            )
        arguments = {
            "baseline_path": baseline_path,
            "baseline_sha256": baseline_sha256,
            "environment_digest": environment_digest,
        }
        validate_arguments("workspace.create", arguments, self.v2)
        if environment_digest != self.environment_digest:
            raise ProtocolViolation("workspace environment digest does not match host")
        baseline = validate_artifact_path(baseline_path, self.v2)
        if not baseline.is_file() or sha256_file(baseline) != baseline_sha256:
            raise ProtocolViolation("baseline is missing or its digest does not match")
        workspace_id = str(uuid.uuid4())
        workspace_dir = self.v2 / "workspaces" / workspace_id
        workspace_dir.mkdir(parents=False, exist_ok=False)
        project_path = workspace_dir / "project.project"
        atomic_write_exclusive(project_path, baseline.read_bytes())
        now = utc_now()
        manifest = {
            "schema_version": "1",
            "workspace_id": workspace_id,
            "created_at": now,
            "baseline_path": baseline_path,
            "baseline_digest": baseline_sha256,
            "environment_digest": environment_digest,
            "revision": 0,
            "project_digest": baseline_sha256,
            "pending_job_id": None,
            "last_successful_build": None,
            "frozen_artifacts": [],
            "last_native_save": None,
            "native_candidates": [],
        }
        atomic_write_exclusive(workspace_dir / "manifest.json", canonical_json_bytes(manifest))
        return self.workspace_status(workspace_id)

    def _workspace_paths(self, workspace_id: str) -> tuple[Path, Path]:
        value = validate_workspace_id(workspace_id)
        assert value is not None
        directory = self.v2 / "workspaces" / value
        manifest_path = directory / "manifest.json"
        if not directory.is_dir() or not manifest_path.is_file():
            raise ProtocolViolation(f"unknown workspace_id: {workspace_id}")
        return directory, manifest_path

    def _load_workspace(self, workspace_id: str) -> dict[str, Any]:
        directory, manifest_path = self._workspace_paths(workspace_id)
        try:
            manifest = strict_json_loads_v2(manifest_path.read_bytes())
        except (OSError, ProtocolViolation) as exc:
            raise ProtocolViolation("invalid workspace manifest") from exc
        if set(manifest) not in (_WORKSPACE_KEYS, _LEGACY_WORKSPACE_KEYS) or manifest["schema_version"] != "1":
            raise ProtocolViolation("workspace manifest shape mismatch")
        manifest.setdefault("last_native_save", None)
        manifest.setdefault("native_candidates", [])
        if manifest["workspace_id"] != workspace_id:
            raise ProtocolViolation("workspace manifest identity mismatch")
        validate_digest(manifest["baseline_digest"])
        validate_digest(manifest["environment_digest"])
        validate_digest(manifest["project_digest"])
        if manifest["environment_digest"] != self.environment_digest:
            raise ProtocolViolation("workspace environment drift")
        revision = manifest["revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ProtocolViolation("workspace revision is invalid")
        if manifest["pending_job_id"] is not None:
            validate_correlation_id(manifest["pending_job_id"])
        project_path = directory / "project.project"
        if not project_path.is_file():
            raise ProtocolViolation("workspace project is missing")
        return manifest

    def _write_workspace(self, manifest: dict[str, Any]) -> None:
        _, manifest_path = self._workspace_paths(manifest["workspace_id"])
        atomic_write_replace(manifest_path, canonical_json_bytes(manifest))

    def workspace_status(self, workspace_id: str) -> dict[str, Any]:
        directory, _ = self._workspace_paths(workspace_id)
        manifest = self._load_workspace(workspace_id)
        actual_digest = sha256_file(directory / "project.project")
        return {
            "schema_version": "1",
            "record_type": "WorkspaceHandle",
            "workspace_id": workspace_id,
            "created_at": manifest["created_at"],
            "baseline_project_digest": manifest["baseline_digest"],
            "current_project_digest": actual_digest,
            "environment_digest": manifest["environment_digest"],
            "revision": manifest["revision"],
            "dirty": actual_digest != manifest["project_digest"],
            "state": "blocked"
            if actual_digest != manifest["project_digest"]
            else ("frozen" if manifest["frozen_artifacts"] else "active"),
        }

    def _publish_operation_input(
        self, job_id: str, arguments: dict[str, Any], role: str
    ) -> dict[str, str]:
        input_path = self.v2 / "inputs" / f"{job_id}.arguments.json"
        input_raw = canonical_json_bytes(arguments)
        atomic_write_exclusive(input_path, input_raw)
        return {
            "role": role,
            "path": f"inputs/{input_path.name}",
            "sha256": sha256_bytes(input_raw),
        }

    def _queue_workspace_operation(
        self,
        operation: str,
        arguments: dict[str, Any],
        timeout_seconds: int = 300,
        *,
        reserved_job_id: str | None = None,
        reserved_correlation_id: str | None = None,
        _fenced: bool = False,
    ) -> dict[str, Any]:
        validate_arguments(operation, arguments, self.v2)
        workspace_id = arguments["workspace_id"]
        require_single_workspace(self.v2, workspace_id)
        if not _fenced:
            with self._workspace_fence(workspace_id):
                return self._queue_workspace_operation(
                    operation, arguments, timeout_seconds,
                    reserved_job_id=reserved_job_id,
                    reserved_correlation_id=reserved_correlation_id,
                    _fenced=True,
                )
        manifest = self._load_workspace(workspace_id)
        if manifest["pending_job_id"] is not None:
            raise ProtocolViolation("workspace already has a pending mutation job")
        if manifest["revision"] != arguments["expected_revision"]:
            raise ProtocolViolation("workspace revision mismatch")
        directory, _ = self._workspace_paths(workspace_id)
        if sha256_file(directory / "project.project") != manifest["project_digest"]:
            raise ProtocolViolation("workspace project changed outside the worker protocol")
        job_id = reserved_job_id or str(uuid.uuid4())
        correlation_id = reserved_correlation_id or str(uuid.uuid4())
        validate_correlation_id(job_id)
        validate_correlation_id(correlation_id)
        descriptor = self._publish_operation_input(job_id, arguments, "input")
        transport_profile = arguments.get("transport_profile", "lan")
        workflow_deadline_at = arguments.get("workflow_deadline_at")
        request = make_request_v2(
            correlation_id=correlation_id,
            job_id=job_id,
            operation=operation,
            created_at=utc_now(),
            timeout_seconds=timeout_seconds,
            environment_digest=self.environment_digest,
            workspace_digest=manifest["project_digest"],
            expected_revision=manifest["revision"],
            authorization_digest=None,
            artifacts=[descriptor],
            scratch_root=self.v2,
            transport_profile=transport_profile,
            workflow_deadline_at=workflow_deadline_at,
        )
        queued = self.queue.submit(request)
        manifest["pending_job_id"] = job_id
        self._write_workspace(manifest)
        if self.supervisor is not None:
            try:
                queued["worker"] = self.supervisor.ensure_running(workspace_id, job_id)
            except (OSError, ProtocolViolation) as exc:
                # The request remains immutable evidence, but a launcher failure
                # must not strand the workspace lease or become an invisible job.
                self.queue.cancel(job_id)
                manifest["pending_job_id"] = None
                self._write_workspace(manifest)
                raise ProtocolViolation("AX worker launch failed closed") from exc
        queued["workspace_id"] = workspace_id
        queued["revision"] = manifest["revision"]
        if workflow_deadline_at is not None:
            queued["workflow_deadline_at"] = workflow_deadline_at
        return queued

    def source_upsert_st(
        self,
        workspace_id: str,
        expected_revision: int,
        pou_name: str,
        pou_kind: str,
        declaration: str,
        implementation: str,
        return_type: str | None = None,
    ) -> dict[str, Any]:
        return self._queue_workspace_operation(
            "source.upsert.st",
            {
                "workspace_id": workspace_id,
                "expected_revision": expected_revision,
                "pou_name": pou_name,
                "pou_kind": pou_kind,
                "return_type": return_type,
                "declaration": declaration,
                "implementation": implementation,
            },
        )

    def source_read(
        self, workspace_id: str, expected_revision: int, target: str
    ) -> dict[str, Any]:
        return self._queue_workspace_operation(
            "source.read",
            {
                "workspace_id": workspace_id,
                "expected_revision": expected_revision,
                "target": target,
            },
        )

    def ladder_networks_apply(
        self, workspace_id: str, expected_revision: int, recipe: dict[str, Any]
    ) -> dict[str, Any]:
        return self._queue_workspace_operation(
            "ladder.networks.apply",
            {"workspace_id": workspace_id, "expected_revision": expected_revision, "recipe": recipe},
        )

    def revision_apply(
        self,
        workspace_id: str,
        expected_revision: int,
        idempotency_key: str,
        st_changes: list[dict[str, Any]],
        ladder_changes: list[dict[str, Any]],
        _fenced: bool = False,
    ) -> dict[str, Any]:
        arguments = {
            "workspace_id": workspace_id,
            "expected_revision": expected_revision,
            "idempotency_key": idempotency_key,
            "st_changes": st_changes,
            "ladder_changes": ladder_changes,
        }
        validate_arguments("revision.apply", arguments, self.v2)
        for change in ladder_changes:
            try:
                validate_revision_subset(change["recipe"])
            except (LadderRecipeError, KeyError) as exc:
                raise ProtocolViolation("LD_REVISION_UNSUPPORTED: " + str(exc)) from exc
        if not _fenced:
            with self._workspace_fence(workspace_id):
                return self.revision_apply(
                    workspace_id, expected_revision, idempotency_key,
                    st_changes, ladder_changes, _fenced=True,
                )
        require_single_workspace(self.v2, workspace_id)
        directory, _ = self._workspace_paths(workspace_id)
        key_digest = sha256_bytes(idempotency_key.encode("utf-8"))
        identity_dir = directory / "idempotency"
        # The shortened name is only one part of the explicit path preflight.
        identity_path = identity_dir / f"{key_digest[7:39]}.json"
        arguments_digest = sha256_bytes(canonical_json_bytes(arguments))
        if identity_path.exists():
            identity = strict_json_loads_v2(identity_path.read_bytes())
            if identity.get("key_digest") != key_digest:
                raise ProtocolViolation("idempotency key filename collision")
            if identity.get("arguments_digest") != arguments_digest:
                raise ProtocolViolation("idempotency key was used for a different revision payload")
            try:
                status = self.job_status(identity["job_id"])
            except ProtocolViolation as exc:
                raise ProtocolViolation("reserved revision job has no reconcilable queue state") from exc
            status["idempotent_replay"] = True
            return status
        manifest = self._load_workspace(workspace_id)
        if manifest["pending_job_id"] is not None or manifest["revision"] != expected_revision:
            raise ProtocolViolation("workspace is not at the requested stable revision")
        if sha256_file(directory / "project.project") != manifest["project_digest"]:
            raise ProtocolViolation("workspace project changed outside the worker protocol")
        if identity_dir.exists():
            for other in identity_dir.glob("*.json"):
                reservation = strict_json_loads_v2(other.read_bytes())
                if reservation.get("record_type") != "RevisionIdempotency":
                    raise ProtocolViolation("workspace has a corrupt revision reservation")
                try:
                    state = self.queue.status(reservation["job_id"])["state"]
                except ProtocolViolation:
                    raise ProtocolViolation("workspace has an unreconciled revision reservation")
                if state in {"queued", "running"}:
                    raise ProtocolViolation("workspace has an outstanding revision reservation")
        job_id, correlation_id = str(uuid.uuid4()), str(uuid.uuid4())
        self._revision_path_budget(workspace_id, job_id, correlation_id, identity_path)
        identity_dir.mkdir(exist_ok=True)
        atomic_write_exclusive(identity_path, canonical_json_bytes({
            "schema_version": "1", "record_type": "RevisionIdempotency",
            "key_digest": key_digest,
            "arguments_digest": arguments_digest,
            "job_id": job_id, "correlation_id": correlation_id,
            "workspace_id": workspace_id,
        }))
        return self._queue_workspace_operation(
            "revision.apply", arguments,
            reserved_job_id=job_id, reserved_correlation_id=correlation_id,
            _fenced=True,
        )

    def _native_save_receipt(self, manifest: dict[str, Any]) -> dict[str, Any]:
        receipt = manifest["last_native_save"]
        if not isinstance(receipt, dict) or receipt.get("record_type") != "RevisionClosedReceipt":
            raise ProtocolViolation("workspace has no verified native revision receipt")
        if (receipt.get("workspace_id") != manifest["workspace_id"]
                or receipt.get("expected_revision") != manifest["revision"] - 1
                or receipt.get("after_project_digest") != manifest["project_digest"]
                or receipt.get("environment_digest") != self.environment_digest
                or receipt.get("closed") is not True
                or receipt.get("reopen_verified") is not True
                or receipt.get("save_count") != 1):
            raise ProtocolViolation("native revision receipt does not bind the current workspace")
        transaction = self.v2 / "state" / "revision-transactions" / receipt["job_id"]
        receipt_path = transaction / "receipt.json"
        if not receipt_path.is_file() or strict_json_loads_v2(receipt_path.read_bytes()) != receipt:
            raise ProtocolViolation("native revision receipt is missing or changed")
        generation = transaction / "project.project"
        if not generation.is_file() or sha256_file(generation) != receipt["after_project_digest"]:
            raise ProtocolViolation("closed native generation is missing or changed")
        return receipt

    def candidate_snapshot(self, workspace_id: str, expected_revision: int,
                           _fenced: bool = False) -> dict[str, Any]:
        validate_arguments("candidate.snapshot", {
            "workspace_id": workspace_id, "expected_revision": expected_revision,
        }, self.v2)
        if not _fenced:
            with self._workspace_fence(workspace_id):
                return self.candidate_snapshot(workspace_id, expected_revision, _fenced=True)
        require_single_workspace(self.v2, workspace_id)
        manifest = self._load_workspace(workspace_id)
        if manifest["pending_job_id"] is not None or manifest["revision"] != expected_revision:
            raise ProtocolViolation("workspace is not at the requested stable revision")
        receipt = self._native_save_receipt(manifest)
        if self.supervisor is not None:
            guard_health = getattr(self.supervisor, "guard_health", None)
            if not callable(guard_health):
                raise ProtocolViolation("AX supervisor cannot verify launch-guard health")
            guard_health(workspace_id)
        directory, _ = self._workspace_paths(workspace_id)
        project = directory / "project.project"
        digest = sha256_file(project)
        if digest != receipt["after_project_digest"]:
            raise ProtocolViolation("workspace does not match closed native generation")
        candidate_dir = self.v2 / "native_candidates"
        candidate = candidate_dir / (digest.removeprefix("sha256:") + ".project")
        if candidate.exists():
            if sha256_file(candidate) != digest:
                raise ProtocolViolation("immutable native candidate bytes are corrupt")
        else:
            atomic_write_exclusive(candidate, project.read_bytes())
        if self.supervisor is not None:
            guard_health(workspace_id)
            if sha256_file(project) != digest:
                raise ProtocolViolation("workspace changed while native candidate was published")
        record = {
            "schema_version": "1", "record_type": "NativeSavedUncompiledCandidate",
            "status": "NATIVE_SAVED_UNCOMPILED", "workspace_id": workspace_id,
            "revision": expected_revision, "project_digest": digest,
            "environment_digest": self.environment_digest,
            "revision_job_id": receipt["job_id"],
            "closed_receipt_digest": sha256_bytes(canonical_json_bytes(receipt)),
            "path": f"native_candidates/{candidate.name}",
            "build_digest": None, "deployable": False,
        }
        if record not in manifest["native_candidates"]:
            manifest["native_candidates"].append(record)
            self._write_workspace(manifest)
        return record

    def project_build(
        self,
        workspace_id: str,
        expected_revision: int,
        mode: str = "rebuild",
        max_attempts: int = 10,
        transport_profile: str = "lan",
        workflow_deadline_at: str | None = None,
    ) -> dict[str, Any]:
        if transport_profile not in {"lan", "diacloud"}:
            raise ProtocolViolation("unsupported transport_profile")
        if transport_profile == "diacloud" and workflow_deadline_at is None:
            workflow_deadline_at = (
                datetime.now(UTC) + timedelta(seconds=600)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        if workflow_deadline_at is not None:
            remaining = (parse_utc(workflow_deadline_at) - datetime.now(UTC)).total_seconds()
            maximum = 600 if transport_profile == "diacloud" else 300
            if not 0 < remaining <= maximum:
                raise ProtocolViolation("workflow deadline is expired or exceeds the profile budget")
            if transport_profile == "diacloud" and (
                self.supervisor is None
                or not getattr(self.supervisor, "supports_workflow_deadline_v1", False)
            ):
                raise ProtocolViolation("AX build worker lacks DIACloud deadline capability")
        release_identity = sha256_bytes(
            canonical_json_bytes(
                {
                    "schema_version": "1",
                    "record_type": "RuntimeReleaseIdentity",
                    "workspace_id": workspace_id,
                    "expected_revision": expected_revision,
                    "nonce": str(uuid.uuid4()),
                    "environment_digest": self.environment_digest,
                }
            )
        )
        arguments = {
            "workspace_id": workspace_id,
            "expected_revision": expected_revision,
            "mode": mode,
            "max_attempts": max_attempts,
            "runtime_release_digest": release_identity,
        }
        if workflow_deadline_at is not None:
            arguments.update(
                {"transport_profile": transport_profile, "workflow_deadline_at": workflow_deadline_at}
            )
        return self._queue_workspace_operation(
            "project.build",
            arguments,
            timeout_seconds=600 if transport_profile == "diacloud" else 300,
        )

    def ingest_worker_result(self, result: dict[str, Any]) -> dict[str, Any]:
        """Validate and commit one worker result to host state.

        This is intentionally not an MCP tool.  The bounded vendor-side worker
        publishes the result file, then the host calls this method while holding
        its single-writer lease.
        """

        validate_result_v2(result, self.v2)
        running = self.queue._matching_request(result["job_id"], "running")
        if running is None:
            raise ProtocolViolation("worker result has no running request")
        request, _ = load_v2(running)
        arguments: dict[str, Any] | None = None
        for artifact in request["artifacts"]:
            if artifact["path"].startswith("inputs/"):
                arguments = strict_json_loads_v2(
                    validate_artifact_path(artifact["path"], self.v2).read_bytes()
                )
                break
        if arguments is None or "workspace_id" not in arguments:
            return self.queue.finish(result)
        workspace_id = arguments["workspace_id"]
        manifest = self._load_workspace(workspace_id)
        if manifest["pending_job_id"] != result["job_id"]:
            raise ProtocolViolation("worker result does not own the workspace pending lease")
        manifest["pending_job_id"] = None
        if result["status"] == "succeeded":
            if result["before_project_digest"] != manifest["project_digest"]:
                raise ProtocolViolation("worker result baseline digest mismatch")
            if result["after_project_digest"] is None:
                raise ProtocolViolation("successful workspace result requires after_project_digest")
            directory, _ = self._workspace_paths(workspace_id)
            if sha256_file(directory / "project.project") != result["after_project_digest"]:
                raise ProtocolViolation("worker result does not match workspace project bytes")
            expected_result_revision = (
                manifest["revision"] + 1
                if result["operation"] in {"source.upsert.st", "ladder.networks.apply", "revision.apply"}
                else manifest["revision"]
            )
            if result["revision"] != expected_result_revision:
                raise ProtocolViolation("worker result revision violates operation semantics")
            manifest["revision"] = expected_result_revision
            manifest["project_digest"] = result["after_project_digest"]
            if result["operation"] == "revision.apply":
                outputs = [artifact for artifact in result["artifacts"] if artifact["role"] == "output"]
                if len(outputs) != 1:
                    raise ProtocolViolation("revision result lacks one output payload")
                output_path = validate_artifact_path(outputs[0]["path"], self.v2)
                payload = strict_json_loads_v2(output_path.read_bytes())
                receipt = payload.get("receipt") if isinstance(payload, dict) else None
                if not isinstance(receipt, dict) or receipt.get("job_id") != result["job_id"]:
                    raise ProtocolViolation("revision output lacks a bound closed receipt")
                if (receipt.get("before_project_digest") != result["before_project_digest"]
                        or receipt.get("after_project_digest") != result["after_project_digest"]
                        or receipt.get("expected_revision") != arguments["expected_revision"]
                        or receipt.get("closed") is not True):
                    raise ProtocolViolation("revision closed receipt/result binding mismatch")
                manifest["last_native_save"] = receipt
                self._native_save_receipt(manifest)
            if result["operation"] == "project.build":
                build_record, build_digest = self._load_build_result(
                    result, workspace_id, arguments["runtime_release_digest"]
                )
                error_count = build_record["error_count"]
                warning_count = build_record["warning_count"]
                if error_count == 0 and warning_count == 0:
                    manifest["last_successful_build"] = {
                        "job_id": result["job_id"],
                        "revision": result["revision"],
                        "project_digest": result["after_project_digest"],
                        "completed_at": result["created_at"],
                        "build_digest": build_digest,
                        "runtime_release_digest": build_record["runtime_release_digest"],
                    }
                else:
                    manifest["last_successful_build"] = None
            elif result["operation"] in {"source.upsert.st", "ladder.networks.apply", "revision.apply"}:
                manifest["last_successful_build"] = None
                if result["operation"] != "revision.apply":
                    manifest["last_native_save"] = None
        status = self.queue.finish(result)
        self._write_workspace(manifest)
        return status

    def _load_build_result(
        self, result: dict[str, Any], workspace_id: str, runtime_release_digest: str
    ) -> tuple[dict[str, Any], str]:
        outputs = [artifact for artifact in result["artifacts"] if artifact["role"] == "output"]
        if len(outputs) != 1:
            raise ProtocolViolation("successful build requires exactly one BuildResult output artifact")
        descriptor = outputs[0]
        path = validate_artifact_path(descriptor["path"], self.v2)
        build = strict_json_loads_v2(path.read_bytes())
        expected = {
            "schema_version",
            "record_type",
            "build_id",
            "created_at",
            "workspace_id",
            "revision",
            "kind",
            "status",
            "error_count",
            "warning_count",
            "diagnostics",
            "project_digest",
            "artifact_digest",
            "runtime_release_digest",
            "environment_digest",
        }
        if set(build) != expected or build["schema_version"] != "1" or build["record_type"] != "BuildResult":
            raise ProtocolViolation("BuildResult artifact shape mismatch")
        validate_correlation_id(build["build_id"])
        parse_utc(build["created_at"])
        if build["workspace_id"] != workspace_id or build["revision"] != result["revision"]:
            raise ProtocolViolation("BuildResult workspace or revision mismatch")
        if build["kind"] not in {"build", "rebuild"} or build["status"] not in {
            "passed",
            "failed",
            "blocked",
        }:
            raise ProtocolViolation("BuildResult kind or status is invalid")
        for field in ("error_count", "warning_count"):
            if isinstance(build[field], bool) or not isinstance(build[field], int) or build[field] < 0:
                raise ProtocolViolation(f"BuildResult {field} is invalid")
        if not isinstance(build["diagnostics"], list):
            raise ProtocolViolation("BuildResult diagnostics must be a list")
        validate_digest(build["project_digest"])
        if build["project_digest"] != result["after_project_digest"]:
            raise ProtocolViolation("BuildResult project digest mismatch")
        if build["artifact_digest"] is not None:
            validate_digest(build["artifact_digest"])
        validate_digest(build["runtime_release_digest"])
        if build["runtime_release_digest"] != runtime_release_digest:
            raise ProtocolViolation("BuildResult runtime release identity mismatch")
        if build["environment_digest"] != self.environment_digest:
            raise ProtocolViolation("BuildResult environment mismatch")
        if build["status"] != "passed":
            raise ProtocolViolation("successful AXQ result contains a non-passing BuildResult")
        return build, descriptor["sha256"]

    def artifact_freeze(self, workspace_id: str, expected_revision: int,
                        _fenced: bool = False,
                        _launcher_fenced: bool = False) -> dict[str, Any]:
        arguments = {"workspace_id": workspace_id, "expected_revision": expected_revision}
        validate_arguments("artifact.freeze", arguments, self.v2)
        if not _fenced:
            with self._workspace_fence(workspace_id):
                return self.artifact_freeze(workspace_id, expected_revision, _fenced=True)
        require_single_workspace(self.v2, workspace_id)
        if self.supervisor is not None and not _launcher_fenced:
            launcher_fence = getattr(self.supervisor, "launcher_fence", None)
            if not callable(launcher_fence):
                raise ProtocolViolation("AX supervisor cannot exclude replacement launches")
            with launcher_fence():
                return self.artifact_freeze(
                    workspace_id, expected_revision,
                    _fenced=True, _launcher_fenced=True,
                )
        manifest = self._load_workspace(workspace_id)
        if manifest["pending_job_id"] is not None or manifest["revision"] != expected_revision:
            raise ProtocolViolation("workspace is not at the requested stable revision")
        build = manifest["last_successful_build"]
        if not isinstance(build, dict):
            raise ProtocolViolation("workspace has no qualifying zero-error/zero-warning build")
        if build.get("revision") != expected_revision or build.get("project_digest") != manifest["project_digest"]:
            raise ProtocolViolation("qualifying build does not match current workspace")
        directory, _ = self._workspace_paths(workspace_id)
        project = directory / "project.project"
        actual_digest = sha256_file(project)
        if actual_digest != manifest["project_digest"]:
            raise ProtocolViolation("workspace changed after qualifying build")
        if self.supervisor is not None:
            request_shutdown = getattr(self.supervisor, "request_shutdown", None)
            wait_for_shutdown = getattr(self.supervisor, "wait_for_idle_shutdown", None)
            if not callable(request_shutdown) or not callable(wait_for_shutdown):
                raise ProtocolViolation("AX supervisor cannot verify bounded hidden shutdown")
            latest_launch = self.supervisor._latest_workspace_launch(workspace_id)
            request_shutdown(workspace_id)
            wait_for_shutdown(workspace_id)
            if self.supervisor._live_lease(workspace_id) is not None:
                raise ProtocolViolation("AX worker lease remained live after shutdown")
            if self.supervisor._latest_workspace_launch(workspace_id) != latest_launch:
                raise ProtocolViolation("AX worker launch changed during artifact freeze")
        # The closed-project bytes and build evidence must still be identical
        # after the worker/guard exit, while the workspace fence excludes launch.
        closed_manifest = self._load_workspace(workspace_id)
        if (closed_manifest["pending_job_id"] is not None
                or closed_manifest["revision"] != expected_revision
                or closed_manifest["project_digest"] != actual_digest
                or closed_manifest["last_successful_build"] != build
                or sha256_file(project) != actual_digest):
            raise ProtocolViolation("workspace or build identity changed during AX shutdown")
        build_result_path = self.v2 / "results" / f"{build['job_id']}.result.json"
        build_result, _ = load_v2(build_result_path)
        if build_result.get("job_id") != build["job_id"]:
            raise ProtocolViolation("qualifying build result identity changed")
        _build_record, closed_build_digest = self._load_build_result(
            build_result, workspace_id, build["runtime_release_digest"]
        )
        if closed_build_digest != build["build_digest"]:
            raise ProtocolViolation("qualifying build evidence changed during AX shutdown")
        artifact_path = self.v2 / "artifacts" / f"{actual_digest.removeprefix('sha256:')}.project"
        if artifact_path.exists():
            if sha256_file(artifact_path) != actual_digest:
                raise ProtocolViolation("immutable artifact path contains different bytes")
        else:
            atomic_write_exclusive(artifact_path, project.read_bytes())
        record = {
            "artifact_digest": actual_digest,
            "workspace_id": workspace_id,
            "revision": expected_revision,
            "environment_digest": self.environment_digest,
            "build_job_id": build["job_id"],
            "build_digest": build["build_digest"],
            "runtime_release_digest": build["runtime_release_digest"],
            "path": f"artifacts/{artifact_path.name}",
            "status": "READY_FOR_DEPLOY",
        }
        if record not in closed_manifest["frozen_artifacts"]:
            closed_manifest["frozen_artifacts"].append(record)
            self._write_workspace(closed_manifest)
        return record

    def _artifact_path(self, digest: str) -> Path:
        validate_digest(digest)
        path = self.v2 / "artifacts" / f"{digest.removeprefix('sha256:')}.project"
        if not path.is_file() or sha256_file(path) != digest:
            raise ProtocolViolation(f"artifact is missing or corrupt: {digest}")
        return path

    def _frozen_artifact_record(self, digest: str) -> dict[str, Any]:
        self._artifact_path(digest)
        for manifest_path in sorted((self.v2 / "workspaces").glob("*/manifest.json")):
            manifest = strict_json_loads_v2(manifest_path.read_bytes())
            for record in manifest.get("frozen_artifacts", []):
                if record.get("artifact_digest") == digest:
                    if "build_digest" not in record or "runtime_release_digest" not in record:
                        raise ProtocolViolation("frozen artifact is missing build/runtime identity")
                    validate_digest(record["build_digest"])
                    validate_digest(record["runtime_release_digest"])
                    return record
        raise ProtocolViolation("artifact is not registered as READY_FOR_DEPLOY")

    def _qualifying_build_workflow(self, plan: dict[str, Any]) -> tuple[str, str | None]:
        """Read the archived qualifying build, not a caller-supplied new budget."""

        frozen = self._frozen_artifact_record(plan["artifact_digest"])
        if (
            frozen["build_digest"] != plan["build_digest"]
            or frozen["runtime_release_digest"] != plan["runtime_release_digest"]
        ):
            raise ProtocolViolation("deployment plan qualifying build identity changed")
        build_job_id = frozen["build_job_id"]
        validate_correlation_id(build_job_id)
        result_path = self.v2 / "results" / f"{build_job_id}.result.json"
        try:
            request, result = load_archived_result_chain_v2(result_path, self.v2)
        except (OSError, ProtocolViolation) as exc:
            raise ProtocolViolation("qualifying build result chain is missing or corrupt") from exc
        if (
            request["job_id"] != build_job_id
            or request["operation"] != "project.build"
            or request["environment_digest"] != self.environment_digest
            or result["status"] != "succeeded"
            or result["after_project_digest"] != plan["artifact_digest"]
        ):
            raise ProtocolViolation("qualifying build result does not match deployment plan")
        outputs = [item for item in result["artifacts"] if item["role"] == "output"]
        inputs = [item for item in request["artifacts"] if item["role"] == "input"]
        if (
            len(outputs) != 1
            or outputs[0]["sha256"] != plan["build_digest"]
            or len(inputs) != 1
        ):
            raise ProtocolViolation("qualifying build artifacts do not match deployment plan")
        arguments = strict_json_loads_v2(
            validate_artifact_path(inputs[0]["path"], self.v2).read_bytes()
        )
        validate_arguments("project.build", arguments, self.v2)
        if (
            arguments["workspace_id"] != frozen["workspace_id"]
            or arguments["expected_revision"] != frozen["revision"]
            or arguments["runtime_release_digest"] != plan["runtime_release_digest"]
            or request["workspace_digest"] != plan["artifact_digest"]
        ):
            raise ProtocolViolation("qualifying build request identity mismatch")
        profile = arguments.get("transport_profile", "lan")
        deadline = arguments.get("workflow_deadline_at")
        if profile != request.get("transport_profile", "lan") or (
            deadline is not None and deadline != request["deadline_at"]
        ):
            raise ProtocolViolation("qualifying build workflow request binding mismatch")
        return profile, deadline

    def _target_allowed(self, fingerprint: str) -> bool:
        validate_digest(fingerprint)
        return any(item["fingerprint"] == fingerprint for item in self.config["target_allowlist"])

    def _profile_allowed(self, profile: dict[str, Any]) -> None:
        validate_observation_profile(profile)
        allowed = set(self.config["observation_expression_allowlist"])
        if not set(profile["expressions"]).issubset(allowed):
            raise ProtocolViolation("observation profile contains a non-allowlisted expression")

    def deployment_prepare(
        self,
        artifact_digest: str,
        target_fingerprint: str,
        rollback_digest: str,
        observation_profile: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_no_unresolved_deployment()
        arguments = {
            "artifact_digest": artifact_digest,
            "target_fingerprint": target_fingerprint,
            "rollback_digest": rollback_digest,
            "observation_profile": observation_profile,
        }
        validate_arguments("deployment.prepare", arguments, self.v2)
        artifact_record = self._frozen_artifact_record(artifact_digest)
        rollback_record = self._frozen_artifact_record(rollback_digest)
        if not self._target_allowed(target_fingerprint):
            raise ProtocolViolation("target fingerprint is not allowlisted")
        self._profile_allowed(observation_profile)
        plan_id = str(uuid.uuid4())
        challenge_nonce = str(uuid.uuid4())
        created = datetime.now(UTC).replace(microsecond=0)
        observation_raw = canonical_json_bytes(observation_profile)
        observation_digest = sha256_bytes(observation_raw)
        plan = {
            "schema_version": "1",
            "record_type": "DeploymentPlan",
            "plan_id": plan_id,
            "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expires_at": (created + timedelta(seconds=self.config["deployment_ttl_seconds"])).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "valid_for_seconds": self.config["deployment_ttl_seconds"],
            "artifact_digest": artifact_digest,
            "build_digest": artifact_record["build_digest"],
            "runtime_release_digest": artifact_record["runtime_release_digest"],
            "target_fingerprint": target_fingerprint,
            "rollback_digest": rollback_digest,
            "rollback_runtime_release_digest": rollback_record["runtime_release_digest"],
            "observation_digest": observation_digest,
            "challenge_nonce": challenge_nonce,
            "authority_digest": None,
            "one_shot": True,
            "state": "prepared",
            "reconciliation_required": False,
        }
        plan_path = self.v2 / "plans" / f"{plan_id}.plan.json"
        plan_raw = canonical_json_bytes(plan)
        atomic_write_exclusive(plan_path, plan_raw)
        observation_path = self.v2 / "plans" / f"{plan_id}.observation.json"
        atomic_write_exclusive(observation_path, observation_raw)
        challenge = {
            "schema_version": "1",
            "record_type": "DeploymentChallenge",
            "plan_id": plan_id,
            "plan_digest": sha256_bytes(plan_raw),
            "observation_digest": observation_digest,
            "challenge_nonce": challenge_nonce,
            "expires_at": plan["expires_at"],
        }
        atomic_write_exclusive(
            self.v2 / "plans" / f"{plan_id}.challenge.json",
            canonical_json_bytes(challenge),
        )
        response = dict(plan)
        response["challenge_nonce"] = challenge["challenge_nonce"]
        response["observation_digest"] = challenge["observation_digest"]
        return response

    def _load_plan(self, plan_id: str) -> dict[str, Any]:
        validate_correlation_id(plan_id)
        path = self.v2 / "plans" / f"{plan_id}.plan.json"
        try:
            plan = strict_json_loads_v2(path.read_bytes())
        except (OSError, ProtocolViolation) as exc:
            raise ProtocolViolation(f"unknown or invalid deployment plan: {plan_id}") from exc
        expected = {
            "schema_version",
            "record_type",
            "plan_id",
            "created_at",
            "expires_at",
            "valid_for_seconds",
            "artifact_digest",
            "build_digest",
            "runtime_release_digest",
            "target_fingerprint",
            "rollback_digest",
            "rollback_runtime_release_digest",
            "observation_digest",
            "challenge_nonce",
            "authority_digest",
            "one_shot",
            "state",
            "reconciliation_required",
        }
        if (
            set(plan) != expected
            or plan["schema_version"] != "1"
            or plan["record_type"] != "DeploymentPlan"
            or plan["state"] != "prepared"
            or plan["authority_digest"] is not None
            or plan["one_shot"] is not True
            or plan["reconciliation_required"] is not False
        ):
            raise ProtocolViolation("deployment plan shape or state mismatch")
        if plan["plan_id"] != plan_id:
            raise ProtocolViolation("deployment plan identity mismatch")
        parse_utc(plan["created_at"])
        created_at = parse_utc(plan["created_at"])
        expires_at = parse_utc(plan["expires_at"])
        valid_for = plan["valid_for_seconds"]
        if isinstance(valid_for, bool) or not isinstance(valid_for, int) or not 1 <= valid_for <= 600:
            raise ProtocolViolation("deployment plan valid_for_seconds is invalid")
        if expires_at != created_at + timedelta(seconds=valid_for):
            raise ProtocolViolation("deployment plan expiry invariant failed")
        for field in (
            "artifact_digest",
            "build_digest",
            "runtime_release_digest",
            "target_fingerprint",
            "rollback_digest",
            "rollback_runtime_release_digest",
            "observation_digest",
        ):
            validate_digest(plan[field])
        validate_correlation_id(plan["challenge_nonce"])
        return plan

    def _load_challenge(self, plan_id: str) -> dict[str, Any]:
        path = self.v2 / "plans" / f"{plan_id}.challenge.json"
        try:
            challenge = strict_json_loads_v2(path.read_bytes())
        except (OSError, ProtocolViolation) as exc:
            raise ProtocolViolation("deployment challenge is missing or invalid") from exc
        expected = {
            "schema_version",
            "record_type",
            "plan_id",
            "plan_digest",
            "observation_digest",
            "challenge_nonce",
            "expires_at",
        }
        if (
            set(challenge) != expected
            or challenge["schema_version"] != "1"
            or challenge["record_type"] != "DeploymentChallenge"
            or challenge["plan_id"] != plan_id
        ):
            raise ProtocolViolation("deployment challenge shape mismatch")
        validate_digest(challenge["plan_digest"])
        validate_digest(challenge["observation_digest"])
        validate_correlation_id(challenge["challenge_nonce"])
        parse_utc(challenge["expires_at"])
        plan = self._load_plan(plan_id)
        plan_path = self.v2 / "plans" / f"{plan_id}.plan.json"
        observation_path = self.v2 / "plans" / f"{plan_id}.observation.json"
        if sha256_file(plan_path) != challenge["plan_digest"]:
            raise ProtocolViolation("deployment challenge plan digest mismatch")
        if sha256_file(observation_path) != challenge["observation_digest"]:
            raise ProtocolViolation("deployment challenge observation digest mismatch")
        if plan["observation_digest"] != challenge["observation_digest"]:
            raise ProtocolViolation("deployment plan observation binding mismatch")
        profile = strict_json_loads_v2(observation_path.read_bytes())
        self._profile_allowed(profile)
        return challenge

    def deployment_execute(
        self,
        plan_id: str,
        artifact_digest: str,
        target_fingerprint: str,
        rollback_digest: str,
        expires_at: str,
        challenge_nonce: str,
        authorization_phrase: str,
        transport_profile: str = "lan",
        workflow_deadline_at: str | None = None,
    ) -> dict[str, Any]:
        if transport_profile not in {"lan", "diacloud"}:
            raise ProtocolViolation("unsupported transport_profile")
        if transport_profile == "diacloud" and workflow_deadline_at is None:
            raise ProtocolViolation("DIACloud deployment requires the build workflow deadline")
        if workflow_deadline_at is not None:
            remaining = (parse_utc(workflow_deadline_at) - datetime.now(UTC)).total_seconds()
            maximum = 600 if transport_profile == "diacloud" else 330
            if not 0 < remaining <= maximum:
                raise ProtocolViolation("workflow deadline is expired or exceeds the profile budget")
            if transport_profile == "diacloud" and (
                self.deployment_supervisor is None
                or not getattr(self.deployment_supervisor, "supports_workflow_deadline_v1", False)
            ):
                raise ProtocolViolation("AX deployment worker lacks DIACloud deadline capability")
        arguments = {
            "plan_id": plan_id,
            "artifact_digest": artifact_digest,
            "target_fingerprint": target_fingerprint,
            "rollback_digest": rollback_digest,
            "expires_at": expires_at,
            "challenge_nonce": challenge_nonce,
            "authorization_phrase": authorization_phrase,
        }
        if workflow_deadline_at is not None:
            arguments.update(
                {"transport_profile": transport_profile, "workflow_deadline_at": workflow_deadline_at}
            )
        validate_arguments("deployment.execute", arguments, self.v2)
        plan = self._load_plan(plan_id)
        challenge = self._load_challenge(plan_id)
        if authorization_phrase != self.config["deployment_authority_phrase"]:
            raise ProtocolViolation("explicit deployment authority phrase is missing")
        bindings = {
            "artifact_digest": artifact_digest,
            "target_fingerprint": target_fingerprint,
            "rollback_digest": rollback_digest,
            "expires_at": expires_at,
            "challenge_nonce": challenge_nonce,
            "observation_digest": challenge["observation_digest"],
            "runtime_release_digest": plan["runtime_release_digest"],
            "rollback_runtime_release_digest": plan["rollback_runtime_release_digest"],
        }
        if any(plan[field] != value for field, value in bindings.items() if field != "challenge_nonce"):
            raise ProtocolViolation("deployment authorization binding does not match prepared plan")
        if (
            plan["challenge_nonce"] != challenge_nonce
            or challenge["challenge_nonce"] != challenge_nonce
            or challenge["expires_at"] != expires_at
        ):
            raise ProtocolViolation("deployment challenge binding mismatch")
        build_profile, build_deadline = self._qualifying_build_workflow(plan)
        if (build_profile == "diacloud" or transport_profile == "diacloud") and (
            transport_profile != build_profile or workflow_deadline_at != build_deadline
        ):
            raise ProtocolViolation("deployment workflow differs from qualifying build")
        if not self._target_allowed(target_fingerprint):
            raise ProtocolViolation("deployment target fingerprint is no longer allowlisted")
        if parse_utc(plan["expires_at"]) <= datetime.now(UTC):
            raise ProtocolViolation("deployment plan has expired")
        self._artifact_path(plan["artifact_digest"])
        self._artifact_path(plan["rollback_digest"])
        self._require_deployment_supervisor()
        consumed = self.v2 / "plans" / f"{plan_id}.consumed.json"
        if consumed.is_file():
            try:
                prior_authority = strict_json_loads_v2(consumed.read_bytes())
            except (OSError, ProtocolViolation) as exc:
                raise ProtocolViolation("consumed deployment authority is corrupt") from exc
            self._validate_authority(prior_authority, plan)
            for field, value in bindings.items():
                if prior_authority[field] != value:
                    raise ProtocolViolation(
                        "consumed deployment authority does not match this retry"
                    )
            if (
                prior_authority.get("transport_profile", "lan") != transport_profile
                or prior_authority.get("workflow_deadline_at") != workflow_deadline_at
            ):
                raise ProtocolViolation("consumed deployment workflow deadline changed")
            expected_request = self._deployment_request_for_authority(
                plan, challenge, prior_authority
            )
            matches: list[dict[str, Any]] = []
            for request_path in self.queue._request_paths():
                try:
                    candidate_request, _prior_raw = load_v2(request_path)
                except (OSError, ProtocolViolation):
                    continue
                if (
                    candidate_request.get("operation") == "deployment.execute"
                    and candidate_request.get("authorization_digest")
                    == prior_authority["authority_digest"]
                ):
                    matches.append(candidate_request)
            if len(matches) > 1:
                raise ProtocolViolation(
                    "consumed deployment authority has multiple durable requests"
                )
            if not matches:
                queued = self.queue.submit(expected_request)
                queued.update(
                    {
                        "plan_id": plan_id,
                        "target_fingerprint": target_fingerprint,
                        "authorization_consumed": True,
                        "authorization_digest": prior_authority["authority_digest"],
                        "blind_retry_allowed": False,
                        "duplicate_suppressed": True,
                        "worker_launch": self._start_target_worker(
                            expected_request["job_id"],
                            "deployment.execute",
                            target_fingerprint,
                        ),
                    }
                )
                return queued
            if canonical_json_bytes(matches[0]) != canonical_json_bytes(expected_request):
                raise ProtocolViolation(
                    "consumed deployment durable request bytes do not match"
                )
            prior_state = self.queue.status(matches[0]["job_id"])["state"]
            if prior_state not in {"queued", "running"}:
                raise ProtocolViolation(
                    "consumed deployment job is terminal and cannot be retried"
                )
            resumed = self._resume_durable_target_request(matches[0])
            resumed.update(
                {
                    "plan_id": plan_id,
                    "authorization_consumed": True,
                    "authorization_digest": prior_authority["authority_digest"],
                    "duplicate_suppressed": True,
                }
            )
            return resumed
        self._require_no_unresolved_deployment()
        issued_at = utc_now()
        authority_binding = {
            "plan_id": plan_id,
            "artifact_digest": artifact_digest,
            "target_fingerprint": target_fingerprint,
            "rollback_digest": rollback_digest,
            "challenge_nonce": challenge_nonce,
            "observation_digest": challenge["observation_digest"],
            "runtime_release_digest": plan["runtime_release_digest"],
            "rollback_runtime_release_digest": plan["rollback_runtime_release_digest"],
            "phrase_digest": _PHRASE_DIGEST,
            "issued_at": issued_at,
            "expires_at": expires_at,
            "single_use": True,
        }
        if workflow_deadline_at is not None:
            authority_binding.update(
                {"transport_profile": transport_profile, "workflow_deadline_at": workflow_deadline_at}
            )
        authority_digest = sha256_bytes(canonical_json_bytes(authority_binding))
        marker = {
            "schema_version": "2" if workflow_deadline_at is not None else "1",
            "record_type": "DeploymentAuthority",
            "status": "consumed",
            "plan_id": plan_id,
            "artifact_digest": artifact_digest,
            "target_fingerprint": target_fingerprint,
            "rollback_digest": rollback_digest,
            "challenge_nonce": challenge_nonce,
            "observation_digest": challenge["observation_digest"],
            "runtime_release_digest": plan["runtime_release_digest"],
            "rollback_runtime_release_digest": plan["rollback_runtime_release_digest"],
            "phrase_digest": _PHRASE_DIGEST,
            "issued_at": issued_at,
            "expires_at": expires_at,
            "single_use": True,
            "consumed_at": issued_at,
            "authority_digest": authority_digest,
        }
        if workflow_deadline_at is not None:
            marker.update(
                {"transport_profile": transport_profile, "workflow_deadline_at": workflow_deadline_at}
            )
        self._validate_authority(marker, plan)
        # Consume before queueing.  An uncertain local failure stays fail-closed
        # and requires reconciliation; the host never recreates this authority.
        atomic_write_exclusive(consumed, canonical_json_bytes(marker))
        request = self._deployment_request_for_authority(plan, challenge, marker)
        job_id = request["job_id"]
        queued = self.queue.submit(request)
        launch = self._start_target_worker(
            job_id, "deployment.execute", target_fingerprint
        )
        queued.update(
            {
                "plan_id": plan_id,
                "target_fingerprint": target_fingerprint,
                "authorization_consumed": True,
                "authorization_digest": authority_digest,
                "blind_retry_allowed": False,
                "worker_launch": launch,
            }
        )
        if workflow_deadline_at is not None:
            queued["workflow_deadline_at"] = workflow_deadline_at
        return queued

    def _deployment_request_for_authority(
        self,
        plan: dict[str, Any],
        challenge: dict[str, Any],
        authority: dict[str, Any],
    ) -> dict[str, Any]:
        """Recreate the sole request identity bound to one consumed authority."""

        self._validate_authority(authority, plan)
        identity = "axmcp/deployment/" + authority["authority_digest"]
        job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, identity + "/job"))
        correlation_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, identity + "/correlation")
        )
        execution_input = {
            "schema_version": authority["schema_version"],
            "record_type": "DeploymentExecuteInput",
            "plan_id": plan["plan_id"],
            "plan_digest": challenge["plan_digest"],
            "artifact_digest": authority["artifact_digest"],
            "target_fingerprint": authority["target_fingerprint"],
            "rollback_digest": authority["rollback_digest"],
            "expires_at": authority["expires_at"],
            "challenge_nonce": authority["challenge_nonce"],
            "observation_digest": authority["observation_digest"],
            "runtime_release_digest": authority["runtime_release_digest"],
            "rollback_runtime_release_digest": authority[
                "rollback_runtime_release_digest"
            ],
            "authority_digest": authority["authority_digest"],
        }
        if authority["schema_version"] == "2":
            execution_input.update(
                {
                    "transport_profile": authority["transport_profile"],
                    "workflow_deadline_at": authority["workflow_deadline_at"],
                }
            )
        input_descriptor = self._publish_operation_input_idempotent(
            job_id, execution_input, "input"
        )
        return make_request_v2(
            correlation_id=correlation_id,
            job_id=job_id,
            operation="deployment.execute",
            created_at=authority["issued_at"],
            timeout_seconds=600 if authority.get("transport_profile") == "diacloud" else 330,
            environment_digest=self.environment_digest,
            workspace_digest=None,
            expected_revision=None,
            authorization_digest=authority["authority_digest"],
            artifacts=[
                input_descriptor,
                {
                    "role": "input",
                    "path": f"plans/{plan['plan_id']}.plan.json",
                    "sha256": challenge["plan_digest"],
                },
                {
                    "role": "observation",
                    "path": f"plans/{plan['plan_id']}.observation.json",
                    "sha256": challenge["observation_digest"],
                },
                {
                    "role": "authorization",
                    "path": f"plans/{plan['plan_id']}.consumed.json",
                    "sha256": sha256_file(
                        self.v2 / "plans" / f"{plan['plan_id']}.consumed.json"
                    ),
                },
            ],
            scratch_root=self.v2,
            transport_profile=authority.get("transport_profile", "lan"),
            workflow_deadline_at=authority.get("workflow_deadline_at"),
        )

    def _unknown_deployment_evidence(
        self,
        original_job_id: str,
        target_fingerprint: str,
        artifact_digest: str,
        rollback_digest: str,
    ) -> tuple[dict[str, Any], str, dict[str, Any], dict[str, Any] | None]:
        validate_correlation_id(original_job_id)
        result_path = self.v2 / "results" / f"{original_job_id}.result.json"
        try:
            result, _ = load_v2(result_path)
        except (OSError, ProtocolViolation) as exc:
            raise ProtocolViolation("unknown deployment result is missing or corrupt") from exc
        if (
            result.get("job_id") != original_job_id
            or result.get("operation") != "deployment.execute"
            or result.get("status") != "unknown_outcome"
            or result.get("environment_digest") != self.environment_digest
        ):
            raise ProtocolViolation("deployment is not an unresolved target mutation")
        result_digest = sha256_file(result_path)
        existing = self._terminal_reconciliation_for(original_job_id, result_digest)
        evidence_descriptors = [
            item for item in result["artifacts"] if item.get("role") == "evidence"
        ]
        if len(evidence_descriptors) != 1:
            raise ProtocolViolation("unknown deployment evidence descriptor is missing")
        descriptor = evidence_descriptors[0]
        evidence_path = validate_artifact_path(descriptor["path"], self.v2)
        if not evidence_path.is_file() or sha256_file(evidence_path) != descriptor["sha256"]:
            raise ProtocolViolation("unknown deployment evidence bytes changed")
        evidence = strict_json_loads_v2(evidence_path.read_bytes())
        if (
            not isinstance(evidence, dict)
            or evidence.get("record_type") != "AxDeploymentWorkerEvidence"
            or evidence.get("job_id") != original_job_id
            or evidence.get("target_fingerprint") != target_fingerprint
            or evidence.get("candidate_digest") != artifact_digest
            or evidence.get("rollback_digest") != rollback_digest
            or evidence.get("reconciliation_required") is not True
        ):
            raise ProtocolViolation("unknown deployment evidence binding mismatch")
        return result, result_digest, evidence, existing

    def deployment_reconcile(
        self,
        original_job_id: str,
        target_fingerprint: str,
        artifact_digest: str,
        rollback_digest: str,
    ) -> dict[str, Any]:
        """Queue a read-only target reconciliation; never retry a deployment."""

        arguments = {
            "original_job_id": original_job_id,
            "target_fingerprint": target_fingerprint,
            "artifact_digest": artifact_digest,
            "rollback_digest": rollback_digest,
        }
        validate_arguments("deployment.reconcile", arguments, self.v2)
        if not self._target_allowed(target_fingerprint):
            raise ProtocolViolation("reconciliation target fingerprint is not allowlisted")
        self._artifact_path(artifact_digest)
        self._artifact_path(rollback_digest)
        self._recover_target_queue_if_inactive()
        _result, result_digest, _evidence, existing = self._unknown_deployment_evidence(
            original_job_id,
            target_fingerprint,
            artifact_digest,
            rollback_digest,
        )
        if existing is not None:
            return {
                "job_id": existing["reconciliation_job_id"],
                "operation": "deployment.reconcile",
                "state": "succeeded",
                "original_job_id": original_job_id,
                "original_result_digest": result_digest,
                "outcome": existing["outcome"],
                "already_resolved": True,
                "blind_retry_allowed": False,
            }
        with _RECONCILIATION_PROCESS_LOCK:
            pending = self._reconciliation_claim_for(arguments)
            if pending is not None and pending["state"] in {"queued", "running"}:
                request_path = self.queue._matching_request(pending["job_id"])
                if request_path is None:
                    raise ProtocolViolation(
                        "durable reconciliation claim has no request"
                    )
                durable_request, _durable_raw = load_v2(request_path)
                resumed = self._resume_durable_target_request(durable_request)
                return {
                    "job_id": pending["job_id"],
                    "operation": "deployment.reconcile",
                    "state": resumed["state"],
                    "original_job_id": original_job_id,
                    "original_result_digest": result_digest,
                    "target_fingerprint": target_fingerprint,
                    "already_resolved": False,
                    "duplicate_suppressed": True,
                    "blind_retry_allowed": False,
                    "worker_launch": resumed["worker_launch"],
                }
            if pending is not None and pending["state"] == "result_pending_terminal":
                self._require_deployment_supervisor()
                launch = self._start_target_worker(
                    pending["job_id"], "deployment.reconcile", target_fingerprint
                )
                return {
                    "job_id": pending["job_id"],
                    "operation": "deployment.reconcile",
                    "state": "running",
                    "original_job_id": original_job_id,
                    "original_result_digest": result_digest,
                    "target_fingerprint": target_fingerprint,
                    "already_resolved": False,
                    "duplicate_suppressed": True,
                    "result_pending_terminal": True,
                    "blind_retry_allowed": False,
                    "worker_launch": launch,
                }
            payload = {
                "schema_version": "1",
                "record_type": "DeploymentReconcileInput",
                **arguments,
            }
            claim_digest = sha256_bytes(canonical_json_bytes(payload))
            if pending is None:
                archived_claims = list(
                    (self.v2 / "archive" / "reconciliation-claims").glob(
                        f"{claim_digest[7:]}.*.claim.json"
                    )
                )
                generation = len(archived_claims) + 1
                identity = f"axmcp/reconciliation/{claim_digest}/{generation}"
                claim = {
                    "schema_version": "1",
                    "record_type": "DeploymentReconciliationClaim",
                    "request_key_digest": claim_digest,
                    "generation": generation,
                    "job_id": str(uuid.uuid5(uuid.NAMESPACE_URL, identity + "/job")),
                    "correlation_id": str(
                        uuid.uuid5(uuid.NAMESPACE_URL, identity + "/correlation")
                    ),
                    "created_at": utc_now(),
                }
                claim_path = self._reconciliation_claim_path(claim_digest)
                try:
                    atomic_write_exclusive(claim_path, canonical_json_bytes(claim))
                except ProtocolViolation:
                    winner = self._reconciliation_claim_for(arguments)
                    if winner is None:
                        # Another process may still be completing the exclusive
                        # publish. Deterministic IDs suppress a second launch.
                        return {
                            "job_id": claim["job_id"],
                            "operation": "deployment.reconcile",
                            "state": "queued",
                            "original_job_id": original_job_id,
                            "original_result_digest": result_digest,
                            "target_fingerprint": target_fingerprint,
                            "already_resolved": False,
                            "duplicate_suppressed": True,
                            "blind_retry_allowed": False,
                        }
                    if winner["state"] in {"queued", "running"}:
                        request_path = self.queue._matching_request(winner["job_id"])
                        if request_path is None:
                            raise ProtocolViolation(
                                "winning reconciliation claim has no durable request"
                            )
                        durable_request, _durable_raw = load_v2(request_path)
                        resumed = self._resume_durable_target_request(durable_request)
                        return {
                            "job_id": winner["job_id"],
                            "operation": "deployment.reconcile",
                            "state": resumed["state"],
                            "original_job_id": original_job_id,
                            "original_result_digest": result_digest,
                            "target_fingerprint": target_fingerprint,
                            "already_resolved": False,
                            "duplicate_suppressed": True,
                            "blind_retry_allowed": False,
                            "worker_launch": resumed["worker_launch"],
                        }
                    if winner["state"] == "missing":
                        claim = winner
                    else:
                        raise ProtocolViolation(
                            "winning reconciliation claim is not resumable"
                        )
            else:
                claim = pending
            self._require_deployment_supervisor()
            job_id = claim["job_id"]
            descriptor = self._publish_operation_input_idempotent(
                job_id, payload, "input"
            )
            request = make_request_v2(
                correlation_id=claim["correlation_id"],
                job_id=job_id,
                operation="deployment.reconcile",
                created_at=claim["created_at"],
                timeout_seconds=60,
                environment_digest=self.environment_digest,
                workspace_digest=None,
                expected_revision=None,
                authorization_digest=None,
                artifacts=[descriptor],
                scratch_root=self.v2,
            )
            try:
                queued = self.queue.submit(request)
            except ProtocolViolation:
                try:
                    durable = self.queue.status(job_id)
                except ProtocolViolation:
                    order_matches = list(
                        (self.v2 / "state" / "order").glob(
                            f"*_{request['correlation_id']}.order.json"
                        )
                    )
                    if len(order_matches) != 1:
                        raise
                    order = strict_json_loads_v2(order_matches[0].read_bytes())
                    if (
                        set(order)
                        != {
                            "schema_version",
                            "sequence",
                            "correlation_id",
                            "job_id",
                            "request_digest",
                        }
                        or order["schema_version"] != "1"
                        or order["correlation_id"] != request["correlation_id"]
                        or order["job_id"] != job_id
                        or order["request_digest"]
                        != sha256_bytes(canonical_json_bytes(request))
                    ):
                        raise ProtocolViolation(
                            "reconciliation queue order race binding mismatch"
                        )
                    durable = {"state": "queued"}
                if durable["state"] not in {"queued", "running"}:
                    raise
                request_path = self.queue._matching_request(job_id)
                if request_path is None:
                    raise ProtocolViolation(
                        "reconciliation order exists without a durable request"
                    )
                durable_request, _durable_raw = load_v2(request_path)
                resumed = self._resume_durable_target_request(durable_request)
                return {
                    "job_id": job_id,
                    "operation": "deployment.reconcile",
                    "state": resumed["state"],
                    "original_job_id": original_job_id,
                    "original_result_digest": result_digest,
                    "target_fingerprint": target_fingerprint,
                    "already_resolved": False,
                    "duplicate_suppressed": True,
                    "blind_retry_allowed": False,
                    "worker_launch": resumed["worker_launch"],
                }
            queued.update(
                {
                    "original_job_id": original_job_id,
                    "original_result_digest": result_digest,
                    "target_fingerprint": target_fingerprint,
                    "blind_retry_allowed": False,
                    "worker_launch": self._start_target_worker(
                        job_id, "deployment.reconcile", target_fingerprint
                    ),
                }
            )
            return queued

    def _reconciliation_claim_path(self, claim_digest: str) -> Path:
        validate_digest(claim_digest)
        return (
            self.v2
            / "state"
            / "reconciliation-claims"
            / f"{claim_digest[7:]}.claim.json"
        )

    def _reconciliation_claim_for(
        self, arguments: dict[str, Any]
    ) -> dict[str, Any] | None:
        expected_payload = {
            "schema_version": "1",
            "record_type": "DeploymentReconcileInput",
            **arguments,
        }
        claim_digest = sha256_bytes(canonical_json_bytes(expected_payload))
        claim_path = self._reconciliation_claim_path(claim_digest)
        if not claim_path.is_file():
            return None
        try:
            claim = strict_json_loads_v2(claim_path.read_bytes())
        except (OSError, ProtocolViolation) as exc:
            raise ProtocolViolation("reconciliation idempotency claim is corrupt") from exc
        if (
            set(claim)
            != {
                "schema_version",
                "record_type",
                "request_key_digest",
                "generation",
                "job_id",
                "correlation_id",
                "created_at",
            }
            or claim["schema_version"] != "1"
            or claim["record_type"] != "DeploymentReconciliationClaim"
            or claim["request_key_digest"] != claim_digest
            or isinstance(claim["generation"], bool)
            or not isinstance(claim["generation"], int)
            or claim["generation"] < 1
        ):
            raise ProtocolViolation("reconciliation idempotency claim binding mismatch")
        validate_correlation_id(claim["job_id"])
        validate_correlation_id(claim["correlation_id"])
        parse_utc(claim["created_at"])
        try:
            state = self.queue.status(claim["job_id"])["state"]
        except ProtocolViolation:
            state = "missing"
        stale_missing = (
            state == "missing"
            and datetime.now(UTC) >= parse_utc(claim["created_at"]) + timedelta(seconds=60)
        )
        if state in {"blocked", "failed", "cancelled", "unknown_outcome"} or stale_missing:
            archive = (
                self.v2
                / "archive"
                / "reconciliation-claims"
                / f"{claim_digest[7:]}.{claim['job_id']}.claim.json"
            )
            archive.parent.mkdir(parents=True, exist_ok=True)
            if archive.exists():
                raise ProtocolViolation("reconciliation claim archive collision")
            try:
                os.replace(claim_path, archive)
            except FileNotFoundError as exc:
                replacement = self._reconciliation_claim_for(arguments)
                if replacement is not None:
                    return replacement
                raise ProtocolViolation("reconciliation claim changed during retirement") from exc
            return None
        if state == "succeeded":
            request_path = self.queue._matching_request(claim["job_id"])
            if request_path is None or request_path.parent.name not in {"inbox", "running"}:
                raise ProtocolViolation(
                    "successful reconciliation claim has no terminal reconciliation record"
                )
            request, request_raw = load_v2(request_path)
            if (
                request.get("protocol") != "AXQ-FILE/2"
                or request.get("message_type") != "request"
                or request.get("job_id") != claim["job_id"]
                or request.get("correlation_id") != claim["correlation_id"]
                or request.get("operation") != "deployment.reconcile"
                or request.get("environment_digest") != self.environment_digest
                or request.get("workspace_digest") is not None
                or request.get("expected_revision") is not None
                or request.get("authorization_digest") is not None
                or len(request.get("artifacts", [])) != 1
                or sha256_bytes(request_raw)
                != strict_json_loads_v2(
                    (
                        self.v2
                        / "results"
                        / f"{claim['job_id']}.result.json"
                    ).read_bytes()
                ).get("request_digest")
            ):
                raise ProtocolViolation(
                    "pre-published reconciliation request/result binding mismatch"
                )
            descriptor = request["artifacts"][0]
            if (
                descriptor.get("role") != "input"
                or strict_json_loads_v2(
                    validate_artifact_path(descriptor.get("path"), self.v2).read_bytes()
                )
                != expected_payload
            ):
                raise ProtocolViolation(
                    "pre-published reconciliation input binding mismatch"
                )
            result = dict(claim)
            result["state"] = "result_pending_terminal"
            return result
        result = dict(claim)
        result["state"] = state
        return result

    def _publish_operation_input_idempotent(
        self, job_id: str, payload: dict[str, Any], role: str
    ) -> dict[str, str]:
        validate_correlation_id(job_id)
        raw = canonical_json_bytes(payload)
        path = self.v2 / "inputs" / f"{job_id}.{role}.json"
        if path.exists():
            if path.read_bytes() != raw:
                raise ProtocolViolation("reconciliation input changed after idempotency claim")
        else:
            atomic_write_exclusive(path, raw)
        return {
            "role": role,
            "path": f"inputs/{path.name}",
            "sha256": sha256_bytes(raw),
        }

    def _validate_authority(self, authority: dict[str, Any], plan: dict[str, Any]) -> None:
        expected = {
            "schema_version",
            "record_type",
            "status",
            "plan_id",
            "artifact_digest",
            "target_fingerprint",
            "rollback_digest",
            "challenge_nonce",
            "observation_digest",
            "runtime_release_digest",
            "rollback_runtime_release_digest",
            "phrase_digest",
            "issued_at",
            "expires_at",
            "single_use",
            "consumed_at",
            "authority_digest",
        }
        version = authority.get("schema_version")
        if version == "2":
            expected |= {"transport_profile", "workflow_deadline_at"}
        if (
            set(authority) != expected
            or version not in {"1", "2"}
            or authority["record_type"] != "DeploymentAuthority"
            or authority["status"] != "consumed"
            or authority["single_use"] is not True
            or authority["phrase_digest"] != _PHRASE_DIGEST
            or authority["consumed_at"] != authority["issued_at"]
        ):
            raise ProtocolViolation("deployment authority shape or state mismatch")
        for field in (
            "plan_id",
            "artifact_digest",
            "target_fingerprint",
            "rollback_digest",
            "challenge_nonce",
            "observation_digest",
            "runtime_release_digest",
            "rollback_runtime_release_digest",
            "expires_at",
        ):
            if authority[field] != plan[field]:
                raise ProtocolViolation(f"deployment authority {field} binding mismatch")
        issued = parse_utc(authority["issued_at"])
        expires = parse_utc(authority["expires_at"])
        if not issued < expires or (expires - issued).total_seconds() > 600:
            raise ProtocolViolation("deployment authority time invariant failed")
        if version == "2":
            profile = authority["transport_profile"]
            workflow_deadline = parse_utc(authority["workflow_deadline_at"])
            maximum = 600 if profile == "diacloud" else 330
            if profile not in {"lan", "diacloud"} or not issued < workflow_deadline <= issued + timedelta(seconds=maximum):
                raise ProtocolViolation("deployment authority workflow deadline is invalid")
        binding = {
            "plan_id": authority["plan_id"],
            "artifact_digest": authority["artifact_digest"],
            "target_fingerprint": authority["target_fingerprint"],
            "rollback_digest": authority["rollback_digest"],
            "challenge_nonce": authority["challenge_nonce"],
            "observation_digest": authority["observation_digest"],
            "runtime_release_digest": authority["runtime_release_digest"],
            "rollback_runtime_release_digest": authority["rollback_runtime_release_digest"],
            "phrase_digest": authority["phrase_digest"],
            "issued_at": authority["issued_at"],
            "expires_at": authority["expires_at"],
            "single_use": authority["single_use"],
        }
        if version == "2":
            binding.update(
                {
                    "transport_profile": authority["transport_profile"],
                    "workflow_deadline_at": authority["workflow_deadline_at"],
                }
            )
        if authority["authority_digest"] != sha256_bytes(canonical_json_bytes(binding)):
            raise ProtocolViolation("deployment authority digest invariant failed")

    def target_observe_start(
        self, target_fingerprint: str, observation_profile: dict[str, Any]
    ) -> dict[str, Any]:
        self._require_no_unresolved_deployment()
        arguments = {
            "target_fingerprint": target_fingerprint,
            "observation_profile": observation_profile,
        }
        validate_arguments("target.observe.start", arguments, self.v2)
        if not self._target_allowed(target_fingerprint):
            raise ProtocolViolation("observation target fingerprint is not allowlisted")
        self._profile_allowed(observation_profile)
        if any(
            invariant["action"] == "stop_and_rollback"
            for invariant in observation_profile["fault_invariants"]
        ):
            raise ProtocolViolation(
                "standalone observation cannot authorize stop_and_rollback"
            )
        self._require_deployment_supervisor()
        identity_digest = sha256_bytes(canonical_json_bytes(arguments))
        observation_id: str | None = None
        manifest: dict[str, Any] | None = None
        manifest_path: Path | None = None
        for generation in range(10_000):
            identity = "axmcp/observation/" + identity_digest
            if generation:
                identity += f"/{generation}"
            candidate_id = str(uuid.uuid5(uuid.NAMESPACE_URL, identity))
            candidate_manifest_path = (
                self.v2 / "observations" / f"{candidate_id}.manifest.json"
            )
            if not candidate_manifest_path.exists():
                observation_id = candidate_id
                manifest_path = candidate_manifest_path
                break

            candidate_manifest = self._observation_manifest(candidate_id)
            if (
                candidate_manifest["target_fingerprint"] != target_fingerprint
                or candidate_manifest["profile"] != observation_profile
            ):
                raise ProtocolViolation("observation manifest identity collision")

            lifecycle = self._observation_lifecycle(candidate_id)
            if lifecycle is not None:
                if lifecycle["state"] == "running":
                    observation_id = candidate_id
                    manifest = candidate_manifest
                    manifest_path = candidate_manifest_path
                    break
                continue

            request_path = self.queue._matching_request(candidate_id)
            result_path = self.v2 / "results" / f"{candidate_id}.result.json"
            if request_path is None and not result_path.exists():
                # The manifest is the first durable record.  Reuse it after a
                # crash between manifest publication and queue submission.
                observation_id = candidate_id
                manifest = candidate_manifest
                manifest_path = candidate_manifest_path
                break
            state = self.queue.status(candidate_id)["state"]
            if state in {"queued", "running"}:
                observation_id = candidate_id
                manifest = candidate_manifest
                manifest_path = candidate_manifest_path
                break
            # A terminal request without lifecycle evidence is still a closed
            # generation; an intentional later call gets the next identity.
        if observation_id is None or manifest_path is None:
            raise ProtocolViolation("observation generation limit exceeded")
        correlation_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, "axmcp/observation-correlation/" + observation_id)
        )
        if manifest is None:
            manifest = {
                "schema_version": "1",
                "observation_id": observation_id,
                "target_fingerprint": target_fingerprint,
                "profile": observation_profile,
                "created_at": utc_now(),
                "data_path": f"observations/{observation_id}.jsonl",
            }
            atomic_write_exclusive(manifest_path, canonical_json_bytes(manifest))
        input_descriptor = self._publish_operation_input_idempotent(
            observation_id, arguments, "observation"
        )
        request = make_request_v2(
            correlation_id=correlation_id,
            job_id=observation_id,
            operation="target.observe.start",
            created_at=manifest["created_at"],
            timeout_seconds=300,
            environment_digest=self.environment_digest,
            workspace_digest=None,
            expected_revision=None,
            authorization_digest=None,
            artifacts=[input_descriptor],
            scratch_root=self.v2,
        )
        existing_path = self.queue._matching_request(observation_id)
        if existing_path is None:
            queued = self.queue.submit(request)
        else:
            existing, existing_raw = load_v2(existing_path)
            if existing_raw != canonical_json_bytes(request):
                raise ProtocolViolation("durable observation start request mismatch")
            queued = self._resume_durable_target_request(existing)
        launch = queued.pop("worker_launch", None)
        if launch is None and queued.get("state") in {"queued", "running"}:
            launch = self._start_target_worker(
                observation_id, "target.observe.start", target_fingerprint
            )
        queued["observation_id"] = observation_id
        queued["worker_launch"] = launch
        return queued

    def _observation_manifest(self, observation_id: str) -> dict[str, Any]:
        validate_correlation_id(observation_id)
        path = self.v2 / "observations" / f"{observation_id}.manifest.json"
        try:
            manifest = strict_json_loads_v2(path.read_bytes())
        except (OSError, ProtocolViolation) as exc:
            raise ProtocolViolation(f"unknown observation_id: {observation_id}") from exc
        expected = {
            "schema_version",
            "observation_id",
            "target_fingerprint",
            "profile",
            "created_at",
            "data_path",
        }
        if set(manifest) != expected or manifest["observation_id"] != observation_id:
            raise ProtocolViolation("observation manifest mismatch")
        validate_digest(manifest["target_fingerprint"])
        validate_arguments(
            "target.observe.start",
            {
                "target_fingerprint": manifest["target_fingerprint"],
                "observation_profile": manifest["profile"],
            },
            self.v2,
        )
        parse_utc(manifest["created_at"])
        expected_data_path = f"observations/{observation_id}.jsonl"
        if manifest["data_path"] != expected_data_path:
            raise ProtocolViolation("observation manifest data path mismatch")
        return manifest

    def _observation_lifecycle(self, observation_id: str) -> dict[str, Any] | None:
        directory = self.v2 / "state" / "observations" / observation_id
        if not directory.is_dir():
            return None
        records: list[dict[str, Any]] = []
        allowed_states = {"running", "completed", "fault", "stopped", "worker_lost"}
        expected_keys = {
            "schema_version",
            "record_type",
            "sequence",
            "observation_id",
            "state",
            "created_at",
            "worker_id",
            "target_fingerprint",
            "sample_count",
        }
        for path in sorted(directory.glob("*.json")):
            record = strict_json_loads_v2(path.read_bytes())
            sequence = len(records) + 1
            if (
                not isinstance(record, dict)
                or set(record) != expected_keys
                or record["schema_version"] != "1"
                or record["record_type"] != "ObservationLifecycle"
                or record["sequence"] != sequence
                or record["observation_id"] != observation_id
                or record["state"] not in allowed_states
                or path.name != f"{sequence:08d}-{record['state']}.json"
                or isinstance(record["sample_count"], bool)
                or not isinstance(record["sample_count"], int)
                or record["sample_count"] < 0
            ):
                raise ProtocolViolation("observation lifecycle binding mismatch")
            parse_utc(record["created_at"])
            validate_digest(record["target_fingerprint"])
            if record["worker_id"] is not None:
                validate_correlation_id(record["worker_id"])
            if sequence == 1 and record["state"] != "running":
                raise ProtocolViolation("observation lifecycle must begin running")
            if sequence > 1 and records[-1]["state"] != "running":
                raise ProtocolViolation("observation lifecycle changed after terminal state")
            records.append(record)
        return records[-1] if records else None

    def target_observe_read(self, observation_id: str) -> dict[str, Any]:
        manifest = self._observation_manifest(observation_id)
        self._require_deployment_supervisor()
        data_path = validate_artifact_path(manifest["data_path"], self.v2)
        samples: list[dict[str, Any]] = []
        if data_path.exists():
            for line in data_path.read_bytes().splitlines()[-100:]:
                samples.append(strict_json_loads_v2(line))
        data_digest = sha256_file(data_path) if data_path.is_file() else None
        lifecycle = self._observation_lifecycle(observation_id)
        lifecycle_digest = (
            sha256_bytes(canonical_json_bytes(lifecycle))
            if lifecycle is not None
            else None
        )
        read_identity = sha256_bytes(
            canonical_json_bytes(
                {
                    "observation_id": observation_id,
                    "lifecycle_digest": lifecycle_digest,
                    "data_digest": data_digest,
                }
            )
        )
        read_job_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, "axmcp/observation-read/" + read_identity)
        )
        read_correlation_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "axmcp/observation-read-correlation/" + read_identity,
            )
        )
        intent_path = (
            self.v2 / "observations" / f"{observation_id}.{read_job_id}.read-intent.json"
        )
        if intent_path.exists():
            intent = strict_json_loads_v2(intent_path.read_bytes())
            if (
                not isinstance(intent, dict)
                or set(intent)
                != {
                    "schema_version",
                    "record_type",
                    "observation_id",
                    "job_id",
                    "correlation_id",
                    "created_at",
                    "target_fingerprint",
                    "lifecycle_digest",
                    "data_digest",
                }
                or intent["schema_version"] != "1"
                or intent["record_type"] != "ObservationReadIntent"
                or intent["observation_id"] != observation_id
                or intent["job_id"] != read_job_id
                or intent["correlation_id"] != read_correlation_id
                or intent["target_fingerprint"] != manifest["target_fingerprint"]
                or intent["lifecycle_digest"] != lifecycle_digest
                or intent["data_digest"] != data_digest
            ):
                raise ProtocolViolation("observation read intent mismatch")
            parse_utc(intent["created_at"])
        else:
            intent = {
                "schema_version": "1",
                "record_type": "ObservationReadIntent",
                "observation_id": observation_id,
                "job_id": read_job_id,
                "correlation_id": read_correlation_id,
                "created_at": utc_now(),
                "target_fingerprint": manifest["target_fingerprint"],
                "lifecycle_digest": lifecycle_digest,
                "data_digest": data_digest,
            }
            atomic_write_exclusive(intent_path, canonical_json_bytes(intent))
        arguments = {"observation_id": observation_id}
        request = make_request_v2(
            correlation_id=read_correlation_id,
            job_id=read_job_id,
            operation="target.observe.read",
            created_at=intent["created_at"],
            timeout_seconds=30,
            environment_digest=self.environment_digest,
            workspace_digest=None,
            expected_revision=None,
            authorization_digest=None,
            artifacts=[
                self._publish_operation_input_idempotent(
                    read_job_id, arguments, "observation"
                )
            ],
            scratch_root=self.v2,
        )
        existing_path = self.queue._matching_request(read_job_id)
        if existing_path is None:
            queued = self.queue.submit(request)
        else:
            existing, existing_raw = load_v2(existing_path)
            if existing_raw != canonical_json_bytes(request):
                raise ProtocolViolation("durable observation read request mismatch")
            queued = self._resume_durable_target_request(existing)
        launch = queued.pop("worker_launch", None)
        if launch is None and queued.get("state") in {"queued", "running"}:
            launch = self._start_target_worker(
                read_job_id, "target.observe.read", manifest["target_fingerprint"]
            )
        if launch is not None:
            queued["worker_launch"] = launch
        return {
            "observation_id": observation_id,
            "job": self.job_status(read_job_id),
            "sample_count": len(samples),
            "samples": samples,
            "data_digest": data_digest,
            "session_state": (lifecycle or {"state": None})["state"],
        }

    def target_observe_stop(self, observation_id: str) -> dict[str, Any]:
        self._require_no_unresolved_deployment()
        manifest = self._observation_manifest(observation_id)
        self._require_deployment_supervisor()
        arguments = {"observation_id": observation_id}
        validate_arguments("target.observe.stop", arguments, self.v2)
        intent_path = self.v2 / "observations" / f"{observation_id}.stop-intent.json"
        job_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, "axmcp/observation-stop/" + observation_id)
        )
        correlation_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, "axmcp/observation-stop-correlation/" + observation_id)
        )
        if intent_path.exists():
            intent = strict_json_loads_v2(intent_path.read_bytes())
            if (
                not isinstance(intent, dict)
                or set(intent)
                != {
                    "schema_version",
                    "record_type",
                    "observation_id",
                    "job_id",
                    "correlation_id",
                    "created_at",
                    "target_fingerprint",
                }
                or intent["schema_version"] != "1"
                or intent["record_type"] != "ObservationStopIntent"
                or intent["observation_id"] != observation_id
                or intent["job_id"] != job_id
                or intent["correlation_id"] != correlation_id
                or intent["target_fingerprint"] != manifest["target_fingerprint"]
            ):
                raise ProtocolViolation("observation stop intent mismatch")
            parse_utc(intent["created_at"])
        else:
            intent = {
                "schema_version": "1",
                "record_type": "ObservationStopIntent",
                "observation_id": observation_id,
                "job_id": job_id,
                "correlation_id": correlation_id,
                "created_at": utc_now(),
                "target_fingerprint": manifest["target_fingerprint"],
            }
            atomic_write_exclusive(intent_path, canonical_json_bytes(intent))
        request = make_request_v2(
            correlation_id=correlation_id,
            job_id=job_id,
            operation="target.observe.stop",
            created_at=intent["created_at"],
            timeout_seconds=30,
            environment_digest=self.environment_digest,
            workspace_digest=None,
            expected_revision=None,
            authorization_digest=None,
            artifacts=[self._publish_operation_input_idempotent(job_id, arguments, "observation")],
            scratch_root=self.v2,
        )
        existing_path = self.queue._matching_request(job_id)
        if existing_path is None:
            queued = self.queue.submit(request)
        else:
            existing, existing_raw = load_v2(existing_path)
            if existing_raw != canonical_json_bytes(request):
                raise ProtocolViolation("durable observation stop request mismatch")
            queued = self._resume_durable_target_request(existing)
        launch = queued.pop("worker_launch", None)
        if launch is None and queued.get("state") in {"queued", "running"}:
            launch = self._start_target_worker(
                request["job_id"], "target.observe.stop", manifest["target_fingerprint"]
            )
        queued["observation_id"] = observation_id
        queued["worker_launch"] = launch
        return queued

    def _load_job_request_after_atomic_move(
        self, job_id: str
    ) -> dict[str, Any] | None:
        """Load a request across the worker's inbox-to-running rename window."""

        for _attempt in range(4):
            request_path = self.queue._matching_request(job_id)
            if request_path is None:
                continue
            try:
                request, _ = load_v2(request_path)
                return request
            except FileNotFoundError:
                # The worker claimed or the host archived the immutable request
                # between discovery and open. Re-resolve its new directory.
                continue
        return None

    def job_status(self, job_id: str) -> dict[str, Any]:
        request_before = self.queue._matching_request(job_id)
        request_raw_before: bytes | None = None
        if request_before is not None:
            try:
                # Recovery may atomically claim inbox -> running. Retain the
                # exact immutable bytes now so terminal result binding never
                # depends on the stale pre-recovery path.
                request_value, request_raw_before = load_v2(request_before)
            except (OSError, ProtocolViolation):
                request_value = None
                request_raw_before = None
            if request_value is not None and request_value.get("operation") in {
                "source.read",
                "source.upsert.st",
                "ladder.networks.apply",
                "project.build",
                "revision.apply",
            } and self.supervisor is not None:
                arguments = None
                for artifact in request_value["artifacts"]:
                    if artifact["role"] == "input":
                        arguments = strict_json_loads_v2(
                            validate_artifact_path(artifact["path"], self.v2).read_bytes()
                        )
                        break
                if arguments is None:
                    raise ProtocolViolation("workspace job input is missing during recovery")
                recovery_args = (
                    arguments["workspace_id"], job_id, request_value["workspace_digest"],
                )
                if request_value["operation"] == "revision.apply":
                    recovery = self.supervisor.recover_workspace_job(
                        *recovery_args, "revision.apply",
                    )
                else:
                    recovery = self.supervisor.recover_workspace_job(*recovery_args)
                if recovery["status"] in {"unknown_outcome", "blocked"}:
                    if request_raw_before is None:
                        raise ProtocolViolation(
                            "workspace request bytes are unavailable during recovery"
                        )
                    terminal_status = recovery["status"]
                    changed = terminal_status == "unknown_outcome"
                    uncertain = {
                        "protocol": "AXQ-FILE/2",
                        "message_type": "result",
                        "correlation_id": request_value["correlation_id"],
                        "job_id": job_id,
                        "operation": request_value["operation"],
                        "created_at": utc_now(),
                        "status": terminal_status,
                        "environment_digest": request_value["environment_digest"],
                        "workspace_digest": request_value["workspace_digest"],
                        "revision": request_value["expected_revision"],
                        "before_project_digest": request_value["workspace_digest"],
                        "after_project_digest": recovery["actual_project_digest"],
                        "request_digest": sha256_bytes(request_raw_before),
                        "artifacts": [],
                        "diagnostics": [
                            {
                                "severity": "critical" if changed else "error",
                                "code": (
                                    "AX.WORKER.LOST_AFTER_PROJECT_CHANGE"
                                    if changed
                                    else "AX.WORKER.START_ATTEMPTS_EXHAUSTED"
                                ),
                                "message": (
                                    "AX worker disappeared after workspace project bytes "
                                    "changed; automatic replay was refused."
                                    if changed
                                    else recovery["diagnostic"]
                                ),
                                "source": None,
                                "line": None,
                                "column": None,
                            }
                        ],
                    }
                    self.ingest_worker_result(uncertain)
            if request_value is not None and request_value.get("operation") in {
                "deployment.execute",
                "deployment.reconcile",
                "target.observe.start",
                "target.observe.read",
                "target.observe.stop",
            }:
                self._recover_target_queue_if_inactive()
                refreshed_path = self.queue._matching_request(job_id)
                if (
                    refreshed_path is not None
                    and not (self.v2 / "results" / f"{job_id}.result.json").exists()
                ):
                    refreshed, _refreshed_raw = load_v2(refreshed_path)
                    if refreshed.get("operation") in {
                        "deployment.execute",
                        "deployment.reconcile",
                        "target.observe.start",
                        "target.observe.read",
                        "target.observe.stop",
                    } and self.queue.status(job_id)["state"] in {"queued", "running"}:
                        self._resume_durable_target_request(refreshed)
        result_path = self.v2 / "results" / f"{job_id}.result.json"
        running_path = self.queue._matching_request(job_id, "running")
        if result_path.is_file() and running_path is not None:
            result, _ = load_v2(result_path)
            self.ingest_worker_result(result)
        internal = self.queue.status(job_id)
        request = self._load_job_request_after_atomic_move(job_id)
        result = internal.get("result")
        if result is not None:
            created_at = request["created_at"] if request is not None else result["created_at"]
            evidence = next(
                (artifact["sha256"] for artifact in result["artifacts"] if artifact["role"] == "evidence"),
                None,
            )
            output = None
            output_descriptors = [
                artifact for artifact in result["artifacts"] if artifact["role"] == "output"
            ]
            if len(output_descriptors) > 1:
                raise ProtocolViolation("job result has multiple primary output artifacts")
            if output_descriptors:
                output = strict_json_loads_v2(
                    validate_artifact_path(
                        output_descriptors[0]["path"], self.v2
                    ).read_bytes()
                )
            return {
                "schema_version": "1",
                "record_type": "JobResult",
                "job_id": job_id,
                "correlation_id": result["correlation_id"],
                "operation": result["operation"],
                "status": result["status"],
                "created_at": created_at,
                "started_at": created_at,
                "completed_at": result["created_at"],
                "result_digest": sha256_bytes(canonical_json_bytes(result)),
                "evidence_digest": evidence,
                "unknown_outcome": result["status"] == "unknown_outcome",
                "diagnostics": [item["message"] for item in result["diagnostics"]],
                "output": output,
            }
        if request is None:
            raise ProtocolViolation(f"job request evidence is missing: {job_id}")
        return {
            "schema_version": "1",
            "record_type": "JobResult",
            "job_id": job_id,
            "correlation_id": request["correlation_id"],
            "operation": request["operation"],
            "status": internal["state"],
            "created_at": request["created_at"],
            "started_at": request["created_at"] if internal["state"] == "running" else None,
            "completed_at": None,
            "result_digest": None,
            "evidence_digest": None,
            "unknown_outcome": False,
            "diagnostics": ["cancellation requested"] if internal.get("cancel_requested") else [],
            "output": None,
        }

    def job_progress(self, job_id: str) -> dict[str, Any]:
        return read_job_progress(self.queue, job_id)

    def job_cancel(self, job_id: str) -> dict[str, Any]:
        before = self.queue.status(job_id)
        self.queue.cancel(job_id)
        operation = before.get("operation")
        if operation in {
            "source.read",
            "source.upsert.st",
            "ladder.networks.apply",
            "revision.apply",
            "project.build",
        }:
            request_path = self.queue._matching_request(job_id, "archive")
            if request_path is not None:
                request, _ = self.queue_load(request_path)
                workspace_id = None
                for artifact in request["artifacts"]:
                    if artifact["path"].startswith("inputs/"):
                        operation_input = strict_json_loads_v2(
                            validate_artifact_path(artifact["path"], self.v2).read_bytes()
                        )
                        workspace_id = operation_input.get("workspace_id")
                        break
                if workspace_id is not None:
                    manifest = self._load_workspace(workspace_id)
                    if manifest["pending_job_id"] == job_id:
                        manifest["pending_job_id"] = None
                        self._write_workspace(manifest)
        return self.job_status(job_id)

    @staticmethod
    def queue_load(path: Path) -> tuple[dict[str, Any], bytes]:
        raw = path.read_bytes()
        return strict_json_loads_v2(raw), raw
