# -*- coding: utf-8 -*-
"""Bounded AXQ-FILE/2 worker for DIADesigner-AX 1.9 / CODESYS SP18.

The script is intentionally self-contained so DIADesigner can execute it with
``--runscript``.  It exposes a fixed project-only dispatch table.  There is no
eval, shell, online, force, target-discovery, or arbitrary filesystem surface.
"""
from __future__ import print_function

import calendar
import hashlib
import io
import json
import os
import re
import shutil
import sys
import time
import traceback


try:
    TEXT_TYPES = (basestring,)
except NameError:  # Python 3 qualification runtime.
    TEXT_TYPES = (str,)


_LADDER_HELPER_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "ladder_import.py"
)
ladder_import = None
LADDER_IMPORT_ERROR = "helper has not passed its byte-identity gate"


PROTOCOL = "AXQ-FILE/2"
CONFIG_VERSION = "2"
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
BUILD_COMPLETE_RE = re.compile(
    r"^Compile complete -- ([0-9]+) errors?, ([0-9]+) warnings?$", re.IGNORECASE
)

RUNTIME_MARKER_NAME = "AXMCP_RuntimeMarker"
GLOBAL_VARIABLE_LIST_TYPE_GUID = "ffbfa93a-b94d-45fc-a329-229860183b1d"

READ_OPERATIONS = (
    "environment.probe",
    "project.tree.read",
    "project.inventory.read",
    "source.read",
    "project.export.native",
    "workspace.status",
)
MUTATION_OPERATIONS = (
    "workspace.create",
    "project.build",
    "source.upsert.st",
    "ladder.networks.apply",
    "artifact.freeze",
)
SUPPORTED_OPERATIONS = READ_OPERATIONS + MUTATION_OPERATIONS
TARGET_OPERATIONS = (
    "deployment.execute",
    "deployment.reconcile",
    "target.observe.start",
    "target.observe.read",
    "target.observe.stop",
)

# No project-specific warning exemption is enabled in the universal backend.
# The mechanism is retained for independently reviewed per-release profiles;
# synthetic regression tests explicitly set these values in their module copy.
OPTIONAL_EMPTY_SLOT_BASELINE_WARNING = "device description for '<empty>' is missing"
OPTIONAL_EMPTY_SLOT_BASELINE_SHA256 = None
OPTIONAL_EMPTY_SLOT_SECOND_BASELINE_SHA256 = None
OPTIONAL_EMPTY_SLOT_BASELINE_COUNT = None

CONFIG_KEYS = set(
    (
        "protocol",
        "config_version",
        "worker_id",
        "environment_digest",
        "workspace_project",
        "initial_project_sha256",
        "ide_executable_sha256",
        "script_engine_sha256",
        "ladder_helper_sha256",
        "ide_profile",
        "allowed_operations",
        "heartbeat_seconds",
        "lease_timeout_seconds",
        "max_request_seconds",
        "max_runtime_seconds",
        "max_requests",
        "idle_exit_seconds",
    )
)
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
ARTIFACT_KEYS = set(("role", "path", "sha256"))
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
        "request_digest",
        "before_project_digest",
        "after_project_digest",
        "revision",
        "artifacts",
        "diagnostics",
    )
)
BUILD_RESULT_KEYS = set(
    (
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
    )
)
BUILD_TRANSACTION_KEYS = set(
    (
        "protocol",
        "record_type",
        "job_id",
        "correlation_id",
        "operation",
        "environment_digest",
        "workspace_digest",
        "revision",
        "runtime_release_digest",
        "request_digest",
        "snapshot_digest",
        "created_at",
    )
)
BUILD_PASS_KEYS = set(
    (
        "protocol",
        "record_type",
        "revision",
        "job_id",
        "correlation_id",
        "request_digest",
        "project_digest",
        "runtime_release_digest",
        "environment_digest",
        "created_at",
    )
)
LEASE_KEYS = set(
    (
        "protocol",
        "worker_id",
        "pid",
        "process_start_fingerprint",
        "started_at",
        "heartbeat_at",
        "environment_digest",
        "sequence",
    )
)
_PROCESS_MARKER = str(os.getpid()) + "|" + str(time.time()) + "|" + str(sys.executable)
PROCESS_START_FINGERPRINT = "sha256:" + hashlib.sha256(_PROCESS_MARKER.encode("utf-8")).hexdigest()


class WorkerError(Exception):
    """Fail-closed input, state, or vendor capability error."""


class VendorCapabilityError(WorkerError):
    """Exact OEM ScriptEngine capability is absent or failed."""


def _reject_duplicate_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WorkerError("duplicate JSON key: " + str(key))
        result[key] = value
    return result


def _reject_constant(value):
    raise WorkerError("non-finite JSON number is forbidden: " + str(value))


def _load_json(path):
    handle = open(path, "rb")
    try:
        raw = handle.read()
    finally:
        handle.close()
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
    return value, raw


def _json_bytes(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    if not isinstance(text, bytes):
        text = text.encode("utf-8")
    return text


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


def _load_ladder_helper(expected_digest):
    """Authenticate the fixed sibling helper before executing any of its code."""
    global ladder_import, LADDER_IMPORT_ERROR
    if not isinstance(expected_digest, TEXT_TYPES) or not DIGEST_RE.match(expected_digest):
        raise WorkerError("ladder_helper_sha256 is not a sha256 digest")
    if not os.path.isfile(_LADDER_HELPER_PATH):
        raise WorkerError("bounded LadderRecipe helper is missing")
    actual_digest = _sha256_file(_LADDER_HELPER_PATH)
    if actual_digest != expected_digest:
        raise WorkerError("bounded LadderRecipe helper bytes changed")
    environment_digest = os.environ.get("AXMCP_LADDER_HELPER_SHA256")
    if environment_digest is not None and environment_digest != expected_digest:
        raise WorkerError("launcher and worker LadderRecipe helper digests differ")
    try:
        try:
            import importlib.util as _importlib_util

            ladder_spec = _importlib_util.spec_from_file_location(
                "axmcp_bounded_ladder_import", _LADDER_HELPER_PATH
            )
            helper = _importlib_util.module_from_spec(ladder_spec)
            ladder_spec.loader.exec_module(helper)
        except ImportError:  # ScriptEngine's Python 2 runtime.
            import imp as _imp

            helper = _imp.load_source(
                "axmcp_bounded_ladder_import", _LADDER_HELPER_PATH
            )
    except Exception as exc:
        ladder_import = None
        LADDER_IMPORT_ERROR = str(exc)
        raise WorkerError("bounded LadderRecipe helper load failed: " + str(exc))
    ladder_import = helper
    LADDER_IMPORT_ERROR = None
    return helper


def _utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _timestamp(value):
    try:
        return calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        raise WorkerError("invalid UTC timestamp")


def _safe_stamp():
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _validate_relative_path(value):
    if not isinstance(value, TEXT_TYPES) or not value:
        raise WorkerError("path must be non-empty text")
    if value.startswith("/") or value.startswith("\\") or "\\" in value or ":" in value:
        raise WorkerError("absolute, drive, UNC, device and backslash paths are forbidden")
    if "%2e" in value.lower() or "%2f" in value.lower() or "%5c" in value.lower():
        raise WorkerError("percent-encoded traversal is forbidden")
    parts = value.split("/")
    for part in parts:
        if part in ("", ".", "..") or not PATH_SEGMENT_RE.match(part):
            raise WorkerError("invalid path segment")
    return parts


def _contained_path(root, relative):
    parts = _validate_relative_path(relative)
    root_abs = os.path.abspath(root)
    target = os.path.abspath(os.path.join(root_abs, *parts))
    prefix = root_abs.rstrip(os.sep) + os.sep
    if target == root_abs or not target.startswith(prefix):
        raise WorkerError("path escapes or aliases scratch root")
    cursor = root_abs
    for part in parts[:-1]:
        cursor = os.path.join(cursor, part)
        if os.path.lexists(cursor) and os.path.islink(cursor):
            raise WorkerError("symlinked scratch path is forbidden")
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
        raise WorkerError("final output already exists")
    _ensure_parent(path)
    partial = path + ".partial"
    if os.path.exists(partial):
        raise WorkerError("partial output already exists")
    handle = _open_binary(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        _write_all(handle, raw)
        try:
            os.fsync(handle)
        except Exception:
            pass
    finally:
        os.close(handle)
    if os.path.exists(path):
        os.remove(partial)
        raise WorkerError("final output appeared during publish")
    os.rename(partial, path)


def _atomic_replace_bytes(path, raw):
    partial = path + ".restore.partial"
    if os.path.exists(partial):
        raise WorkerError("project restore partial already exists")
    handle = _open_binary(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        _write_all(handle, raw)
        try:
            os.fsync(handle)
        except Exception:
            pass
    finally:
        os.close(handle)
    replacer = getattr(os, "replace", None)
    if replacer is not None:
        replacer(partial, path)
        return
    backup = path + ".restore.backup"
    if os.path.exists(backup):
        os.remove(partial)
        raise WorkerError("project restore backup already exists")
    try:
        from System.IO import File

        File.Replace(partial, path, backup)
        if File.Exists(backup):
            File.Delete(backup)
    except Exception as exc:
        if os.path.exists(partial):
            os.remove(partial)
        raise WorkerError("atomic project restore is unavailable: " + str(exc))


def _atomic_move(source, target):
    if os.path.exists(target):
        raise WorkerError("atomic move target already exists")
    _ensure_parent(target)
    os.rename(source, target)


def _safe_name(obj):
    try:
        return str(obj.get_name())
    except Exception:
        return str(getattr(obj, "name", "<unnamed>"))


def _is_global_variable_list(obj):
    explicit = getattr(obj, "is_global_variable_list", None)
    if explicit is not None:
        return bool(explicit)
    # ScriptEngine 4.0's real ExtendedObject does not expose the convenience
    # flag used by newer APIs. Its stable object-type GUID is the positive,
    # exact identity available in DIADesigner-AX 1.9.
    return str(getattr(obj, "type", "")).lower() == GLOBAL_VARIABLE_LIST_TYPE_GUID


def _document_text(document):
    if document is None:
        return ""
    if hasattr(document, "text"):
        return str(document.text)
    if hasattr(document, "get_text"):
        return str(document.get_text())
    return ""


def _replace_document(document, value):
    if document is None or not hasattr(document, "replace"):
        raise WorkerError("AX textual document has no supported replace method")
    document.replace(new_text=value)


def _walk_tree(obj, depth, max_depth):
    entry = {
        "name": _safe_name(obj),
        "type": str(getattr(obj, "type", "")),
        "guid": str(getattr(obj, "guid", "")),
        "is_folder": bool(getattr(obj, "is_folder", False)),
    }
    if depth < max_depth:
        try:
            children = list(obj.get_children(False))
        except Exception:
            children = []
        if children:
            entry["children"] = [_walk_tree(child, depth + 1, max_depth) for child in children]
    return entry


def _process_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False
    try:
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    except Exception:
        return None


def _validate_config(value):
    if set(value) != CONFIG_KEYS:
        raise WorkerError("worker config fields do not match AXQ-FILE/2")
    if value["protocol"] != PROTOCOL or value["config_version"] != CONFIG_VERSION:
        raise WorkerError("unsupported worker config protocol/version")
    if not UUID_RE.match(value["worker_id"]):
        raise WorkerError("worker_id must be a canonical lowercase UUID")
    for field in (
        "environment_digest",
        "initial_project_sha256",
        "ide_executable_sha256",
        "script_engine_sha256",
        "ladder_helper_sha256",
    ):
        if not DIGEST_RE.match(value[field]):
            raise WorkerError(field + " is not a sha256 digest")
    if value["initial_project_sha256"] == "sha256:" + ("0" * 64):
        raise WorkerError("initial project digest cannot be a placeholder")
    parts = _validate_relative_path(value["workspace_project"])
    if parts[0] != "workspaces" or not value["workspace_project"].endswith(".project"):
        raise WorkerError("workspace project must be a .project below workspaces/")
    if not isinstance(value["ide_profile"], TEXT_TYPES) or not value["ide_profile"]:
        raise WorkerError("ide_profile must be non-empty text")
    allowed = value["allowed_operations"]
    if not isinstance(allowed, list) or not allowed or len(set(allowed)) != len(allowed):
        raise WorkerError("allowed_operations must be a unique non-empty list")
    for operation in allowed:
        if operation not in SUPPORTED_OPERATIONS:
            raise WorkerError("unsupported operation in config: " + str(operation))
    for key in (
        "heartbeat_seconds",
        "lease_timeout_seconds",
        "max_request_seconds",
        "max_runtime_seconds",
        "max_requests",
        "idle_exit_seconds",
    ):
        if not isinstance(value[key], int) or isinstance(value[key], bool) or value[key] <= 0:
            raise WorkerError(key + " must be a positive integer")
    if value["heartbeat_seconds"] >= value["lease_timeout_seconds"]:
        raise WorkerError("heartbeat must be shorter than lease timeout")
    if value["max_request_seconds"] > value["max_runtime_seconds"]:
        raise WorkerError("request timeout cannot exceed worker runtime")


def _validate_artifacts(artifacts, root):
    if not isinstance(artifacts, list):
        raise WorkerError("artifacts must be a list")
    seen = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict) or set(artifact) != ARTIFACT_KEYS:
            raise WorkerError("artifact descriptor fields are invalid")
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
        path = _contained_path(root, artifact["path"])
        if not DIGEST_RE.match(artifact["sha256"]):
            raise WorkerError("artifact digest is invalid")
        if not os.path.isfile(path) or _sha256_file(path) != artifact["sha256"]:
            raise WorkerError("artifact missing or digest mismatch")


def _validate_operation_artifacts(operation, artifacts):
    requires_input = operation in (
        "source.read",
        "source.upsert.st",
        "ladder.networks.apply",
        "project.build",
    )
    if requires_input:
        if len(artifacts) != 1 or artifacts[0]["role"] != "input":
            raise WorkerError(operation + " requires exactly one role=input artifact")
    elif artifacts:
        raise WorkerError(operation + " accepts no input artifacts")


def _validate_request(value, raw, root, config, now=None):
    if set(value) != REQUEST_KEYS:
        raise WorkerError("request fields do not match AXQ-FILE/2")
    if value["protocol"] != PROTOCOL or value["message_type"] != "request":
        raise WorkerError("unsupported request protocol/type")
    if not UUID_RE.match(value["correlation_id"]):
        raise WorkerError("invalid correlation_id")
    if not UUID_RE.match(value["job_id"]):
        raise WorkerError("invalid job_id")
    operation = value["operation"]
    if operation not in config["allowed_operations"] or operation not in SUPPORTED_OPERATIONS:
        raise WorkerError("operation is not allowlisted")
    if value["environment_digest"] != config["environment_digest"]:
        raise WorkerError("environment digest mismatch")
    workspace_digest = value["workspace_digest"]
    if workspace_digest is not None and (
        not isinstance(workspace_digest, TEXT_TYPES) or not DIGEST_RE.match(workspace_digest)
    ):
        raise WorkerError("workspace_digest must be null or sha256")
    expected_revision = value["expected_revision"]
    if expected_revision is not None and (
        not isinstance(expected_revision, int)
        or isinstance(expected_revision, bool)
        or expected_revision < 0
    ):
        raise WorkerError("expected_revision must be null or a non-negative integer")
    if value["authorization_digest"] is not None:
        raise WorkerError("this IDE-only worker accepts no authorization artifact")
    created = _timestamp(value["created_at"])
    deadline = _timestamp(value["deadline_at"])
    current = time.time() if now is None else now
    if deadline <= created or deadline - created > config["max_request_seconds"]:
        raise WorkerError("request deadline window is invalid")
    if current > deadline:
        raise WorkerError("request is stale")
    _validate_artifacts(value["artifacts"], root)
    _validate_operation_artifacts(operation, value["artifacts"])
    return _sha256_bytes(raw)


class AxWorker(object):
    def __init__(
        self,
        root,
        projects_api=None,
        system_api=None,
        languages_api=None,
        pou_types_api=None,
        conflict_resolve_api=None,
    ):
        self.root = os.path.abspath(root)
        self.config, self.config_raw = _load_json(os.path.join(self.root, "worker-config.json"))
        _validate_config(self.config)
        _load_ladder_helper(self.config["ladder_helper_sha256"])
        self.projects = projects_api
        self.system = system_api
        self.languages = languages_api
        self.pou_types = pou_types_api
        self.conflict_resolve = conflict_resolve_api
        self.project = None
        self.started = time.time()
        self.last_activity = self.started
        self.processed = 0
        self.heartbeat_counter = self._read_heartbeat_counter()
        self.lease_path = os.path.join(self.root, "state", "worker.lease.json")
        self.last_sequence = self._read_last_sequence()
        self.revision = self._read_revision()
        self.workspace_identity_digest = _sha256_bytes(
            _json_bytes(
                {
                    "environment_digest": self.config["environment_digest"],
                    "initial_project_sha256": self.config["initial_project_sha256"],
                    "workspace_project": self.config["workspace_project"],
                    "worker_id": self.config["worker_id"],
                }
            )
        )

    def _workspace_path(self):
        return _contained_path(self.root, self.config["workspace_project"])

    def _read_last_sequence(self):
        state_dir = os.path.join(self.root, "state", "sequences")
        if not os.path.isdir(state_dir):
            return -1
        values = []
        for name in os.listdir(state_dir):
            match = re.match(r"^(\d+)\.json$", name)
            if match:
                values.append(int(match.group(1)))
        return max(values) if values else -1

    def _read_heartbeat_counter(self):
        directory = os.path.join(self.root, "state", "heartbeats", self.config["worker_id"])
        if not os.path.isdir(directory):
            return 0
        values = []
        for name in os.listdir(directory):
            match = re.match(r"^(\d+)\.json$", name)
            if match:
                values.append(int(match.group(1)))
        return max(values) if values else 0

    def _read_revision(self):
        state_dir = os.path.join(self.root, "state", "revisions")
        if not os.path.isdir(state_dir):
            return 0
        values = []
        for name in os.listdir(state_dir):
            match = re.match(r"^(\d+)\.json$", name)
            if match:
                values.append(int(match.group(1)))
        return max(values) if values else 0

    def _record_sequence(self, sequence, request, request_digest):
        path = os.path.join(self.root, "state", "sequences", str(sequence) + ".json")
        _atomic_publish(
            path,
            _json_bytes(
                {
                    "protocol": PROTOCOL,
                    "sequence": sequence,
                    "correlation_id": request["correlation_id"],
                    "job_id": request["job_id"],
                    "request_digest": request_digest,
                    "completed_at": _utc_now(),
                }
            ),
        )
        self.last_sequence = sequence

    def _record_revision(self, request, project_digest):
        next_revision = self.revision + 1
        path = os.path.join(self.root, "state", "revisions", str(next_revision) + ".json")
        _atomic_publish(
            path,
            _json_bytes(
                {
                    "protocol": PROTOCOL,
                    "revision": next_revision,
                    "operation": request["operation"],
                    "correlation_id": request["correlation_id"],
                    "job_id": request["job_id"],
                    "project_digest": project_digest,
                    "created_at": _utc_now(),
                }
            ),
        )
        self.revision = next_revision

    def _seen_identity(self, request, request_digest):
        directory = os.path.join(self.root, "state", "sequences")
        if not os.path.isdir(directory):
            return False
        for name in os.listdir(directory):
            if not name.endswith(".json"):
                continue
            try:
                record, _ = _load_json(os.path.join(directory, name))
            except Exception:
                continue
            if (
                record.get("correlation_id") == request["correlation_id"]
                or record.get("job_id") == request["job_id"]
                or record.get("request_digest") == request_digest
            ):
                return True
        return False

    def _latest_heartbeat_age(self):
        directory = os.path.join(self.root, "state", "heartbeats", self.config["worker_id"])
        if not os.path.isdir(directory):
            return None
        mtimes = [
            os.path.getmtime(os.path.join(directory, name))
            for name in os.listdir(directory)
            if name.endswith(".json")
        ]
        return time.time() - max(mtimes) if mtimes else None

    def _archive_lease(self, label):
        directory = os.path.join(self.root, "archive", "leases")
        base = _safe_stamp() + "-" + label + "-" + self.config["worker_id"]
        counter = 0
        target = os.path.join(directory, base + ".json")
        while os.path.exists(target):
            counter += 1
            target = os.path.join(directory, base + "-%03d.json" % counter)
        _atomic_move(self.lease_path, target)

    def acquire_lease(self):
        if os.path.exists(self.lease_path):
            lease, _ = _load_json(self.lease_path)
            if set(lease) != LEASE_KEYS or lease.get("protocol") != PROTOCOL:
                raise WorkerError("existing lease is corrupt")
            same_owner = (
                lease.get("worker_id") == self.config["worker_id"]
                and lease.get("pid") == os.getpid()
                and lease.get("process_start_fingerprint") == PROCESS_START_FINGERPRINT
                and lease.get("environment_digest") == self.config["environment_digest"]
            )
            if same_owner:
                self.write_heartbeat("adopted")
                return "adopted"
            alive = _process_alive(lease.get("pid"))
            age = self._latest_heartbeat_age()
            if alive is True:
                raise WorkerError("another live or hung worker owns the lease")
            if alive is None and (age is None or age <= self.config["lease_timeout_seconds"]):
                raise WorkerError("lease owner state is unknown and heartbeat is not stale")
            if alive is False or (age is not None and age > self.config["lease_timeout_seconds"]):
                self._archive_lease("recovered")
            else:
                raise WorkerError("lease cannot be recovered safely")
        lease = {
            "protocol": PROTOCOL,
            "worker_id": self.config["worker_id"],
            "pid": os.getpid(),
            "process_start_fingerprint": PROCESS_START_FINGERPRINT,
            "started_at": _utc_now(),
            "heartbeat_at": _utc_now(),
            "environment_digest": self.config["environment_digest"],
            "sequence": self.last_sequence,
        }
        _atomic_publish(self.lease_path, _json_bytes(lease))
        self.write_heartbeat("started")
        return "acquired"

    def write_heartbeat(self, state):
        self.heartbeat_counter += 1
        path = os.path.join(
            self.root,
            "state",
            "heartbeats",
            self.config["worker_id"],
            "%08d.json" % self.heartbeat_counter,
        )
        _atomic_publish(
            path,
            _json_bytes(
                {
                    "protocol": PROTOCOL,
                    "worker_id": self.config["worker_id"],
                    "pid": os.getpid(),
                    "created_at": _utc_now(),
                    "state": state,
                    "processed": self.processed,
                    "last_sequence": self.last_sequence,
                }
            ),
        )

    def release_lease(self):
        if os.path.exists(self.lease_path):
            lease, _ = _load_json(self.lease_path)
            if (
                lease.get("worker_id") == self.config["worker_id"]
                and lease.get("pid") == os.getpid()
                and lease.get("process_start_fingerprint") == PROCESS_START_FINGERPRINT
            ):
                self._archive_lease("released")

    def _primary_project(self):
        if self.project is not None:
            return self.project
        if self.projects is None:
            raise WorkerError("ScriptEngine projects API is unavailable")
        primary = getattr(self.projects, "primary", None)
        if primary is None:
            marker = os.path.join(self.root, "state", "workspace-baseline.json")
            if not os.path.isfile(marker) or not hasattr(self.projects, "open"):
                raise WorkerError("scratch project is not open")
            primary = self.projects.open(self._workspace_path(), primary=True)
        expected = os.path.normcase(os.path.abspath(self._workspace_path()))
        actual = os.path.normcase(os.path.abspath(str(getattr(primary, "path", ""))))
        if actual != expected:
            raise WorkerError("primary project is not the configured scratch project")
        self.project = primary
        return primary

    def _verify_initial_workspace(self):
        path = self._workspace_path()
        if not os.path.isfile(path):
            raise WorkerError("configured scratch project does not exist")
        marker = os.path.join(self.root, "state", "workspace-baseline.json")
        digest = _sha256_file(path)
        if not os.path.exists(marker):
            if digest != self.config["initial_project_sha256"]:
                raise WorkerError("initial scratch project digest does not match canonical baseline")
            _atomic_publish(
                marker,
                _json_bytes(
                    {
                        "protocol": PROTOCOL,
                        "workspace_project": self.config["workspace_project"],
                        "initial_project_sha256": digest,
                        "recorded_at": _utc_now(),
                    }
                ),
            )
        return digest

    def _open_project(self):
        self._verify_initial_workspace()
        if self.projects is None or not hasattr(self.projects, "open"):
            raise WorkerError("ScriptEngine projects.open is unavailable")
        primary = getattr(self.projects, "primary", None)
        if primary is not None:
            actual = os.path.normcase(os.path.abspath(str(getattr(primary, "path", ""))))
            expected = os.path.normcase(os.path.abspath(self._workspace_path()))
            if actual != expected:
                raise WorkerError("refusing to replace an unexpected primary project")
            self.project = primary
        else:
            self.project = self.projects.open(self._workspace_path(), primary=True)
        return {
            "project_path": self.config["workspace_project"],
            "project_digest": _sha256_file(self._workspace_path()),
            "project_dirty": bool(getattr(self.project, "dirty", False)),
            "real_target_contacted": False,
        }

    def _close_project(self):
        project = self._primary_project()
        if bool(getattr(project, "dirty", False)):
            raise WorkerError("refusing to close a dirty project; save explicitly first")
        project.close()
        self.project = None

    def _find_source(self, name):
        project = self._primary_project()
        matches = list(project.find(name, True))
        textual = [item for item in matches if hasattr(item, "textual_declaration")]
        if len(textual) > 1:
            raise WorkerError("source target is ambiguous")
        return textual[0] if textual else None

    def _source_payload(self, item):
        declaration = _document_text(getattr(item, "textual_declaration", None))
        implementation = _document_text(getattr(item, "textual_implementation", None))
        identity = {
            "name": _safe_name(item),
            "declaration": declaration,
            "implementation": implementation,
        }
        identity["source_digest"] = _sha256_bytes(_json_bytes(identity))
        return identity

    def _operation_payload(self, request):
        artifacts = request["artifacts"]
        if not artifacts:
            return None
        artifact = artifacts[0]
        path = _contained_path(self.root, artifact["path"])
        payload, _ = _load_json(path)
        operation = request["operation"]
        if operation == "source.read":
            if set(payload) != set(("workspace_id", "expected_revision", "target")):
                raise WorkerError("SourceReadInput fields are invalid")
            if (
                not isinstance(payload["workspace_id"], TEXT_TYPES)
                or not UUID_RE.match(payload["workspace_id"])
                or payload["workspace_id"] != self.config["worker_id"]
            ):
                raise WorkerError("SourceReadInput workspace identity is invalid")
            if (
                not isinstance(payload["expected_revision"], int)
                or isinstance(payload["expected_revision"], bool)
                or payload["expected_revision"] != request["expected_revision"]
            ):
                raise WorkerError("SourceReadInput revision is invalid")
            if not isinstance(payload["target"], TEXT_TYPES) or not NAME_RE.match(payload["target"]):
                raise WorkerError("source target is not a bounded IEC identifier")
        elif operation == "source.upsert.st":
            exact = set(
                (
                    "workspace_id",
                    "expected_revision",
                    "pou_name",
                    "pou_kind",
                    "return_type",
                    "declaration",
                    "implementation",
                )
            )
            if set(payload) != exact:
                raise WorkerError("SourceUpsertST fields are invalid")
            if not isinstance(payload["workspace_id"], TEXT_TYPES) or not UUID_RE.match(
                payload["workspace_id"]
            ):
                raise WorkerError("workspace_id must be a canonical lowercase UUID")
            if (
                not isinstance(payload["expected_revision"], int)
                or isinstance(payload["expected_revision"], bool)
            ):
                raise WorkerError("input expected_revision must be an integer")
            if payload["expected_revision"] != request["expected_revision"]:
                raise WorkerError("input expected_revision does not match request envelope")
            if not isinstance(payload["pou_name"], TEXT_TYPES) or not NAME_RE.match(
                payload["pou_name"]
            ):
                raise WorkerError("POU name is not a bounded IEC identifier")
            if payload["pou_kind"] not in (
                "program",
                "function_block",
                "function",
                "gvl",
            ):
                raise WorkerError("unsupported POU kind")
            if payload["pou_kind"] == "function":
                if not isinstance(payload["return_type"], TEXT_TYPES) or not NAME_RE.match(
                    payload["return_type"]
                ):
                    raise WorkerError("function return_type is invalid")
            elif payload["return_type"] is not None:
                raise WorkerError("return_type is only valid for functions")
            for key in ("declaration", "implementation"):
                if not isinstance(payload[key], TEXT_TYPES) or len(payload[key]) > 1024 * 1024:
                    raise WorkerError(key + " must be bounded text")
        elif operation == "project.build":
            if set(payload) != set(
                (
                    "workspace_id",
                    "expected_revision",
                    "mode",
                    "max_attempts",
                    "runtime_release_digest",
                )
            ):
                raise WorkerError("BuildRequest fields are invalid")
            if not isinstance(payload["workspace_id"], TEXT_TYPES) or not UUID_RE.match(
                payload["workspace_id"]
            ):
                raise WorkerError("workspace_id must be a canonical lowercase UUID")
            if (
                not isinstance(payload["expected_revision"], int)
                or isinstance(payload["expected_revision"], bool)
            ):
                raise WorkerError("input expected_revision must be an integer")
            if payload["expected_revision"] != request["expected_revision"]:
                raise WorkerError("input expected_revision does not match request envelope")
            if payload["mode"] not in ("build", "rebuild"):
                raise WorkerError("build mode is invalid")
            if (
                not isinstance(payload["max_attempts"], int)
                or isinstance(payload["max_attempts"], bool)
                or payload["max_attempts"] < 1
                or payload["max_attempts"] > 10
            ):
                raise WorkerError("max_attempts must be between 1 and 10")
            if (
                not isinstance(payload["runtime_release_digest"], TEXT_TYPES)
                or not DIGEST_RE.match(payload["runtime_release_digest"])
            ):
                raise WorkerError(
                    "runtime_release_digest must be a canonical sha256 digest"
                )
        elif operation == "ladder.networks.apply":
            if set(payload) != set(("workspace_id", "expected_revision", "recipe")):
                raise WorkerError("LadderApplyInput fields are invalid")
            if not isinstance(payload["workspace_id"], TEXT_TYPES) or not UUID_RE.match(
                payload["workspace_id"]
            ):
                raise WorkerError("workspace_id must be a canonical lowercase UUID")
            if (
                not isinstance(payload["expected_revision"], int)
                or isinstance(payload["expected_revision"], bool)
                or payload["expected_revision"] != request["expected_revision"]
            ):
                raise WorkerError("input expected_revision does not match request envelope")
            if ladder_import is None:
                raise VendorCapabilityError(
                    "bounded LadderRecipe helper is unavailable: "
                    + str(LADDER_IMPORT_ERROR or "unknown load failure")
                )
            try:
                payload["recipe"] = ladder_import.validate_recipe(payload["recipe"])
            except Exception as exc:
                raise WorkerError("LadderRecipe validation failed: " + str(exc))
        else:
            raise WorkerError("unexpected operation input payload")
        return payload

    def _upsert_source(self, arguments):
        project = self._primary_project()
        item = self._find_source(arguments["pou_name"])
        if item is None:
            application = getattr(project, "active_application", None)
            if application is None:
                raise VendorCapabilityError("scratch project has no active application")
            if arguments["pou_kind"] == "gvl":
                method = getattr(application, "create_gvl", None)
                if method is None:
                    raise VendorCapabilityError(
                        "installed ScriptEngine application.create_gvl capability is unavailable"
                    )
                item = method(name=arguments["pou_name"])
                change = "created"
            else:
                method = getattr(application, "create_pou", None)
                language = (
                    getattr(self.languages, "st", None)
                    if self.languages is not None
                    else None
                )
                kind_names = {
                    "program": "Program",
                    "function_block": "FunctionBlock",
                    "function": "Function",
                }
                kind = (
                    getattr(self.pou_types, kind_names[arguments["pou_kind"]], None)
                    if self.pou_types is not None
                    else None
                )
                if method is None or language is None or kind is None:
                    raise VendorCapabilityError(
                        "installed ScriptEngine create_pou/PouType/ImplementationLanguages.st "
                        "capability is unavailable"
                    )
                item = method(
                    name=arguments["pou_name"],
                    type=kind,
                    language=language,
                    return_type=(
                        arguments["return_type"]
                        if arguments["pou_kind"] == "function"
                        else None
                    ),
                    base_type=None,
                    interfaces=None,
                )
                change = "created"
        else:
            change = "updated"
        _replace_document(item.textual_declaration, arguments["declaration"])
        implementation_document = getattr(item, "textual_implementation", None)
        if arguments["pou_kind"] == "gvl":
            if arguments["implementation"]:
                raise WorkerError("GVL implementation must be empty")
        else:
            _replace_document(implementation_document, arguments["implementation"])
        payload = self._source_payload(item)
        payload["change"] = change
        return payload

    def _inventory(self):
        project = self._primary_project()
        result = {"devices": [], "libraries": [], "tasks": [], "applications": []}
        stack = list(project.get_children(False))
        while stack:
            item = stack.pop(0)
            name = _safe_name(item)
            type_text = str(getattr(item, "type", ""))
            class_text = str(type(item))
            summary = {"name": name, "type": type_text, "guid": str(getattr(item, "guid", ""))}
            combined = (type_text + " " + class_text).lower()
            if "device" in combined:
                result["devices"].append(summary)
            if "library" in combined:
                result["libraries"].append(summary)
            if "task" in combined:
                result["tasks"].append(summary)
            if bool(getattr(item, "is_application", False)):
                result["applications"].append(summary)
            try:
                stack.extend(list(item.get_children(False)))
            except Exception:
                pass
        result["real_target_contacted"] = False
        return result

    def _messages(self, strict=False):
        if self.system is None:
            if strict:
                raise VendorCapabilityError("ScriptEngine diagnostic API is unavailable")
            return []
        getter = getattr(self.system, "get_message_objects", None)
        if getter is None:
            if strict:
                raise VendorCapabilityError(
                    "ScriptEngine diagnostic read API is unavailable"
                )
            return []
        objects = []
        categories = []
        category_getter = getattr(self.system, "get_message_categories", None)
        if category_getter is not None:
            try:
                categories = list(category_getter(True))
            except Exception as exc:
                if strict:
                    raise VendorCapabilityError(
                        "ScriptEngine diagnostic category read failed: " + str(exc)
                    )
                categories = []
        if categories:
            for category in categories:
                try:
                    objects.extend((message, str(category)) for message in getter(category))
                except Exception as exc:
                    if strict:
                        raise VendorCapabilityError(
                            "ScriptEngine diagnostic read failed: " + str(exc)
                        )
        else:
            try:
                objects = [(message, None) for message in getter()]
            except Exception as exc:
                if strict:
                    raise VendorCapabilityError(
                        "ScriptEngine diagnostic read failed: " + str(exc)
                    )
                return []
        normalized = []
        for message, category in objects[:1000]:
            message_text = str(
                getattr(message, "text", getattr(message, "message", message))
            )
            severity = str(getattr(message, "severity", "unknown")).lower()
            if "fatal" in severity:
                severity = "critical"
            elif "error" in severity:
                severity = "error"
            elif "warning" in severity:
                severity = "warning"
            else:
                severity = "info"
            prefix = getattr(message, "prefix", None)
            number = getattr(message, "number", None)
            code = "AX.BUILD.MESSAGE"
            if prefix is not None or number is not None:
                code = str(prefix or "AX") + "." + str(number or "MESSAGE")
            line = int(getattr(message, "line", 0) or 0)
            column = int(getattr(message, "column", 0) or 0)
            source = getattr(message, "object", getattr(message, "source", None))
            if source is None:
                source_name = None
            elif isinstance(source, TEXT_TYPES):
                source_name = str(source) if NAME_RE.match(str(source)) else None
            else:
                candidate = _safe_name(source)
                source_name = candidate if NAME_RE.match(candidate) else None
            normalized.append(
                {
                    "severity": severity,
                    "code": code,
                    "message": message_text,
                    "source": source_name,
                    "line": line if line > 0 else None,
                    "column": column if column > 0 else None,
                }
            )
            if category is not None:
                normalized[-1]["category"] = category
        optional_candidates = [
            item
            for item in normalized
            if item["severity"] == "warning"
            and item["message"].strip().lower()
            == OPTIONAL_EMPTY_SLOT_BASELINE_WARNING
        ]
        exact_optional_slot_baseline = (
            OPTIONAL_EMPTY_SLOT_BASELINE_COUNT is not None
            and self.config["initial_project_sha256"]
            in (OPTIONAL_EMPTY_SLOT_BASELINE_SHA256, OPTIONAL_EMPTY_SLOT_SECOND_BASELINE_SHA256)
            and len(optional_candidates) == OPTIONAL_EMPTY_SLOT_BASELINE_COUNT
            and all(
                item["source"] is None
                and item["line"] is None
                and item["column"] is None
                for item in optional_candidates
            )
        )
        if exact_optional_slot_baseline:
            for item in optional_candidates:
                item["severity"] = "info"
                item["code"] = "AX.BASELINE.OPTIONAL_EMPTY_SLOT"
        return normalized

    def _verified_build_counts(self, diagnostics):
        # Vendor categories are not chronological events. Environment warnings
        # and additional analysis can precede/follow a zero-warning compiler
        # summary. Verify that summary against its own category, but return ALL
        # severities so dependency/build/freeze gates remain unchanged.
        markers = []
        for item in diagnostics:
            match = BUILD_COMPLETE_RE.match((item.get("message") or "").strip())
            if match is not None:
                markers.append(((int(match.group(1)), int(match.group(2))), item.get("category")))
        if len(markers) != 1:
            raise VendorCapabilityError(
                "current build has no unique compiler completion summary"
            )
        summary, category = markers[0]
        if any("category" in item for item in diagnostics):
            if not category or any(not item.get("category") for item in diagnostics):
                raise VendorCapabilityError("diagnostic category context is incomplete")
            compiler = [item for item in diagnostics if item["category"] == category]
        else:
            # Older workers/test doubles without category metadata retain the
            # original strict aggregate comparison; no warning is ignored.
            compiler = diagnostics
        def counts(items):
            return (
                len([item for item in items if item["severity"] in ("error", "critical")]),
                len([item for item in items if item["severity"] == "warning"]),
            )
        if summary != counts(compiler):
            raise VendorCapabilityError(
                "compiler completion counts do not match normalized diagnostics"
            )
        return counts(diagnostics)

    def _clear_messages(self, strict=False):
        if self.system is None:
            if strict:
                raise VendorCapabilityError("ScriptEngine system API is unavailable")
            return
        category_getter = getattr(self.system, "get_message_categories", None)
        clear = getattr(self.system, "clear_messages", None)
        getter = getattr(self.system, "get_message_objects", None)
        if category_getter is None or clear is None or getter is None:
            if strict:
                raise VendorCapabilityError(
                    "ScriptEngine diagnostic clear/read API is unavailable"
                )
            return
        try:
            for category in list(category_getter(False)):
                clear(category)
        except Exception as exc:
            if strict:
                raise VendorCapabilityError(
                    "ScriptEngine diagnostic clear failed: " + str(exc)
                )

    def _preflight_marker_path(self):
        return os.path.join(self.root, "state", "dependency-preflight.json")

    def _dependency_preflight_passed(self):
        path = self._preflight_marker_path()
        if not os.path.isfile(path):
            return False
        try:
            record, _ = _load_json(path)
        except Exception:
            return False
        return (
            record.get("protocol") == PROTOCOL
            and record.get("status") == "passed"
            and record.get("environment_digest") == self.config["environment_digest"]
            and record.get("workspace_identity_digest")
            == self.workspace_identity_digest
            and record.get("baseline_project_digest")
            == self.config["initial_project_sha256"]
        )

    def _ensure_dependency_preflight(self, request):
        marker_path = self._preflight_marker_path()
        if os.path.isfile(marker_path):
            record, _ = _load_json(marker_path)
            if (
                record.get("protocol") != PROTOCOL
                or record.get("environment_digest") != self.config["environment_digest"]
                or record.get("workspace_identity_digest")
                != self.workspace_identity_digest
                or record.get("baseline_project_digest")
                != self.config["initial_project_sha256"]
            ):
                raise WorkerError("dependency preflight marker identity mismatch")
            if record.get("status") == "passed":
                self._require_dependency_preflight()
                return None, "passed"
            if record.get("status") == "blocked":
                return record, "blocked"
            raise WorkerError("dependency preflight marker status is invalid")
        self._open_project()
        return self._dependency_preflight(request)

    def _require_dependency_preflight(self):
        if not self._dependency_preflight_passed():
            raise WorkerError("dependency preflight has not passed; mutation is blocked")
        project = self._primary_project()
        if bool(getattr(project, "dirty", False)):
            raise WorkerError("scratch project became dirty outside the bounded worker")
        if self.revision == 0 and (
            self._project_digest_if_present() != self.config["initial_project_sha256"]
        ):
            raise WorkerError("scratch baseline changed after dependency preflight")

    def _dependency_preflight(self, request):
        project = self._primary_project()
        project_path = self._workspace_path()
        blockers = []
        diagnostics = []
        initial_digest = self._project_digest_if_present()
        if self.revision != 0:
            blockers.append("workspace revision is not the initial revision")
        if initial_digest != self.config["initial_project_sha256"]:
            blockers.append("scratch project digest is not the locked baseline")
        if bool(getattr(project, "dirty", False)):
            blockers.append("scratch project is dirty before baseline rebuild")
        expected_root = os.path.normcase(os.path.realpath(self.root)) + os.sep
        real_project = os.path.normcase(os.path.realpath(project_path))
        if not real_project.startswith(expected_root):
            blockers.append("scratch project real path escapes the worker root")
        if blockers:
            return self._dependency_preflight_payload(
                request, initial_digest, blockers, diagnostics, False
            )

        application = getattr(project, "active_application", None)
        rebuild = getattr(application, "rebuild", None) if application is not None else None
        if rebuild is None:
            blockers.append("active application rebuild capability is unavailable")
            return self._dependency_preflight_payload(
                request, initial_digest, blockers, diagnostics, False
            )
        try:
            self._clear_messages(strict=True)
        except Exception as exc:
            blockers.append(str(exc))
            return self._dependency_preflight_payload(
                request, initial_digest, blockers, diagnostics, False
            )
        rebuild_attempted = True
        try:
            rebuild()
        except Exception as exc:
            blockers.append("baseline rebuild raised: " + str(exc))
        try:
            diagnostics = self._messages(strict=True)
            self._verified_build_counts(diagnostics)
        except Exception as exc:
            blockers.append(str(exc))
            # Keep already-read messages even when summary verification fails.
            # They are the evidence needed to diagnose the blocked preflight.
        for item in diagnostics:
            text = (item.get("message") or "").lower()
            if item.get("severity") in ("error", "critical"):
                blockers.append("baseline rebuild emitted an error: " + text)
            if item.get("severity") == "warning":
                blockers.append("baseline rebuild emitted a warning: " + text)
            if (
                "<empty>" in text
                and item.get("code") != "AX.BASELINE.OPTIONAL_EMPTY_SLOT"
            ):
                blockers.append("unresolved <Empty> device/library dependency: " + text)
            if (
                ("library" in text or "kütüphane" in text)
                and (
                    "could not open" in text
                    or "not installed" in text
                    or "has not been installed" in text
                    or "bulunamad" in text
                    or "eksik" in text
                )
            ):
                blockers.append("unresolved library dependency: " + text)
            if (
                "internal error" in text
                or "nullreference" in text
                or "object reference" in text
                or "nesne başvurusu" in text
            ):
                blockers.append("baseline rebuild internal error: " + text)
        after_digest = self._project_digest_if_present()
        if after_digest != initial_digest:
            blockers.append("baseline rebuild changed the scratch project file")
        if bool(getattr(project, "dirty", False)):
            blockers.append("baseline rebuild left the scratch project dirty")
        return self._dependency_preflight_payload(
            request, initial_digest, blockers, diagnostics, rebuild_attempted
        )

    def _dependency_preflight_payload(
        self, request, initial_digest, blockers, diagnostics, rebuild_attempted
    ):
        unique_blockers = []
        for blocker in blockers:
            if blocker not in unique_blockers:
                unique_blockers.append(blocker)
        status = "blocked" if unique_blockers else "passed"
        payload = {
            "schema_version": "1",
            "record_type": "DependencyPreflight",
            "preflight_id": request["job_id"],
            "created_at": _utc_now(),
            "status": status,
            "workspace_digest": initial_digest,
            "baseline_project_digest": initial_digest,
            "project_path": self.config["workspace_project"],
            "project_dirty": bool(getattr(self._primary_project(), "dirty", False)),
            "baseline_rebuild_attempted": rebuild_attempted,
            "blockers": unique_blockers[:100],
            "diagnostics": diagnostics,
            "environment_digest": self.config["environment_digest"],
            "real_target_contacted": False,
        }
        marker = {
            "protocol": PROTOCOL,
            "status": status,
            "environment_digest": self.config["environment_digest"],
            "workspace_identity_digest": self.workspace_identity_digest,
            "baseline_project_digest": initial_digest,
            "blockers": unique_blockers[:100],
            "created_at": payload["created_at"],
        }
        marker_path = self._preflight_marker_path()
        if os.path.exists(marker_path):
            existing, _ = _load_json(marker_path)
            stable_existing = dict(existing)
            stable_existing.pop("created_at", None)
            stable_marker = dict(marker)
            stable_marker.pop("created_at", None)
            if stable_existing != stable_marker:
                raise WorkerError("dependency preflight marker already exists and differs")
        else:
            _atomic_publish(marker_path, _json_bytes(marker))
        return payload, status

    def _runtime_marker_declaration(self, runtime_release_digest):
        if (
            not isinstance(runtime_release_digest, TEXT_TYPES)
            or not DIGEST_RE.match(runtime_release_digest)
        ):
            raise WorkerError(
                "runtime_release_digest must be a canonical sha256 digest"
            )
        return (
            "VAR_GLOBAL CONSTANT\n"
            " ReleaseDigest : STRING(71) := '"
            + runtime_release_digest
            + "';\n"
            "END_VAR"
        )

    def _install_runtime_marker(self, runtime_release_digest):
        declaration = self._runtime_marker_declaration(runtime_release_digest)
        project = self._primary_project()
        item = self._find_source(RUNTIME_MARKER_NAME)
        if item is None:
            application = getattr(project, "active_application", None)
            method = (
                getattr(application, "create_gvl", None)
                if application is not None
                else None
            )
            if method is None:
                raise VendorCapabilityError(
                    "installed ScriptEngine application.create_gvl capability is unavailable"
                )
            item = method(name=RUNTIME_MARKER_NAME)
        if not _is_global_variable_list(item):
            raise WorkerError("AXMCP_RuntimeMarker is not an exact GVL")
        _replace_document(item.textual_declaration, declaration)
        if _document_text(getattr(item, "textual_implementation", None)):
            raise WorkerError("AXMCP_RuntimeMarker GVL implementation must be empty")

        self._save_project()
        self._close_project()
        self._open_project()
        persisted = self._find_source(RUNTIME_MARKER_NAME)
        if persisted is None:
            raise WorkerError("AXMCP_RuntimeMarker disappeared after save/reopen")
        if not _is_global_variable_list(persisted):
            raise WorkerError("persisted AXMCP_RuntimeMarker is not an exact GVL")
        if _document_text(persisted.textual_declaration) != declaration:
            raise WorkerError(
                "AXMCP_RuntimeMarker release digest changed after save/reopen"
            )
        if _document_text(getattr(persisted, "textual_implementation", None)):
            raise WorkerError(
                "persisted AXMCP_RuntimeMarker GVL implementation is not empty"
            )

    def _project_snapshot(self):
        path = self._workspace_path()
        handle = open(path, "rb")
        try:
            raw = handle.read()
        finally:
            handle.close()
        return raw, _sha256_bytes(raw)

    def _build_transaction_directory(self, job_id):
        if not isinstance(job_id, TEXT_TYPES) or not UUID_RE.match(job_id):
            raise WorkerError("build transaction job_id is invalid")
        return _contained_path(
            self.root, "state/build-transactions/" + job_id
        )

    def _begin_build_transaction(
        self, request, runtime_release_digest, snapshot, snapshot_digest
    ):
        directory = self._build_transaction_directory(request["job_id"])
        if os.path.exists(directory):
            raise WorkerError("build transaction already exists")
        running_path = _contained_path(
            self.root,
            "running/" + request["correlation_id"] + ".request.json",
        )
        persisted_request, request_raw = _load_json(running_path)
        if persisted_request != request:
            raise WorkerError("running build request changed before transaction prepare")
        request_digest = _sha256_bytes(request_raw)
        if snapshot_digest != request["workspace_digest"]:
            raise WorkerError("build snapshot does not match request workspace digest")
        snapshot_path = os.path.join(directory, "snapshot.project")
        intent_path = os.path.join(directory, "intent.json")
        _atomic_publish(snapshot_path, snapshot)
        _atomic_publish(
            intent_path,
            _json_bytes(
                {
                    "protocol": PROTOCOL,
                    "record_type": "BuildTransactionIntent",
                    "job_id": request["job_id"],
                    "correlation_id": request["correlation_id"],
                    "operation": "project.build",
                    "environment_digest": request["environment_digest"],
                    "workspace_digest": request["workspace_digest"],
                    "revision": self.revision,
                    "runtime_release_digest": runtime_release_digest,
                    "request_digest": request_digest,
                    "snapshot_digest": snapshot_digest,
                    "created_at": _utc_now(),
                }
            ),
        )

    def _restore_project_snapshot(self, raw, expected_digest):
        project = self._primary_project()
        if bool(getattr(project, "dirty", False)):
            # This is a scratch-only rollback path. Persisting the failed
            # in-memory transaction first avoids an OEM save prompt during
            # close; the bytes are immediately replaced by the verified
            # pre-build snapshot below.
            self._save_project()
            if bool(getattr(project, "dirty", False)):
                raise WorkerError("failed build could not be made closable for rollback")
        self._close_project()
        _atomic_replace_bytes(self._workspace_path(), raw)
        self._open_project()
        if self._project_digest_if_present() != expected_digest:
            raise WorkerError("project byte snapshot restore digest mismatch")
        if bool(getattr(self._primary_project(), "dirty", False)):
            raise WorkerError("restored project reopened dirty")

    def _build(self, kind, request, workspace_id, runtime_release_digest):
        snapshot, snapshot_digest = self._project_snapshot()
        self._begin_build_transaction(
            request, runtime_release_digest, snapshot, snapshot_digest
        )
        method_name = kind
        try:
            self._install_runtime_marker(runtime_release_digest)
            project = self._primary_project()
            application = getattr(project, "active_application", None)
            if application is None:
                raise WorkerError("scratch project has no active application")
            method = getattr(application, method_name, None)
            if method is None:
                raise VendorCapabilityError(
                    "active application has no " + method_name + " capability"
                )
            self._clear_messages(strict=True)
            method()
            diagnostics = self._messages(strict=True)
            errors, warnings = self._verified_build_counts(diagnostics)
        except Exception as exc:
            try:
                self._restore_project_snapshot(snapshot, snapshot_digest)
            except Exception as restore_exc:
                raise WorkerError(
                    "build failed (%s); exact snapshot rollback also failed (%s)"
                    % (str(exc), str(restore_exc))
                )
            raise
        if errors or warnings:
            self._restore_project_snapshot(snapshot, snapshot_digest)
        return {
            "schema_version": "1",
            "record_type": "BuildResult",
            "build_id": request["job_id"],
            "created_at": _utc_now(),
            "workspace_id": workspace_id,
            "revision": self.revision,
            "kind": method_name,
            "status": "failed" if errors or warnings else "passed",
            "error_count": errors,
            "warning_count": warnings,
            "diagnostics": diagnostics,
            "project_digest": _sha256_file(self._workspace_path()),
            "artifact_digest": None,
            "runtime_release_digest": runtime_release_digest,
            "environment_digest": self.config["environment_digest"],
        }

    def _save_project(self):
        project = self._primary_project()
        try:
            project.save()
            return "save"
        except Exception as exc:
            raise VendorCapabilityError(
                "project.save failed; same-path save_as retry is forbidden: " + str(exc)
            )

    def _native_export(self, correlation):
        project = self._primary_project()
        relative = "outputs/" + correlation + ".native.xml"
        target = _contained_path(self.root, relative)
        if os.path.exists(target):
            raise WorkerError("native export target already exists")
        _ensure_parent(target)
        objects = list(project.get_children(False))
        method = getattr(project, "export_xml", None)
        if method is None:
            raise WorkerError("exact AX project has no export_xml capability")
        method(
            objects=objects,
            reporter=None,
            path=target,
            recursive=True,
            export_folder_structure=True,
            declarations_as_plaintext=True,
        )
        if not os.path.isfile(target):
            raise WorkerError("export_xml returned without an artifact")
        return {"role": "source", "path": relative, "sha256": _sha256_file(target)}

    def _find_exact_object(self, name):
        project = self._primary_project()
        try:
            matches = [item for item in list(project.find(name, True)) if _safe_name(item) == name]
        except Exception:
            matches = []
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise WorkerError("expected one exact project object named " + name)

        # DIADesigner-AX 1.9 imports PLCopen POUs under the active
        # application, but ScriptProject.find may not index that new object
        # immediately after a close/reopen. Walk the bounded project tree as
        # the exact-name fallback; never select a partial or ambiguous match.
        try:
            queue = [(item, 1) for item in list(project.get_children(False))]
        except Exception as exc:
            raise WorkerError("unable to enumerate the project object tree: " + str(exc))
        matches = []
        visited = 0
        while queue:
            item, depth = queue.pop(0)
            visited += 1
            if visited > 4096:
                raise WorkerError("project object tree exceeds the bounded lookup limit")
            if _safe_name(item) == name:
                matches.append(item)
            if depth < 16:
                try:
                    queue.extend((child, depth + 1) for child in list(item.get_children(False)))
                except Exception:
                    pass
        if len(matches) != 1:
            raise WorkerError("expected one exact project object named " + name)
        return matches[0]

    def _apply_ladder(self, arguments, correlation):
        if ladder_import is None:
            raise VendorCapabilityError(
                "bounded LadderRecipe helper is unavailable: "
                + str(LADDER_IMPORT_ERROR or "unknown load failure")
            )
        project = self._primary_project()
        application = getattr(project, "active_application", None)
        import_method = (
            getattr(application, "import_xml", None)
            if application is not None
            else None
        )
        conflict_replace = (
            getattr(self.conflict_resolve, "Replace", None)
            if self.conflict_resolve is not None
            else None
        )
        if import_method is None or conflict_replace is None:
            raise VendorCapabilityError(
                "installed ScriptEngine application.import_xml/ConflictResolve.Replace "
                "capability is unavailable"
            )
        try:
            xml_raw = ladder_import.render_plcopenxml(arguments["recipe"])
        except Exception as exc:
            raise WorkerError("bounded PLCopenXML render failed: " + str(exc))
        import_relative = "outputs/" + correlation + ".ladder-import.plcopen.xml"
        import_path = _contained_path(self.root, import_relative)
        _atomic_publish(import_path, xml_raw)
        try:
            xml_text = xml_raw.decode("utf-8")
            outcome = import_method(
                dataOrPath=xml_text,
                conflictResolve=conflict_replace,
                import_folder_structure=False,
            )
        except Exception as exc:
            raise VendorCapabilityError("bounded application.import_xml failed: " + str(exc))
        # ScriptEngine 4.0 returns False even when a new PLCopen POU was
        # imported successfully.  The authoritative success gate is the exact
        # save/reopen lookup plus byte-exported semantic round-trip below.
        save_method = self._save_project()
        self._close_project()
        self._open_project()
        imported = self._find_exact_object(arguments["recipe"]["pou"])
        export_relative = "outputs/" + correlation + ".ladder-roundtrip.plcopen.xml"
        export_path = _contained_path(self.root, export_relative)
        if os.path.exists(export_path):
            raise WorkerError("Ladder round-trip export target already exists")
        export_method = getattr(self._primary_project(), "export_xml", None)
        if export_method is None:
            raise VendorCapabilityError("project.export_xml capability is unavailable")
        try:
            export_method(
                objects=[imported],
                reporter=None,
                path=export_path,
                recursive=True,
                export_folder_structure=False,
                declarations_as_plaintext=True,
            )
        except Exception as exc:
            raise VendorCapabilityError("Ladder round-trip export failed: " + str(exc))
        if not os.path.isfile(export_path):
            raise VendorCapabilityError("Ladder round-trip export produced no artifact")
        try:
            export_handle = io.open(export_path, "r", encoding="utf-8")
            try:
                export_raw = export_handle.read()
            finally:
                export_handle.close()
            verified = ladder_import.verify_roundtrip(
                export_raw, arguments["recipe"]
            )
        except Exception as exc:
            raise WorkerError("Ladder semantic round-trip failed: " + str(exc))
        import_artifact = {
            "role": "source",
            "path": import_relative,
            "sha256": _sha256_file(import_path),
        }
        export_artifact = {
            "role": "source",
            "path": export_relative,
            "sha256": _sha256_file(export_path),
        }
        payload = {
            "schema_version": "1",
            "record_type": "LadderApplyResult",
            "workspace_id": arguments["workspace_id"],
            "pou": verified["pou"],
            "network_count": verified["network_count"],
            "recipe_digest": verified["recipe_digest"],
            "import_artifact": import_artifact,
            "roundtrip_artifact": export_artifact,
            "save_method": save_method,
            "reopen_verified": True,
            "semantic_roundtrip_verified": True,
            "vendor_import_return": bool(outcome),
            "real_target_contacted": False,
        }
        return payload, [import_artifact, export_artifact]

    def _freeze(self, correlation):
        self._save_project()
        source = self._workspace_path()
        relative = "outputs/" + correlation + ".project"
        target = _contained_path(self.root, relative)
        if os.path.exists(target):
            raise WorkerError("frozen project target already exists")
        _ensure_parent(target)
        shutil.copyfile(source, target)
        return {"role": "project", "path": relative, "sha256": _sha256_file(target)}

    def _diagnostic(self, severity, code, message, source=None, line=None, column=None):
        return {
            "severity": severity,
            "code": code,
            "message": message,
            "source": source,
            "line": line,
            "column": column,
        }

    def _validate_workspace_request(self, request):
        operation = request["operation"]
        if operation in ("environment.probe", "workspace.create"):
            if request["workspace_digest"] is not None or request["expected_revision"] is not None:
                raise WorkerError(operation + " requires null workspace/revision")
            return
        if request["workspace_digest"] != self._project_digest_if_present():
            raise WorkerError("workspace digest mismatch")
        if request["expected_revision"] != self.revision:
            raise WorkerError("workspace revision mismatch")

    def _load_build_transaction(self, directory):
        intent_path = os.path.join(directory, "intent.json")
        snapshot_path = os.path.join(directory, "snapshot.project")
        if not os.path.isfile(intent_path):
            return None, None
        intent, _ = _load_json(intent_path)
        if set(intent) != BUILD_TRANSACTION_KEYS:
            raise WorkerError("build transaction intent fields are invalid")
        if (
            intent["protocol"] != PROTOCOL
            or intent["record_type"] != "BuildTransactionIntent"
            or intent["operation"] != "project.build"
            or intent["environment_digest"] != self.config["environment_digest"]
            or not UUID_RE.match(str(intent["job_id"]))
            or not UUID_RE.match(str(intent["correlation_id"]))
            or os.path.basename(directory) != intent["job_id"]
            or not isinstance(intent["revision"], int)
            or isinstance(intent["revision"], bool)
            or intent["revision"] < 0
        ):
            raise WorkerError("build transaction intent identity is invalid")
        for field in (
            "workspace_digest",
            "runtime_release_digest",
            "request_digest",
            "snapshot_digest",
        ):
            if not isinstance(intent[field], TEXT_TYPES) or not DIGEST_RE.match(
                intent[field]
            ):
                raise WorkerError("build transaction " + field + " is invalid")
        _timestamp(intent["created_at"])
        if intent["workspace_digest"] != intent["snapshot_digest"]:
            raise WorkerError("build transaction workspace/snapshot mismatch")
        if not os.path.isfile(snapshot_path):
            raise WorkerError("build transaction snapshot is missing")
        if _sha256_file(snapshot_path) != intent["snapshot_digest"]:
            raise WorkerError("build transaction snapshot digest mismatch")
        return intent, snapshot_path

    def _build_pass_path(self, intent):
        return os.path.join(
            self.root,
            "state",
            "build-passes",
            str(intent["revision"]),
            intent["job_id"] + ".json",
        )

    def _valid_build_pass(self, intent):
        path = self._build_pass_path(intent)
        if not os.path.isfile(path):
            return None
        try:
            record, _ = _load_json(path)
        except Exception:
            return None
        if set(record) != BUILD_PASS_KEYS:
            return None
        if (
            record.get("protocol") != PROTOCOL
            or record.get("record_type") != "BuildPass"
            or record.get("revision") != intent["revision"]
            or record.get("job_id") != intent["job_id"]
            or record.get("correlation_id") != intent["correlation_id"]
            or record.get("request_digest") != intent["request_digest"]
            or record.get("runtime_release_digest")
            != intent["runtime_release_digest"]
            or record.get("environment_digest") != intent["environment_digest"]
            or not isinstance(record.get("project_digest"), TEXT_TYPES)
            or not DIGEST_RE.match(record["project_digest"])
        ):
            return None
        try:
            _timestamp(record["created_at"])
        except Exception:
            return None
        return record

    def _load_transaction_result(self, intent):
        path = os.path.join(self.root, "results", intent["job_id"] + ".result.json")
        if not os.path.isfile(path):
            return path, None
        try:
            result, _ = _load_json(path)
        except Exception:
            return path, None
        if set(result) != RESULT_KEYS:
            return path, None
        if (
            result.get("protocol") != PROTOCOL
            or result.get("message_type") != "result"
            or result.get("correlation_id") != intent["correlation_id"]
            or result.get("job_id") != intent["job_id"]
            or result.get("operation") != "project.build"
            or result.get("environment_digest") != intent["environment_digest"]
            or result.get("workspace_digest") != intent["workspace_digest"]
            or result.get("request_digest") != intent["request_digest"]
            or result.get("before_project_digest") != intent["snapshot_digest"]
            or result.get("revision") != intent["revision"]
        ):
            return path, None
        try:
            _timestamp(result["created_at"])
            _validate_artifacts(result["artifacts"], self.root)
        except Exception:
            return path, None
        if not isinstance(result["diagnostics"], list):
            return path, None
        return path, result

    def _valid_successful_build_result(self, intent, result, build_pass):
        if result is None or build_pass is None or result.get("status") != "succeeded":
            return False
        if result.get("after_project_digest") != build_pass["project_digest"]:
            return False
        outputs = [
            artifact
            for artifact in result["artifacts"]
            if artifact.get("role") == "output"
        ]
        expected_path = "outputs/" + intent["correlation_id"] + ".payload.json"
        if (
            len(result["artifacts"]) != 1
            or len(outputs) != 1
            or outputs[0].get("path") != expected_path
        ):
            return False
        try:
            payload, _ = _load_json(_contained_path(self.root, expected_path))
        except Exception:
            return False
        if set(payload) != BUILD_RESULT_KEYS:
            return False
        try:
            _timestamp(payload["created_at"])
        except Exception:
            return False
        return (
            payload.get("schema_version") == "1"
            and payload.get("record_type") == "BuildResult"
            and payload.get("build_id") == intent["job_id"]
            and payload.get("workspace_id") == self.config["worker_id"]
            and payload.get("revision") == intent["revision"]
            and payload.get("kind") in ("build", "rebuild")
            and payload.get("status") == "passed"
            and isinstance(payload.get("error_count"), int)
            and not isinstance(payload.get("error_count"), bool)
            and payload.get("error_count") == 0
            and isinstance(payload.get("warning_count"), int)
            and not isinstance(payload.get("warning_count"), bool)
            and payload.get("warning_count") == 0
            and isinstance(payload.get("diagnostics"), list)
            and payload.get("project_digest") == build_pass["project_digest"]
            and payload.get("artifact_digest") is None
            and payload.get("runtime_release_digest")
            == intent["runtime_release_digest"]
            and payload.get("environment_digest") == intent["environment_digest"]
            and self._project_digest_if_present() == build_pass["project_digest"]
        )

    def _valid_failed_build_result(self, intent, result):
        if result is None or result.get("status") not in ("failed", "blocked"):
            return False
        if result.get("after_project_digest") != intent["snapshot_digest"]:
            return False
        if not result["artifacts"]:
            return True
        outputs = [
            artifact
            for artifact in result["artifacts"]
            if artifact.get("role") == "output"
        ]
        expected_path = "outputs/" + intent["correlation_id"] + ".payload.json"
        if (
            len(result["artifacts"]) != 1
            or len(outputs) != 1
            or outputs[0].get("path") != expected_path
        ):
            return False
        try:
            payload, _ = _load_json(_contained_path(self.root, expected_path))
        except Exception:
            return False
        if set(payload) != BUILD_RESULT_KEYS:
            return False
        try:
            _timestamp(payload["created_at"])
        except Exception:
            return False
        return (
            payload.get("schema_version") == "1"
            and payload.get("record_type") == "BuildResult"
            and payload.get("build_id") == intent["job_id"]
            and payload.get("workspace_id") == self.config["worker_id"]
            and payload.get("revision") == intent["revision"]
            and payload.get("kind") in ("build", "rebuild")
            and payload.get("status") == "failed"
            and isinstance(payload.get("error_count"), int)
            and not isinstance(payload.get("error_count"), bool)
            and isinstance(payload.get("warning_count"), int)
            and not isinstance(payload.get("warning_count"), bool)
            and isinstance(payload.get("diagnostics"), list)
            and payload.get("project_digest") == intent["snapshot_digest"]
            and payload.get("artifact_digest") is None
            and payload.get("runtime_release_digest")
            == intent["runtime_release_digest"]
            and payload.get("environment_digest") == intent["environment_digest"]
            and (payload.get("error_count", 0) > 0 or payload.get("warning_count", 0) > 0)
        )

    def _archive_build_transaction(self, directory, intent, label, extras):
        job_id = intent["job_id"] if intent is not None else os.path.basename(directory)
        base = _contained_path(
            self.root,
            "archive/build-transactions/" + job_id + "." + label,
        )
        target = base
        counter = 0
        while os.path.exists(target):
            counter += 1
            target = base + "-%03d" % counter
        for prefix, path in extras:
            if os.path.isfile(path):
                _atomic_move(
                    path,
                    os.path.join(directory, prefix + "-" + os.path.basename(path)),
                )
        _ensure_parent(target)
        os.rename(directory, target)

    def _recover_build_transaction(self, directory):
        intent, snapshot_path = self._load_build_transaction(directory)
        if intent is None:
            self._archive_build_transaction(directory, None, "orphan-prepared", [])
            return
        result_path, result = self._load_transaction_result(intent)
        build_pass_path = self._build_pass_path(intent)
        build_pass = self._valid_build_pass(intent)
        if self._valid_successful_build_result(intent, result, build_pass):
            self._archive_build_transaction(directory, intent, "committed", [])
            return

        snapshot_handle = open(snapshot_path, "rb")
        try:
            snapshot = snapshot_handle.read()
        finally:
            snapshot_handle.close()
        if (
            self._project_digest_if_present() != intent["snapshot_digest"]
            or bool(getattr(self._primary_project(), "dirty", False))
        ):
            self._restore_project_snapshot(snapshot, intent["snapshot_digest"])
        valid_failure = self._valid_failed_build_result(intent, result)
        extras = []
        if os.path.isfile(build_pass_path):
            extras.append(("orphan-build-pass", build_pass_path))
        payload_path = _contained_path(
            self.root, "outputs/" + intent["correlation_id"] + ".payload.json"
        )
        if not valid_failure:
            if os.path.isfile(result_path):
                extras.append(("orphan-result", result_path))
            if os.path.isfile(payload_path):
                extras.append(("orphan-payload", payload_path))
        self._archive_build_transaction(
            directory,
            intent,
            "failed" if valid_failure else "recovered",
            extras,
        )

    def _recover_build_transactions(self):
        root = os.path.join(self.root, "state", "build-transactions")
        if not os.path.isdir(root):
            return
        for name in sorted(os.listdir(root)):
            path = os.path.join(root, name)
            if not os.path.isdir(path):
                raise WorkerError("unexpected build transaction state entry")
            if not UUID_RE.match(name):
                raise WorkerError("build transaction directory name is invalid")
            self._recover_build_transaction(path)

    def _record_build_pass(self, request, request_digest):
        directory = self._build_transaction_directory(request["job_id"])
        intent, _snapshot_path = self._load_build_transaction(directory)
        if intent is None or intent["request_digest"] != request_digest:
            raise WorkerError("build pass has no matching durable transaction")
        path = os.path.join(
            self.root,
            "state",
            "build-passes",
            str(self.revision),
            request["job_id"] + ".json",
        )
        _atomic_publish(
            path,
            _json_bytes(
                {
                    "protocol": PROTOCOL,
                    "record_type": "BuildPass",
                    "revision": self.revision,
                    "job_id": request["job_id"],
                    "correlation_id": request["correlation_id"],
                    "request_digest": request_digest,
                    "project_digest": self._project_digest_if_present(),
                    "runtime_release_digest": intent["runtime_release_digest"],
                    "environment_digest": request["environment_digest"],
                    "created_at": _utc_now(),
                }
            ),
        )

    def _has_build_pass(self):
        directory = os.path.join(
            self.root, "state", "build-passes", str(self.revision)
        )
        if not os.path.isdir(directory):
            return False
        project_digest = self._project_digest_if_present()
        for name in sorted(os.listdir(directory)):
            if not name.endswith(".json"):
                continue
            try:
                record, _ = _load_json(os.path.join(directory, name))
            except Exception:
                continue
            if (
                set(record) == BUILD_PASS_KEYS
                and record.get("protocol") == PROTOCOL
                and record.get("record_type") == "BuildPass"
                and record.get("revision") == self.revision
                and record.get("environment_digest")
                == self.config["environment_digest"]
                and record.get("project_digest") == project_digest
            ):
                return True
        return False

    def _execute(self, request):
        operation = request["operation"]
        input_payload = self._operation_payload(request)
        artifacts = []
        terminal_status = "succeeded"
        result_diagnostics = [
            self._diagnostic(
                "info",
                "AXQ.WORKER.COMPLETED",
                "bounded AXQ-FILE/2 operation completed",
            )
        ]
        if operation == "environment.probe":
            payload = {
                "executable": str(getattr(self.system, "executable_filename", "unknown")),
                "product_version": str(getattr(self.system, "version", "unknown")),
                "python": sys.version,
                "ide_profile": self.config["ide_profile"],
                "ide_executable_sha256": self.config["ide_executable_sha256"],
                "script_engine_sha256": self.config["script_engine_sha256"],
                "real_target_contacted": False,
            }
        elif operation == "workspace.create":
            opened = self._open_project()
            payload, preflight_status = self._dependency_preflight(request)
            terminal_status = "succeeded" if preflight_status == "passed" else "blocked"
            payload["workspace"] = opened
            payload["revision"] = self.revision
            if terminal_status == "blocked":
                result_diagnostics = [
                    self._diagnostic(
                        "error",
                        "AX.SP18.STOP_DEPENDENCY",
                        "fail-closed dependency preflight blocked project mutation",
                    )
                ]
        elif operation == "workspace.status":
            project = self._primary_project()
            payload = {
                "workspace_digest": self._project_digest_if_present(),
                "revision": self.revision,
                "project_digest": self._project_digest_if_present(),
                "dirty": bool(getattr(project, "dirty", False)),
                "real_target_contacted": False,
            }
        elif operation == "project.tree.read":
            project = self._primary_project()
            payload = {
                "tree": [_walk_tree(child, 1, 8) for child in project.get_children(False)],
                "real_target_contacted": False,
            }
        elif operation == "project.inventory.read":
            payload = self._inventory()
        elif operation == "source.read":
            self._open_project()
            item = self._find_source(input_payload["target"])
            if item is None:
                raise WorkerError("source target was not found")
            payload = self._source_payload(item)
            payload["real_target_contacted"] = False
        elif operation == "source.upsert.st":
            preflight, preflight_status = self._ensure_dependency_preflight(request)
            if preflight_status == "blocked":
                payload = {
                    "status": "blocked",
                    "reason": "dependency_preflight",
                    "preflight": preflight,
                    "real_target_contacted": False,
                }
                terminal_status = "blocked"
                result_diagnostics = [
                    self._diagnostic(
                        "error",
                        "AX.SP18.STOP_DEPENDENCY",
                        "fail-closed dependency preflight blocked source mutation",
                    )
                ]
            else:
                self._require_dependency_preflight()
                payload = self._upsert_source(input_payload)
                save_method = self._save_project()
                self._close_project()
                self._open_project()
                persisted = self._find_source(input_payload["pou_name"])
                if persisted is None:
                    raise WorkerError("ST source disappeared after save/reopen")
                persisted_payload = self._source_payload(persisted)
                if persisted_payload["source_digest"] != payload["source_digest"]:
                    raise WorkerError("ST source digest changed after save/reopen")
                payload["save_method"] = save_method
                payload["reopen_verified"] = True
            payload["real_target_contacted"] = False
        elif operation == "ladder.networks.apply":
            preflight, preflight_status = self._ensure_dependency_preflight(request)
            if preflight_status == "blocked":
                payload = {
                    "status": "blocked",
                    "reason": "dependency_preflight",
                    "preflight": preflight,
                    "real_target_contacted": False,
                }
                terminal_status = "blocked"
                result_diagnostics = [
                    self._diagnostic(
                        "error",
                        "AX.SP18.STOP_DEPENDENCY",
                        "fail-closed dependency preflight blocked Ladder mutation",
                    )
                ]
            else:
                self._require_dependency_preflight()
                payload, ladder_artifacts = self._apply_ladder(
                    input_payload, request["correlation_id"]
                )
                artifacts.extend(ladder_artifacts)
        elif operation == "project.build":
            preflight, preflight_status = self._ensure_dependency_preflight(request)
            if preflight_status == "blocked":
                payload = {
                    "status": "blocked",
                    "reason": "dependency_preflight",
                    "preflight": preflight,
                    "real_target_contacted": False,
                }
                terminal_status = "blocked"
                result_diagnostics = [
                    self._diagnostic(
                        "error",
                        "AX.SP18.STOP_DEPENDENCY",
                        "fail-closed dependency preflight blocked project build",
                    )
                ]
            else:
                self._require_dependency_preflight()
                payload = self._build(
                    input_payload["mode"],
                    request,
                    input_payload["workspace_id"],
                    input_payload["runtime_release_digest"],
                )
                if payload["status"] != "passed":
                    terminal_status = "failed"
                    result_diagnostics = payload["diagnostics"] or [
                        self._diagnostic(
                            "error", "AX.BUILD.FAILED", "build failed without diagnostics"
                        )
                    ]
        elif operation == "project.export.native":
            artifact = self._native_export(request["correlation_id"])
            payload = {"export": artifact, "real_target_contacted": False}
            artifacts.append(artifact)
        elif operation == "artifact.freeze":
            self._require_dependency_preflight()
            if not self._has_build_pass():
                raise WorkerError("artifact.freeze requires a passed build at current revision")
            artifact = self._freeze(request["correlation_id"])
            payload = {"artifact": artifact, "real_target_contacted": False}
            artifacts.append(artifact)
        else:
            raise WorkerError("unreachable operation")
        relative = "outputs/" + request["correlation_id"] + ".payload.json"
        output = _contained_path(self.root, relative)
        _atomic_publish(output, _json_bytes(payload))
        artifacts.insert(
            0, {"role": "output", "path": relative, "sha256": _sha256_file(output)}
        )
        return artifacts, terminal_status, result_diagnostics

    def _result(self, request, request_raw, status, artifacts, diagnostics, before, after):
        return {
            "protocol": PROTOCOL,
            "message_type": "result",
            "correlation_id": request["correlation_id"],
            "job_id": request["job_id"],
            "operation": request["operation"],
            "created_at": _utc_now(),
            "status": status,
            "environment_digest": request["environment_digest"],
            "workspace_digest": request["workspace_digest"],
            "request_digest": _sha256_bytes(request_raw),
            "before_project_digest": before,
            "after_project_digest": after,
            "revision": (
                self.revision if request["operation"] != "environment.probe" else None
            ),
            "artifacts": artifacts,
            "diagnostics": diagnostics,
        }

    def _project_digest_if_present(self):
        path = self._workspace_path()
        return _sha256_file(path) if os.path.isfile(path) else None

    def _archive_request(self, running_path, request, label, sequence=None):
        if sequence is None:
            sequence = self.last_sequence + 1
        target = os.path.join(
            self.root,
            "archive",
            label,
            "%012d-%s.request.json" % (sequence, request["correlation_id"]),
        )
        _atomic_move(running_path, target)

    def _candidate_requests(self):
        inbox = os.path.join(self.root, "inbox")
        if not os.path.isdir(inbox):
            return []
        candidates = []
        for name in os.listdir(inbox):
            if name.endswith(".partial"):
                continue
            if not name.endswith(".request.json"):
                continue
            path = os.path.join(inbox, name)
            try:
                request_hint, _request_raw = _load_json(path)
            except Exception:
                request_hint = None
            if (
                isinstance(request_hint, dict)
                and request_hint.get("operation") in TARGET_OPERATIONS
            ):
                # A separately leased deployment worker owns these operations.
                # Leaving the immutable request in inbox prevents the project
                # worker from quarantining or racing a target-side job.
                continue
            try:
                order = os.path.getmtime(path)
            except Exception:
                order = float("inf")
            candidates.append((order, name, path))
        candidates.sort(key=lambda item: (item[0], item[1]))
        return candidates

    def process_next(self):
        self._recover_build_transactions()
        candidates = self._candidate_requests()
        if not candidates:
            return False
        _order_hint, name, source = candidates[0]
        running = os.path.join(self.root, "running", name)
        _atomic_move(source, running)
        request = None
        request_raw = b""
        result_published = False
        before = self._project_digest_if_present()
        internal_sequence = self.last_sequence + 1
        try:
            request, request_raw = _load_json(running)
            digest = _validate_request(request, request_raw, self.root, self.config)
            self._validate_workspace_request(request)
            correlation = request["correlation_id"]
            if name != correlation + ".request.json":
                raise WorkerError("request filename/correlation mismatch")
            result_path = os.path.join(
                self.root, "results", request["job_id"] + ".result.json"
            )
            if os.path.exists(result_path):
                raise WorkerError("duplicate correlation result already exists")
            if self._seen_identity(request, digest):
                raise WorkerError("correlation, job or final request digest replay was rejected")
            artifacts, terminal_status, result_diagnostics = self._execute(request)
            after = self._project_digest_if_present()
            if terminal_status == "succeeded" and request["operation"] in (
                "source.upsert.st",
                "ladder.networks.apply",
            ):
                self._record_revision(request, after)
            if terminal_status == "succeeded" and request["operation"] == "project.build":
                self._record_build_pass(request, digest)
            result = self._result(
                request,
                request_raw,
                terminal_status,
                artifacts,
                result_diagnostics,
                before,
                after,
            )
            _atomic_publish(result_path, _json_bytes(result))
            result_published = True
            if request["operation"] == "project.build":
                self._recover_build_transactions()
            self._record_sequence(internal_sequence, request, digest)
        except Exception as exc:
            can_emit = (
                isinstance(request, dict)
                and set(request) == REQUEST_KEYS
                and UUID_RE.match(str(request.get("correlation_id", "")))
                and UUID_RE.match(str(request.get("job_id", "")))
                and request.get("operation") in SUPPORTED_OPERATIONS
                and request.get("environment_digest") == self.config["environment_digest"]
            )
            if can_emit:
                result_path = os.path.join(
                    self.root, "results", request["job_id"] + ".result.json"
                )
            else:
                result_path = None
            if result_path is not None and not os.path.exists(result_path):
                failure_status = "failed" if isinstance(exc, VendorCapabilityError) else "blocked"
                result = self._result(
                    request,
                    request_raw,
                    failure_status,
                    [],
                    [
                        self._diagnostic(
                            "error",
                            (
                                "AX.SP18.CAPABILITY"
                                if failure_status == "failed"
                                else "AXQ.REQUEST.BLOCKED"
                            ),
                            "fail-closed: " + str(exc),
                        )
                    ],
                    before,
                    self._project_digest_if_present(),
                )
                _atomic_publish(result_path, _json_bytes(result))
                result_published = True
                if request.get("operation") == "project.build":
                    self._recover_build_transactions()
                request_digest = _sha256_bytes(request_raw)
                self._record_sequence(internal_sequence, request, request_digest)
            label = "rejected"
            try:
                if result_published:
                    pass
                elif can_emit:
                    self._archive_request(running, request, label, internal_sequence)
                else:
                    quarantine = os.path.join(
                        self.root, "archive", "quarantine", _safe_stamp() + "-" + name
                    )
                    _atomic_move(running, quarantine)
            except Exception:
                quarantine = os.path.join(
                    self.root, "archive", "quarantine", _safe_stamp() + "-" + name
                )
                _atomic_move(running, quarantine)
        self.processed += 1
        self.last_activity = time.time()
        return True

    def _shutdown_requested(self):
        path = os.path.join(self.root, "control", "shutdown.request.json")
        if not os.path.isfile(path):
            return False
        value, _ = _load_json(path)
        if set(value) != set(("protocol", "worker_id", "requested_at", "reason")):
            raise WorkerError("shutdown request fields are invalid")
        if value["protocol"] != PROTOCOL or value["worker_id"] != self.config["worker_id"]:
            raise WorkerError("shutdown request identity mismatch")
        _timestamp(value["requested_at"])
        target = os.path.join(self.root, "archive", "control", _safe_stamp() + "-shutdown.json")
        _atomic_move(path, target)
        return True

    def run_loop(self):
        lease_state = self.acquire_lease()
        reason = "bounded_runtime"
        next_heartbeat = time.time() + self.config["heartbeat_seconds"]
        try:
            self._recover_build_transactions()
            while True:
                now = time.time()
                if self._shutdown_requested():
                    reason = "graceful_shutdown"
                    break
                if self.processed >= self.config["max_requests"]:
                    reason = "max_requests"
                    break
                if now - self.started >= self.config["max_runtime_seconds"]:
                    reason = "max_runtime"
                    break
                if now - self.last_activity >= self.config["idle_exit_seconds"]:
                    reason = "idle_exit"
                    break
                processed = self.process_next()
                if time.time() >= next_heartbeat:
                    self.write_heartbeat("running")
                    next_heartbeat = time.time() + self.config["heartbeat_seconds"]
                if not processed:
                    if self.system is not None and hasattr(self.system, "delay"):
                        self.system.delay(100)
                    else:
                        time.sleep(0.1)
            self.write_heartbeat(reason)
            return {
                "status": "stopped",
                "reason": reason,
                "lease": lease_state,
                "processed": self.processed,
                "last_sequence": self.last_sequence,
            }
        finally:
            self.release_lease()


def main():
    root = os.environ.get("AXMCP_WORKER_ROOT")
    if not root:
        print("AXMCP_WORKER_ROOT is required", file=sys.stderr)
        return 2
    try:
        worker = AxWorker(
            root,
            projects_api=globals().get("projects"),
            system_api=globals().get("system"),
            languages_api=globals().get("ImplementationLanguages"),
            pou_types_api=globals().get("PouType"),
            conflict_resolve_api=globals().get("ConflictResolve"),
        )
        result = worker.run_loop()
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 3


if __name__ == "__main__":
    code = main()
    try:
        if "system" in globals() and hasattr(system, "exit"):
            system.exit(code)
    except Exception:
        pass
    sys.exit(code)
