from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from .constants import JOB_STATES, PROTOCOL_V2, PUBLIC_OPERATIONS
from .security import (
    ProtocolViolation,
    canonical_json_bytes,
    ensure_json_safe_v2,
    parse_utc,
    sha256_bytes,
    sha256_file,
    strict_json_loads_v2,
    validate_artifact_path,
    validate_correlation_id,
    validate_digest,
)

_REQUEST_KEYS = {
    "protocol",
    "message_type",
    "correlation_id",
    "job_id",
    "operation",
    "created_at",
    "deadline_at",
    "environment_digest",
    "workspace_digest",
    "expected_revision",
    "authorization_digest",
    "artifacts",
}
_RESULT_KEYS = {
    "protocol",
    "message_type",
    "correlation_id",
    "job_id",
    "operation",
    "created_at",
    "status",
    "environment_digest",
    "workspace_digest",
    "revision",
    "before_project_digest",
    "after_project_digest",
    "request_digest",
    "artifacts",
    "diagnostics",
}
_DIAGNOSTIC_KEYS = {"severity", "code", "message", "source", "line", "column"}
_DEPLOYMENT_EVIDENCE_KEYS = {
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
_SIDE_EFFECT_MARKER_KEYS = {
    "schema_version",
    "job_id",
    "worker_id",
    "operation",
    "environment_digest",
    "started_at",
}
_DEPLOYMENT_JOURNAL_KEYS = {
    "schema_version",
    "record_type",
    "sequence",
    "job_id",
    "event",
    "created_at",
    "details",
}
_DEPLOYMENT_LAUNCH_UNCERTAINTY_KEYS = {
    "schema_version",
    "record_type",
    "created_at",
    "correlation_id",
    "job_id",
    "operation",
    "environment_digest",
    "target_fingerprint",
    "request_digest",
    "launch_state",
}
_REQUEST_ARTIFACT_ROLES = {
    "input",
    "output",
    "project",
    "source",
    "recipe",
    "authorization",
    "observation",
    "evidence",
}
_IDENTIFIER_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_")


def _exact(value: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise ProtocolViolation(
            f"{label} key mismatch; missing={sorted(expected - actual)}, "
            f"unknown={sorted(actual - expected)}"
        )


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolViolation(f"{label} must be an object")
    return value


def _non_empty(value: Any, label: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ProtocolViolation(f"{label} must be non-empty text up to {maximum} characters")
    if "\x00" in value:
        raise ProtocolViolation(f"{label} contains NUL")
    return value


def _identifier(value: Any, label: str) -> str:
    text = _non_empty(value, label, 128)
    if text[0].isdigit() or any(char not in _IDENTIFIER_CHARS for char in text):
        raise ProtocolViolation(f"{label} must be an IEC-style identifier")
    return text


def _expression(value: Any) -> str:
    text = _non_empty(value, "observation expression", 256)
    parts = text.split(".")
    if any(not part for part in parts):
        raise ProtocolViolation("observation expression contains an empty segment")
    for part in parts:
        _identifier(part, "observation expression segment")
    return text


def _bounded_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ProtocolViolation(f"{label} must be an integer between {minimum} and {maximum}")
    return value


def validate_public_operation(operation: Any) -> str:
    if not isinstance(operation, str) or operation not in PUBLIC_OPERATIONS:
        raise ProtocolViolation(f"AXQ-FILE/2 operation is not allowlisted: {operation!r}")
    return operation


def validate_workspace_id(value: Any, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    return validate_correlation_id(value)


def validate_artifacts_v2(
    value: Any,
    scratch_root: Path | None = None,
    *,
    request: bool,
    verify_files: bool = False,
) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > 32:
        raise ProtocolViolation("artifacts must be a list with at most 32 entries")
    seen: set[tuple[str, str, str]] = set()
    seen_paths: set[str] = set()
    for artifact in value:
        if not isinstance(artifact, dict) or set(artifact) != {"role", "path", "sha256"}:
            raise ProtocolViolation("artifact requires exact role, path and sha256")
        role = artifact["role"]
        if not isinstance(role, str) or not role or (request and role not in _REQUEST_ARTIFACT_ROLES):
            raise ProtocolViolation("artifact role is invalid")
        path = artifact["path"]
        target = validate_artifact_path(path, scratch_root)
        digest = validate_digest(artifact["sha256"])
        identity = (role, path, digest)
        if identity in seen or path in seen_paths:
            raise ProtocolViolation("artifact descriptors and paths must be unique")
        seen.add(identity)
        seen_paths.add(path)
        if verify_files:
            if scratch_root is None or not target.is_file():
                raise ProtocolViolation(f"artifact file is missing: {path}")
            if sha256_file(target) != digest:
                raise ProtocolViolation(f"artifact digest mismatch: {path}")
    return value


def validate_ladder_recipe(value: Any) -> dict[str, Any]:
    try:
        from .ladder import LadderRecipeError, validate_ladder_recipe as canonical_validate
    except ModuleNotFoundError as exc:  # Parallel integration guard; never accepts a looser recipe.
        raise ProtocolViolation("canonical LadderRecipe validator is not installed") from exc
    try:
        return canonical_validate(value)
    except LadderRecipeError as exc:
        raise ProtocolViolation(str(exc)) from exc


def validate_observation_profile(value: Any) -> dict[str, Any]:
    profile = _object(value, "observation_profile")
    _exact(
        profile,
        {
            "schema_version",
            "record_type",
            "profile_id",
            "expressions",
            "interval_ms",
            "duration_seconds",
            "fault_invariants",
        },
        "ObservationProfile",
    )
    if profile["schema_version"] != "1" or profile["record_type"] != "ObservationProfile":
        raise ProtocolViolation("unsupported ObservationProfile version or type")
    profile_id = _non_empty(profile["profile_id"], "profile_id", 64)
    if not profile_id[0].islower() or any(
        char not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for char in profile_id
    ) or len(profile_id) < 3:
        raise ProtocolViolation("profile_id format is invalid")
    expressions = profile["expressions"]
    if not isinstance(expressions, list) or not 1 <= len(expressions) <= 64:
        raise ProtocolViolation("expressions must contain between 1 and 64 names")
    normalized = [_expression(item) for item in expressions]
    if len(set(normalized)) != len(normalized):
        raise ProtocolViolation("observation expressions must be unique")
    _bounded_int(profile["interval_ms"], "interval_ms", 100, 10000)
    _bounded_int(profile["duration_seconds"], "duration_seconds", 1, 300)
    invariants = profile["fault_invariants"]
    if not isinstance(invariants, list) or len(invariants) > 32:
        raise ProtocolViolation("fault_invariants must be a bounded list")
    normalized_invariants: list[bytes] = []
    for invariant in invariants:
        item = _object(invariant, "fault invariant")
        _exact(item, {"expression", "operator", "expected", "action"}, "fault invariant")
        _expression(item["expression"])
        if item["operator"] not in {"eq", "ne", "lt", "le", "gt", "ge"}:
            raise ProtocolViolation("fault invariant operator is invalid")
        if not isinstance(item["expected"], (bool, int, float, str)):
            raise ProtocolViolation("fault invariant expected value is invalid")
        if item["action"] not in {"record", "stop_and_rollback"}:
            raise ProtocolViolation("fault invariant action is invalid")
        normalized_invariants.append(canonical_json_bytes(item))
    if len(set(normalized_invariants)) != len(normalized_invariants):
        raise ProtocolViolation("fault_invariants must be unique")
    return profile


def validate_arguments(operation: str, value: Any, scratch_root: Path | None = None) -> dict[str, Any]:
    arguments = _object(value, "arguments")
    if operation in {
        "environment.probe",
        "project.tree.read",
        "project.inventory.read",
        "project.export.native",
    }:
        _exact(arguments, set(), f"{operation} arguments")
    elif operation == "workspace.create":
        _exact(arguments, {"baseline_path", "baseline_sha256", "environment_digest"}, operation)
        path = _non_empty(arguments["baseline_path"], "baseline_path")
        validate_artifact_path(path, scratch_root)
        if not path.startswith("baselines/") or not path.endswith(".project"):
            raise ProtocolViolation("baseline_path must be a baselines/*.project artifact")
        validate_digest(arguments["baseline_sha256"])
        validate_digest(arguments["environment_digest"])
    elif operation == "source.read":
        _exact(arguments, {"workspace_id", "expected_revision", "target"}, operation)
        validate_workspace_id(arguments["workspace_id"])
        _bounded_int(arguments["expected_revision"], "expected_revision", 0, 2**31 - 1)
        _identifier(arguments["target"], "target")
    elif operation in {"workspace.status", "artifact.freeze"}:
        expected = {"workspace_id"} if operation == "workspace.status" else {
            "workspace_id",
            "expected_revision",
        }
        _exact(arguments, expected, operation)
        validate_workspace_id(arguments["workspace_id"])
        if "expected_revision" in arguments:
            _bounded_int(arguments["expected_revision"], "expected_revision", 0, 2**31 - 1)
    elif operation == "source.upsert.st":
        _exact(
            arguments,
            {
                "workspace_id",
                "expected_revision",
                "pou_name",
                "pou_kind",
                "return_type",
                "declaration",
                "implementation",
            },
            operation,
        )
        validate_workspace_id(arguments["workspace_id"])
        _bounded_int(arguments["expected_revision"], "expected_revision", 0, 2**31 - 1)
        _identifier(arguments["pou_name"], "pou_name")
        if arguments["pou_kind"] not in {"program", "function", "function_block", "gvl"}:
            raise ProtocolViolation("unsupported pou_kind")
        if arguments["pou_kind"] == "function":
            _identifier(arguments["return_type"], "return_type")
        elif arguments["return_type"] is not None:
            raise ProtocolViolation("return_type is only valid for function POUs")
        _non_empty(arguments["declaration"], "declaration", 250_000)
        if arguments["pou_kind"] == "gvl":
            if arguments["implementation"] != "":
                raise ProtocolViolation("GVL implementation must be empty")
        else:
            implementation = arguments["implementation"]
            if not isinstance(implementation, str) or len(implementation) > 1_000_000:
                raise ProtocolViolation("implementation must be bounded text")
    elif operation == "ladder.networks.apply":
        _exact(arguments, {"workspace_id", "expected_revision", "recipe"}, operation)
        validate_workspace_id(arguments["workspace_id"])
        _bounded_int(arguments["expected_revision"], "expected_revision", 0, 2**31 - 1)
        validate_ladder_recipe(arguments["recipe"])
    elif operation == "project.build":
        _exact(
            arguments,
            {"workspace_id", "expected_revision", "mode", "max_attempts", "runtime_release_digest"},
            operation,
        )
        validate_workspace_id(arguments["workspace_id"])
        _bounded_int(arguments["expected_revision"], "expected_revision", 0, 2**31 - 1)
        if arguments["mode"] not in {"build", "rebuild"}:
            raise ProtocolViolation("build mode must be build or rebuild")
        _bounded_int(arguments["max_attempts"], "max_attempts", 1, 10)
        validate_digest(arguments["runtime_release_digest"])
    elif operation == "deployment.prepare":
        _exact(
            arguments,
            {"artifact_digest", "target_fingerprint", "rollback_digest", "observation_profile"},
            operation,
        )
        for field in ("artifact_digest", "target_fingerprint", "rollback_digest"):
            validate_digest(arguments[field])
        validate_observation_profile(arguments["observation_profile"])
    elif operation == "deployment.execute":
        _exact(
            arguments,
            {
                "plan_id",
                "artifact_digest",
                "target_fingerprint",
                "rollback_digest",
                "expires_at",
                "challenge_nonce",
                "authorization_phrase",
            },
            operation,
        )
        validate_correlation_id(arguments["plan_id"])
        validate_correlation_id(arguments["challenge_nonce"])
        for field in ("artifact_digest", "target_fingerprint", "rollback_digest"):
            validate_digest(arguments[field])
        parse_utc(arguments["expires_at"])
        _non_empty(arguments["authorization_phrase"], "authorization_phrase", 64)
    elif operation == "deployment.reconcile":
        _exact(
            arguments,
            {
                "original_job_id",
                "target_fingerprint",
                "artifact_digest",
                "rollback_digest",
            },
            operation,
        )
        validate_correlation_id(arguments["original_job_id"])
        for field in ("target_fingerprint", "artifact_digest", "rollback_digest"):
            validate_digest(arguments[field])
    elif operation == "target.observe.start":
        _exact(arguments, {"target_fingerprint", "observation_profile"}, operation)
        validate_digest(arguments["target_fingerprint"])
        validate_observation_profile(arguments["observation_profile"])
    elif operation in {"target.observe.read", "target.observe.stop"}:
        _exact(arguments, {"observation_id"}, operation)
        validate_correlation_id(arguments["observation_id"])
    elif operation in {"job.status", "job.cancel"}:
        _exact(arguments, {"job_id"}, operation)
        validate_correlation_id(arguments["job_id"])
    else:  # pragma: no cover - validate_public_operation makes this unreachable
        raise ProtocolViolation(f"unsupported arguments for {operation}")
    ensure_json_safe_v2(arguments)
    return arguments


def validate_request_v2(record: Any, scratch_root: Path | None = None) -> dict[str, Any]:
    request = _object(record, "request")
    _exact(request, _REQUEST_KEYS, "AXQ-FILE/2 request")
    if request["protocol"] != PROTOCOL_V2 or request["message_type"] != "request":
        raise ProtocolViolation("unsupported request protocol or message type")
    validate_correlation_id(request["correlation_id"])
    validate_correlation_id(request["job_id"])
    operation = validate_public_operation(request["operation"])
    created = parse_utc(request["created_at"])
    deadline = parse_utc(request["deadline_at"])
    if deadline <= created:
        raise ProtocolViolation("deadline must follow created_at")
    maximum = 330 if operation == "deployment.execute" else 300
    if (deadline - created).total_seconds() > maximum:
        raise ProtocolViolation(
            f"{operation} deadline exceeds the {maximum}-second protocol bound"
        )
    validate_digest(request["environment_digest"])
    if request["workspace_digest"] is not None:
        validate_digest(request["workspace_digest"])
    if request["expected_revision"] is not None:
        _bounded_int(request["expected_revision"], "expected_revision", 0, 2**31 - 1)
    if request["authorization_digest"] is not None:
        validate_digest(request["authorization_digest"])
    validate_artifacts_v2(request["artifacts"], scratch_root, request=True)
    authorization_artifacts = [
        artifact for artifact in request["artifacts"] if artifact["role"] == "authorization"
    ]
    if operation == "deployment.execute":
        if request["authorization_digest"] is None or len(authorization_artifacts) != 1:
            raise ProtocolViolation(
                "deployment.execute requires non-null authorization_digest and exactly one authority artifact"
            )
    return request


def request_v2_bytes(record: dict[str, Any], scratch_root: Path | None = None) -> bytes:
    validate_request_v2(record, scratch_root)
    return canonical_json_bytes(record)


def make_request_v2(
    *,
    correlation_id: str,
    job_id: str,
    operation: str,
    created_at: str,
    timeout_seconds: int,
    environment_digest: str,
    workspace_digest: str | None,
    expected_revision: int | None,
    authorization_digest: str | None,
    artifacts: list[dict[str, str]],
    scratch_root: Path | None = None,
) -> dict[str, Any]:
    operation = validate_public_operation(operation)
    maximum = 330 if operation == "deployment.execute" else 300
    _bounded_int(timeout_seconds, "timeout_seconds", 1, maximum)
    created = parse_utc(created_at)
    request = {
        "protocol": PROTOCOL_V2,
        "message_type": "request",
        "correlation_id": correlation_id,
        "job_id": job_id,
        "operation": operation,
        "created_at": created_at,
        "deadline_at": (created + timedelta(seconds=timeout_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "environment_digest": environment_digest,
        "workspace_digest": workspace_digest,
        "expected_revision": expected_revision,
        "authorization_digest": authorization_digest,
        "artifacts": artifacts,
    }
    return validate_request_v2(request, scratch_root)


def validate_diagnostics(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 10_000:
        raise ProtocolViolation("diagnostics must be a bounded list")
    for diagnostic in value:
        item = _object(diagnostic, "diagnostic")
        _exact(item, _DIAGNOSTIC_KEYS | ({"category"} if "category" in item else set()), "diagnostic")
        if "category" in item:
            _non_empty(item["category"], "diagnostic category", 128)
        if item["severity"] not in {"info", "warning", "error", "critical"}:
            raise ProtocolViolation("invalid diagnostic severity")
        _non_empty(item["code"], "diagnostic code", 128)
        _non_empty(item["message"], "diagnostic message", 4096)
        if item["source"] is not None:
            validate_artifact_path(_non_empty(item["source"], "diagnostic source"))
        for field in ("line", "column"):
            if item[field] is not None:
                _bounded_int(item[field], f"diagnostic {field}", 1, 2**31 - 1)
    return value


def validate_result_v2(record: Any, scratch_root: Path | None = None) -> dict[str, Any]:
    result = _object(record, "result")
    _exact(result, _RESULT_KEYS, "AXQ-FILE/2 result")
    if result["protocol"] != PROTOCOL_V2 or result["message_type"] != "result":
        raise ProtocolViolation("unsupported result protocol or message type")
    validate_correlation_id(result["correlation_id"])
    validate_correlation_id(result["job_id"])
    validate_public_operation(result["operation"])
    parse_utc(result["created_at"])
    if result["status"] not in JOB_STATES[2:]:
        raise ProtocolViolation("result status must be terminal")
    validate_digest(result["environment_digest"])
    if result["workspace_digest"] is not None:
        validate_digest(result["workspace_digest"])
    if result["revision"] is not None:
        _bounded_int(result["revision"], "revision", 0, 2**31 - 1)
    for field in ("before_project_digest", "after_project_digest"):
        if result[field] is not None:
            validate_digest(result[field])
    validate_digest(result["request_digest"])
    validate_artifacts_v2(result["artifacts"], scratch_root, request=False)
    validate_diagnostics(result["diagnostics"])
    return result


def load_v2(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    return strict_json_loads_v2(raw), raw


def validate_result_chain_v2(
    request: dict[str, Any],
    request_raw: bytes,
    result: dict[str, Any],
    scratch_root: Path,
) -> None:
    validate_request_v2(request, scratch_root)
    validate_result_v2(result, scratch_root)
    for field in ("correlation_id", "job_id", "operation", "environment_digest", "workspace_digest"):
        if request[field] != result[field]:
            raise ProtocolViolation(f"request/result {field} mismatch")
    if result["request_digest"] != sha256_bytes(request_raw):
        raise ProtocolViolation("result request digest mismatch")
    if (
        parse_utc(result["created_at"]) > parse_utc(request["deadline_at"])
        and result["status"] not in {"blocked", "unknown_outcome"}
    ):
        raise ProtocolViolation("result is stale")
    expected_revision = request["expected_revision"]
    if expected_revision is not None:
        if result["revision"] is None:
            raise ProtocolViolation("workspace result revision is missing")
        expected_result_revision = (
            expected_revision + 1
            if result["status"] == "succeeded"
            and result["operation"] in {"source.upsert.st", "ladder.networks.apply"}
            else expected_revision
        )
        if result["revision"] != expected_result_revision:
            raise ProtocolViolation("result revision does not match operation semantics")
    if request["workspace_digest"] is not None and result["before_project_digest"] != request["workspace_digest"]:
        raise ProtocolViolation("result before_project_digest does not match request workspace_digest")


def load_archived_result_chain_v2(
    result_path: Path, scratch_root: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load an immutable terminal result and prove it belongs to its archived request."""

    result, _result_raw = load_v2(result_path)
    validate_result_v2(result, scratch_root)
    request_path = (
        scratch_root
        / "archive"
        / f"{result['correlation_id']}.request.json"
    )
    try:
        request, request_raw = load_v2(request_path)
    except (OSError, ProtocolViolation) as exc:
        raise ProtocolViolation("terminal result archived request is missing or corrupt") from exc
    validate_result_chain_v2(request, request_raw, result, scratch_root)
    validate_artifacts_v2(request["artifacts"], scratch_root, request=True, verify_files=True)
    validate_artifacts_v2(result["artifacts"], scratch_root, request=False, verify_files=True)
    return request, result


def validate_deployment_worker_evidence_v1(
    record: Any, result: dict[str, Any]
) -> dict[str, Any]:
    """Validate exact worker evidence shape and bind it to one AXQ-FILE/2 result."""

    evidence = _object(record, "AxDeploymentWorkerEvidence")
    _exact(evidence, _DEPLOYMENT_EVIDENCE_KEYS, "AxDeploymentWorkerEvidence")
    if evidence["schema_version"] != "1" or evidence["record_type"] != "AxDeploymentWorkerEvidence":
        raise ProtocolViolation("unsupported deployment worker evidence")
    parse_utc(evidence["created_at"])
    validate_correlation_id(evidence["correlation_id"])
    validate_correlation_id(evidence["job_id"])
    validate_public_operation(evidence["operation"])
    for field in (
        "target_fingerprint",
        "authority_digest",
        "candidate_digest",
        "rollback_digest",
    ):
        if evidence[field] is not None:
            validate_digest(evidence[field])
    if not isinstance(evidence["reconciliation_required"], bool):
        raise ProtocolViolation("deployment reconciliation_required must be boolean")
    _bounded_int(evidence["sample_count"], "deployment sample_count", 0, 10_000_000)
    diagnostics = evidence["diagnostics"]
    if not isinstance(diagnostics, list) or len(diagnostics) > 10_000:
        raise ProtocolViolation("deployment evidence diagnostics must be bounded")
    for message in diagnostics:
        _non_empty(message, "deployment evidence diagnostic", 4096)
    for field in ("correlation_id", "job_id", "operation"):
        if evidence[field] != result[field]:
            raise ProtocolViolation(f"deployment evidence/result {field} mismatch")
    return evidence


def validate_deployment_side_effect_marker_v1(
    record: Any, *, job_id: str, environment_digest: str
) -> dict[str, Any]:
    marker = _object(record, "deployment side-effect marker")
    _exact(marker, _SIDE_EFFECT_MARKER_KEYS, "deployment side-effect marker")
    if marker["schema_version"] != "1":
        raise ProtocolViolation("unsupported deployment side-effect marker")
    validate_correlation_id(marker["job_id"])
    validate_correlation_id(marker["worker_id"])
    validate_digest(marker["environment_digest"])
    parse_utc(marker["started_at"])
    if (
        marker["job_id"] != job_id
        or marker["operation"] != "deployment.execute"
        or marker["environment_digest"] != environment_digest
    ):
        raise ProtocolViolation("deployment side-effect marker binding mismatch")
    return marker


def validate_deployment_launch_uncertainty_v1(
    record: Any,
    *,
    request: dict[str, Any],
    request_raw: bytes,
    target_fingerprint: str,
) -> dict[str, Any]:
    """Validate immutable host evidence for an unprovable target launch."""

    item = _object(record, "DeploymentLaunchUncertainty")
    _exact(
        item,
        _DEPLOYMENT_LAUNCH_UNCERTAINTY_KEYS,
        "DeploymentLaunchUncertainty",
    )
    if (
        item["schema_version"] != "1"
        or item["record_type"] != "DeploymentLaunchUncertainty"
        or item["operation"] != "deployment.execute"
        or item["launch_state"] != "unknown"
    ):
        raise ProtocolViolation("unsupported deployment launch uncertainty")
    parse_utc(item["created_at"])
    validate_correlation_id(item["correlation_id"])
    validate_correlation_id(item["job_id"])
    for field in ("environment_digest", "target_fingerprint", "request_digest"):
        validate_digest(item[field])
    if (
        request.get("operation") != "deployment.execute"
        or item["correlation_id"] != request.get("correlation_id")
        or item["job_id"] != request.get("job_id")
        or item["environment_digest"] != request.get("environment_digest")
        or item["target_fingerprint"] != target_fingerprint
        or item["request_digest"] != sha256_bytes(request_raw)
    ):
        raise ProtocolViolation("deployment launch uncertainty binding mismatch")
    return item


def validate_deployment_reconciliation_head_v1(
    scratch_root: Path,
    *,
    request: dict[str, Any],
    request_raw: bytes,
    target_fingerprint: str,
) -> dict[str, str]:
    """Select the one admissible provenance head for read-only reconciliation.

    A target-worker journal is authoritative only once it contains a durable
    full-download intent.  Before that boundary, the exact host launch record
    and the exact worker pre-intent prefix form one composite head; partial or
    corrupt journals never fall back.
    """

    job_id = request.get("job_id")
    validate_correlation_id(job_id)
    validate_digest(target_fingerprint)
    journal_entries = sorted(
        (scratch_root / "state" / "deployments" / job_id).glob("*.json")
    )
    if journal_entries:
        digest = validate_deployment_journal_v1(journal_entries, job_id=job_id)
        events = [strict_json_loads_v2(path.read_bytes()) for path in journal_entries]
        if any(
            item.get("event")
            in {"candidate-full-download-intent", "rollback-full-download-intent"}
            for item in events
        ):
            return {"kind": "journal", "digest": digest}
        if [item["event"] for item in events] not in (
            ["authority-claimed"],
            ["authority-claimed", "target-preflight-passed"],
        ):
            raise ProtocolViolation("deployment pre-intent journal prefix is invalid")
        if events[0]["details"] != {
            "authority_digest": request.get("authorization_digest"),
            "reconciliation_required": False,
        } or (
            len(events) == 2
            and events[1]["details"] != {"forced_values_present": False}
        ):
            raise ProtocolViolation("deployment pre-intent journal binding mismatch")
        launch_path = (
            scratch_root
            / "state"
            / "deployment-launch-uncertainty"
            / f"{job_id}.json"
        )
        try:
            launch_raw = launch_path.read_bytes()
            launch = strict_json_loads_v2(launch_raw)
        except (OSError, ProtocolViolation) as exc:
            raise ProtocolViolation(
                "pre-intent journal requires exact launch uncertainty provenance"
            ) from exc
        validate_deployment_launch_uncertainty_v1(
            launch,
            request=request,
            request_raw=request_raw,
            target_fingerprint=target_fingerprint,
        )
        composite = sha256_bytes(
            canonical_json_bytes(
                {
                    "journal_head_digest": digest,
                    "launch_uncertainty_digest": sha256_bytes(launch_raw),
                }
            )
        )
        return {"kind": "pre_intent_journal", "digest": composite}
    launch_path = (
        scratch_root
        / "state"
        / "deployment-launch-uncertainty"
        / f"{job_id}.json"
    )
    try:
        launch_raw = launch_path.read_bytes()
        launch = strict_json_loads_v2(launch_raw)
    except (OSError, ProtocolViolation) as exc:
        raise ProtocolViolation(
            "deployment journal and launch uncertainty provenance are missing or corrupt"
        ) from exc
    validate_deployment_launch_uncertainty_v1(
        launch,
        request=request,
        request_raw=request_raw,
        target_fingerprint=target_fingerprint,
    )
    return {"kind": "launch_uncertainty", "digest": sha256_bytes(launch_raw)}


def validate_deployment_journal_v1(
    paths: list[Path], *, job_id: str
) -> str:
    validate_correlation_id(job_id)
    if not paths or len(paths) > 10_000:
        raise ProtocolViolation("deployment journal must be a bounded non-empty chain")
    chain_digest: str | None = None
    for expected_sequence, path in enumerate(sorted(paths), start=1):
        raw = path.read_bytes()
        try:
            event = strict_json_loads_v2(raw)
        except (OSError, ProtocolViolation) as exc:
            raise ProtocolViolation("deployment journal event is missing or corrupt") from exc
        item = _object(event, "DeploymentJournalEvent")
        _exact(item, _DEPLOYMENT_JOURNAL_KEYS, "DeploymentJournalEvent")
        if item["schema_version"] != "1" or item["record_type"] != "DeploymentJournalEvent":
            raise ProtocolViolation("unsupported deployment journal event")
        _bounded_int(item["sequence"], "deployment journal sequence", 1, 10_000)
        validate_correlation_id(item["job_id"])
        name = _non_empty(item["event"], "deployment journal event", 128)
        if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in name):
            raise ProtocolViolation("deployment journal event name is invalid")
        parse_utc(item["created_at"])
        if not isinstance(item["details"], dict):
            raise ProtocolViolation("deployment journal details must be an object")
        ensure_json_safe_v2(item["details"])
        if (
            item["sequence"] != expected_sequence
            or item["job_id"] != job_id
            or path.name != f"{expected_sequence:04d}-{name}.json"
        ):
            raise ProtocolViolation("deployment journal sequence or filename binding mismatch")
        event_digest = sha256_bytes(raw)
        chain_digest = (
            event_digest
            if chain_digest is None
            else sha256_bytes(
                canonical_json_bytes(
                    {"previous_digest": chain_digest, "event_digest": event_digest}
                )
            )
        )
    assert chain_digest is not None
    return chain_digest
