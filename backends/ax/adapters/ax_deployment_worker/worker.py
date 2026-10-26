# -*- coding: utf-8 -*-
"""Fail-closed AX/SP18 deployment and observation adapter.

This module is intentionally self-contained for DIADesigner-AX 1.9's
ScriptEngine.  It consumes only the frozen AXQ-FILE/2 deployment and target
observation envelopes.  Vendor objects are injected so every target-facing
transition can be tested without launching an IDE or contacting a PLC.

The only target mutation sequence implemented here is the one-shot,
digest-bound deployment chain.  No generic ScriptEngine, discovery, value
write, force, reset, firmware, library, or safety surface is exposed.
"""
from __future__ import print_function

import calendar
import hashlib
import json
import math
import os
import re
import sys
import threading
import time
import traceback
import uuid


try:  # ScriptEngine 4.0 uses Python 2 compatible text/integer types.
    TEXT_TYPES = (basestring,)
except NameError:  # pragma: no cover - exercised by the CPython 3 test host
    TEXT_TYPES = (str,)

try:
    INTEGER_TYPES = (int, long)
except NameError:  # pragma: no cover - exercised by the CPython 3 test host
    INTEGER_TYPES = (int,)

NUMBER_TYPES = INTEGER_TYPES + (float,)


PROTOCOL = "AXQ-FILE/2"
WORKFLOW_DEADLINE_CAPABILITY = 1
PHRASE_DIGEST = "sha256:d254948f417a488968d144e12a6b87dfdeafb5582dbe86fa807dac952341088c"
ROLLBACK_RESERVE_SECONDS = 20
SUPPORTED_OPERATIONS = (
    "deployment.execute",
    "deployment.reconcile",
    "target.observe.start",
    "target.observe.read",
    "target.observe.stop",
)
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
EXPRESSION_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")

REQUEST_KEYS = set(
    (
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
    )
)
RESULT_KEYS = set(
    (
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
    )
)
DIAGNOSTIC_KEYS = set(("severity", "code", "message", "source", "line", "column"))
DEPLOYMENT_EVIDENCE_KEYS = set(
    (
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
    )
)
DEPLOYMENT_JOURNAL_KEYS = set(
    (
        "schema_version",
        "record_type",
        "sequence",
        "job_id",
        "event",
        "created_at",
        "details",
    )
)
DEPLOYMENT_LAUNCH_UNCERTAINTY_KEYS = set(
    (
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
    )
)
OBSERVATION_LIFECYCLE_KEYS = set(
    (
        "schema_version",
        "record_type",
        "sequence",
        "observation_id",
        "state",
        "created_at",
        "worker_id",
        "target_fingerprint",
        "sample_count",
    )
)
OBSERVATION_STATES = (
    "running",
    "completed",
    "fault",
    "stopped",
    "worker_lost",
)
ARTIFACT_KEYS = set(("role", "path", "sha256"))
SIDE_EFFECT_KEYS = set(
    (
        "schema_version",
        "job_id",
        "worker_id",
        "operation",
        "environment_digest",
        "started_at",
    )
)
PLAN_KEYS = set(
    (
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
    )
)
EXECUTION_INPUT_KEYS = set(
    (
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
    )
)
AUTHORITY_KEYS = set(
    (
        "schema_version",
        "record_type",
        "status",
        "plan_id",
        "artifact_digest",
        "target_fingerprint",
        "rollback_digest",
        "runtime_release_digest",
        "rollback_runtime_release_digest",
        "observation_digest",
        "challenge_nonce",
        "phrase_digest",
        "issued_at",
        "expires_at",
        "single_use",
        "consumed_at",
        "authority_digest",
    )
)
PROFILE_KEYS = set(
    (
        "schema_version",
        "record_type",
        "profile_id",
        "expressions",
        "interval_ms",
        "duration_seconds",
        "fault_invariants",
    )
)
INVARIANT_KEYS = set(("expression", "operator", "expected", "action"))
TARGET_KEYS = set(
    (
        "schema_version",
        "record_type",
        "status",
        "captured_at",
        "model",
        "device_identity",
        "runtime_version",
        "firmware_version",
        "gateway_route_digest",
        "application_identity",
        "forced_values_present",
        "physical_outputs_energized",
        "isolation_evidence_digest",
        "fingerprint",
    )
)
RECONCILIATION_KEYS = set(
    (
        "target_fingerprint",
        "runtime_release_digest",
        "application_state",
        "forced_values_present",
        "physical_outputs_energized",
    )
)
RECONCILIATION_INPUT_KEYS = set(
    (
        "schema_version",
        "record_type",
        "original_job_id",
        "target_fingerprint",
        "artifact_digest",
        "rollback_digest",
    )
)
RECONCILIATION_RECORD_KEYS = set(
    (
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
    )
)
DEPLOYMENT_CONFIG_KEYS = set(
    (
        "schema_version",
        "worker_id",
        "ide_executable",
        "ide_executable_sha256",
        "script_engine",
        "script_engine_sha256",
        "ide_profile",
        "worker_script",
        "worker_script_sha256",
        "target_adapter",
        "target_adapter_sha256",
        "autonomous_config",
        "autonomous_config_sha256",
        "target_enrollment",
        "target_enrollment_sha256",
        "environment_digest",
        "target_fingerprint",
        "observation_expression_allowlist",
        "heartbeat_seconds",
        "lease_timeout_seconds",
        "max_runtime_seconds",
        "max_requests",
        "idle_exit_seconds",
        "startup_timeout_seconds",
    )
)
DEPLOYMENT_LEASE_KEYS = set(
    (
        "protocol",
        "worker_id",
        "pid",
        "process_start_fingerprint",
        "started_at",
        "heartbeat_at",
        "environment_digest",
        "target_fingerprint",
        "config_digest",
        "sequence",
    )
)
DEPLOYMENT_HEARTBEAT_KEYS = set(
    (
        "protocol",
        "worker_id",
        "pid",
        "process_start_fingerprint",
        "created_at",
        "state",
        "processed",
        "target_fingerprint",
        "config_digest",
    )
)
_PROCESS_MARKER = str(os.getpid()) + "|" + str(time.time()) + "|" + str(sys.executable)
PROCESS_START_FINGERPRINT = "sha256:" + hashlib.sha256(
    _PROCESS_MARKER.encode("utf-8")
).hexdigest()


class WorkerError(Exception):
    """Malformed or unsupported AXQ input."""


class GateBlocked(WorkerError):
    """A valid request was rejected before an unsafe transition."""


def _reject_duplicate_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise WorkerError("duplicate JSON key: " + str(key))
        value[key] = item
    return value


def _reject_constant(value):
    raise WorkerError("non-finite JSON number is forbidden: " + str(value))


def _json_bytes(value):
    text = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    if not isinstance(text, bytes):
        text = text.encode("utf-8")
    return text


def _safe_diagnostic(error):
    try:
        try:
            value = unicode(error)
        except NameError:  # pragma: no cover - CPython 3
            value = str(error)
        escaped = value.encode("unicode_escape")
        return escaped.decode("ascii") if isinstance(escaped, bytes) else escaped
    except BaseException:
        return "exception text unavailable"


def _load_json_bytes(raw):
    if raw.startswith(b"\xef\xbb\xbf"):
        raise WorkerError("UTF-8 BOM is forbidden")
    try:
        text = raw.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except WorkerError:
        raise
    except Exception as exc:
        raise WorkerError("invalid JSON: " + str(exc))
    if not isinstance(value, dict):
        raise WorkerError("top-level JSON must be an object")
    return value


def _load_json_file(path):
    handle = open(path, "rb")
    try:
        raw = handle.read()
    finally:
        handle.close()
    return _load_json_bytes(raw), raw


def _sha256_bytes(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _sha256_file(path):
    digest = hashlib.sha256()
    handle = open(path, "rb")
    try:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        handle.close()
    return "sha256:" + digest.hexdigest()


def _timestamp(value):
    try:
        return calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        raise WorkerError("invalid UTC timestamp")


def _utc_at(value):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))


def _validate_uuid(value, label):
    if not isinstance(value, TEXT_TYPES) or not UUID_RE.match(value):
        raise WorkerError(label + " must be a canonical lowercase UUID")


def _validate_digest(value, label):
    if not isinstance(value, TEXT_TYPES) or not DIGEST_RE.match(value):
        raise WorkerError(label + " must be a lowercase SHA-256 digest")


def _validate_relative_path(value):
    if not isinstance(value, TEXT_TYPES) or not value:
        raise WorkerError("artifact path must be non-empty text")
    lowered = value.lower()
    if (
        value.startswith("/")
        or value.startswith("\\")
        or "\\" in value
        or ":" in value
        or "%2e" in lowered
        or "%2f" in lowered
        or "%5c" in lowered
    ):
        raise WorkerError("absolute, device, drive, UNC and encoded paths are forbidden")
    parts = value.split("/")
    for part in parts:
        if part in ("", ".", "..") or not PATH_SEGMENT_RE.match(part):
            raise WorkerError("invalid artifact path segment")
    return parts


def _contained_path(root, relative):
    parts = _validate_relative_path(relative)
    root_abs = os.path.abspath(root)
    target = os.path.abspath(os.path.join(root_abs, *parts))
    prefix = root_abs.rstrip(os.sep) + os.sep
    if target == root_abs or not target.startswith(prefix):
        raise WorkerError("artifact path escapes scratch root")
    cursor = root_abs
    for part in parts[:-1]:
        cursor = os.path.join(cursor, part)
        if os.path.lexists(cursor) and os.path.islink(cursor):
            raise WorkerError("symlinked artifact path is forbidden")
    if os.path.lexists(target) and os.path.islink(target):
        raise WorkerError("symlinked artifact file is forbidden")
    return target


def _ensure_parent(path):
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        os.makedirs(parent)


def _write_all(descriptor, raw):
    offset = 0
    while offset < len(raw):
        written = os.write(descriptor, raw[offset:])
        if not written:
            raise WorkerError("binary write made no progress")
        offset += written


def _open_binary(path, flags):
    return os.open(path, flags | getattr(os, "O_BINARY", 0))


def _atomic_publish(path, raw):
    if os.path.exists(path):
        raise WorkerError("immutable output already exists")
    _ensure_parent(path)
    partial = path + ".partial"
    if os.path.exists(partial):
        raise WorkerError("partial output already exists")
    descriptor = _open_binary(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        _write_all(descriptor, raw)
        try:
            os.fsync(descriptor)
        except Exception:
            pass
    finally:
        os.close(descriptor)
    if os.path.exists(path):
        os.remove(partial)
        raise WorkerError("immutable output appeared during publish")
    os.rename(partial, path)


def _append_json_line(path, value):
    _ensure_parent(path)
    raw = _json_bytes(value)
    descriptor = _open_binary(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY)
    try:
        _write_all(descriptor, raw)
        try:
            os.fsync(descriptor)
        except Exception:
            pass
    finally:
        os.close(descriptor)


def _diagnostic(code, message, severity="error"):
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "source": None,
        "line": None,
        "column": None,
    }


def _dispose_online(application):
    if application is None:
        return
    try:
        if bool(getattr(application, "is_logged_in", False)):
            application.logout()
    except Exception:
        pass
    try:
        disposer = getattr(application, "Dispose", None)
        if disposer is not None:
            disposer()
    except Exception:
        pass


def _close_project(project):
    if project is None:
        return
    try:
        project.close()
    except Exception:
        pass


def _state_is(value, expected, name):
    if value == expected:
        return True
    rendered = str(value).lower()
    return rendered == name or rendered.endswith("." + name)


def _coerce_online_value(value):
    if isinstance(value, (bool,) + NUMBER_TYPES):
        return value
    text = str(value).strip()
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if re.match(r"^-?(0|[1-9][0-9]*)$", text):
        try:
            return int(text)
        except Exception:
            return text
    if re.match(r"^-?(0|[1-9][0-9]*)\.[0-9]+$", text):
        try:
            return float(text)
        except Exception:
            return text
    return text


def _comparison(operator, actual, expected):
    if operator == "eq":
        return actual == expected
    if operator == "ne":
        return actual != expected
    try:
        if operator == "lt":
            return actual < expected
        if operator == "le":
            return actual <= expected
        if operator == "gt":
            return actual > expected
        if operator == "ge":
            return actual >= expected
    except Exception:
        return False
    return False


def _validate_profile(profile):
    if not isinstance(profile, dict) or set(profile) != PROFILE_KEYS:
        raise WorkerError("ObservationProfile shape mismatch")
    if profile["schema_version"] != "1" or profile["record_type"] != "ObservationProfile":
        raise WorkerError("unsupported ObservationProfile")
    profile_id = profile["profile_id"]
    if not isinstance(profile_id, TEXT_TYPES) or not re.match(
        r"^[a-z][a-z0-9._-]{2,63}$", profile_id
    ):
        raise WorkerError("invalid observation profile_id")
    expressions = profile["expressions"]
    if (
        not isinstance(expressions, list)
        or not 1 <= len(expressions) <= 64
        or len(set(expressions)) != len(expressions)
    ):
        raise WorkerError("observation expressions must be a unique bounded list")
    for expression in expressions:
        if not isinstance(expression, TEXT_TYPES) or not EXPRESSION_RE.match(expression):
            raise WorkerError("invalid observation expression")
    interval = profile["interval_ms"]
    duration = profile["duration_seconds"]
    if (
        isinstance(interval, bool)
        or not isinstance(interval, INTEGER_TYPES)
        or not 100 <= interval <= 10000
    ):
        raise WorkerError("observation interval is outside 100..10000 ms")
    if (
        isinstance(duration, bool)
        or not isinstance(duration, INTEGER_TYPES)
        or not 1 <= duration <= 300
    ):
        raise WorkerError("observation duration is outside 1..300 seconds")
    invariants = profile["fault_invariants"]
    if not isinstance(invariants, list) or len(invariants) > 32:
        raise WorkerError("fault invariants are not bounded")
    seen = set()
    for invariant in invariants:
        if not isinstance(invariant, dict) or set(invariant) != INVARIANT_KEYS:
            raise WorkerError("fault invariant shape mismatch")
        if invariant["expression"] not in expressions:
            raise WorkerError("fault invariant expression is not observed")
        if invariant["operator"] not in ("eq", "ne", "lt", "le", "gt", "ge"):
            raise WorkerError("fault invariant operator is invalid")
        expected = invariant["expected"]
        if not isinstance(expected, (bool,) + NUMBER_TYPES + TEXT_TYPES):
            raise WorkerError("fault invariant expected value has an invalid JSON scalar type")
        if isinstance(expected, float) and (math.isnan(expected) or math.isinf(expected)):
            raise WorkerError("fault invariant expected value must be finite")
        if invariant["action"] not in ("record", "stop_and_rollback"):
            raise WorkerError("fault invariant action is invalid")
        identity = _json_bytes(invariant)
        if identity in seen:
            raise WorkerError("duplicate fault invariant")
        seen.add(identity)
    return profile


def _validate_target_record(record, expected_status):
    if not isinstance(record, dict) or set(record) != TARGET_KEYS:
        raise GateBlocked("target identity shape mismatch")
    if (
        record["schema_version"] != "1"
        or record["record_type"] != "TargetEnrollment"
        or record["status"] != expected_status
        or record["model"] != "AX-308"
    ):
        raise GateBlocked("target is not an enrolled AX-308")
    if record["physical_outputs_energized"] is not False:
        raise GateBlocked("physical outputs must be confirmed de-energized")
    if record["forced_values_present"] is not False:
        raise GateBlocked("forced values are present or unknown")
    _timestamp(record["captured_at"])
    for field in (
        "device_identity",
        "runtime_version",
        "firmware_version",
        "application_identity",
    ):
        if not isinstance(record[field], TEXT_TYPES) or not record[field]:
            raise GateBlocked("target " + field + " is missing")
    for field in (
        "gateway_route_digest",
        "isolation_evidence_digest",
        "fingerprint",
    ):
        _validate_digest(record[field], "target " + field)
    return record


def _validate_deployment_journal(paths, job_id):
    _validate_uuid(job_id, "deployment journal job_id")
    if not paths or len(paths) > 10000:
        raise WorkerError("deployment journal must be a bounded non-empty chain")
    ordered = sorted(paths)
    chain_digest = None
    for expected_sequence, path in enumerate(ordered, 1):
        event, event_raw = _load_json_file(path)
        if not isinstance(event, dict) or set(event) != DEPLOYMENT_JOURNAL_KEYS:
            raise WorkerError("DeploymentJournalEvent shape mismatch")
        if (
            event["schema_version"] != "1"
            or event["record_type"] != "DeploymentJournalEvent"
        ):
            raise WorkerError("unsupported DeploymentJournalEvent")
        _bounded_int(event["sequence"], "deployment journal sequence", 1, 10000)
        _validate_uuid(event["job_id"], "deployment journal event job_id")
        _timestamp(event["created_at"])
        name = event["event"]
        if (
            not isinstance(name, TEXT_TYPES)
            or not name
            or len(name) > 128
            or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in name)
        ):
            raise WorkerError("deployment journal event name is invalid")
        if not isinstance(event["details"], dict):
            raise WorkerError("deployment journal details must be an object")
        expected_name = "%04d-%s.json" % (expected_sequence, name)
        if (
            event["sequence"] != expected_sequence
            or event["job_id"] != job_id
            or os.path.basename(path) != expected_name
        ):
            raise WorkerError("deployment journal sequence or filename binding mismatch")
        event_digest = _sha256_bytes(event_raw)
        if chain_digest is None:
            chain_digest = event_digest
        else:
            chain_digest = _sha256_bytes(
                _json_bytes(
                    {
                        "previous_digest": chain_digest,
                        "event_digest": event_digest,
                    }
                )
            )
    return chain_digest


def _validate_deployment_launch_uncertainty(
    record, request, request_raw, target_fingerprint
):
    if not isinstance(record, dict) or set(record) != DEPLOYMENT_LAUNCH_UNCERTAINTY_KEYS:
        raise WorkerError("DeploymentLaunchUncertainty shape mismatch")
    if (
        record["schema_version"] != "1"
        or record["record_type"] != "DeploymentLaunchUncertainty"
        or record["operation"] != "deployment.execute"
        or record["launch_state"] != "unknown"
    ):
        raise WorkerError("unsupported DeploymentLaunchUncertainty")
    _timestamp(record["created_at"])
    _validate_uuid(record["correlation_id"], "launch uncertainty correlation_id")
    _validate_uuid(record["job_id"], "launch uncertainty job_id")
    for field in ("environment_digest", "target_fingerprint", "request_digest"):
        _validate_digest(record[field], "launch uncertainty " + field)
    if (
        request.get("operation") != "deployment.execute"
        or record["correlation_id"] != request.get("correlation_id")
        or record["job_id"] != request.get("job_id")
        or record["environment_digest"] != request.get("environment_digest")
        or record["target_fingerprint"] != target_fingerprint
        or record["request_digest"] != _sha256_bytes(request_raw)
    ):
        raise WorkerError("deployment launch uncertainty binding mismatch")
    return record


def _deployment_reconciliation_head(
    root, request, request_raw, target_fingerprint
):
    job_id = request.get("job_id")
    _validate_uuid(job_id, "deployment provenance job_id")
    _validate_digest(target_fingerprint, "deployment provenance target")
    journal_dir = os.path.dirname(
        _contained_path(root, "state/deployments/" + job_id + "/sentinel.tmp")
    )
    entries = sorted(
        os.path.join(journal_dir, name)
        for name in os.listdir(journal_dir)
        if name.endswith(".json")
    ) if os.path.isdir(journal_dir) else []
    if entries:
        digest = _validate_deployment_journal(entries, job_id)
        events = [_load_json_file(path)[0] for path in entries]
        if any(
            event.get("event") in (
                "candidate-full-download-intent",
                "rollback-full-download-intent",
            )
            for event in events
        ):
            return {"kind": "journal", "digest": digest}
        if [event["event"] for event in events] not in (
            ["authority-claimed"],
            ["authority-claimed", "target-preflight-passed"],
        ):
            raise WorkerError("deployment pre-intent journal prefix is invalid")
        if events[0]["details"] != {
            "authority_digest": request.get("authorization_digest"),
            "reconciliation_required": False,
        } or (
            len(events) == 2
            and events[1]["details"] != {"forced_values_present": False}
        ):
            raise WorkerError("deployment pre-intent journal binding mismatch")
        launch_path = _contained_path(
            root, "state/deployment-launch-uncertainty/" + job_id + ".json"
        )
        launch, launch_raw = _load_json_file(launch_path)
        _validate_deployment_launch_uncertainty(
            launch, request, request_raw, target_fingerprint
        )
        composite = _sha256_bytes(
            _json_bytes(
                {
                    "journal_head_digest": digest,
                    "launch_uncertainty_digest": _sha256_bytes(launch_raw),
                }
            )
        )
        return {"kind": "pre_intent_journal", "digest": composite}
    launch_path = _contained_path(
        root, "state/deployment-launch-uncertainty/" + job_id + ".json"
    )
    launch, launch_raw = _load_json_file(launch_path)
    _validate_deployment_launch_uncertainty(
        launch, request, request_raw, target_fingerprint
    )
    return {"kind": "launch_uncertainty", "digest": _sha256_bytes(launch_raw)}


def _absolute_regular_file(value, label):
    if not isinstance(value, TEXT_TYPES) or not os.path.isabs(value):
        raise WorkerError(label + " must be an absolute path")
    path = os.path.abspath(value)
    if not os.path.isfile(path) or os.path.islink(path):
        raise WorkerError(label + " must be an existing non-symlink file")
    return path


def _bounded_int(value, label, minimum, maximum):
    if (
        isinstance(value, bool)
        or not isinstance(value, INTEGER_TYPES)
        or value < minimum
        or value > maximum
    ):
        raise WorkerError(label + " is outside the bounded range")
    return value


def _process_alive(pid):
    if isinstance(pid, bool) or not isinstance(pid, INTEGER_TYPES) or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False if ctypes.get_last_error() == 87 else None
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return None
                return exit_code.value == 259
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return None
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return None


def _load_deployment_config(root, config_path):
    config_path = _absolute_regular_file(config_path, "deployment config")
    expected_config = os.path.abspath(
        os.path.join(os.path.abspath(root), os.pardir, "deployment-launcher-config.json")
    )
    if os.path.normcase(config_path) != os.path.normcase(expected_config):
        raise WorkerError("deployment config is not the fixed scratch-root sibling")
    config, config_raw = _load_json_file(config_path)
    version = config.get("schema_version") if isinstance(config, dict) else None
    expected = DEPLOYMENT_CONFIG_KEYS | (
        set(("runtime_capabilities",)) if version == "2" else set()
    )
    if set(config) != expected or version not in ("1", "2"):
        raise WorkerError("deployment config shape or version mismatch")
    if version == "2" and config["runtime_capabilities"] != ["workflow_deadline_v1", "transfer_retention_v1"]:
        raise WorkerError("deployment runtime capabilities are invalid")
    _validate_uuid(config["worker_id"], "worker_id")
    _validate_digest(config["environment_digest"], "environment_digest")
    _validate_digest(config["target_fingerprint"], "target_fingerprint")
    if config["ide_profile"] != "DIADesigner-AX 1.9":
        raise WorkerError("deployment profile is not frozen to DIADesigner-AX 1.9")
    paths = {}
    for field in (
        "ide_executable",
        "script_engine",
        "worker_script",
        "target_adapter",
        "autonomous_config",
        "target_enrollment",
    ):
        path = _absolute_regular_file(config[field], field)
        expected = config[field + "_sha256"]
        _validate_digest(expected, field + "_sha256")
        if _sha256_file(path) != expected:
            raise WorkerError(field + " bytes changed after deployment configuration")
        paths[field] = path
    if os.path.normcase(paths["worker_script"]) != os.path.normcase(os.path.abspath(__file__)):
        raise WorkerError("deployment config does not pin this worker script")
    autonomous, _autonomous_raw = _load_json_file(paths["autonomous_config"])
    expected_autonomous = set(
        (
            "config_version",
            "environment_digest",
            "target_allowlist",
            "deployment_authority_phrase",
            "deployment_ttl_seconds",
            "observation_expression_allowlist",
        )
    )
    if set(autonomous) != expected_autonomous or autonomous["config_version"] != "2":
        raise WorkerError("autonomous config shape or version mismatch")
    if autonomous["environment_digest"] != config["environment_digest"]:
        raise WorkerError("deployment/autonomous environment digest mismatch")
    if autonomous["deployment_authority_phrase"] != "yükle":
        raise WorkerError("deployment authority phrase drifted")
    targets = autonomous["target_allowlist"]
    if not isinstance(targets, list) or not any(
        isinstance(item, dict)
        and set(item) == set(("label", "fingerprint"))
        and item.get("fingerprint") == config["target_fingerprint"]
        for item in targets
    ):
        raise WorkerError("deployment target is not in the current host allowlist")
    expressions = config["observation_expression_allowlist"]
    if (
        not isinstance(expressions, list)
        or not expressions
        or len(expressions) > 64
        or len(set(expressions)) != len(expressions)
        or expressions != autonomous["observation_expression_allowlist"]
    ):
        raise WorkerError("deployment observation allowlist mismatch")
    enrollment, _enrollment_raw = _load_json_file(paths["target_enrollment"])
    expected_enrollment = os.path.abspath(
        os.path.join(os.path.abspath(root), "target-enrollment.json")
    )
    if os.path.normcase(paths["target_enrollment"]) != os.path.normcase(
        expected_enrollment
    ):
        raise WorkerError("target enrollment is not the fixed v2 record")
    _validate_target_record(enrollment, "enrolled")
    if enrollment["fingerprint"] != config["target_fingerprint"]:
        raise WorkerError("deployment enrollment fingerprint mismatch")
    heartbeat = _bounded_int(config["heartbeat_seconds"], "heartbeat_seconds", 1, 30)
    lease_timeout = _bounded_int(
        config["lease_timeout_seconds"], "lease_timeout_seconds", 2, 120
    )
    if heartbeat >= lease_timeout:
        raise WorkerError("heartbeat must be shorter than lease timeout")
    _bounded_int(config["max_runtime_seconds"], "max_runtime_seconds", 30, 660 if version == "2" else 600)
    _bounded_int(config["max_requests"], "max_requests", 1, 100)
    _bounded_int(config["idle_exit_seconds"], "idle_exit_seconds", 1, 60)
    _bounded_int(config["startup_timeout_seconds"], "startup_timeout_seconds", 1, 60)
    return config, config_raw, enrollment


def _load_target_adapter(path, expected_digest, context):
    path = _absolute_regular_file(path, "target_adapter")
    if _sha256_file(path) != expected_digest:
        raise WorkerError("target adapter bytes changed before load")
    name = "_axmcp_pinned_target_adapter"
    module = None
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise WorkerError("target adapter import spec is unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except ImportError:
        import imp

        module = imp.load_source(name, path)
    if getattr(module, "ADAPTER_KIND", None) != "DIADESIGNER_AX308_SP18_TARGET/1":
        raise WorkerError("target adapter kind is not the frozen AX-308/SP18 interface")
    factory = getattr(module, "create_target_api", None)
    if not callable(factory):
        raise WorkerError("target adapter factory is unavailable")
    target = factory(context)
    for method in (
        "probe",
        "current_application",
        "verify_running_artifact",
        "reconcile",
    ):
        if not callable(getattr(target, method, None)):
            raise WorkerError("target adapter is missing method " + method)
    if _sha256_file(path) != expected_digest:
        raise WorkerError("target adapter bytes changed during load")
    return target


def _verify_loaded_script_engine(expected_path, expected_digest, assemblies=None):
    expected_path = _absolute_regular_file(expected_path, "script_engine")
    if _sha256_file(expected_path) != expected_digest:
        raise WorkerError("script engine bytes changed before runtime identity check")
    if assemblies is None:
        try:
            import System

            assemblies = list(System.AppDomain.CurrentDomain.GetAssemblies())
        except Exception as exc:
            raise WorkerError("loaded ScriptEngine assembly inventory is unavailable: " + str(exc))
    expected = os.path.normcase(os.path.abspath(expected_path))
    loaded = []
    for assembly in assemblies:
        try:
            location = os.path.normcase(os.path.abspath(str(assembly.Location)))
        except Exception:
            continue
        if location == expected:
            loaded.append(location)
    if len(loaded) != 1:
        raise WorkerError("the exact pinned ScriptEngine assembly is not loaded")
    if _sha256_file(expected_path) != expected_digest:
        raise WorkerError("script engine bytes changed during runtime identity check")
    return expected_path


class AxDeploymentWorker(object):
    """AXQ-FILE/2 adapter with injected ScriptEngine and target probes."""

    def __init__(
        self,
        root,
        environment_digest,
        target_enrollment,
        observation_expression_allowlist,
        projects_api,
        online_api,
        target_api,
        online_change_options_api,
        application_states_api,
        version_update_flags_api,
        credential_source_kind_api,
        worker_id=None,
        side_effect_callback=None,
        now=None,
        sleeper=None,
        operating_states_api=None,
        workflow_deadline_capability=False,
    ):
        self.root = os.path.abspath(root)
        if not os.path.isdir(self.root):
            raise WorkerError("AXQ scratch root does not exist")
        _validate_digest(environment_digest, "environment_digest")
        self.environment_digest = environment_digest
        self.enrollment = _validate_target_record(target_enrollment, "enrolled")
        if (
            not isinstance(observation_expression_allowlist, list)
            or not observation_expression_allowlist
            or len(observation_expression_allowlist) > 64
            or len(set(observation_expression_allowlist))
            != len(observation_expression_allowlist)
        ):
            raise WorkerError("observation expression allowlist is invalid")
        for expression in observation_expression_allowlist:
            if not isinstance(expression, TEXT_TYPES) or not EXPRESSION_RE.match(expression):
                raise WorkerError("observation expression allowlist contains an invalid entry")
        self.observation_expression_allowlist = set(observation_expression_allowlist)
        self.projects = projects_api
        self.online = online_api
        self.target = target_api
        self.change_options = online_change_options_api
        self.application_states = application_states_api
        self.version_update_flags = version_update_flags_api
        self.credential_sources = credential_source_kind_api
        self.operating_states = operating_states_api
        self.workflow_deadline_capability = workflow_deadline_capability is True
        # A native transfer call retains its IDE until completion is proven.
        self._transfer_held = False
        self._held_transfer = None
        if worker_id is not None:
            _validate_uuid(worker_id, "worker_id")
        if side_effect_callback is not None and not callable(side_effect_callback):
            raise WorkerError("side_effect_callback must be callable")
        self.worker_id = worker_id
        self._side_effect_callback = side_effect_callback
        self._now = now or time.time
        self._sleep = sleeper or time.sleep
        self._sessions = {}
        for name in ("results", "evidence", "state", "observations", "artifacts", "workspaces"):
            path = os.path.dirname(_contained_path(self.root, name + "/sentinel.tmp"))
            if not os.path.isdir(path):
                os.makedirs(path)
        if not hasattr(self.credential_sources, "none"):
            raise WorkerError("installed ScriptEngine lacks non-interactive credential mode")
        if not hasattr(self.version_update_flags, "NoUpdates"):
            raise WorkerError("installed ScriptEngine lacks NoUpdates project mode")
        if not hasattr(self.change_options, "Keep") or not hasattr(self.change_options, "Never"):
            raise WorkerError("installed ScriptEngine lacks bounded login modes")
        self.online.auth_fallback_modes = self.credential_sources.none

    def _require_profile_allowed(self, profile):
        if not set(profile["expressions"]).issubset(
            self.observation_expression_allowlist
        ):
            raise GateBlocked("observation profile contains a non-allowlisted expression")

    def _validate_request(self, request, raw, allow_expired=False):
        profile = request.get("transport_profile", "lan") if isinstance(request, dict) else None
        expected_keys = REQUEST_KEYS | (set(("transport_profile",)) if profile == "diacloud" else set())
        if not isinstance(request, dict) or set(request) != expected_keys:
            raise WorkerError("AXQ-FILE/2 request shape mismatch")
        if profile not in ("lan", "diacloud"):
            raise WorkerError("unsupported deployment transport profile")
        if profile == "diacloud" and not self.workflow_deadline_capability:
            raise WorkerError("legacy deployment worker cannot accept DIACloud deadline")
        if request["protocol"] != PROTOCOL or request["message_type"] != "request":
            raise WorkerError("unsupported protocol or message type")
        _validate_uuid(request["correlation_id"], "correlation_id")
        _validate_uuid(request["job_id"], "job_id")
        if request["operation"] not in SUPPORTED_OPERATIONS:
            raise WorkerError("operation is not supported by deployment worker")
        if request["environment_digest"] != self.environment_digest:
            raise WorkerError("environment digest mismatch")
        if request["workspace_digest"] is not None or request["expected_revision"] is not None:
            raise WorkerError("target operations cannot carry workspace state")
        created = _timestamp(request["created_at"])
        deadline = _timestamp(request["deadline_at"])
        maximum = (
            600 if profile == "diacloud" and request["operation"] == "deployment.execute"
            else 330 if request["operation"] == "deployment.execute" else 300
        )
        if deadline <= created or deadline - created > maximum:
            raise WorkerError("request deadline window is invalid")
        if not allow_expired and self._now() > deadline:
            raise GateBlocked("request deadline has expired")
        artifacts = request["artifacts"]
        if not isinstance(artifacts, list) or len(artifacts) > 32:
            raise WorkerError("request artifacts are invalid")
        seen = set()
        for artifact in artifacts:
            if not isinstance(artifact, dict) or set(artifact) != ARTIFACT_KEYS:
                raise WorkerError("artifact descriptor shape mismatch")
            if artifact["role"] not in (
                "input",
                "output",
                "project",
                "source",
                "recipe",
                "authorization",
                "observation",
                "evidence",
            ):
                raise WorkerError("artifact role is invalid")
            if artifact["path"] in seen:
                raise WorkerError("duplicate artifact path")
            seen.add(artifact["path"])
            _validate_digest(artifact["sha256"], "artifact sha256")
            path = _contained_path(self.root, artifact["path"])
            if not os.path.isfile(path) or _sha256_file(path) != artifact["sha256"]:
                raise WorkerError("artifact is missing or digest-mismatched")
        if request["operation"] == "deployment.execute":
            if request["authorization_digest"] is None:
                raise WorkerError("deployment.execute requires authorization_digest")
            _validate_digest(request["authorization_digest"], "authorization_digest")
        elif request["authorization_digest"] is not None:
            raise WorkerError("read-only observation cannot carry deployment authority")
        return _sha256_bytes(raw), deadline

    def _validate_terminal_result_chain(
        self, result_path, supplied_request=None, supplied_request_raw=None
    ):
        result, unused_result_raw = _load_json_file(result_path)
        del unused_result_raw
        if not isinstance(result, dict) or set(result) != RESULT_KEYS:
            raise WorkerError("AXQ-FILE/2 result shape mismatch")
        if result["protocol"] != PROTOCOL or result["message_type"] != "result":
            raise WorkerError("unsupported result protocol or message type")
        _validate_uuid(result["correlation_id"], "result correlation_id")
        _validate_uuid(result["job_id"], "result job_id")
        if result["operation"] not in SUPPORTED_OPERATIONS:
            raise WorkerError("result operation is not supported")
        _timestamp(result["created_at"])
        if result["status"] not in (
            "succeeded",
            "blocked",
            "failed",
            "cancelled",
            "unknown_outcome",
        ):
            raise WorkerError("result status is not terminal")
        if result["environment_digest"] != self.environment_digest:
            raise WorkerError("result environment digest mismatch")
        if (
            result["workspace_digest"] is not None
            or result["revision"] is not None
            or result["before_project_digest"] is not None
            or result["after_project_digest"] is not None
        ):
            raise WorkerError("target result carries workspace state")
        _validate_digest(result["request_digest"], "result request_digest")
        artifacts = result["artifacts"]
        if not isinstance(artifacts, list) or len(artifacts) > 32:
            raise WorkerError("result artifacts are invalid")
        seen = set()
        for artifact in artifacts:
            if not isinstance(artifact, dict) or set(artifact) != ARTIFACT_KEYS:
                raise WorkerError("result artifact descriptor shape mismatch")
            if artifact["path"] in seen:
                raise WorkerError("duplicate result artifact path")
            seen.add(artifact["path"])
            _validate_digest(artifact["sha256"], "result artifact sha256")
            artifact_path = _contained_path(self.root, artifact["path"])
            if not os.path.isfile(artifact_path) or _sha256_file(artifact_path) != artifact["sha256"]:
                raise WorkerError("result artifact is missing or digest-mismatched")
        diagnostics = result["diagnostics"]
        if not isinstance(diagnostics, list) or len(diagnostics) > 10000:
            raise WorkerError("result diagnostics are invalid")
        for diagnostic in diagnostics:
            if not isinstance(diagnostic, dict) or set(diagnostic) != DIAGNOSTIC_KEYS:
                raise WorkerError("result diagnostic shape mismatch")
            if diagnostic["severity"] not in ("info", "warning", "error", "critical"):
                raise WorkerError("result diagnostic severity is invalid")
            if not isinstance(diagnostic["code"], TEXT_TYPES) or not diagnostic["code"]:
                raise WorkerError("result diagnostic code is invalid")
            if not isinstance(diagnostic["message"], TEXT_TYPES) or not diagnostic["message"]:
                raise WorkerError("result diagnostic message is invalid")
        if supplied_request is None:
            request_path = _contained_path(
                self.root,
                "archive/" + result["correlation_id"] + ".request.json",
            )
            request, request_raw = _load_json_file(request_path)
        else:
            request = supplied_request
            request_raw = supplied_request_raw
            if request_raw is None:
                raise WorkerError("supplied request bytes are missing")
        self._validate_request(request, request_raw, allow_expired=True)
        for field in (
            "correlation_id",
            "job_id",
            "operation",
            "environment_digest",
            "workspace_digest",
        ):
            if request[field] != result[field]:
                raise WorkerError("request/result " + field + " mismatch")
        if result["request_digest"] != _sha256_bytes(request_raw):
            raise WorkerError("result request digest mismatch")
        return request, result

    def _validate_worker_evidence(self, evidence, result):
        if not isinstance(evidence, dict) or set(evidence) != DEPLOYMENT_EVIDENCE_KEYS:
            raise WorkerError("AxDeploymentWorkerEvidence shape mismatch")
        if (
            evidence["schema_version"] != "1"
            or evidence["record_type"] != "AxDeploymentWorkerEvidence"
        ):
            raise WorkerError("unsupported AxDeploymentWorkerEvidence")
        _timestamp(evidence["created_at"])
        _validate_uuid(evidence["correlation_id"], "evidence correlation_id")
        _validate_uuid(evidence["job_id"], "evidence job_id")
        if evidence["operation"] not in SUPPORTED_OPERATIONS:
            raise WorkerError("evidence operation is not supported")
        for field in (
            "target_fingerprint",
            "authority_digest",
            "candidate_digest",
            "rollback_digest",
        ):
            if evidence[field] is not None:
                _validate_digest(evidence[field], "evidence " + field)
        if not isinstance(evidence["reconciliation_required"], bool):
            raise WorkerError("evidence reconciliation_required must be boolean")
        _bounded_int(evidence["sample_count"], "evidence sample_count", 0, 10000000)
        messages = evidence["diagnostics"]
        if not isinstance(messages, list) or len(messages) > 10000:
            raise WorkerError("evidence diagnostics are invalid")
        for message in messages:
            if not isinstance(message, TEXT_TYPES) or not message or len(message) > 4096:
                raise WorkerError("evidence diagnostic message is invalid")
        for field in ("correlation_id", "job_id", "operation"):
            if evidence[field] != result[field]:
                raise WorkerError("evidence/result " + field + " mismatch")
        return evidence

    def _artifact_values(self, request):
        values = []
        for descriptor in request["artifacts"]:
            path = _contained_path(self.root, descriptor["path"])
            value, raw = _load_json_file(path)
            values.append((descriptor, value, raw))
        return values

    def _require_registered_artifact(self, digest):
        _validate_digest(digest, "deployment artifact")
        relative = "artifacts/" + digest[7:] + ".project"
        path = _contained_path(self.root, relative)
        if not os.path.isfile(path) or _sha256_file(path) != digest:
            raise GateBlocked("READY_FOR_DEPLOY artifact is missing or corrupt")
        workspaces = os.path.join(self.root, "workspaces")
        registered = None
        for name in sorted(os.listdir(workspaces)):
            if not UUID_RE.match(name):
                continue
            workspace_path = os.path.join(workspaces, name)
            if os.path.islink(workspace_path):
                raise GateBlocked("symlinked workspace registration is forbidden")
            manifest_path = os.path.join(workspace_path, "manifest.json")
            if os.path.islink(manifest_path):
                raise GateBlocked("symlinked artifact registration is forbidden")
            if not os.path.isfile(manifest_path):
                continue
            try:
                manifest, unused = _load_json_file(manifest_path)
                del unused
            except Exception:
                continue
            records = manifest.get("frozen_artifacts")
            if not isinstance(records, list):
                continue
            for record in records:
                if (
                    isinstance(record, dict)
                    and record.get("artifact_digest") == digest
                    and record.get("status") == "READY_FOR_DEPLOY"
                    and record.get("path") == relative
                    and record.get("environment_digest") == self.environment_digest
                ):
                    build_digest = record.get("build_digest")
                    _validate_digest(build_digest, "frozen artifact build_digest")
                    _validate_digest(
                        record.get("runtime_release_digest"),
                        "frozen artifact runtime_release_digest",
                    )
                    registered = record
                    break
            if registered:
                break
        if not registered:
            raise GateBlocked("artifact is not registered as READY_FOR_DEPLOY")
        return path, registered

    def _validate_plan(self, plan):
        if not isinstance(plan, dict) or set(plan) != PLAN_KEYS:
            raise GateBlocked("DeploymentPlan shape mismatch")
        if (
            plan["schema_version"] != "1"
            or plan["record_type"] != "DeploymentPlan"
            or plan["one_shot"] is not True
            or plan["state"] != "prepared"
            or plan["authority_digest"] is not None
            or plan["reconciliation_required"] is not False
        ):
            raise GateBlocked("DeploymentPlan is not an unmodified prepared plan")
        _validate_uuid(plan["plan_id"], "plan_id")
        _validate_uuid(plan["challenge_nonce"], "challenge_nonce")
        for field in (
            "artifact_digest",
            "build_digest",
            "runtime_release_digest",
            "target_fingerprint",
            "rollback_digest",
            "rollback_runtime_release_digest",
            "observation_digest",
        ):
            _validate_digest(plan[field], "plan " + field)
        created = _timestamp(plan["created_at"])
        expires = _timestamp(plan["expires_at"])
        valid_for = plan["valid_for_seconds"]
        if (
            isinstance(valid_for, bool)
            or not isinstance(valid_for, INTEGER_TYPES)
            or not 1 <= valid_for <= 600
            or expires != created + valid_for
        ):
            raise GateBlocked("DeploymentPlan expiry invariant failed")
        return expires

    def _validate_authority(self, authority, plan, request, execution):
        version = authority.get("schema_version") if isinstance(authority, dict) else None
        workflow_keys = set(("transport_profile", "workflow_deadline_at")) if version == "2" else set()
        if not isinstance(authority, dict) or set(authority) != AUTHORITY_KEYS | workflow_keys:
            raise GateBlocked("DeploymentAuthority shape mismatch")
        if (
            version not in ("1", "2")
            or authority["record_type"] != "DeploymentAuthority"
            or authority["status"] != "consumed"
            or authority["single_use"] is not True
            or authority["phrase_digest"] != PHRASE_DIGEST
            or authority["consumed_at"] != authority["issued_at"]
        ):
            raise GateBlocked("current consumed authority is required")
        for field in (
            "plan_id",
            "artifact_digest",
            "target_fingerprint",
            "rollback_digest",
            "runtime_release_digest",
            "rollback_runtime_release_digest",
            "observation_digest",
            "challenge_nonce",
            "expires_at",
        ):
            if authority[field] != plan[field] or execution[field] != plan[field]:
                raise GateBlocked("deployment binding mismatch: " + field)
        issued = _timestamp(authority["issued_at"])
        expires = _timestamp(authority["expires_at"])
        if not issued < expires or expires - issued > 600 or self._now() > expires:
            raise GateBlocked("deployment authority is expired or invalid")
        if issued > _timestamp(request["created_at"]):
            raise GateBlocked("deployment request predates consumed authority")
        if version == "2":
            profile = authority["transport_profile"]
            maximum = 600 if profile == "diacloud" else 330
            if (
                profile not in ("lan", "diacloud")
                or request.get("transport_profile", "lan") != profile
                or execution["transport_profile"] != profile
                or request["deadline_at"] != authority["workflow_deadline_at"]
                or execution["workflow_deadline_at"] != authority["workflow_deadline_at"]
                or execution["schema_version"] != version
                or not issued < _timestamp(authority["workflow_deadline_at"]) <= issued + maximum
            ):
                raise GateBlocked("deployment workflow deadline binding mismatch")
        elif request.get("transport_profile", "lan") != "lan":
            raise GateBlocked("legacy authority cannot bind DIACloud deadline")
        binding = {
            "plan_id": authority["plan_id"],
            "artifact_digest": authority["artifact_digest"],
            "target_fingerprint": authority["target_fingerprint"],
            "rollback_digest": authority["rollback_digest"],
            "runtime_release_digest": authority["runtime_release_digest"],
            "rollback_runtime_release_digest": authority["rollback_runtime_release_digest"],
            "observation_digest": authority["observation_digest"],
            "challenge_nonce": authority["challenge_nonce"],
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
        expected = _sha256_bytes(_json_bytes(binding))
        if authority["authority_digest"] != expected:
            raise GateBlocked("deployment authority digest invariant failed")
        if (
            request["authorization_digest"] != expected
            or execution["authority_digest"] != expected
        ):
            raise GateBlocked("request authority digest mismatch")
        return expected

    def _load_deployment_bundle(self, request, deadline):
        inputs = []
        observations = []
        authorities = []
        for descriptor, value, raw in self._artifact_values(request):
            if descriptor["role"] == "input":
                inputs.append((value, raw))
            elif descriptor["role"] == "observation":
                observations.append((value, raw))
            elif descriptor["role"] == "authorization":
                authorities.append((value, raw))
            else:
                raise WorkerError("deployment request contains an unexpected artifact role")
        execution_matches = [item for item in inputs if item[0].get("record_type") == "DeploymentExecuteInput"]
        plan_matches = [item for item in inputs if item[0].get("record_type") == "DeploymentPlan"]
        if len(inputs) != 2 or len(execution_matches) != 1 or len(plan_matches) != 1:
            raise WorkerError("deployment requires exact execution and plan inputs")
        if len(observations) != 1 or len(authorities) != 1:
            raise WorkerError("deployment requires exact observation and authorization artifacts")
        execution, unused_execution_raw = execution_matches[0]
        plan, plan_raw = plan_matches[0]
        profile, profile_raw = observations[0]
        authority, unused_authority_raw = authorities[0]
        del unused_execution_raw, unused_authority_raw
        version = execution.get("schema_version") if isinstance(execution, dict) else None
        workflow_keys = set(("transport_profile", "workflow_deadline_at")) if version == "2" else set()
        if not isinstance(execution, dict) or set(execution) != EXECUTION_INPUT_KEYS | workflow_keys:
            raise GateBlocked("DeploymentExecuteInput shape mismatch")
        if version not in ("1", "2") or execution["record_type"] != "DeploymentExecuteInput":
            raise GateBlocked("unsupported DeploymentExecuteInput")
        _validate_uuid(execution["plan_id"], "execution plan_id")
        _validate_uuid(execution["challenge_nonce"], "execution challenge_nonce")
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
            _validate_digest(execution[field], "execution " + field)
        plan_expiry = self._validate_plan(plan)
        _validate_profile(profile)
        self._require_profile_allowed(profile)
        if execution["plan_digest"] != _sha256_bytes(plan_raw):
            raise GateBlocked("DeploymentPlan digest mismatch")
        if execution["observation_digest"] != _sha256_bytes(profile_raw):
            raise GateBlocked("ObservationProfile digest mismatch")
        if plan["observation_digest"] != execution["observation_digest"]:
            raise GateBlocked("DeploymentPlan observation binding mismatch")
        if self._now() > plan_expiry:
            raise GateBlocked("DeploymentPlan has expired")
        if (
            request.get("transport_profile", "lan") == "lan"
            and deadline - self._now() <= profile["duration_seconds"] + 5
        ):
            raise GateBlocked("request deadline cannot contain the bounded observation")
        authority_digest = self._validate_authority(authority, plan, request, execution)
        candidate_path, candidate_record = self._require_registered_artifact(
            execution["artifact_digest"]
        )
        rollback_path, rollback_record = self._require_registered_artifact(
            execution["rollback_digest"]
        )
        if candidate_record["build_digest"] != plan["build_digest"]:
            raise GateBlocked("candidate build digest does not match DeploymentPlan")
        if (
            candidate_record["runtime_release_digest"] != plan["runtime_release_digest"]
            or rollback_record["runtime_release_digest"]
            != plan["rollback_runtime_release_digest"]
        ):
            raise GateBlocked("runtime release identity does not match DeploymentPlan")
        if execution["target_fingerprint"] != self.enrollment["fingerprint"]:
            raise GateBlocked("deployment target is not the exact enrolled fingerprint")
        return {
            "execution": execution,
            "plan": plan,
            "profile": profile,
            "authority": authority,
            "authority_digest": authority_digest,
            "candidate_path": candidate_path,
            "rollback_path": rollback_path,
            "rollback_build_digest": rollback_record["build_digest"],
            "rollback_runtime_release_digest": rollback_record["runtime_release_digest"],
        }

    def _claim_authority(self, request, request_digest, authority_digest):
        path = _contained_path(
            self.root,
            "state/deployment-authorities/"
            + authority_digest[7:]
            + ".claimed.json",
        )
        value = {
            "schema_version": "1",
            "record_type": "DeploymentAuthorityClaim",
            "authority_digest": authority_digest,
            "job_id": request["job_id"],
            "correlation_id": request["correlation_id"],
            "request_digest": request_digest,
            "claimed_at": _utc_at(self._now()),
        }
        try:
            _atomic_publish(path, _json_bytes(value))
        except WorkerError:
            raise GateBlocked("consumed authority has already been claimed")

    def _journal(self, request, event, details=None):
        relative = "state/deployments/" + request["job_id"]
        directory = os.path.dirname(_contained_path(self.root, relative + "/sentinel.tmp"))
        if not os.path.isdir(directory):
            os.makedirs(directory)
        sequence = len([name for name in os.listdir(directory) if name.endswith(".json")]) + 1
        value = {
            "schema_version": "1",
            "record_type": "DeploymentJournalEvent",
            "sequence": sequence,
            "job_id": request["job_id"],
            "event": event,
            "created_at": _utc_at(self._now()),
            "details": details or {},
        }
        path = os.path.join(directory, "%04d-%s.json" % (sequence, event))
        _atomic_publish(path, _json_bytes(value))
        return value

    def _mark_host_side_effect_started(self, request):
        """Publish the exact durable marker consumed by FileJobQueue.recover.

        A runtime may inject FileJobQueue.mark_side_effect_started (or an
        equivalent adapter) as the callback.  The worker verifies the exact
        durable bytes at the host path before any Never-login is attempted.
        """
        worker_id = self.worker_id or request["job_id"]
        marker_path = _contained_path(
            self.root,
            "state/side-effects/" + request["job_id"] + ".started.json",
        )
        if self._side_effect_callback is None:
            marker = {
                "schema_version": "1",
                "job_id": request["job_id"],
                "worker_id": worker_id,
                "operation": request["operation"],
                "environment_digest": self.environment_digest,
                "started_at": _utc_at(self._now()),
            }
            _atomic_publish(marker_path, _json_bytes(marker))
        else:
            self._side_effect_callback(
                request["job_id"], worker_id, self.environment_digest
            )
        try:
            marker, _raw = _load_json_file(marker_path)
        except Exception as exc:
            raise WorkerError("durable host side-effect marker is unavailable: " + str(exc))
        if (
            set(marker) != SIDE_EFFECT_KEYS
            or marker["schema_version"] != "1"
            or marker["job_id"] != request["job_id"]
            or marker["worker_id"] != worker_id
            or marker["operation"] != request["operation"]
            or marker["environment_digest"] != self.environment_digest
        ):
            raise WorkerError("durable host side-effect marker mismatch")
        _validate_uuid(marker["job_id"], "side-effect job_id")
        _validate_uuid(marker["worker_id"], "side-effect worker_id")
        _timestamp(marker["started_at"])
        return marker

    def _materialize_project_copy(self, request, label, source, digest):
        """Copy an immutable artifact before allowing the IDE to open it."""
        target = _contained_path(
            self.root,
            "state/deployments/"
            + request["job_id"]
            + "/"
            + label
            + "-"
            + digest[7:]
            + ".project",
        )
        handle = open(source, "rb")
        try:
            raw = handle.read()
        finally:
            handle.close()
        if _sha256_bytes(raw) != digest:
            raise GateBlocked(label + " artifact changed before materialization")
        _atomic_publish(target, raw)
        if _sha256_file(target) != digest:
            raise GateBlocked(label + " task-local copy digest mismatch")
        return target

    def _open_project(self, path):
        project = self.projects.open(
            path=path,
            primary=True,
            update_flags=self.version_update_flags.NoUpdates,
            allow_readonly=False,
        )
        application = getattr(project, "active_application", None)
        if application is None:
            _close_project(project)
            raise GateBlocked("deployment artifact has no active application")
        return project, application

    def _verify_live_target(self, application, expected_fingerprint):
        live = self.target.probe(application)
        _validate_target_record(live, "enrolled")
        if live["fingerprint"] != expected_fingerprint:
            raise GateBlocked("live target fingerprint mismatch")
        for field in (
            "model",
            "device_identity",
            "runtime_version",
            "firmware_version",
            "gateway_route_digest",
            "application_identity",
            "isolation_evidence_digest",
        ):
            if live[field] != self.enrollment[field]:
                raise GateBlocked("live target identity mismatch: " + field)
        return live

    def _new_online_application(self, application, timeout_seconds):
        online_application = self.online.create_online_application(application)
        if online_application is None:
            raise GateBlocked("ScriptEngine did not create an online application")
        online_application.timeout = max(1, min(60, int(timeout_seconds)))
        return online_application

    def _read_only_login(self, application, timeout_seconds):
        online_application = self._new_online_application(application, timeout_seconds)
        try:
            online_application.login(self.change_options.Keep, False)
            if not bool(getattr(online_application, "is_logged_in", False)):
                raise GateBlocked("read-only target login did not complete")
            forced = list(online_application.get_forced_expressions())
            if forced:
                raise GateBlocked("forced values are present on the target")
            return online_application
        except Exception:
            _dispose_online(online_application)
            raise

    def _full_download_start(self, request, application, label, deadline, context, project):
        states = self.operating_states
        if states is None or any(
            not hasattr(states, name)
            for name in ("download", "run_after_download", "program_loaded")
        ):
            raise GateBlocked("installed ScriptEngine lacks verified transfer state flags")
        online_application = self._new_online_application(
            application, self._remaining_time(deadline, label + " online creation")
        )
        if not hasattr(online_application, "operation_state"):
            _dispose_online(online_application)
            raise GateBlocked("installed ScriptEngine lacks operation_state")
        try:
            # Persist an intent before the host marker.  If the process dies
            # after the marker but before the next journal publish, recovery
            # still has a complete, immutable boundary proving which artifact
            # could have reached the target.
            self._journal(
                request,
                label + "-full-download-intent",
                {
                    "authority_digest": context["authority_digest"],
                    "artifact_digest": context[label + "_digest"],
                    "reconciliation_required": True,
                },
            )
            if not context["side_effect_started"]:
                marker = self._mark_host_side_effect_started(request)
                context["side_effect_started"] = True
            else:
                marker = None
            self._journal(
                request,
                label + "-full-download-api-intent",
                {
                    "authority_digest": context["authority_digest"],
                    "artifact_digest": context[label + "_digest"],
                    "host_side_effect_marker_sha256": (
                        _sha256_bytes(_json_bytes(marker)) if marker is not None else None
                    ),
                    "reconciliation_required": True,
                },
            )
            online_application.timeout = max(
                1,
                min(60, int(self._remaining_time(deadline, label + " download login"))),
            )
            self._transfer_held = True
            self._held_transfer = {
                "request": request,
                "label": label,
                "online": online_application,
                "project": project,
                "last_state": None,
                "observed_active": False,
                "terminal_observed": False,
                "online_released": False,
                "project_closed": False,
            }
            online_application.login(self.change_options.Never, False)
            # A returned login does not prove that target initialization ended.
            state = online_application.operation_state
            try:
                observed = int(state)
                download = int(states.download)
                initializing = int(states.run_after_download)
                loaded = int(states.program_loaded)
            except (TypeError, ValueError):
                raise RuntimeError(label + " transfer state cannot be interpreted")
            self._journal(
                request,
                label + "-transfer-state-observed",
                {"operation_state": observed, "reconciliation_required": True},
            )
            self._held_transfer["last_state"] = observed
            if observed & (download | initializing) or not observed & loaded:
                self._held_transfer["observed_active"] = bool(observed & (download | initializing))
                raise RuntimeError(label + " download or initialization is incomplete")
            self._transfer_held = False
            self._held_transfer = None
            self._journal(
                request,
                label + "-download-initialization-complete",
                {"operation_state": observed, "reconciliation_required": False},
            )
            self._remaining_time(deadline, label + " download login")
            if not bool(getattr(online_application, "is_logged_in", False)):
                raise RuntimeError(label + " full-download login did not complete")
            self._remaining_time(deadline, label + " runtime artifact verification")
            verified = self.target.verify_running_artifact(
                online_application,
                context[label + "_runtime_release_digest"],
            )
            if verified is not True:
                raise RuntimeError(label + " runtime artifact marker did not match")
            online_application.timeout = max(
                1,
                min(60, int(self._remaining_time(deadline, label + " application start"))),
            )
            online_application.start()
            self._remaining_time(deadline, label + " application start")
            if not _state_is(
                online_application.application_state,
                self.application_states.run,
                "run",
            ):
                raise RuntimeError(label + " application did not reach RUN")
            verified = self.target.verify_running_artifact(
                online_application,
                context[label + "_runtime_release_digest"],
            )
            if verified is not True:
                raise RuntimeError(label + " runtime artifact marker drifted after RUN")
            self._remaining_time(deadline, label + " post-start verification")
            self._journal(
                request,
                label + "-run-verified",
                {
                    "artifact_digest": context[label + "_digest"],
                    "reconciliation_required": False,
                },
            )
            return online_application
        except BaseException:
            if not self._transfer_held:
                _dispose_online(online_application)
            raise

    def observe_held_transfer(self):
        """Append only changed late state; never infer a safe retry or start."""
        held = self._held_transfer
        if not self._transfer_held or held is None:
            return False
        if held["terminal_observed"]:
            return self._release_held_transfer()
        try:
            observed = int(held["online"].operation_state)
        except (TypeError, ValueError):
            return False
        if observed == held["last_state"]:
            return False
        states = self.operating_states
        active = bool(observed & (int(states.download) | int(states.run_after_download)))
        loaded = bool(observed & int(states.program_loaded))
        terminal = held["observed_active"] and not active and loaded
        self._journal(
            held["request"], held["label"] + "-late-transfer-state-observed",
            {
                "operation_state": observed,
                "transfer_busy_observed": held["observed_active"] or active,
                "terminal_transfer_observed": terminal,
                "reconciliation_required": True,
            },
        )
        held["last_state"] = observed
        held["observed_active"] = held["observed_active"] or active
        if terminal:
            held["terminal_observed"] = True
            return self._release_held_transfer()
        return True

    def _release_held_transfer(self):
        held = self._held_transfer
        if held is None or not held["terminal_observed"]:
            return False
        online_application = held["online"]
        if not held["online_released"]:
            if bool(getattr(online_application, "is_logged_in", False)):
                online_application.logout()
            disposer = getattr(online_application, "Dispose", None)
            if callable(disposer):
                disposer()
            held["online_released"] = True
        if not held["project_closed"]:
            held["project"].close()
            held["project_closed"] = True
        self._journal(
            held["request"], held["label"] + "-transfer-owner-released",
            {"terminal_transfer_observed": True, "reconciliation_required": True},
        )
        self._transfer_held = False
        self._held_transfer = None
        return True

    def _sample(self, observation_id, target_fingerprint, profile, online_application):
        raw_values = list(online_application.read_values(tuple(profile["expressions"])))
        if len(raw_values) != len(profile["expressions"]):
            raise RuntimeError("ScriptEngine returned an incomplete observation sample")
        values = {}
        for index, expression in enumerate(profile["expressions"]):
            values[expression] = _coerce_online_value(raw_values[index])
        violations = []
        for invariant in profile["fault_invariants"]:
            actual = values[invariant["expression"]]
            if not _comparison(invariant["operator"], actual, invariant["expected"]):
                violations.append(
                    {
                        "expression": invariant["expression"],
                        "operator": invariant["operator"],
                        "expected": invariant["expected"],
                        "actual": actual,
                        "action": invariant["action"],
                    }
                )
        sample = {
            "schema_version": "1",
            "record_type": "ObservationSample",
            "observation_id": observation_id,
            "captured_at": _utc_at(self._now()),
            "target_fingerprint": target_fingerprint,
            "values": values,
            "violations": violations,
        }
        path = _contained_path(
            self.root, "observations/" + observation_id + ".jsonl"
        )
        _append_json_line(path, sample)
        return sample

    def _remaining_time(self, deadline, label, reserve=0):
        remaining = deadline - self._now()
        if remaining <= reserve:
            raise RuntimeError(label + " exceeded the request deadline budget")
        return remaining

    def _observe_deployment(self, request, bundle, online_application, deadline):
        profile = bundle["profile"]
        start = self._now()
        duration = profile["duration_seconds"]
        if deadline - start < duration + ROLLBACK_RESERVE_SECONDS:
            raise RuntimeError("deployment observation has insufficient deadline budget")
        interval = profile["interval_ms"] / 1000.0
        limit = int(math.ceil(duration / interval)) + 1
        samples = []
        for unused in range(limit):
            del unused
            sample_budget = self._remaining_time(
                deadline, "deployment observation", ROLLBACK_RESERVE_SECONDS
            ) - ROLLBACK_RESERVE_SECONDS
            if self._now() - start >= duration:
                break
            online_application.timeout = max(
                1, min(5, int(math.ceil(sample_budget)))
            )
            sample = self._sample(
                request["job_id"],
                bundle["execution"]["target_fingerprint"],
                profile,
                online_application,
            )
            samples.append(sample)
            if any(item["action"] == "stop_and_rollback" for item in sample["violations"]):
                return samples, True
            remaining = duration - (self._now() - start)
            if remaining > 0:
                deadline_remaining = self._remaining_time(
                    deadline, "deployment observation", ROLLBACK_RESERVE_SECONDS
                ) - ROLLBACK_RESERVE_SECONDS
                sleep_for = min(interval, remaining, deadline_remaining)
                if sleep_for <= 0:
                    raise RuntimeError("deployment observation deadline is exhausted")
                self._sleep(sleep_for)
        if self._now() - start < duration:
            raise RuntimeError("bounded observation clock did not advance")
        self._remaining_time(deadline, "deployment observation", ROLLBACK_RESERVE_SECONDS)
        return samples, False

    def _execute_deployment(self, request, request_digest, deadline, context):
        bundle = self._load_deployment_bundle(request, deadline)
        diacloud = request.get("transport_profile") == "diacloud"
        execution = bundle["execution"]
        context.update(
            {
                "target_fingerprint": execution["target_fingerprint"],
                "authority_digest": bundle["authority_digest"],
                "candidate_digest": execution["artifact_digest"],
                "candidate_build_digest": bundle["plan"]["build_digest"],
                "candidate_runtime_release_digest": bundle["plan"]["runtime_release_digest"],
                "rollback_digest": execution["rollback_digest"],
                "rollback_build_digest": bundle["rollback_build_digest"],
                "rollback_runtime_release_digest": bundle[
                    "rollback_runtime_release_digest"
                ],
            }
        )
        self._claim_authority(request, request_digest, bundle["authority_digest"])
        self._journal(
            request,
            "authority-claimed",
            {"authority_digest": bundle["authority_digest"], "reconciliation_required": False},
        )
        candidate_copy = self._materialize_project_copy(
            request,
            "candidate",
            bundle["candidate_path"],
            execution["artifact_digest"],
        )
        rollback_copy = self._materialize_project_copy(
            request,
            "rollback",
            bundle["rollback_path"],
            execution["rollback_digest"],
        )
        candidate_project = None
        candidate_online = None
        try:
            candidate_project, candidate_application = self._open_project(candidate_copy)
            self._verify_live_target(candidate_application, execution["target_fingerprint"])
            preflight = self._read_only_login(
                candidate_application,
                self._remaining_time(deadline, "candidate preflight"),
            )
            _dispose_online(preflight)
            self._journal(request, "target-preflight-passed", {"forced_values_present": False})
            if not diacloud and (
                deadline - self._now()
                <= bundle["profile"]["duration_seconds"] + ROLLBACK_RESERVE_SECONDS
            ):
                raise GateBlocked(
                    "candidate deployment has insufficient observation and rollback budget"
                )
            candidate_online = self._full_download_start(
                request,
                candidate_application,
                "candidate",
                deadline,
                context,
                candidate_project,
            )
            self._remaining_time(
                deadline, "candidate deployment", 0 if diacloud else ROLLBACK_RESERVE_SECONDS
            )
            self._verify_live_target(candidate_application, execution["target_fingerprint"])
            observation_deadline = (
                self._now() + bundle["profile"]["duration_seconds"] + ROLLBACK_RESERVE_SECONDS
                if diacloud else deadline
            )
            self._journal(
                request,
                "candidate-watch-started",
                {
                    "deadline_at": _utc_at(observation_deadline),
                    "duration_seconds": bundle["profile"]["duration_seconds"],
                    "reconciliation_required": False,
                },
            )
            samples, rollback_required = self._observe_deployment(
                request, bundle, candidate_online, observation_deadline
            )
            if not rollback_required:
                self._journal(
                    request,
                    "candidate-observation-passed",
                    {"sample_count": len(samples), "reconciliation_required": False},
                )
                return {
                    "outcome": "succeeded",
                    "reconciliation_required": False,
                    "samples": samples,
                    "diagnostics": [],
                }
            self._journal(
                request,
                "fault-invariant-triggered",
                {"sample_count": len(samples), "reconciliation_required": True},
            )
            stop_deadline = self._now() + 5 if diacloud else deadline
            stop_budget = self._remaining_time(stop_deadline, "candidate stop before rollback")
            candidate_online.timeout = max(1, min(5, int(stop_budget)))
            candidate_online.stop()
            self._remaining_time(stop_deadline, "candidate stop before rollback")
            if not _state_is(
                candidate_online.application_state,
                self.application_states.stop,
                "stop",
            ):
                raise RuntimeError("candidate application did not reach STOP for rollback")
            _dispose_online(candidate_online)
            candidate_online = None
            _close_project(candidate_project)
            candidate_project = None

            rollback_project, rollback_application = self._open_project(rollback_copy)
            rollback_online = None
            rollback_deadline = self._now() + 600 if diacloud else deadline
            self._journal(
                request,
                "rollback-budget-started",
                {"deadline_at": _utc_at(rollback_deadline), "reconciliation_required": True},
            )
            try:
                self._remaining_time(rollback_deadline, "rollback target verification")
                self._verify_live_target(rollback_application, execution["target_fingerprint"])
                rollback_preflight = self._read_only_login(
                    rollback_application,
                    self._remaining_time(rollback_deadline, "rollback preflight"),
                )
                _dispose_online(rollback_preflight)
                self._remaining_time(rollback_deadline, "rollback download")
                self._journal(
                    request,
                    "rollback-preflight-passed",
                    {
                        "forced_values_present": False,
                        "build_digest": bundle["rollback_build_digest"],
                    },
                )
                rollback_online = self._full_download_start(
                    request,
                    rollback_application,
                    "rollback",
                    rollback_deadline,
                    context,
                    rollback_project,
                )
                self._remaining_time(rollback_deadline, "rollback verification")
                self._verify_live_target(rollback_application, execution["target_fingerprint"])
                self._journal(
                    request,
                    "rollback-verified",
                    {
                        "artifact_digest": execution["rollback_digest"],
                        "reconciliation_required": False,
                    },
                )
                self._remaining_time(rollback_deadline, "rollback completion")
                return {
                    "outcome": "rolled_back",
                    "reconciliation_required": False,
                    "samples": samples,
                    "diagnostics": ["fault invariant triggered verified rollback"],
                }
            finally:
                if not self._transfer_held:
                    _dispose_online(rollback_online)
                    _close_project(rollback_project)
        finally:
            if not self._transfer_held:
                _dispose_online(candidate_online)
                _close_project(candidate_project)

    def _load_observation_input(self, request, expected_keys):
        values = self._artifact_values(request)
        if len(values) != 1 or values[0][0]["role"] != "observation":
            raise WorkerError("observation operation requires one role=observation input")
        payload = values[0][1]
        if not isinstance(payload, dict) or set(payload) != expected_keys:
            raise WorkerError("observation input shape mismatch")
        return payload

    def _observation_lifecycle(self, observation_id):
        directory = _contained_path(
            self.root, "state/observations/" + observation_id
        )
        if not os.path.isdir(directory):
            return []
        records = []
        for name in sorted(os.listdir(directory)):
            if not name.endswith(".json"):
                continue
            record, unused_raw = _load_json_file(os.path.join(directory, name))
            del unused_raw
            sequence = len(records) + 1
            expected_name = "%08d-%s.json" % (sequence, record.get("state"))
            if (
                not isinstance(record, dict)
                or set(record) != OBSERVATION_LIFECYCLE_KEYS
                or record.get("schema_version") != "1"
                or record.get("record_type") != "ObservationLifecycle"
                or record.get("sequence") != sequence
                or record.get("observation_id") != observation_id
                or record.get("state") not in OBSERVATION_STATES
                or name != expected_name
                or (
                    record.get("worker_id") is not None
                    and not isinstance(record.get("worker_id"), TEXT_TYPES)
                )
                or isinstance(record.get("sample_count"), bool)
                or not isinstance(record.get("sample_count"), INTEGER_TYPES)
                or record.get("sample_count") < 0
            ):
                raise WorkerError("observation lifecycle binding mismatch")
            _timestamp(record["created_at"])
            _validate_digest(
                record["target_fingerprint"], "observation lifecycle target"
            )
            if record["worker_id"] is not None:
                _validate_uuid(record["worker_id"], "observation lifecycle worker_id")
            if sequence == 1 and record["state"] != "running":
                raise WorkerError("observation lifecycle must begin in running state")
            if sequence > 1 and records[-1]["state"] != "running":
                raise WorkerError("observation lifecycle changed after terminal state")
            records.append(record)
        return records

    def _publish_observation_lifecycle(
        self, observation_id, target_fingerprint, state, sample_count
    ):
        if state not in OBSERVATION_STATES:
            raise WorkerError("observation lifecycle state is invalid")
        records = self._observation_lifecycle(observation_id)
        if records:
            if records[-1]["state"] != "running":
                if (
                    records[-1]["state"] == state
                    and records[-1]["target_fingerprint"] == target_fingerprint
                    and records[-1]["sample_count"] == sample_count
                ):
                    return records[-1]
                raise WorkerError("observation lifecycle is already terminal")
            if state == "running":
                raise WorkerError("observation lifecycle already has a running owner")
        elif state != "running":
            raise WorkerError("observation lifecycle cannot skip running state")
        sequence = len(records) + 1
        record = {
            "schema_version": "1",
            "record_type": "ObservationLifecycle",
            "sequence": sequence,
            "observation_id": observation_id,
            "state": state,
            "created_at": _utc_at(self._now()),
            "worker_id": self.worker_id,
            "target_fingerprint": target_fingerprint,
            "sample_count": sample_count,
        }
        path = _contained_path(
            self.root,
            "state/observations/%s/%08d-%s.json"
            % (observation_id, sequence, state),
        )
        _atomic_publish(path, _json_bytes(record))
        return record

    def recover_orphaned_observations(self):
        directory = _contained_path(self.root, "state/observations")
        if not os.path.isdir(directory):
            return 0
        recovered = 0
        for observation_id in sorted(os.listdir(directory)):
            if observation_id in self._sessions:
                continue
            try:
                _validate_uuid(observation_id, "observation_id")
            except WorkerError:
                continue
            records = self._observation_lifecycle(observation_id)
            if records and records[-1]["state"] == "running":
                last = records[-1]
                self._publish_observation_lifecycle(
                    observation_id,
                    last["target_fingerprint"],
                    "worker_lost",
                    last["sample_count"],
                )
                recovered += 1
        return recovered

    def _start_observation(self, request, deadline, context):
        payload = self._load_observation_input(
            request, set(("target_fingerprint", "observation_profile"))
        )
        _validate_digest(payload["target_fingerprint"], "observation target fingerprint")
        profile = _validate_profile(payload["observation_profile"])
        self._require_profile_allowed(profile)
        if any(
            invariant["action"] == "stop_and_rollback"
            for invariant in profile["fault_invariants"]
        ):
            raise GateBlocked(
                "standalone observation cannot authorize stop_and_rollback"
            )
        if payload["target_fingerprint"] != self.enrollment["fingerprint"]:
            raise GateBlocked("observation target is not the exact enrolled fingerprint")
        if request["job_id"] in self._sessions:
            raise GateBlocked("observation session already exists")
        application = self.target.current_application()
        self._verify_live_target(application, payload["target_fingerprint"])
        online_application = self._read_only_login(application, deadline - self._now())
        context["target_fingerprint"] = payload["target_fingerprint"]
        session = {
            "observation_id": request["job_id"],
            "target_fingerprint": payload["target_fingerprint"],
            "profile": profile,
            "online": online_application,
            "started": self._now(),
            "next_sample": self._now(),
            "state": "active",
            "sample_count": 0,
        }
        self._sessions[request["job_id"]] = session
        try:
            self._publish_observation_lifecycle(
                request["job_id"], payload["target_fingerprint"], "running", 0
            )
            sample = self._poll_one_session(session)
        except Exception:
            self._close_observation_session(session, "failed")
            raise
        return {
            "outcome": session["state"],
            "reconciliation_required": False,
            "samples": [sample] if sample is not None else [],
            "diagnostics": [],
        }

    def _poll_one_session(self, session):
        if session["state"] != "active" or self._now() < session["next_sample"]:
            return None
        if self._now() - session["started"] >= session["profile"]["duration_seconds"]:
            self._close_observation_session(session, "completed")
            return None
        sample = self._sample(
            session["observation_id"],
            session["target_fingerprint"],
            session["profile"],
            session["online"],
        )
        session["sample_count"] += 1
        session["next_sample"] = self._now() + session["profile"]["interval_ms"] / 1000.0
        if sample["violations"]:
            # A standalone observation has no deployment authority or rollback
            # artifact.  Even a stop_and_rollback invariant therefore remains
            # read-only: record, disconnect, and require a new authorized plan.
            self._close_observation_session(session, "fault_recorded_no_mutation")
        return sample

    def poll_observations(self):
        samples = []
        for observation_id in sorted(self._sessions):
            session = self._sessions[observation_id]
            try:
                sample = self._poll_one_session(session)
            except Exception:
                self._close_observation_session(session, "failed")
                raise
            if sample is not None:
                samples.append(sample)
        return samples

    def _close_observation_session(self, session, state):
        if session["state"] == "active":
            lifecycle_state = {
                "failed": "worker_lost",
                "fault_recorded_no_mutation": "fault",
            }.get(state, state)
            self._publish_observation_lifecycle(
                session["observation_id"],
                session["target_fingerprint"],
                lifecycle_state,
                session["sample_count"],
            )
            _dispose_online(session["online"])
        session["state"] = state

    def close_active_observations(self):
        for observation_id in sorted(self._sessions):
            session = self._sessions[observation_id]
            if session.get("state") == "active":
                self._close_observation_session(session, "failed")

    def _read_observation(self, request, context):
        payload = self._load_observation_input(request, set(("observation_id",)))
        _validate_uuid(payload["observation_id"], "observation_id")
        result = self.read_observation(payload["observation_id"])
        context["target_fingerprint"] = result["target_fingerprint"]
        return {
            "outcome": result["state"],
            "reconciliation_required": False,
            "samples": result["samples"],
            "diagnostics": [],
        }

    def read_observation(self, observation_id):
        _validate_uuid(observation_id, "observation_id")
        session = self._sessions.get(observation_id)
        lifecycle = self._observation_lifecycle(observation_id)
        if session is None and not lifecycle:
            raise GateBlocked("observation session evidence is missing")
        path = _contained_path(
            self.root, "observations/" + observation_id + ".jsonl"
        )
        samples = []
        if os.path.isfile(path):
            handle = open(path, "rb")
            try:
                lines = handle.read().splitlines()[-100:]
            finally:
                handle.close()
            for line in lines:
                samples.append(_load_json_bytes(line))
        return {
            "observation_id": observation_id,
            "target_fingerprint": (
                session["target_fingerprint"]
                if session is not None
                else lifecycle[-1]["target_fingerprint"]
            ),
            "state": (
                "running"
                if session is not None and session["state"] == "active"
                else (
                    {"failed": "worker_lost", "fault_recorded_no_mutation": "fault"}.get(
                        session["state"], session["state"]
                    )
                    if session is not None
                    else lifecycle[-1]["state"]
                )
            ),
            "sample_count": (
                session["sample_count"]
                if session is not None
                else lifecycle[-1]["sample_count"]
            ),
            "samples": samples,
        }

    def _stop_observation(self, request, context):
        payload = self._load_observation_input(request, set(("observation_id",)))
        _validate_uuid(payload["observation_id"], "observation_id")
        session = self._sessions.get(payload["observation_id"])
        if session is None:
            result = self.read_observation(payload["observation_id"])
            context["target_fingerprint"] = result["target_fingerprint"]
            return {
                "outcome": result["state"],
                "reconciliation_required": False,
                "samples": result["samples"],
                "diagnostics": [],
            }
        context["target_fingerprint"] = session["target_fingerprint"]
        self._close_observation_session(session, "stopped")
        return {
            "outcome": "stopped",
            "reconciliation_required": False,
            "samples": [],
            "diagnostics": [],
        }

    def _publish_result(self, request, request_digest, status, context, outcome):
        messages = list(outcome.get("diagnostics", []))
        evidence = {
            "schema_version": "1",
            "record_type": "AxDeploymentWorkerEvidence",
            "created_at": _utc_at(self._now()),
            "correlation_id": request["correlation_id"],
            "job_id": request["job_id"],
            "operation": request["operation"],
            "outcome": outcome.get("outcome", status),
            "target_fingerprint": context.get("target_fingerprint"),
            "authority_digest": context.get("authority_digest"),
            "candidate_digest": context.get("candidate_digest"),
            "rollback_digest": context.get("rollback_digest"),
            "reconciliation_required": bool(outcome.get("reconciliation_required", False)),
            "sample_count": len(outcome.get("samples", [])),
            "diagnostics": messages,
        }
        evidence_relative = "evidence/" + request["job_id"] + ".ax-deployment.json"
        evidence_path = _contained_path(self.root, evidence_relative)
        if os.path.isfile(evidence_path):
            existing_evidence, evidence_raw = _load_json_file(evidence_path)
            evidence["created_at"] = existing_evidence.get("created_at")
            if existing_evidence != evidence:
                raise WorkerError("pre-published deployment evidence mismatch")
        else:
            evidence_raw = _json_bytes(evidence)
            _atomic_publish(evidence_path, evidence_raw)
        diagnostics = []
        for message in messages:
            severity = "critical" if status == "unknown_outcome" else "error"
            diagnostics.append(_diagnostic("AX.P5." + status.upper(), message, severity))
        result = {
            "protocol": PROTOCOL,
            "message_type": "result",
            "correlation_id": request["correlation_id"],
            "job_id": request["job_id"],
            "operation": request["operation"],
            "created_at": _utc_at(self._now()),
            "status": status,
            "environment_digest": self.environment_digest,
            "workspace_digest": None,
            "request_digest": request_digest,
            "before_project_digest": None,
            "after_project_digest": None,
            "revision": None,
            "artifacts": [
                {
                    "role": "evidence",
                    "path": evidence_relative,
                    "sha256": _sha256_bytes(evidence_raw),
                }
            ],
            "diagnostics": diagnostics,
        }
        result_path = _contained_path(
            self.root, "results/" + request["job_id"] + ".result.json"
        )
        if os.path.isfile(result_path):
            unused_request, existing_result = self._validate_terminal_result_chain(
                result_path, request, _json_bytes(request)
            )
            del unused_request
            result["created_at"] = existing_result.get("created_at")
            if existing_result != result:
                raise WorkerError("pre-published deployment result mismatch")
            result = existing_result
        else:
            _atomic_publish(result_path, _json_bytes(result))
        if request["operation"] == "deployment.reconcile" and status in (
            "succeeded",
            "unknown_outcome",
        ):
            self._complete_reconciliation_transaction(request, _json_bytes(request), result_path)
        return result

    def process_request(self, request, raw=None):
        raw = _json_bytes(request) if raw is None else raw
        request_digest, deadline = self._validate_request(request, raw)
        context = {"side_effect_started": False}
        try:
            if request["operation"] == "deployment.execute":
                outcome = self._execute_deployment(
                    request, request_digest, deadline, context
                )
            elif request["operation"] == "deployment.reconcile":
                outcome = self._execute_reconciliation(request, context)
            elif request["operation"] == "target.observe.start":
                outcome = self._start_observation(request, deadline, context)
            elif request["operation"] == "target.observe.read":
                outcome = self._read_observation(request, context)
            else:
                outcome = self._stop_observation(request, context)
            if request["operation"] == "deployment.reconcile":
                status = (
                    "unknown_outcome"
                    if outcome.get("reconciliation_required") is True
                    else "succeeded"
                )
            else:
                status = (
                    "failed" if outcome.get("outcome") == "rolled_back" else "succeeded"
                )
            return self._publish_result(
                request, request_digest, status, context, outcome
            )
        except GateBlocked as exc:
            if context["side_effect_started"]:
                outcome = {
                    "outcome": "unknown_outcome",
                    "reconciliation_required": True,
                    "samples": [],
                    "diagnostics": [_safe_diagnostic(exc)],
                }
                try:
                    self._journal(
                        request,
                        "unknown-outcome",
                        {
                            "reason": _safe_diagnostic(exc),
                            "reconciliation_required": True,
                            "blind_retry_allowed": False,
                        },
                    )
                except Exception:
                    pass
                return self._publish_result(
                    request, request_digest, "unknown_outcome", context, outcome
                )
            outcome = {
                "outcome": "blocked",
                "reconciliation_required": False,
                "samples": [],
                "diagnostics": [_safe_diagnostic(exc)],
            }
            return self._publish_result(
                request, request_digest, "blocked", context, outcome
            )
        except Exception as exc:
            if context["side_effect_started"]:
                status = "unknown_outcome"
                outcome_name = "unknown_outcome"
                reconciliation = True
                try:
                    self._journal(
                        request,
                        "unknown-outcome",
                        {
                            "reason": _safe_diagnostic(exc),
                            "reconciliation_required": True,
                            "blind_retry_allowed": False,
                        },
                    )
                except Exception:
                    pass
            else:
                status = "failed"
                outcome_name = "failed_before_target_mutation"
                reconciliation = False
            outcome = {
                "outcome": outcome_name,
                "reconciliation_required": reconciliation,
                "samples": [],
                "diagnostics": [_safe_diagnostic(exc)],
            }
            return self._publish_result(
                request, request_digest, status, context, outcome
            )

    def _reconciliation_input(self, request):
        values = self._artifact_values(request)
        if len(values) != 1 or values[0][0]["role"] != "input":
            raise WorkerError("deployment.reconcile requires one role=input artifact")
        payload = values[0][1]
        if not isinstance(payload, dict) or set(payload) != RECONCILIATION_INPUT_KEYS:
            raise WorkerError("DeploymentReconcileInput shape mismatch")
        if (
            payload["schema_version"] != "1"
            or payload["record_type"] != "DeploymentReconcileInput"
        ):
            raise WorkerError("unsupported DeploymentReconcileInput")
        _validate_uuid(payload["original_job_id"], "original_job_id")
        for field in ("target_fingerprint", "artifact_digest", "rollback_digest"):
            _validate_digest(payload[field], field)
        if payload["target_fingerprint"] != self.enrollment["fingerprint"]:
            raise GateBlocked("reconciliation target is not the enrolled fingerprint")
        unused_candidate_path, candidate_record = self._require_registered_artifact(
            payload["artifact_digest"]
        )
        unused_rollback_path, rollback_record = self._require_registered_artifact(
            payload["rollback_digest"]
        )
        del unused_candidate_path, unused_rollback_path
        enriched = dict(payload)
        enriched["runtime_release_digest"] = candidate_record["runtime_release_digest"]
        enriched["rollback_runtime_release_digest"] = rollback_record[
            "runtime_release_digest"
        ]
        return enriched

    def _reconciliation_paths(self, request, payload):
        stem = payload["original_job_id"] + "." + request["job_id"]
        prepared = _contained_path(
            self.root,
            "state/reconciliation-prepared/" + stem + ".prepared.json",
        )
        terminal = _contained_path(
            self.root, "state/reconciliations/" + stem + ".json"
        )
        return prepared, terminal

    def _validate_reconciliation_record(self, record, request, payload, chain):
        if not isinstance(record, dict) or set(record) != RECONCILIATION_RECORD_KEYS:
            raise WorkerError("reconciliation record shape mismatch")
        if (
            record["schema_version"] != "1"
            or record["record_type"] != "DeploymentReconciliation"
            or record["original_job_id"] != payload["original_job_id"]
            or record["reconciliation_job_id"] != request["job_id"]
            or record["target_fingerprint"] != payload["target_fingerprint"]
            or record["artifact_digest"] != payload["artifact_digest"]
            or record["rollback_digest"] != payload["rollback_digest"]
            or record["runtime_release_digest"] != payload["runtime_release_digest"]
            or record["rollback_runtime_release_digest"]
            != payload["rollback_runtime_release_digest"]
            or record["original_result_digest"] != chain["result_digest"]
            or record["side_effect_marker_digest"]
            != chain["side_effect_marker_digest"]
            or record["provenance_head_kind"] != chain["provenance_head_kind"]
            or record["journal_head_digest"] != chain["journal_head_digest"]
        ):
            raise WorkerError("reconciliation record binding mismatch")
        _validate_uuid(record["original_job_id"], "reconciliation original_job_id")
        _validate_uuid(
            record["reconciliation_job_id"], "reconciliation reconciliation_job_id"
        )
        _timestamp(record["created_at"])
        for field in (
            "target_fingerprint",
            "artifact_digest",
            "rollback_digest",
            "runtime_release_digest",
            "rollback_runtime_release_digest",
            "original_result_digest",
            "side_effect_marker_digest",
            "journal_head_digest",
        ):
            _validate_digest(record[field], "reconciliation " + field)
        if record["outcome"] not in ("succeeded", "rolled_back", "unknown_outcome"):
            raise WorkerError("reconciliation outcome is invalid")
        if record["reconciliation_required"] is not (
            record["outcome"] == "unknown_outcome"
        ):
            raise WorkerError("reconciliation terminal-state invariant failed")
        observed = record["observed"]
        if not isinstance(observed, dict) or set(observed) != RECONCILIATION_KEYS:
            raise WorkerError("reconciliation observed-state shape mismatch")
        if observed["target_fingerprint"] is not None:
            _validate_digest(
                observed["target_fingerprint"], "observed target fingerprint"
            )
        if observed["runtime_release_digest"] is not None:
            _validate_digest(
                observed["runtime_release_digest"], "observed runtime release"
            )
        if (
            not isinstance(observed["application_state"], TEXT_TYPES)
            or not observed["application_state"]
            or not isinstance(observed["forced_values_present"], bool)
            or not isinstance(observed["physical_outputs_energized"], bool)
        ):
            raise WorkerError("reconciliation observed-state binding mismatch")
        if record["outcome"] in ("succeeded", "rolled_back"):
            expected_release = (
                payload["runtime_release_digest"]
                if record["outcome"] == "succeeded"
                else payload["rollback_runtime_release_digest"]
            )
            if (
                observed["target_fingerprint"] != payload["target_fingerprint"]
                or observed["runtime_release_digest"] != expected_release
                or not _state_is(
                    observed["application_state"], self.application_states.run, "run"
                )
                or observed["forced_values_present"] is not False
                or observed["physical_outputs_energized"] is not False
            ):
                raise WorkerError("reconciliation resolved-state semantic mismatch")
        return record

    def _complete_reconciliation_transaction(self, request, request_raw, result_path):
        """Validate result/evidence, then atomically expose a resolving record.

        The prepared record is outside the terminal namespace.  A crash before
        result publication is retryable, and a crash after result publication
        is finalized without probing or mutating the target a second time.
        """
        unused_request, result = self._validate_terminal_result_chain(
            result_path, request, request_raw
        )
        del unused_request
        payload = self._reconciliation_input(request)
        prepared_path, terminal_path = self._reconciliation_paths(request, payload)
        source_path = prepared_path if os.path.isfile(prepared_path) else terminal_path
        if not os.path.isfile(source_path):
            raise WorkerError("prepared reconciliation record is missing")
        record, record_raw = _load_json_file(source_path)
        chain = self._original_unknown_chain(payload)
        self._validate_reconciliation_record(record, request, payload, chain)
        expected_status = (
            "unknown_outcome" if record["reconciliation_required"] else "succeeded"
        )
        if result["status"] != expected_status:
            raise WorkerError("reconciliation result/record status mismatch")
        evidence_descriptors = [
            item for item in result["artifacts"] if item.get("role") == "evidence"
        ]
        if len(result["artifacts"]) != 1 or len(evidence_descriptors) != 1:
            raise WorkerError("reconciliation result evidence binding mismatch")
        evidence, evidence_raw = _load_json_file(
            _contained_path(self.root, evidence_descriptors[0]["path"])
        )
        if _sha256_bytes(evidence_raw) != evidence_descriptors[0]["sha256"]:
            raise WorkerError("reconciliation evidence digest mismatch")
        self._validate_worker_evidence(evidence, result)
        if (
            evidence["outcome"] != record["outcome"]
            or evidence["target_fingerprint"] != record["target_fingerprint"]
            or evidence["candidate_digest"] != record["artifact_digest"]
            or evidence["rollback_digest"] != record["rollback_digest"]
            or evidence["authority_digest"] is not None
            or evidence["reconciliation_required"]
            is not record["reconciliation_required"]
        ):
            raise WorkerError("reconciliation evidence semantic mismatch")
        if record["reconciliation_required"]:
            return result
        if os.path.isfile(terminal_path):
            terminal, terminal_raw = _load_json_file(terminal_path)
            self._validate_reconciliation_record(terminal, request, payload, chain)
            if terminal_raw != record_raw:
                raise WorkerError("conflicting terminal reconciliation record")
            return result
        if source_path != prepared_path:
            raise WorkerError("terminal reconciliation source invariant failed")
        _ensure_parent(terminal_path)
        os.rename(prepared_path, terminal_path)
        return result

    def _original_unknown_chain(self, payload):
        job_id = payload["original_job_id"]
        result_path = _contained_path(self.root, "results/" + job_id + ".result.json")
        unused_result, result_raw = _load_json_file(result_path)
        del unused_result
        original_request, result = self._validate_terminal_result_chain(result_path)
        if (
            not isinstance(result, dict)
            or result.get("protocol") != PROTOCOL
            or result.get("message_type") != "result"
            or result.get("job_id") != job_id
            or result.get("operation") != "deployment.execute"
            or result.get("status") != "unknown_outcome"
            or result.get("environment_digest") != self.environment_digest
        ):
            raise GateBlocked("original job is not an unresolved deployment")
        evidence_descriptors = [
            item
            for item in result.get("artifacts", [])
            if isinstance(item, dict) and item.get("role") == "evidence"
        ]
        if len(evidence_descriptors) != 1:
            raise GateBlocked("original deployment evidence is missing")
        descriptor = evidence_descriptors[0]
        evidence_path = _contained_path(self.root, descriptor.get("path"))
        evidence, evidence_raw = _load_json_file(evidence_path)
        if _sha256_bytes(evidence_raw) != descriptor.get("sha256"):
            raise GateBlocked("original deployment evidence digest mismatch")
        self._validate_worker_evidence(evidence, result)
        if (
            evidence.get("target_fingerprint") != payload["target_fingerprint"]
            or evidence.get("candidate_digest") != payload["artifact_digest"]
            or evidence.get("rollback_digest") != payload["rollback_digest"]
            or evidence.get("reconciliation_required") is not True
            or evidence.get("outcome") != "unknown_outcome"
        ):
            raise GateBlocked("original deployment evidence binding mismatch")
        marker_path = _contained_path(
            self.root, "state/side-effects/" + job_id + ".started.json"
        )
        marker, marker_raw = _load_json_file(marker_path)
        if (
            set(marker) != SIDE_EFFECT_KEYS
            or marker.get("schema_version") != "1"
            or marker.get("job_id") != job_id
            or marker.get("operation") != "deployment.execute"
            or marker.get("environment_digest") != self.environment_digest
        ):
            raise GateBlocked("original side-effect marker binding mismatch")
        _validate_uuid(marker["worker_id"], "original side-effect worker_id")
        _timestamp(marker["started_at"])
        try:
            provenance = _deployment_reconciliation_head(
                self.root,
                original_request,
                _json_bytes(original_request),
                payload["target_fingerprint"],
            )
        except WorkerError as exc:
            raise GateBlocked("original deployment provenance is invalid: " + str(exc))
        return {
            "result_digest": _sha256_bytes(result_raw),
            "side_effect_marker_digest": _sha256_bytes(marker_raw),
            "provenance_head_kind": provenance["kind"],
            "journal_head_digest": provenance["digest"],
        }

    def _execute_reconciliation(self, request, context):
        payload = self._reconciliation_input(request)
        context.update(
            {
                "target_fingerprint": payload["target_fingerprint"],
                "candidate_digest": payload["artifact_digest"],
                "rollback_digest": payload["rollback_digest"],
            }
        )
        chain = self._original_unknown_chain(payload)
        prepared_path, terminal_path = self._reconciliation_paths(request, payload)
        if os.path.isfile(terminal_path):
            raise WorkerError("reconciliation transaction is already terminal")
        if os.path.isfile(prepared_path):
            record, unused_record_raw = _load_json_file(prepared_path)
            del unused_record_raw
            self._validate_reconciliation_record(record, request, payload, chain)
            return {
                "outcome": record["outcome"],
                "reconciliation_required": record["reconciliation_required"],
                "samples": [],
                "diagnostics": (
                    []
                    if record["reconciliation_required"] is False
                    else ["target state remains unknown after read-only reconciliation"]
                ),
            }
        outcome = self.reconcile(
            payload["original_job_id"],
            payload["target_fingerprint"],
            payload["runtime_release_digest"],
            payload["rollback_runtime_release_digest"],
            chain["provenance_head_kind"],
        )
        if chain["provenance_head_kind"] == "journal":
            journal_dir = os.path.dirname(
                _contained_path(
                    self.root,
                    "state/deployments/"
                    + payload["original_job_id"]
                    + "/sentinel.tmp",
                )
            )
            entries = sorted(
                os.path.join(journal_dir, name)
                for name in os.listdir(journal_dir)
                if name.endswith(".json")
            )
            chain["journal_head_digest"] = _validate_deployment_journal(
                entries, payload["original_job_id"]
            )
        record = {
            "schema_version": "1",
            "record_type": "DeploymentReconciliation",
            "original_job_id": payload["original_job_id"],
            "reconciliation_job_id": request["job_id"],
            "created_at": _utc_at(self._now()),
            "target_fingerprint": payload["target_fingerprint"],
            "artifact_digest": payload["artifact_digest"],
            "rollback_digest": payload["rollback_digest"],
            "runtime_release_digest": payload["runtime_release_digest"],
            "rollback_runtime_release_digest": payload[
                "rollback_runtime_release_digest"
            ],
            "original_result_digest": chain["result_digest"],
            "side_effect_marker_digest": chain["side_effect_marker_digest"],
            "provenance_head_kind": chain["provenance_head_kind"],
            "journal_head_digest": chain["journal_head_digest"],
            "outcome": outcome["outcome"],
            "reconciliation_required": outcome["reconciliation_required"],
            "observed": outcome["observed"],
        }
        if set(record) != RECONCILIATION_RECORD_KEYS:
            raise WorkerError("internal reconciliation record shape mismatch")
        self._validate_reconciliation_record(record, request, payload, chain)
        _atomic_publish(prepared_path, _json_bytes(record))
        return {
            "outcome": outcome["outcome"],
            "reconciliation_required": outcome["reconciliation_required"],
            "samples": [],
            "diagnostics": (
                []
                if outcome["reconciliation_required"] is False
                else ["target state remains unknown after read-only reconciliation"]
            ),
        }

    def reconcile(
        self,
        job_id,
        target_fingerprint,
        candidate_runtime_release_digest,
        rollback_runtime_release_digest,
        provenance_head_kind="journal",
    ):
        """Read-only reconciliation; never retries download/start/rollback."""
        _validate_uuid(job_id, "job_id")
        for label, digest in (
            ("target_fingerprint", target_fingerprint),
            ("candidate_runtime_release_digest", candidate_runtime_release_digest),
            ("rollback_runtime_release_digest", rollback_runtime_release_digest),
        ):
            _validate_digest(digest, label)
        journal_dir = os.path.dirname(
            _contained_path(
                self.root, "state/deployments/" + job_id + "/sentinel.tmp"
            )
        )
        marker_path = _contained_path(
            self.root, "state/side-effects/" + job_id + ".started.json"
        )
        marker, unused_marker_raw = _load_json_file(marker_path)
        del unused_marker_raw
        if (
            set(marker) != SIDE_EFFECT_KEYS
            or marker.get("schema_version") != "1"
            or marker.get("job_id") != job_id
            or marker.get("operation") != "deployment.execute"
            or marker.get("environment_digest") != self.environment_digest
        ):
            raise GateBlocked("deployment side-effect marker is invalid")
        _validate_uuid(marker["worker_id"], "deployment side-effect worker_id")
        _timestamp(marker["started_at"])
        entries = sorted(
            os.path.join(journal_dir, name)
            for name in os.listdir(journal_dir)
            if name.endswith(".json")
        ) if os.path.isdir(journal_dir) else []
        if provenance_head_kind == "journal":
            _validate_deployment_journal(entries, job_id)
            if not any("full-download-intent" in os.path.basename(path) for path in entries):
                raise GateBlocked("deployment side-effect intent is missing")
        elif provenance_head_kind not in (
            "launch_uncertainty",
            "pre_intent_journal",
        ):
            raise GateBlocked("deployment provenance kind is invalid")
        observed = self.target.reconcile(
            candidate_runtime_release_digest, rollback_runtime_release_digest
        )
        if not isinstance(observed, dict) or set(observed) != RECONCILIATION_KEYS:
            raise GateBlocked("reconciliation probe shape mismatch")
        if (
            observed["target_fingerprint"] != target_fingerprint
            or observed["forced_values_present"] is not False
            or observed["physical_outputs_energized"] is not False
        ):
            outcome = "unknown_outcome"
        elif observed["runtime_release_digest"] == candidate_runtime_release_digest and _state_is(
            observed["application_state"], self.application_states.run, "run"
        ):
            outcome = "succeeded"
        elif observed["runtime_release_digest"] == rollback_runtime_release_digest and _state_is(
            observed["application_state"], self.application_states.run, "run"
        ):
            outcome = "rolled_back"
        else:
            outcome = "unknown_outcome"
        if provenance_head_kind == "journal":
            request = {
                "job_id": job_id,
                "correlation_id": job_id,
            }
            self._journal(
                request,
                "reconciliation-observed",
                {
                    "outcome": outcome,
                    "target_fingerprint": observed.get("target_fingerprint"),
                    "runtime_release_digest": observed.get("runtime_release_digest"),
                    "application_state": str(observed.get("application_state")),
                    "reconciliation_required": outcome == "unknown_outcome",
                    "blind_retry_allowed": False,
                },
            )
        return {
            "job_id": job_id,
            "outcome": outcome,
            "reconciliation_required": outcome == "unknown_outcome",
            "blind_retry_allowed": False,
            "observed": observed,
        }


class AxDeploymentRuntime(object):
    """Target-only queue/lease loop around :class:`AxDeploymentWorker`."""

    def __init__(
        self,
        root,
        worker,
        config,
        config_digest,
        worker_factory=None,
        system_api=None,
        process_alive=None,
        now=None,
        sleeper=None,
    ):
        self.root = os.path.abspath(root)
        self.worker = worker
        if worker is None and not callable(worker_factory):
            raise WorkerError("deployment runtime requires a worker or approved worker factory")
        if worker is not None and worker_factory is not None:
            raise WorkerError("deployment runtime worker source is ambiguous")
        self._worker_factory = worker_factory
        self.config = config
        self.config_digest = config_digest
        self.system = system_api
        self._process_alive = process_alive or _process_alive
        self._now = now or time.time
        self._sleep = sleeper or time.sleep
        self.lease_path = os.path.join(
            self.root, "state", "deployment-worker.lease.json"
        )
        self.started = self._now()
        self.last_activity = self.started
        self._next_held_observation_at = self.started
        self._next_held_heartbeat_at = self.started
        self.processed = 0
        self.heartbeat_counter = 0
        self.heartbeat_nonce = uuid.uuid4().hex
        self._heartbeat_lock = threading.Lock()
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread = None
        self._heartbeat_error = None
        for name in (
            "inbox",
            "running",
            "results",
            "archive",
            "rejected",
            "cancellations",
            "state",
        ):
            path = os.path.join(self.root, name)
            if not os.path.isdir(path):
                os.makedirs(path)

    def _archive_lease(self, label):
        directory = os.path.join(self.root, "archive", "leases")
        if not os.path.isdir(directory):
            os.makedirs(directory)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self._now()))
        base = stamp + "-deployment-" + label + "-" + self.config["worker_id"]
        target = os.path.join(directory, base + ".json")
        counter = 0
        while os.path.exists(target):
            counter += 1
            target = os.path.join(directory, base + "-%03d.json" % counter)
        os.rename(self.lease_path, target)

    def _heartbeat_age(self, lease):
        directory = os.path.join(
            self.root, "state", "deployment-heartbeats", self.config["worker_id"]
        )
        latest = None
        if os.path.isdir(directory):
            for name in os.listdir(directory):
                if not name.endswith(".json"):
                    continue
                path = os.path.join(directory, name)
                try:
                    value, _raw = _load_json_file(path)
                    if (
                        not isinstance(value, dict)
                        or set(value) != DEPLOYMENT_HEARTBEAT_KEYS
                        or value.get("protocol") != PROTOCOL
                        or value.get("worker_id") != self.config["worker_id"]
                        or value.get("pid") != lease["pid"]
                        or value.get("process_start_fingerprint")
                        != lease["process_start_fingerprint"]
                        or value.get("target_fingerprint")
                        != self.config["target_fingerprint"]
                        or value.get("config_digest") != self.config_digest
                    ):
                        continue
                    created = _timestamp(value["created_at"])
                    if created > self._now():
                        raise WorkerError("deployment heartbeat is from the future")
                    latest = created if latest is None else max(latest, created)
                except WorkerError:
                    raise
                except Exception:
                    continue
        if latest is None:
            latest = _timestamp(lease["heartbeat_at"])
            if latest > self._now():
                raise WorkerError("deployment lease heartbeat is from the future")
        return self._now() - latest

    def acquire_lease(self):
        if os.path.exists(self.lease_path):
            lease, _raw = _load_json_file(self.lease_path)
            if set(lease) != DEPLOYMENT_LEASE_KEYS or lease.get("protocol") != PROTOCOL:
                raise WorkerError("existing deployment lease is corrupt")
            if (
                isinstance(lease.get("pid"), bool)
                or not isinstance(lease.get("pid"), INTEGER_TYPES)
                or lease.get("pid") <= 0
            ):
                raise WorkerError("existing deployment lease PID is invalid")
            for field in (
                "process_start_fingerprint",
                "environment_digest",
                "target_fingerprint",
                "config_digest",
            ):
                _validate_digest(lease.get(field), "lease " + field)
            same_owner = (
                lease.get("worker_id") == self.config["worker_id"]
                and lease.get("pid") == os.getpid()
                and lease.get("process_start_fingerprint")
                == PROCESS_START_FINGERPRINT
                and lease.get("environment_digest")
                == self.config["environment_digest"]
                and lease.get("target_fingerprint")
                == self.config["target_fingerprint"]
                and lease.get("config_digest") == self.config_digest
            )
            if same_owner:
                self.write_heartbeat("adopted")
                return "adopted"
            alive = self._process_alive(lease.get("pid"))
            age = self._heartbeat_age(lease)
            if alive is True:
                raise WorkerError("another live or hung deployment worker owns the lease")
            if alive is None:
                raise WorkerError("deployment lease owner state is unknown")
            if alive is False:
                self._archive_lease("recovered")
            else:
                raise WorkerError("deployment lease cannot be recovered safely")
        lease = {
            "protocol": PROTOCOL,
            "worker_id": self.config["worker_id"],
            "pid": os.getpid(),
            "process_start_fingerprint": PROCESS_START_FINGERPRINT,
            "started_at": _utc_at(self._now()),
            "heartbeat_at": _utc_at(self._now()),
            "environment_digest": self.config["environment_digest"],
            "target_fingerprint": self.config["target_fingerprint"],
            "config_digest": self.config_digest,
            "sequence": self.processed,
        }
        _atomic_publish(self.lease_path, _json_bytes(lease))
        self.write_heartbeat("started")
        return "acquired"

    def write_heartbeat(self, state):
        with self._heartbeat_lock:
            self.heartbeat_counter += 1
            path = os.path.join(
                self.root,
                "state",
                "deployment-heartbeats",
                self.config["worker_id"],
                self.heartbeat_nonce + "-%08d.json" % self.heartbeat_counter,
            )
            _atomic_publish(
                path,
                _json_bytes(
                    {
                        "protocol": PROTOCOL,
                        "worker_id": self.config["worker_id"],
                        "pid": os.getpid(),
                        "process_start_fingerprint": PROCESS_START_FINGERPRINT,
                        "created_at": _utc_at(self._now()),
                        "state": state,
                        "processed": self.processed,
                        "target_fingerprint": self.config["target_fingerprint"],
                        "config_digest": self.config_digest,
                    }
                ),
            )

    def _heartbeat_pump(self):
        while not self._heartbeat_stop.is_set():
            self._heartbeat_stop.wait(self.config["heartbeat_seconds"])
            if self._heartbeat_stop.is_set():
                return
            try:
                self.write_heartbeat("busy")
            except BaseException as exc:
                self._heartbeat_error = exc
                return

    def _start_heartbeat_pump(self):
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_pump)
        self._heartbeat_thread.daemon = True
        self._heartbeat_thread.start()

    def _stop_heartbeat_pump(self):
        self._heartbeat_stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(self.config["heartbeat_seconds"] + 1)
            self._heartbeat_thread = None
        if self._heartbeat_error is not None:
            error = self._heartbeat_error
            self._heartbeat_error = None
            raise WorkerError("deployment heartbeat writer failed: " + str(error))

    def release_lease(self):
        if not os.path.exists(self.lease_path):
            return
        lease, _raw = _load_json_file(self.lease_path)
        if (
            lease.get("worker_id") == self.config["worker_id"]
            and lease.get("pid") == os.getpid()
            and lease.get("process_start_fingerprint") == PROCESS_START_FINGERPRINT
            and lease.get("config_digest") == self.config_digest
        ):
            self._archive_lease("released")

    def _candidate_requests(self):
        candidates = []
        inbox = os.path.join(self.root, "inbox")
        for name in os.listdir(inbox):
            if not name.endswith(".request.json"):
                continue
            path = os.path.join(inbox, name)
            try:
                request, _raw = _load_json_file(path)
            except Exception:
                continue
            if not isinstance(request, dict) or request.get("operation") not in SUPPORTED_OPERATIONS:
                continue
            try:
                order = os.path.getmtime(path)
            except Exception:
                order = 0
            candidates.append((order, name, path))
        candidates.sort(key=lambda item: (item[0], item[1]))
        return candidates

    def _registered_runtime_release(self, artifact_digest):
        _validate_digest(artifact_digest, "reconciliation artifact")
        artifact_path = _contained_path(
            self.root, "artifacts/" + artifact_digest[7:] + ".project"
        )
        if not os.path.isfile(artifact_path) or _sha256_file(artifact_path) != artifact_digest:
            raise WorkerError("reconciliation artifact is missing or corrupt")
        releases = []
        workspaces = os.path.join(self.root, "workspaces")
        for name in sorted(os.listdir(workspaces)):
            if not UUID_RE.match(name):
                continue
            manifest_path = os.path.join(workspaces, name, "manifest.json")
            if not os.path.isfile(manifest_path) or os.path.islink(manifest_path):
                continue
            manifest, _raw = _load_json_file(manifest_path)
            records = manifest.get("frozen_artifacts")
            if not isinstance(records, list):
                continue
            for record in records:
                if (
                    isinstance(record, dict)
                    and record.get("artifact_digest") == artifact_digest
                    and record.get("environment_digest") == self.config["environment_digest"]
                    and record.get("status") == "READY_FOR_DEPLOY"
                    and record.get("path")
                    == "artifacts/" + artifact_digest[7:] + ".project"
                ):
                    release = record.get("runtime_release_digest")
                    _validate_digest(release, "artifact runtime release")
                    releases.append(release)
        if not releases or len(set(releases)) != 1:
            raise WorkerError("reconciliation artifact release identity is ambiguous")
        return releases[0]

    def _terminal_reconciliation_exists(self, job_id, result_path):
        directory = os.path.join(self.root, "state", "reconciliations")
        if not os.path.isdir(directory):
            return False
        _validate_uuid(job_id, "original reconciliation job_id")
        unused_result, result_raw = _load_json_file(result_path)
        del unused_result
        original_request, result = self.worker._validate_terminal_result_chain(result_path)
        if (
            result.get("job_id") != job_id
            or result.get("operation") != "deployment.execute"
            or result.get("status") != "unknown_outcome"
            or result.get("environment_digest") != self.config["environment_digest"]
        ):
            raise WorkerError("original deployment result binding mismatch")
        result_digest = _sha256_file(result_path)
        evidence_descriptors = [
            item
            for item in result.get("artifacts", [])
            if isinstance(item, dict) and item.get("role") == "evidence"
        ]
        if len(evidence_descriptors) != 1:
            raise WorkerError("original deployment evidence descriptor is missing")
        descriptor = evidence_descriptors[0]
        evidence_path = _contained_path(self.root, descriptor.get("path"))
        evidence, evidence_raw = _load_json_file(evidence_path)
        if (
            _sha256_bytes(evidence_raw) != descriptor.get("sha256")
            or evidence.get("target_fingerprint") != self.config["target_fingerprint"]
            or evidence.get("reconciliation_required") is not True
            or evidence.get("outcome") != "unknown_outcome"
        ):
            raise WorkerError("original deployment evidence binding mismatch")
        self.worker._validate_worker_evidence(evidence, result)
        candidate_digest = evidence.get("candidate_digest")
        rollback_digest = evidence.get("rollback_digest")
        candidate_release = self._registered_runtime_release(candidate_digest)
        rollback_release = self._registered_runtime_release(rollback_digest)
        marker_path = os.path.join(
            self.root, "state", "side-effects", job_id + ".started.json"
        )
        marker, marker_raw = _load_json_file(marker_path)
        if (
            set(marker) != SIDE_EFFECT_KEYS
            or marker.get("schema_version") != "1"
            or marker.get("job_id") != job_id
            or marker.get("operation") != "deployment.execute"
            or marker.get("environment_digest") != self.config["environment_digest"]
        ):
            raise WorkerError("deployment side-effect marker binding mismatch")
        _validate_uuid(marker["worker_id"], "deployment side-effect worker_id")
        _timestamp(marker["started_at"])
        provenance_head = _deployment_reconciliation_head(
            self.root,
            original_request,
            _json_bytes(original_request),
            self.config["target_fingerprint"],
        )
        journal_head_digest = provenance_head["digest"]
        terminal = []
        prefix = job_id + "."
        for name in sorted(os.listdir(directory)):
            if not name.startswith(prefix) or not name.endswith(".json"):
                continue
            value, _raw = _load_json_file(os.path.join(directory, name))
            if set(value) != RECONCILIATION_RECORD_KEYS:
                raise WorkerError("reconciliation record shape mismatch")
            if (
                value.get("schema_version") == "1"
                and value.get("record_type") == "DeploymentReconciliation"
                and value.get("original_job_id") == job_id
                and value.get("original_result_digest") == result_digest
                and value.get("target_fingerprint")
                == self.config["target_fingerprint"]
                and value.get("outcome") in ("succeeded", "rolled_back")
                and value.get("reconciliation_required") is False
            ):
                _validate_uuid(value["reconciliation_job_id"], "reconciliation_job_id")
                _timestamp(value["created_at"])
                for field in (
                    "target_fingerprint",
                    "artifact_digest",
                    "rollback_digest",
                    "runtime_release_digest",
                    "rollback_runtime_release_digest",
                    "side_effect_marker_digest",
                    "journal_head_digest",
                ):
                    _validate_digest(value[field], "reconciliation " + field)
                observed = value["observed"]
                expected_release = (
                    candidate_release
                    if value["outcome"] == "succeeded"
                    else rollback_release
                )
                if (
                    value["artifact_digest"] != candidate_digest
                    or value["rollback_digest"] != rollback_digest
                    or value["runtime_release_digest"] != candidate_release
                    or value["rollback_runtime_release_digest"] != rollback_release
                    or value["side_effect_marker_digest"] != _sha256_bytes(marker_raw)
                    or value["provenance_head_kind"] != provenance_head["kind"]
                    or value["journal_head_digest"] != journal_head_digest
                    or not isinstance(observed, dict)
                    or set(observed) != RECONCILIATION_KEYS
                    or observed["target_fingerprint"] != value["target_fingerprint"]
                    or observed["runtime_release_digest"] != expected_release
                    or observed["application_state"] != "run"
                    or observed["forced_values_present"] is not False
                    or observed["physical_outputs_energized"] is not False
                ):
                    raise WorkerError("deployment reconciliation semantic mismatch")
                reconciliation_result_path = os.path.join(
                    self.root,
                    "results",
                    value["reconciliation_job_id"] + ".result.json",
                )
                reconciliation_request, reconciliation_result = (
                    self.worker._validate_terminal_result_chain(reconciliation_result_path)
                )
                if (
                    reconciliation_result.get("job_id")
                    != value["reconciliation_job_id"]
                    or reconciliation_result.get("operation") != "deployment.reconcile"
                    or reconciliation_result.get("status") != "succeeded"
                    or reconciliation_result.get("environment_digest")
                    != self.config["environment_digest"]
                ):
                    raise WorkerError("reconciliation result binding mismatch")
                if len(reconciliation_request["artifacts"]) != 1:
                    raise WorkerError("reconciliation request input binding mismatch")
                input_descriptor = reconciliation_request["artifacts"][0]
                if input_descriptor["role"] != "input":
                    raise WorkerError("reconciliation request input binding mismatch")
                reconciliation_input, unused_input_raw = _load_json_file(
                    _contained_path(self.root, input_descriptor["path"])
                )
                del unused_input_raw
                if reconciliation_input != {
                    "schema_version": "1",
                    "record_type": "DeploymentReconcileInput",
                    "original_job_id": job_id,
                    "target_fingerprint": value["target_fingerprint"],
                    "artifact_digest": value["artifact_digest"],
                    "rollback_digest": value["rollback_digest"],
                }:
                    raise WorkerError("reconciliation request payload binding mismatch")
                evidence_descriptors = [
                    item
                    for item in reconciliation_result["artifacts"]
                    if item["role"] == "evidence"
                ]
                if (
                    len(reconciliation_result["artifacts"]) != 1
                    or len(evidence_descriptors) != 1
                ):
                    raise WorkerError("reconciliation evidence descriptor binding mismatch")
                reconciliation_evidence, unused_evidence_raw = _load_json_file(
                    _contained_path(self.root, evidence_descriptors[0]["path"])
                )
                del unused_evidence_raw
                self.worker._validate_worker_evidence(
                    reconciliation_evidence, reconciliation_result
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
                    raise WorkerError("reconciliation evidence semantic binding mismatch")
                terminal.append(value)
        if len(terminal) > 1:
            first = _json_bytes(terminal[0])
            if any(_json_bytes(value) != first for value in terminal[1:]):
                raise WorkerError("conflicting terminal reconciliation evidence")
        return bool(terminal)

    def _unresolved_deployment_exists(self):
        results = os.path.join(self.root, "results")
        for name in os.listdir(results):
            if not name.endswith(".result.json"):
                continue
            value, _raw = _load_json_file(os.path.join(results, name))
            if (
                value.get("operation") == "deployment.execute"
                and value.get("status") == "unknown_outcome"
            ):
                if self._terminal_reconciliation_exists(
                    value.get("job_id"), os.path.join(results, name)
                ):
                    continue
                return True
        running = os.path.join(self.root, "running")
        for name in os.listdir(running):
            if not name.endswith(".request.json"):
                continue
            value, _raw = _load_json_file(os.path.join(running, name))
            if value.get("operation") != "deployment.execute":
                continue
            marker = os.path.join(
                self.root,
                "state",
                "side-effects",
                str(value.get("job_id")) + ".started.json",
            )
            if os.path.isfile(marker):
                return True
        return False

    def _finish_request_file(self, running, name, directory):
        target = os.path.join(self.root, directory, name)
        if os.path.exists(target):
            raise WorkerError("deployment request archive target already exists")
        os.rename(running, target)

    def _poll_held_transfer(self):
        if self.worker is None or not self.worker._transfer_held:
            return False
        now = self._now()
        if now < self._next_held_observation_at:
            return False
        self._next_held_observation_at = now + 30
        return self.worker.observe_held_transfer() and not self.worker._transfer_held

    def process_next(self):
        if self.worker is not None and self.worker._transfer_held:
            return False
        candidates = self._candidate_requests()
        if not candidates:
            return False
        _order, name, source = candidates[0]
        candidate, _candidate_raw = _load_json_file(source)
        if candidate.get("operation") == "target.observe.start":
            if self.worker is None:
                raise WorkerError("observation admission requires an initialized worker")
            payload = self.worker._load_observation_input(
                candidate, set(("target_fingerprint", "observation_profile"))
            )
            profile = _validate_profile(payload["observation_profile"])
            remaining_runtime = self.config["max_runtime_seconds"] - (
                self._now() - self.started
            )
            if remaining_runtime <= profile["duration_seconds"] + ROLLBACK_RESERVE_SECONDS:
                raise WorkerError(
                    "remaining worker runtime cannot cover observation duration and cleanup"
                )
        if (
            candidate.get("operation") == "deployment.execute"
            and self._unresolved_deployment_exists()
        ):
            raise WorkerError(
                "an earlier deployment outcome is unresolved; reconciliation is required"
            )
        running = os.path.join(self.root, "running", name)
        try:
            os.rename(source, running)
        except OSError:
            return False
        request = None
        try:
            request, raw = _load_json_file(running)
            if request.get("operation") not in SUPPORTED_OPERATIONS:
                raise WorkerError("deployment runtime claimed a non-target operation")
            if os.path.isfile(
                os.path.join(
                    self.root, "cancellations", request["job_id"] + ".cancel.json"
                )
            ):
                self._finish_request_file(running, name, "archive")
                self.processed += 1
                self.last_activity = self._now()
                return True
            expected = os.path.join(
                self.root, "results", request["job_id"] + ".result.json"
            )
            if os.path.isfile(expected):
                if request.get("operation") != "deployment.reconcile":
                    raise WorkerError(
                        "only reconciliation may recover a pre-published result"
                    )
                result = self.worker._complete_reconciliation_transaction(
                    request, raw, expected
                )
            else:
                result = self.worker.process_request(request, raw)
            if not isinstance(result, dict) or not os.path.isfile(expected):
                raise WorkerError("deployment worker did not publish its immutable result")
            self._finish_request_file(running, name, "archive")
        except BaseException:
            # A BaseException after a side-effect marker intentionally leaves
            # the request in running so host recovery yields UNKNOWN_OUTCOME.
            side_effect = None
            if isinstance(request, dict):
                side_effect = os.path.join(
                    self.root,
                    "state",
                    "side-effects",
                    str(request.get("job_id")) + ".started.json",
                )
            if side_effect is None or not os.path.isfile(side_effect):
                if os.path.exists(running):
                    self._finish_request_file(running, name, "rejected")
            raise
        self.processed += 1
        self.last_activity = self._now()
        return True

    def _active_observations(self):
        return any(
            session.get("state") == "active"
            for session in self.worker._sessions.values()
        )

    def run_loop(self):
        lease_state = None
        lease_acquired = False
        reason = "bounded_runtime"
        try:
            lease_state = self.acquire_lease()
            lease_acquired = True
            self._start_heartbeat_pump()
            if self.worker is None:
                self.worker = self._worker_factory()
            self.worker.recover_orphaned_observations()
            while True:
                now = self._now()
                released = self._poll_held_transfer()
                active = self._active_observations()
                held = self.worker._transfer_held
                if now - self.started >= self.config["max_runtime_seconds"] and not held and not released:
                    reason = "max_runtime"
                    break
                if self.processed >= self.config["max_requests"] and not active and not held and not released:
                    reason = "max_requests"
                    break
                if now - self.last_activity >= self.config["idle_exit_seconds"] and not active and not held and not released:
                    reason = "idle_exit"
                    break
                if not held:
                    self.worker.poll_observations()
                try:
                    processed = self.process_next()
                except BaseException:
                    if not self.worker._transfer_held:
                        raise
                    processed = False
                if self.worker._transfer_held:
                    # The exact PID remains leased even if a status/heartbeat
                    # write fails.  A stale live PID then blocks a second launch.
                    if now >= self._next_held_heartbeat_at:
                        self._next_held_heartbeat_at = now + 1
                        try:
                            self.write_heartbeat("transfer-held")
                        except Exception:
                            pass
                elif self._heartbeat_error is not None:
                    raise WorkerError("deployment heartbeat writer stopped unexpectedly")
                if not processed:
                    try:
                        if self.system is not None and hasattr(self.system, "delay"):
                            self.system.delay(1000 if self.worker._transfer_held else 100)
                        else:
                            self._sleep(1 if self.worker._transfer_held else 0.1)
                    except BaseException:
                        if not self.worker._transfer_held:
                            raise
                        time.sleep(1)
            self.write_heartbeat(reason)
            return {
                "status": "stopped",
                "reason": reason,
                "lease": lease_state,
                "processed": self.processed,
                "target_fingerprint": self.config["target_fingerprint"],
            }
        except BaseException:
            if self.worker is None or not self.worker._transfer_held:
                raise
            # No exception path may unwind into main() and call system.exit
            # while an AX transfer remains owned by this process.
            while self.worker._transfer_held:
                try:
                    self._poll_held_transfer()
                except BaseException:
                    pass
                now = self._now()
                if now >= self._next_held_heartbeat_at:
                    self._next_held_heartbeat_at = now + 1
                    try:
                        self.write_heartbeat("transfer-held")
                    except BaseException:
                        pass
                try:
                    self._sleep(1)
                except BaseException:
                    time.sleep(1)
            raise
        finally:
            primary_error_active = sys.exc_info()[0] is not None
            heartbeat_error = None
            observation_error = None
            try:
                self._stop_heartbeat_pump()
            except Exception as exc:
                heartbeat_error = exc
            if self.worker is not None and not self.worker._transfer_held:
                try:
                    self.worker.close_active_observations()
                except Exception as exc:
                    observation_error = exc
            if lease_acquired and not (self.worker is not None and self.worker._transfer_held):
                self.release_lease()
            if observation_error is not None and not primary_error_active:
                raise observation_error
            if heartbeat_error is not None and not primary_error_active:
                raise heartbeat_error


def main():
    root = os.environ.get("AXMCP_DEPLOYMENT_ROOT")
    config_path = os.environ.get("AXMCP_DEPLOYMENT_CONFIG")
    if not root or not config_path:
        print("AXMCP_DEPLOYMENT_ROOT and AXMCP_DEPLOYMENT_CONFIG are required", file=sys.stderr)
        return 2
    try:
        config, config_raw, enrollment = _load_deployment_config(root, config_path)
        expected_config_digest = os.environ.get("AXMCP_DEPLOYMENT_CONFIG_SHA256")
        _validate_digest(expected_config_digest, "AXMCP_DEPLOYMENT_CONFIG_SHA256")
        if _sha256_bytes(config_raw) != expected_config_digest:
            raise WorkerError("deployment config digest does not match launcher authority")
        _verify_loaded_script_engine(
            config["script_engine"], config["script_engine_sha256"]
        )
        context = {
            "root": os.path.abspath(root),
            "enrollment": enrollment,
            "projects": globals().get("projects"),
            "online": globals().get("online"),
            "system": globals().get("system"),
            "OnlineChangeOption": globals().get("OnlineChangeOption"),
            "ApplicationState": globals().get("ApplicationState"),
            "VersionUpdateFlags": globals().get("VersionUpdateFlags"),
            "CredentialSourceKind": globals().get("CredentialSourceKind"),
            "OperatingState": globals().get("OperatingState"),
        }
        def approved_worker_factory():
            target = _load_target_adapter(
                config["target_adapter"], config["target_adapter_sha256"], context
            )
            return AxDeploymentWorker(
                root,
                config["environment_digest"],
                enrollment,
                config["observation_expression_allowlist"],
                context["projects"],
                context["online"],
                target,
                context["OnlineChangeOption"],
                context["ApplicationState"],
                context["VersionUpdateFlags"],
                context["CredentialSourceKind"],
                worker_id=config["worker_id"],
                side_effect_callback=None,
                operating_states_api=context["OperatingState"],
                workflow_deadline_capability=config["schema_version"] == "2",
            )

        runtime = AxDeploymentRuntime(
            root,
            None,
            config,
            _sha256_bytes(config_raw),
            worker_factory=approved_worker_factory,
            system_api=context["system"],
        )
        print(json.dumps(runtime.run_loop(), sort_keys=True))
        return 0
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 3


__all__ = [
    "AxDeploymentWorker",
    "AxDeploymentRuntime",
    "GateBlocked",
    "WorkerError",
    "PROTOCOL",
    "SUPPORTED_OPERATIONS",
]


if __name__ == "__main__":
    code = main()
    try:
        if "system" in globals() and hasattr(system, "exit"):
            system.exit(code)
    except Exception:
        pass
    sys.exit(code)
