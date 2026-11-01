from __future__ import annotations

import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Collection

from .constants import PROTOCOL_V2
from .protocol_v2 import (
    load_v2,
    request_v2_bytes,
    validate_artifacts_v2,
    validate_deployment_launch_uncertainty_v1,
    validate_request_v2,
    validate_result_chain_v2,
    validate_result_v2,
)
from .security import (
    ProtocolViolation,
    atomic_write_exclusive,
    atomic_write_replace,
    canonical_json_bytes,
    parse_utc,
    sha256_bytes,
    strict_json_loads_v2,
    utc_now,
    validate_artifact_path,
    validate_correlation_id,
    validate_digest,
)

_QUEUE_DIRS = ("inbox", "running", "results", "archive", "rejected", "cancellations")
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
_DEPLOYMENT_RECOVERY_EVIDENCE_KEYS = {
    "schema_version",
    "record_type",
    "created_at",
    "correlation_id",
    "job_id",
    "operation",
    "outcome",
    "target_fingerprint",
    "authority_digest",
    "candidate_digest",
    "rollback_digest",
    "reconciliation_required",
    "sample_count",
    "diagnostics",
}


class FileJobQueue:
    """Durable single-consumer AXQ-FILE/2 queue.

    Publishing is exclusive, claiming is a same-volume atomic rename, and every
    externally visible record is immutable.  State that must change is held in
    separate manifests rather than overwriting request/result evidence.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        for name in _QUEUE_DIRS:
            (self.root / name).mkdir(exist_ok=True)
        (self.root / "state" / "order").mkdir(parents=True, exist_ok=True)
        (self.root / "state" / "side-effects").mkdir(parents=True, exist_ok=True)
        (self.root / "state" / "deployment-launch-uncertainty").mkdir(
            parents=True, exist_ok=True
        )
        self._lock = threading.Lock()

    def _request_paths(self) -> list[Path]:
        paths: list[Path] = []
        for name in ("inbox", "running", "archive", "rejected"):
            paths.extend((self.root / name).glob("*.request.json"))
        return paths

    def _matching_request(self, job_id: str, directory: str | None = None) -> Path | None:
        validate_correlation_id(job_id)
        names = (directory,) if directory else ("inbox", "running", "archive", "rejected")
        matches: list[Path] = []
        for name in names:
            for path in (self.root / name).glob("*.request.json"):
                try:
                    request, _ = load_v2(path)
                except (OSError, ProtocolViolation):
                    continue
                if request.get("job_id") == job_id:
                    matches.append(path)
        if len(matches) > 1:
            raise ProtocolViolation(f"duplicate durable job state for {job_id}")
        return matches[0] if matches else None

    def _next_sequence(self) -> int:
        maximum = 0
        for path in (self.root / "state" / "order").glob("*.order.json"):
            prefix = path.name.split("_", 1)[0]
            if prefix.isdigit():
                maximum = max(maximum, int(prefix))
        return maximum + 1

    def _duplicate_correlation(self, correlation_id: str) -> bool:
        for path in self._request_paths():
            try:
                request, _ = load_v2(path)
                if request.get("correlation_id") == correlation_id:
                    return True
            except (OSError, ProtocolViolation):
                continue
        return False

    def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        validate_request_v2(request, self.root)
        validate_artifacts_v2(request["artifacts"], self.root, request=True, verify_files=True)
        now = datetime.now(UTC)
        if parse_utc(request["deadline_at"]) <= now:
            raise ProtocolViolation("stale request cannot be queued")
        raw = request_v2_bytes(request, self.root)
        with self._lock:
            job_id = request["job_id"]
            order_matches = list(
                (self.root / "state" / "order").glob(
                    f"*_{request['correlation_id']}.order.json"
                )
            )
            if len(order_matches) > 1:
                raise ProtocolViolation("duplicate durable queue order")
            if order_matches:
                order = strict_json_loads_v2(order_matches[0].read_bytes())
                if (
                    not isinstance(order, dict)
                    or set(order)
                    != {
                        "schema_version",
                        "sequence",
                        "correlation_id",
                        "job_id",
                        "request_digest",
                    }
                    or order["schema_version"] != "1"
                    or isinstance(order["sequence"], bool)
                    or not isinstance(order["sequence"], int)
                    or order["sequence"] < 1
                    or order["correlation_id"] != request["correlation_id"]
                    or order["job_id"] != job_id
                    or order["request_digest"] != sha256_bytes(raw)
                ):
                    raise ProtocolViolation("durable queue order binding mismatch")
                if (
                    self._matching_request(job_id) is not None
                    or (self.root / "results" / f"{job_id}.result.json").exists()
                ):
                    raise ProtocolViolation(f"duplicate job_id: {job_id}")
                path = self.root / "inbox" / f"{request['correlation_id']}.request.json"
                atomic_write_exclusive(path, raw)
                return {
                    "job_id": job_id,
                    "correlation_id": request["correlation_id"],
                    "operation": request["operation"],
                    "state": "queued",
                    "queue_sequence": order["sequence"],
                    "request_digest": sha256_bytes(raw),
                }
            if self._matching_request(job_id) is not None or (self.root / "results" / f"{job_id}.result.json").exists():
                raise ProtocolViolation(f"duplicate job_id: {job_id}")
            if self._duplicate_correlation(request["correlation_id"]):
                raise ProtocolViolation(f"duplicate correlation_id: {request['correlation_id']}")
            sequence = self._next_sequence()
            name = f"{request['correlation_id']}.request.json"
            path = self.root / "inbox" / name
            order = {
                "schema_version": "1",
                "sequence": sequence,
                "correlation_id": request["correlation_id"],
                "job_id": job_id,
                "request_digest": sha256_bytes(raw),
            }
            atomic_write_exclusive(
                self.root
                / "state"
                / "order"
                / f"{sequence:020d}_{request['correlation_id']}.order.json",
                canonical_json_bytes(order),
            )
            atomic_write_exclusive(path, raw)
        return {
            "job_id": job_id,
            "correlation_id": request["correlation_id"],
            "operation": request["operation"],
            "state": "queued",
            "queue_sequence": sequence,
            "request_digest": sha256_bytes(raw),
        }

    def claim_next(self) -> tuple[dict[str, Any], bytes] | None:
        with self._lock:
            sources = list((self.root / "inbox").glob("*.request.json"))

            def order_key(path: Path) -> tuple[int, str]:
                correlation = path.name.removesuffix(".request.json")
                matches = list((self.root / "state" / "order").glob(f"*_{correlation}.order.json"))
                if len(matches) != 1:
                    return (2**63 - 1, path.name)
                prefix = matches[0].name.split("_", 1)[0]
                return (int(prefix) if prefix.isdigit() else 2**63 - 1, path.name)

            for source in sorted(sources, key=order_key):
                try:
                    request, raw = load_v2(source)
                    validate_request_v2(request, self.root)
                    validate_artifacts_v2(
                        request["artifacts"], self.root, request=True, verify_files=True
                    )
                    if parse_utc(request["deadline_at"]) <= datetime.now(UTC):
                        self._reject_path(source, "stale")
                        continue
                except (OSError, ProtocolViolation):
                    self._reject_path(source, "corrupt")
                    continue
                destination = self.root / "running" / source.name
                try:
                    os.replace(source, destination)
                except FileNotFoundError:
                    continue
                return request, raw
        return None

    def finish(self, result: dict[str, Any]) -> dict[str, Any]:
        validate_result_v2(result, self.root)
        job_id = result["job_id"]
        with self._lock:
            request_path = self._matching_request(job_id, "running")
            if request_path is None:
                raise ProtocolViolation("result has no claimed running request")
            request, request_raw = load_v2(request_path)
            validate_result_chain_v2(request, request_raw, result, self.root)
            validate_artifacts_v2(
                result["artifacts"], self.root, request=False, verify_files=True
            )
            result_path = self.root / "results" / f"{job_id}.result.json"
            result_raw = canonical_json_bytes(result)
            if result_path.exists():
                if result_path.read_bytes() != result_raw:
                    raise ProtocolViolation("pre-published worker result bytes do not match")
            else:
                atomic_write_exclusive(result_path, result_raw)
            archive_path = self.root / "archive" / request_path.name
            os.replace(request_path, archive_path)
            cancellation = self.root / "cancellations" / f"{job_id}.cancel.json"
            if cancellation.exists():
                cancellation.unlink()
        return self.status(job_id)

    def status(self, job_id: str) -> dict[str, Any]:
        validate_correlation_id(job_id)
        result_path = self.root / "results" / f"{job_id}.result.json"
        if result_path.exists():
            result, _ = load_v2(result_path)
            validate_result_v2(result, self.root)
            return {
                "job_id": job_id,
                "operation": result["operation"],
                "state": result["status"],
                "result": result,
            }
        for directory, state in (
            ("inbox", "queued"),
            ("running", "running"),
            ("rejected", "blocked"),
            ("archive", "cancelled"),
        ):
            path = self._matching_request(job_id, directory)
            if path is not None:
                try:
                    request, _ = load_v2(path)
                    operation = request.get("operation")
                except (OSError, ProtocolViolation):
                    operation = None
                return {
                    "job_id": job_id,
                    "operation": operation,
                    "state": state,
                    "cancel_requested": (self.root / "cancellations" / f"{job_id}.cancel.json").exists(),
                }
        raise ProtocolViolation(f"unknown job_id: {job_id}")

    def cancel(self, job_id: str) -> dict[str, Any]:
        validate_correlation_id(job_id)
        with self._lock:
            result_path = self.root / "results" / f"{job_id}.result.json"
            if result_path.exists():
                return self.status(job_id)
            queued = self._matching_request(job_id, "inbox")
            if queued is not None:
                request, request_raw = load_v2(queued)
                marker = self._ensure_cancel_intent(job_id)
                running = self.root / "running" / queued.name
                os.replace(queued, running)
                result = self._cancelled_result(request, request_raw, marker)
                # finish re-enters no queue lock when called after this block.
            else:
                running = self._matching_request(job_id, "running")
                if running is None:
                    raise ProtocolViolation(f"unknown job_id: {job_id}")
                self._ensure_cancel_intent(job_id)
                return self.status(job_id)
        return self.finish(result)

    def _ensure_cancel_intent(self, job_id: str) -> dict[str, Any]:
        path = self.root / "cancellations" / f"{job_id}.cancel.json"
        if path.exists():
            marker = strict_json_loads_v2(path.read_bytes())
            if (
                not isinstance(marker, dict)
                or set(marker) != {"schema_version", "job_id", "requested_at"}
                or marker["schema_version"] != "1"
                or marker["job_id"] != job_id
            ):
                raise ProtocolViolation("cancel intent binding mismatch")
            parse_utc(marker["requested_at"])
            return marker
        marker = {
            "schema_version": "1",
            "job_id": job_id,
            "requested_at": utc_now(),
        }
        atomic_write_exclusive(path, canonical_json_bytes(marker))
        return marker

    def _cancelled_result(
        self,
        request: dict[str, Any],
        request_raw: bytes,
        marker: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "protocol": PROTOCOL_V2,
            "message_type": "result",
            "correlation_id": request["correlation_id"],
            "job_id": request["job_id"],
            "operation": request["operation"],
            "created_at": marker["requested_at"],
            "status": "cancelled",
            "environment_digest": request["environment_digest"],
            "workspace_digest": request["workspace_digest"],
            "revision": request["expected_revision"],
            "before_project_digest": request["workspace_digest"],
            "after_project_digest": request["workspace_digest"],
            "request_digest": sha256_bytes(request_raw),
            "artifacts": [],
            "diagnostics": [
                {
                    "severity": "info",
                    "code": "cancelled_before_claim",
                    "message": "Job was cancelled before the worker claimed it.",
                    "source": None,
                    "line": None,
                    "column": None,
                }
            ],
        }

    def is_cancel_requested(self, job_id: str) -> bool:
        validate_correlation_id(job_id)
        return (self.root / "cancellations" / f"{job_id}.cancel.json").is_file()

    def mark_side_effect_started(
        self, job_id: str, worker_id: str, environment_digest: str
    ) -> dict[str, Any]:
        validate_correlation_id(job_id)
        validate_correlation_id(worker_id)
        from .security import validate_digest

        validate_digest(environment_digest)
        request_path = self._matching_request(job_id, "running")
        if request_path is None:
            raise ProtocolViolation("side-effect marker requires a running job")
        request, _ = load_v2(request_path)
        if request["environment_digest"] != environment_digest:
            raise ProtocolViolation("side-effect marker environment mismatch")
        marker = {
            "schema_version": "1",
            "job_id": job_id,
            "worker_id": worker_id,
            "operation": request["operation"],
            "environment_digest": environment_digest,
            "started_at": utc_now(),
        }
        atomic_write_exclusive(
            self.root / "state" / "side-effects" / f"{job_id}.started.json",
            canonical_json_bytes(marker),
        )
        return marker

    def recover(self, operations: Collection[str] | None = None) -> dict[str, int]:
        allowed_operations = None if operations is None else frozenset(operations)
        if allowed_operations is not None and any(
            not isinstance(item, str) or not item for item in allowed_operations
        ):
            raise ProtocolViolation("recovery operation filter is invalid")
        counts = {
            "partial_rejected": 0,
            "corrupt_rejected": 0,
            "stale_rejected": 0,
            "requeued": 0,
            "blocked_for_reconciliation": 0,
            "unknown_outcome": 0,
            "cancelled": 0,
        }
        with self._lock:
            if allowed_operations is None:
                for partial in sorted((self.root / "inbox").glob("*.partial")):
                    self._reject_path(partial, "partial")
                    counts["partial_rejected"] += 1
            for directory in ("inbox", "running"):
                for path in sorted((self.root / directory).glob("*.request.json")):
                    try:
                        request, request_raw = load_v2(path)
                        validate_request_v2(request, self.root)
                        validate_artifacts_v2(
                            request["artifacts"], self.root, request=True, verify_files=True
                        )
                    except (OSError, ProtocolViolation):
                        if allowed_operations is None:
                            self._reject_path(path, "corrupt")
                            counts["corrupt_rejected"] += 1
                        continue
                    if (
                        allowed_operations is not None
                        and request["operation"] not in allowed_operations
                    ):
                        continue
                    published_result = (
                        self.root / "results" / f"{request['job_id']}.result.json"
                    )
                    if (
                        directory == "inbox"
                        and request["operation"] == "deployment.reconcile"
                        and published_result.exists()
                    ):
                        # A target worker must finish the prepared->terminal
                        # commit even if the original read-only request deadline
                        # elapsed while the process was down.
                        continue
                    if directory == "running":
                        result_path = published_result
                        if result_path.exists():
                            try:
                                result, _result_raw = load_v2(result_path)
                                request_raw = path.read_bytes()
                                validate_result_chain_v2(
                                    request, request_raw, result, self.root
                                )
                                validate_artifacts_v2(
                                    result["artifacts"],
                                    self.root,
                                    request=False,
                                    verify_files=True,
                                )
                            except (OSError, ProtocolViolation):
                                if allowed_operations is None:
                                    self._reject_path(path, "corrupt")
                                    counts["corrupt_rejected"] += 1
                                continue
                            if request["operation"] == "deployment.reconcile":
                                # The deployment runtime must atomically expose
                                # the prepared reconciliation record only after
                                # this exact result/evidence chain is durable.
                                os.replace(path, self.root / "inbox" / path.name)
                                counts["requeued"] += 1
                            else:
                                os.replace(path, self.root / "archive" / path.name)
                            cancel_path = (
                                self.root
                                / "cancellations"
                                / f"{request['job_id']}.cancel.json"
                            )
                            if cancel_path.exists():
                                cancel_path.unlink()
                            continue
                    cancel_path = (
                        self.root
                        / "cancellations"
                        / f"{request['job_id']}.cancel.json"
                    )
                    side_effect_path = (
                        self.root
                        / "state"
                        / "side-effects"
                        / f"{request['job_id']}.started.json"
                    )
                    launch_uncertainty_path = (
                        self.root
                        / "state"
                        / "deployment-launch-uncertainty"
                        / f"{request['job_id']}.json"
                    )
                    if (
                        cancel_path.exists()
                        and not side_effect_path.exists()
                        and not (
                            directory == "running"
                            and request["operation"] == "deployment.execute"
                            and launch_uncertainty_path.exists()
                        )
                    ):
                        marker = self._ensure_cancel_intent(request["job_id"])
                        if directory == "inbox":
                            running_path = self.root / "running" / path.name
                            os.replace(path, running_path)
                            path = running_path
                        self._recover_cancelled(path, request, request_raw, marker)
                        counts["cancelled"] += 1
                        continue
                    if directory == "running":
                        side_effect = (
                            self.root
                            / "state"
                            / "side-effects"
                            / f"{request['job_id']}.started.json"
                        )
                        launch_uncertainty = (
                            self.root
                            / "state"
                            / "deployment-launch-uncertainty"
                            / f"{request['job_id']}.json"
                        )
                        if (
                            request["operation"] == "deployment.execute"
                            and not side_effect.exists()
                            and launch_uncertainty.exists()
                        ):
                            execution = self._deployment_execution_input(request)
                            validate_deployment_launch_uncertainty_v1(
                                strict_json_loads_v2(launch_uncertainty.read_bytes()),
                                request=request,
                                request_raw=request_raw,
                                target_fingerprint=execution["target_fingerprint"],
                            )
                            self.mark_side_effect_started(
                                request["job_id"],
                                request["job_id"],
                                request["environment_digest"],
                            )
                        if side_effect.exists():
                            terminal = (
                                "unknown_outcome"
                                if request["operation"] == "deployment.execute"
                                else "blocked"
                            )
                            self._recover_terminal(path, request, terminal)
                            key = (
                                "unknown_outcome"
                                if terminal == "unknown_outcome"
                                else "blocked_for_reconciliation"
                            )
                            counts[key] += 1
                        else:
                            if parse_utc(request["deadline_at"]) <= datetime.now(UTC):
                                self._reject_path(path, "stale")
                                counts["stale_rejected"] += 1
                                continue
                            os.replace(path, self.root / "inbox" / path.name)
                            counts["requeued"] += 1
                    elif parse_utc(request["deadline_at"]) <= datetime.now(UTC):
                        self._reject_path(path, "stale")
                        counts["stale_rejected"] += 1
        return counts

    def _recover_cancelled(
        self,
        path: Path,
        request: dict[str, Any],
        request_raw: bytes,
        marker: dict[str, Any],
    ) -> None:
        result = self._cancelled_result(request, request_raw, marker)
        result_raw = canonical_json_bytes(result)
        validate_result_chain_v2(request, request_raw, result, self.root)
        result_path = self.root / "results" / f"{request['job_id']}.result.json"
        if result_path.exists():
            if result_path.read_bytes() != result_raw:
                raise ProtocolViolation("pre-published cancelled result mismatch")
        else:
            atomic_write_exclusive(result_path, result_raw)
        archive_path = self.root / "archive" / path.name
        if archive_path.exists():
            if archive_path.read_bytes() != request_raw:
                raise ProtocolViolation("cancelled request archive mismatch")
            path.unlink()
        else:
            os.replace(path, archive_path)
        cancel_path = self.root / "cancellations" / f"{request['job_id']}.cancel.json"
        if cancel_path.exists():
            cancel_path.unlink()

    def _deployment_execution_input(
        self, request: dict[str, Any]
    ) -> dict[str, Any]:
        execution_inputs: list[dict[str, Any]] = []
        for descriptor in request["artifacts"]:
            if descriptor["role"] != "input":
                continue
            value = strict_json_loads_v2(
                (self.root / descriptor["path"]).read_bytes()
            )
            if isinstance(value, dict) and value.get("record_type") == "DeploymentExecuteInput":
                execution_inputs.append(value)
        if len(execution_inputs) != 1:
            raise ProtocolViolation(
                "deployment recovery requires one exact execution input"
            )
        execution = execution_inputs[0]
        legacy = (
            execution.get("schema_version") == "1"
            and set(execution) == _DEPLOYMENT_EXECUTE_INPUT_KEYS
            and request.get("transport_profile", "lan") == "lan"
        )
        bounded = (
            execution.get("schema_version") == "2"
            and set(execution) == _DEPLOYMENT_EXECUTE_INPUT_KEYS | {"transport_profile", "workflow_deadline_at"}
            and execution.get("transport_profile") in {"lan", "diacloud"}
            and execution["transport_profile"] == request.get("transport_profile", "lan")
            and execution.get("workflow_deadline_at") == request.get("deadline_at")
        )
        if (not (legacy or bounded) or execution["record_type"] != "DeploymentExecuteInput"):
            raise ProtocolViolation("deployment recovery execution input shape mismatch")
        validate_correlation_id(execution["plan_id"])
        validate_correlation_id(execution["challenge_nonce"])
        parse_utc(execution["expires_at"])
        for field in (
            "plan_digest",
            "artifact_digest",
            "runtime_release_digest",
            "target_fingerprint",
            "rollback_digest",
            "rollback_runtime_release_digest",
            "observation_digest",
            "authority_digest",
        ):
            validate_digest(execution[field])
        if request["authorization_digest"] != execution["authority_digest"]:
            raise ProtocolViolation("deployment recovery authority binding mismatch")
        return execution

    def _deployment_recovery_evidence(
        self, request: dict[str, Any], message: str
    ) -> dict[str, str]:
        try:
            execution = self._deployment_execution_input(request)
        except (OSError, ProtocolViolation) as exc:
            raise ProtocolViolation(
                "deployment recovery input is missing or corrupt"
            ) from exc
        relative = f"evidence/{request['job_id']}.ax-deployment-recovery.json"
        path = validate_artifact_path(relative, self.root)
        if path.exists():
            raw = path.read_bytes()
            try:
                existing = strict_json_loads_v2(raw)
            except ProtocolViolation as exc:
                raise ProtocolViolation(
                    "pre-published deployment recovery evidence is corrupt"
                ) from exc
            if not isinstance(existing, dict) or set(existing) != _DEPLOYMENT_RECOVERY_EVIDENCE_KEYS:
                raise ProtocolViolation(
                    "pre-published deployment recovery evidence shape mismatch"
                )
            created_at = existing["created_at"]
            parse_utc(created_at)
        else:
            created_at = utc_now()
        evidence = {
            "schema_version": "1",
            "record_type": "AxDeploymentWorkerEvidence",
            "created_at": created_at,
            "correlation_id": request["correlation_id"],
            "job_id": request["job_id"],
            "operation": "deployment.execute",
            "outcome": "unknown_outcome",
            "target_fingerprint": execution["target_fingerprint"],
            "authority_digest": execution["authority_digest"],
            "candidate_digest": execution["artifact_digest"],
            "rollback_digest": execution["rollback_digest"],
            "reconciliation_required": True,
            "sample_count": 0,
            "diagnostics": [message],
        }
        evidence_raw = canonical_json_bytes(evidence)
        if path.exists():
            if raw != evidence_raw:
                raise ProtocolViolation(
                    "pre-published deployment recovery evidence mismatch"
                )
        else:
            atomic_write_exclusive(path, evidence_raw)
        return {
            "role": "evidence",
            "path": relative,
            "sha256": sha256_bytes(evidence_raw),
        }

    def _recover_terminal(
        self, path: Path, request: dict[str, Any], status: str
    ) -> None:
        raw = path.read_bytes()
        message = (
            "A recorded side effect was interrupted; reconcile exact external state "
            "before any further action."
        )
        artifacts: list[dict[str, str]] = []
        if status == "unknown_outcome" and request["operation"] == "deployment.execute":
            artifacts.append(self._deployment_recovery_evidence(request, message))
        result = {
            "protocol": PROTOCOL_V2,
            "message_type": "result",
            "correlation_id": request["correlation_id"],
            "job_id": request["job_id"],
            "operation": request["operation"],
            "created_at": utc_now(),
            "status": status,
            "environment_digest": request["environment_digest"],
            "workspace_digest": request["workspace_digest"],
            "request_digest": sha256_bytes(raw),
            "before_project_digest": request["workspace_digest"],
            "after_project_digest": None,
            "revision": request["expected_revision"],
            "artifacts": artifacts,
            "diagnostics": [
                {
                    "severity": "critical" if status == "unknown_outcome" else "error",
                    "code": "reconciliation_required",
                    "message": message,
                    "source": None,
                    "line": None,
                    "column": None,
                }
            ],
        }
        validate_result_chain_v2(request, raw, result, self.root)
        atomic_write_exclusive(
            self.root / "results" / f"{request['job_id']}.result.json",
            canonical_json_bytes(result),
        )
        os.replace(path, self.root / "archive" / path.name)

    def _reject_path(self, path: Path, reason: str) -> None:
        destination = self.root / "rejected" / f"{reason}.{path.name}"
        suffix = 0
        while destination.exists():
            suffix += 1
            destination = self.root / "rejected" / f"{reason}.{suffix}.{path.name}"
        os.replace(path, destination)


class WorkerLease:
    def __init__(
        self,
        root: Path,
        stale_seconds: int = 30,
        pid_alive: Callable[[int], bool] | None = None,
    ) -> None:
        if not 5 <= stale_seconds <= 300:
            raise ProtocolViolation("lease stale_seconds must be between 5 and 300")
        self.root = root.resolve()
        self.state = self.root / "state"
        self.state.mkdir(parents=True, exist_ok=True)
        self.path = self.state / "worker-lease.json"
        self.stale_seconds = stale_seconds
        self._pid_alive = pid_alive or self._default_pid_alive

    @staticmethod
    def _default_pid_alive(pid: int) -> bool:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return False
        if os.name == "nt":
            from .window_guard import _pid_alive
            return _pid_alive(pid)
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def _read(self) -> dict[str, Any]:
        try:
            value = strict_json_loads_v2(self.path.read_bytes())
        except (OSError, ProtocolViolation) as exc:
            raise ProtocolViolation("invalid worker lease") from exc
        expected = {
            "schema_version",
            "worker_id",
            "pid",
            "process_start_fingerprint",
            "environment_digest",
            "sequence",
            "acquired_at",
            "heartbeat_at",
        }
        if set(value) != expected or value["schema_version"] != "1":
            raise ProtocolViolation("worker lease shape mismatch")
        validate_correlation_id(value["worker_id"])
        if isinstance(value["pid"], bool) or not isinstance(value["pid"], int) or value["pid"] <= 0:
            raise ProtocolViolation("invalid worker lease PID")
        parse_utc(value["acquired_at"])
        parse_utc(value["heartbeat_at"])
        from .security import validate_digest

        validate_digest(value["process_start_fingerprint"])
        validate_digest(value["environment_digest"])
        if isinstance(value["sequence"], bool) or not isinstance(value["sequence"], int) or value["sequence"] < 0:
            raise ProtocolViolation("worker lease sequence is invalid")
        return value

    def acquire(
        self,
        worker_id: str,
        pid: int,
        process_start_fingerprint: str,
        environment_digest: str,
        now: str | None = None,
    ) -> dict[str, Any]:
        from .security import validate_digest

        validate_correlation_id(worker_id)
        validate_digest(process_start_fingerprint)
        validate_digest(environment_digest)
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ProtocolViolation("worker PID must be positive")
        timestamp = now or utc_now()
        current_time = parse_utc(timestamp)
        if self.path.exists():
            current = self._read()
            age = (current_time - parse_utc(current["heartbeat_at"])).total_seconds()
            if age <= self.stale_seconds or self._pid_alive(current["pid"]):
                raise ProtocolViolation("another worker lease is active")
            archive = self.state / f"worker-lease.{current['worker_id']}.stale.json"
            if archive.exists():
                raise ProtocolViolation("stale lease archive collision")
            os.replace(self.path, archive)
        record = {
            "schema_version": "1",
            "worker_id": worker_id,
            "pid": pid,
            "process_start_fingerprint": process_start_fingerprint,
            "environment_digest": environment_digest,
            "sequence": 0,
            "acquired_at": timestamp,
            "heartbeat_at": timestamp,
        }
        atomic_write_exclusive(self.path, canonical_json_bytes(record))
        return record

    def heartbeat(
        self,
        worker_id: str,
        process_start_fingerprint: str,
        environment_digest: str,
        now: str | None = None,
    ) -> dict[str, Any]:
        current = self._read()
        if current["worker_id"] != validate_correlation_id(worker_id):
            raise ProtocolViolation("worker lease owner mismatch")
        if current["process_start_fingerprint"] != process_start_fingerprint:
            raise ProtocolViolation("worker process-start fingerprint mismatch")
        if current["environment_digest"] != environment_digest:
            raise ProtocolViolation("worker environment digest mismatch")
        timestamp = now or utc_now()
        if parse_utc(timestamp) < parse_utc(current["heartbeat_at"]):
            raise ProtocolViolation("worker heartbeat cannot move backwards")
        current["heartbeat_at"] = timestamp
        current["sequence"] += 1
        atomic_write_replace(self.path, canonical_json_bytes(current))
        return current

    def release(self, worker_id: str) -> None:
        current = self._read()
        if current["worker_id"] != validate_correlation_id(worker_id):
            raise ProtocolViolation("worker lease owner mismatch")
        self.path.unlink()
