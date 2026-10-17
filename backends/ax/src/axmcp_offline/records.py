from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from .constants import ALLOWED_OPERATIONS, PROTOCOL
from .security import (
    ProtocolViolation,
    canonical_json_bytes,
    parse_utc,
    sha256_bytes,
    sha256_file,
    strict_json_loads,
    validate_artifact_path,
    validate_correlation_id,
    validate_digest,
)

_REQUEST_KEYS = {
    "protocol",
    "message_type",
    "correlation_id",
    "operation",
    "created_at",
    "deadline_at",
    "environment_digest",
    "artifacts",
}
_RESULT_KEYS = {
    "protocol",
    "message_type",
    "correlation_id",
    "operation",
    "created_at",
    "status",
    "environment_digest",
    "request_digest",
    "artifacts",
    "diagnostics",
}
_EVIDENCE_KEYS = {
    "schema_version",
    "record_type",
    "created_at",
    "correlation_id",
    "producer",
    "evidence_class",
    "input_digest",
    "output_digest",
    "environment_digest",
    "diagnostics",
}
_ENV_KEYS = {
    "schema_version",
    "record_type",
    "captured_at",
    "executable",
    "profile",
    "product",
    "script_engine",
    "target",
    "devices",
    "libraries",
    "tasks",
}
_CAPABILITY_KEYS = {
    "schema_version",
    "record_type",
    "captured_at",
    "operation",
    "status",
    "mode",
    "limitations",
    "environment_digest",
}
_EVIDENCE_CLASSES = {
    "CLAIM",
    "SOURCE_REVIEWED",
    "STATIC_CHECKED",
    "SIMULATED",
    "HUMAN_IDE_COMPILED",
    "HUMAN_BENCH_VERIFIED",
    "HUMAN_FAT_ACCEPTED",
}


def _exact_keys(record: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(record)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise ProtocolViolation(f"{label} key mismatch; missing={missing}, unknown={unknown}")


def validate_operation(operation: str) -> str:
    if operation not in ALLOWED_OPERATIONS:
        raise ProtocolViolation(f"operation is not allowlisted: {operation!r}")
    return operation


def validate_artifacts(value: Any, scratch_root: Path | None = None) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ProtocolViolation("artifacts must be a list")
    output: list[dict[str, str]] = []
    seen: set[str] = set()
    for artifact in value:
        if not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}:
            raise ProtocolViolation("artifact descriptor must contain exact path and sha256")
        path = artifact["path"]
        validate_artifact_path(path, scratch_root)
        validate_digest(artifact["sha256"])
        if path in seen:
            raise ProtocolViolation("duplicate artifact path")
        seen.add(path)
        output.append({"path": path, "sha256": artifact["sha256"]})
    return output


def verify_artifact_files(value: Any, scratch_root: Path) -> list[dict[str, str]]:
    artifacts = validate_artifacts(value, scratch_root)
    for artifact in artifacts:
        target = validate_artifact_path(artifact["path"], scratch_root)
        if not target.is_file():
            raise ProtocolViolation(f"artifact file is missing: {artifact['path']}")
        if sha256_file(target) != artifact["sha256"]:
            raise ProtocolViolation(f"artifact digest mismatch: {artifact['path']}")
    return artifacts


def validate_environment_identity(record: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ProtocolViolation("EnvironmentIdentity must be an object")
    _exact_keys(record, _ENV_KEYS, "EnvironmentIdentity")
    if record["schema_version"] != "1" or record["record_type"] != "EnvironmentIdentity":
        raise ProtocolViolation("unsupported EnvironmentIdentity version or type")
    parse_utc(record["captured_at"])
    for field in ("executable", "profile", "product", "script_engine", "target"):
        _validate_identity(record[field], field)
    for field in ("devices", "libraries", "tasks"):
        if not isinstance(record[field], list):
            raise ProtocolViolation(f"{field} must be a list")
        for identity in record[field]:
            _validate_identity(identity, field)
    return record


def _validate_identity(value: Any, label: str) -> None:
    if not isinstance(value, dict) or set(value) != {"identity", "version", "sha256"}:
        raise ProtocolViolation(f"{label} identity shape is invalid")
    if not isinstance(value["identity"], str) or not value["identity"]:
        raise ProtocolViolation(f"{label} identity must be non-empty")
    if not isinstance(value["version"], str) or not value["version"]:
        raise ProtocolViolation(f"{label} version must be non-empty")
    validate_digest(value["sha256"])


def environment_digest(record: dict[str, Any]) -> str:
    validate_environment_identity(record)
    return sha256_bytes(canonical_json_bytes(record))


def validate_capability_result(record: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ProtocolViolation("CapabilityResult must be an object")
    _exact_keys(record, _CAPABILITY_KEYS, "CapabilityResult")
    if record["schema_version"] != "1" or record["record_type"] != "CapabilityResult":
        raise ProtocolViolation("unsupported CapabilityResult version or type")
    parse_utc(record["captured_at"])
    validate_operation(record["operation"])
    if record["status"] not in {"verified", "unverified", "blocked"}:
        raise ProtocolViolation("invalid capability status")
    if not isinstance(record["mode"], str) or not record["mode"]:
        raise ProtocolViolation("capability mode must be non-empty")
    if not isinstance(record["limitations"], list) or not all(
        isinstance(item, str) and item for item in record["limitations"]
    ):
        raise ProtocolViolation("limitations must be a list of non-empty strings")
    if len(record["limitations"]) != len(set(record["limitations"])):
        raise ProtocolViolation("limitations must be unique")
    validate_digest(record["environment_digest"])
    return record


def validate_request(record: dict[str, Any], scratch_root: Path | None = None) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ProtocolViolation("request must be an object")
    _exact_keys(record, _REQUEST_KEYS, "request")
    if record["protocol"] != PROTOCOL or record["message_type"] != "request":
        raise ProtocolViolation("unsupported request protocol or message type")
    validate_correlation_id(record["correlation_id"])
    validate_operation(record["operation"])
    created = parse_utc(record["created_at"])
    deadline = parse_utc(record["deadline_at"])
    if deadline <= created:
        raise ProtocolViolation("deadline must follow created_at")
    validate_digest(record["environment_digest"])
    validate_artifacts(record["artifacts"], scratch_root)
    return record


def request_bytes(record: dict[str, Any], scratch_root: Path | None = None) -> bytes:
    validate_request(record, scratch_root)
    return canonical_json_bytes(record)


def make_request(
    correlation_id: str,
    operation: str,
    created_at: str,
    environment_digest_value: str,
    artifacts: list[dict[str, str]],
    timeout_seconds: int,
    scratch_root: Path | None = None,
) -> dict[str, Any]:
    if timeout_seconds < 1 or timeout_seconds > 30:
        raise ProtocolViolation("timeout_seconds must be between 1 and 30")
    created = parse_utc(created_at)
    deadline = (created + timedelta(seconds=timeout_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
    record = {
        "protocol": PROTOCOL,
        "message_type": "request",
        "correlation_id": validate_correlation_id(correlation_id),
        "operation": validate_operation(operation),
        "created_at": created_at,
        "deadline_at": deadline,
        "environment_digest": validate_digest(environment_digest_value),
        "artifacts": artifacts,
    }
    return validate_request(record, scratch_root)


def validate_result(record: dict[str, Any], scratch_root: Path | None = None) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ProtocolViolation("result must be an object")
    _exact_keys(record, _RESULT_KEYS, "result")
    if record["protocol"] != PROTOCOL or record["message_type"] != "result":
        raise ProtocolViolation("unsupported result protocol or message type")
    validate_correlation_id(record["correlation_id"])
    validate_operation(record["operation"])
    parse_utc(record["created_at"])
    if record["status"] not in {"verified", "unverified", "blocked"}:
        raise ProtocolViolation("invalid result status")
    validate_digest(record["environment_digest"])
    validate_digest(record["request_digest"])
    validate_artifacts(record["artifacts"], scratch_root)
    if not isinstance(record["diagnostics"], list) or not all(
        isinstance(item, str) and item for item in record["diagnostics"]
    ):
        raise ProtocolViolation("diagnostics must be a list of non-empty strings")
    return record


def validate_evidence(record: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ProtocolViolation("evidence must be an object")
    _exact_keys(record, _EVIDENCE_KEYS, "evidence")
    if record["schema_version"] != "1" or record["record_type"] != "EvidenceRecord":
        raise ProtocolViolation("unsupported EvidenceRecord version or type")
    parse_utc(record["created_at"])
    validate_correlation_id(record["correlation_id"])
    if record["producer"] not in {"agent", "human"}:
        raise ProtocolViolation("invalid evidence producer")
    if record["evidence_class"] not in _EVIDENCE_CLASSES:
        raise ProtocolViolation("invalid evidence class")
    for field in ("input_digest", "output_digest", "environment_digest"):
        validate_digest(record[field])
    if not isinstance(record["diagnostics"], list) or not all(
        isinstance(item, str) and item for item in record["diagnostics"]
    ):
        raise ProtocolViolation("evidence diagnostics must be non-empty strings")
    return record


def load_strict_json(path: Path) -> tuple[dict[str, Any], bytes]:
    data = path.read_bytes()
    return strict_json_loads(data), data


def validate_chain(
    request: dict[str, Any],
    request_raw: bytes,
    result: dict[str, Any],
    result_raw: bytes,
    evidence: dict[str, Any],
    scratch_root: Path | None = None,
) -> None:
    validate_request(request, scratch_root)
    validate_result(result, scratch_root)
    validate_evidence(evidence)
    if scratch_root is None:
        raise ProtocolViolation("scratch root is required to verify artifact files")
    verify_artifact_files(request["artifacts"], scratch_root)
    verify_artifact_files(result["artifacts"], scratch_root)
    for field in ("correlation_id", "operation", "environment_digest"):
        if request[field] != result[field]:
            raise ProtocolViolation(f"request/result {field} mismatch")
    if evidence["correlation_id"] != request["correlation_id"]:
        raise ProtocolViolation("evidence correlation mismatch")
    if evidence["environment_digest"] != request["environment_digest"]:
        raise ProtocolViolation("evidence environment mismatch")
    if result["request_digest"] != sha256_bytes(request_raw):
        raise ProtocolViolation("result request digest mismatch")
    if evidence["input_digest"] != sha256_bytes(request_raw):
        raise ProtocolViolation("evidence input digest mismatch")
    if evidence["output_digest"] != sha256_bytes(result_raw):
        raise ProtocolViolation("evidence output digest mismatch")
    if parse_utc(result["created_at"]) > parse_utc(request["deadline_at"]):
        raise ProtocolViolation("result is stale")
