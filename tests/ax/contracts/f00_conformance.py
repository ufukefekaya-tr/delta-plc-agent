"""Test-side AXQ-FILE/1 reference conformance checks.

This module is deliberately not a runtime dispatcher.  It only validates
recorded fixture bytes against the frozen F00 protocol.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable


PROTOCOL = "AXQ-FILE/1"
ALLOWED_OPERATIONS = (
    "environment.probe",
    "project.tree.read",
    "project.inventory.read",
    "source.read",
    "project.export.native",
)
RESULT_STATUSES = ("verified", "unverified", "blocked")

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-(0[1-9]|1[0-2])-([0-2][0-9]|3[01])T"
    r"([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]Z$"
)
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

REQUEST_FIELDS = frozenset(
    {
        "protocol",
        "message_type",
        "correlation_id",
        "operation",
        "created_at",
        "deadline_at",
        "environment_digest",
        "artifacts",
    }
)
RESULT_FIELDS = frozenset(
    {
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
)
ARTIFACT_FIELDS = frozenset({"path", "sha256"})


class ConformanceError(ValueError):
    """The bytes are not a fail-closed AXQ-FILE/1 record."""


def sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _reject_number(_: str) -> Any:
    raise ConformanceError("JSON numbers are not permitted")


def _reject_constant(_: str) -> Any:
    raise ConformanceError("non-finite JSON constants are not permitted")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ConformanceError(f"duplicate JSON key: {key}")
        obj[key] = value
    return obj


def strict_load_bytes(data: bytes) -> dict[str, Any]:
    """Load exactly one BOM-less UTF-8 JSON object with unique keys."""

    if data.startswith(b"\xef\xbb\xbf"):
        raise ConformanceError("UTF-8 BOM is not permitted")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ConformanceError("input is not strict UTF-8") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_int=_reject_number,
            parse_float=_reject_number,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, ConformanceError) as exc:
        raise ConformanceError("invalid strict JSON object") from exc
    if not isinstance(value, dict):
        raise ConformanceError("top-level JSON value must be an object")
    return value


def parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or _TIMESTAMP_RE.fullmatch(value) is None:
        raise ConformanceError("timestamp is not exact UTC seconds form")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ConformanceError("timestamp is not a calendar instant") from exc
    return parsed.replace(tzinfo=timezone.utc)


def validate_digest(value: Any) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ConformanceError("digest is not sha256 lowercase hexadecimal")
    return value


def validate_uuid(value: Any) -> str:
    if not isinstance(value, str) or _UUID_RE.fullmatch(value) is None:
        raise ConformanceError("correlation ID is not canonical lowercase UUID")
    return value


def validate_artifact_path(value: Any, scratch_root: Path | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise ConformanceError("artifact path must be a non-empty string")
    if value.startswith(("/", "\\")) or "\\" in value or ":" in value:
        raise ConformanceError("absolute, drive, UNC, device or backslash path")
    if "\x00" in value or "%" in value:
        raise ConformanceError("NUL or encoded traversal is not permitted")
    segments = value.split("/")
    if any(
        segment in {"", ".", ".."} or _SEGMENT_RE.fullmatch(segment) is None
        for segment in segments
    ):
        raise ConformanceError("artifact path contains an unsafe segment")

    if scratch_root is not None:
        root = scratch_root.resolve(strict=True)
        candidate = (root / Path(*segments)).resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ConformanceError("artifact path escapes scratch root") from exc
    return value


def _require_exact_fields(record: dict[str, Any], expected: frozenset[str]) -> None:
    if frozenset(record) != expected:
        missing = sorted(expected - frozenset(record))
        unknown = sorted(frozenset(record) - expected)
        raise ConformanceError(f"field mismatch missing={missing} unknown={unknown}")


def _validate_artifacts(value: Any, scratch_root: Path | None = None) -> None:
    if not isinstance(value, list):
        raise ConformanceError("artifacts must be a list")
    for descriptor in value:
        if not isinstance(descriptor, dict):
            raise ConformanceError("artifact descriptor must be an object")
        _require_exact_fields(descriptor, ARTIFACT_FIELDS)
        validate_artifact_path(descriptor["path"], scratch_root)
        validate_digest(descriptor["sha256"])


def _validate_diagnostics(value: Any) -> None:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ConformanceError("diagnostics must be a list of non-empty strings")


def _ensure_final_name(filename: str) -> None:
    if filename.endswith(".partial"):
        raise ConformanceError("partial files are never consumable")
    if not filename.endswith(".json"):
        raise ConformanceError("final protocol filename must end in .json")


def validate_request(
    data: bytes,
    *,
    filename: str = "record.request.json",
    now: datetime | None = None,
    scratch_root: Path | None = None,
) -> dict[str, Any]:
    _ensure_final_name(filename)
    record = strict_load_bytes(data)
    _require_exact_fields(record, REQUEST_FIELDS)
    if record["protocol"] != PROTOCOL or record["message_type"] != "request":
        raise ConformanceError("request protocol or message_type mismatch")
    validate_uuid(record["correlation_id"])
    if record["operation"] not in ALLOWED_OPERATIONS:
        raise ConformanceError("operation is not allowlisted")
    created = parse_timestamp(record["created_at"])
    deadline = parse_timestamp(record["deadline_at"])
    if deadline <= created:
        raise ConformanceError("deadline must follow request creation")
    if now is not None and now > deadline:
        raise ConformanceError("request is stale or timed out")
    validate_digest(record["environment_digest"])
    _validate_artifacts(record["artifacts"], scratch_root)
    return record


def validate_result(
    data: bytes,
    *,
    request: dict[str, Any],
    request_bytes: bytes,
    filename: str = "record.result.json",
    now: datetime | None = None,
    scratch_root: Path | None = None,
) -> dict[str, Any]:
    _ensure_final_name(filename)
    record = strict_load_bytes(data)
    _require_exact_fields(record, RESULT_FIELDS)
    if record["protocol"] != PROTOCOL or record["message_type"] != "result":
        raise ConformanceError("result protocol or message_type mismatch")
    validate_uuid(record["correlation_id"])
    if record["operation"] not in ALLOWED_OPERATIONS:
        raise ConformanceError("operation is not allowlisted")
    created = parse_timestamp(record["created_at"])
    request_created = parse_timestamp(request["created_at"])
    request_deadline = parse_timestamp(request["deadline_at"])
    if created < request_created or created > request_deadline:
        raise ConformanceError("result is outside request time window")
    if now is not None and now > request_deadline:
        raise ConformanceError("result is stale")
    if record["status"] not in RESULT_STATUSES:
        raise ConformanceError("unknown result status")
    validate_digest(record["environment_digest"])
    validate_digest(record["request_digest"])
    _validate_artifacts(record["artifacts"], scratch_root)
    _validate_diagnostics(record["diagnostics"])
    for field in ("correlation_id", "operation", "environment_digest"):
        if record[field] != request[field]:
            raise ConformanceError(f"request/result {field} mismatch")
    if record["request_digest"] != sha256_digest(request_bytes):
        raise ConformanceError("result request digest mismatch")
    return record


def validate_chain(
    request_bytes: bytes,
    result_bytes: bytes,
    evidence: dict[str, Any],
    *,
    now: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    request = validate_request(request_bytes, now=now)
    result = validate_result(
        result_bytes, request=request, request_bytes=request_bytes, now=now
    )
    if evidence.get("correlation_id") != request["correlation_id"]:
        raise ConformanceError("evidence correlation mismatch")
    if evidence.get("environment_digest") != request["environment_digest"]:
        raise ConformanceError("evidence environment digest mismatch")
    if evidence.get("input_digest") != sha256_digest(request_bytes):
        raise ConformanceError("evidence input digest mismatch")
    if evidence.get("output_digest") != sha256_digest(result_bytes):
        raise ConformanceError("evidence output digest mismatch")
    return request, result


def reject_duplicate_finals(records: Iterable[dict[str, Any]]) -> None:
    seen: set[tuple[Any, Any]] = set()
    for record in records:
        identity = (record.get("correlation_id"), record.get("message_type"))
        if identity in seen:
            raise ConformanceError("duplicate final creates replay ambiguity")
        seen.add(identity)
