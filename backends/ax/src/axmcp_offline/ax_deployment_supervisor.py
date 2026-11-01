from __future__ import annotations

import ast
import os
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .jobs import FileJobQueue
from .protocol_v2 import (
    load_archived_result_chain_v2,
    validate_deployment_reconciliation_head_v1,
    validate_deployment_side_effect_marker_v1,
    validate_deployment_worker_evidence_v1,
)
from .security import (
    ProtocolViolation,
    atomic_write_exclusive,
    atomic_write_replace,
    canonical_json_bytes,
    parse_utc,
    sha256_file,
    strict_json_loads_v2,
    utc_now,
    validate_artifact_path,
    validate_correlation_id,
    validate_digest,
)


TARGET_OPERATIONS = (
    "deployment.execute",
    "deployment.reconcile",
    "target.observe.start",
    "target.observe.read",
    "target.observe.stop",
)

# A target adapter is executable trusted code, not a sandboxed plugin.  A real
# adapter may be enabled only by adding its independently reviewed digest to a
# release.  The current release deliberately ships no live adapter approval.
APPROVED_TARGET_ADAPTER_SHA256: frozenset[str] = frozenset()

_CONFIG_KEYS = {
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
}
_AUTONOMOUS_CONFIG_KEYS = {
    "config_version",
    "environment_digest",
    "target_allowlist",
    "deployment_authority_phrase",
    "deployment_ttl_seconds",
    "observation_expression_allowlist",
}
_TARGET_ENROLLMENT_KEYS = {
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
}
_LEASE_KEYS = {
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
}
_HEARTBEAT_KEYS = {
    "protocol",
    "worker_id",
    "pid",
    "process_start_fingerprint",
    "created_at",
    "state",
    "processed",
    "target_fingerprint",
    "config_digest",
}
_LAUNCH_KEYS = {
    "schema_version",
    "record_type",
    "job_id",
    "operation",
    "target_fingerprint",
    "worker_id",
    "pid",
    "launched_at",
    "config_digest",
    "headless",
}
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
_RECONCILIATION_OBSERVED_KEYS = {
    "target_fingerprint",
    "runtime_release_digest",
    "application_state",
    "forced_values_present",
    "physical_outputs_energized",
}
def _absolute_regular_file(value: Path | str, field: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ProtocolViolation(f"{field} must be an existing absolute non-symlink file")
    return path.resolve()


def _bounded_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ProtocolViolation(f"{field} must be between {minimum} and {maximum}")
    return value


def _pid_alive(pid: int) -> bool | None:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            still_active = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
            if not handle:
                error = ctypes.get_last_error()
                return False if error == 87 else None
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return None
                return exit_code.value == still_active
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return None
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:
        return None
    return True


def _load_strict(path: Path, label: str) -> dict[str, Any]:
    try:
        value = strict_json_loads_v2(path.read_bytes())
    except (OSError, ProtocolViolation) as exc:
        raise ProtocolViolation(f"invalid {label}") from exc
    if not isinstance(value, dict):
        raise ProtocolViolation(f"{label} must contain one JSON object")
    return value


def _validate_target_adapter_source(path: Path) -> None:
    try:
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            raise ProtocolViolation("target adapter UTF-8 BOM is forbidden")
        source = raw.decode("utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        raise ProtocolViolation("target adapter is not strict UTF-8 Python source") from exc
    adapter_kind = False
    factory = False
    forbidden_calls = {"eval", "exec", "compile", "__import__", "open"}
    forbidden_imports = {"subprocess", "socket", "ctypes", "winreg"}
    forbidden_attribute_calls = {
        "start",
        "stop",
        "reset",
        "reset_warm",
        "reset_cold",
        "reset_origin",
        "create_boot_application",
        "write_prepared_values",
        "force_prepared_values",
        "unforce_all_values",
        "set_prepared_value",
        "set_unforce_value",
        "perform_network_scan",
        "find_address_by_ip",
        "set_gateway_and_address",
        "set_gateway_and_device_name",
        "set_gateway_and_ip_address",
    }
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [item.name.split(".", 1)[0] for item in node.names]
                if isinstance(node, ast.Import)
                else [(node.module or "").split(".", 1)[0]]
            )
            if any(name in forbidden_imports for name in names):
                raise ProtocolViolation("target adapter imports a forbidden capability")
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else None
            if name in forbidden_calls:
                raise ProtocolViolation("target adapter calls a forbidden dynamic capability")
            if isinstance(node.func, ast.Attribute) and node.func.attr in forbidden_attribute_calls:
                raise ProtocolViolation("target adapter calls a forbidden target mutation capability")
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if any(isinstance(item, ast.Name) and item.id == "ADAPTER_KIND" for item in targets):
                adapter_kind = (
                    isinstance(value, ast.Constant)
                    and value.value == "DIADESIGNER_AX308_SP18_TARGET/1"
                )
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "create_target_api":
            factory = (
                len(node.args.args) == 1
                and node.args.vararg is None
                and node.args.kwarg is None
            )
    if not adapter_kind or not factory:
        raise ProtocolViolation(
            "target adapter must declare the frozen kind and one-argument create_target_api"
        )


def _validate_enrollment(value: dict[str, Any]) -> str:
    if set(value) != _TARGET_ENROLLMENT_KEYS:
        raise ProtocolViolation("target enrollment shape mismatch")
    if (
        value["schema_version"] != "1"
        or value["record_type"] != "TargetEnrollment"
        or value["status"] != "enrolled"
        or value["model"] != "AX-308"
        or value["forced_values_present"] is not False
        or value["physical_outputs_energized"] is not False
    ):
        raise ProtocolViolation("target enrollment is not an isolated enrolled AX-308")
    parse_utc(value["captured_at"])
    for field in (
        "device_identity",
        "runtime_version",
        "firmware_version",
        "application_identity",
    ):
        if not isinstance(value[field], str) or not value[field]:
            raise ProtocolViolation(f"target enrollment {field} must be non-empty text")
    for field in ("gateway_route_digest", "isolation_evidence_digest", "fingerprint"):
        validate_digest(value[field])
    return value["fingerprint"]


def _validate_autonomous_config(value: dict[str, Any]) -> tuple[str, list[str], set[str]]:
    if set(value) != _AUTONOMOUS_CONFIG_KEYS or value["config_version"] != "2":
        raise ProtocolViolation("autonomous config shape or version mismatch")
    environment_digest = validate_digest(value["environment_digest"])
    targets = value["target_allowlist"]
    if not isinstance(targets, list) or not targets:
        raise ProtocolViolation("deployment launcher requires a non-empty target allowlist")
    fingerprints: set[str] = set()
    for item in targets:
        if not isinstance(item, dict) or set(item) != {"label", "fingerprint"}:
            raise ProtocolViolation("target allowlist entry shape mismatch")
        if not isinstance(item["label"], str) or not item["label"]:
            raise ProtocolViolation("target allowlist label must be non-empty")
        fingerprint = validate_digest(item["fingerprint"])
        if fingerprint in fingerprints:
            raise ProtocolViolation("target allowlist contains a duplicate fingerprint")
        fingerprints.add(fingerprint)
    expressions = value["observation_expression_allowlist"]
    if (
        not isinstance(expressions, list)
        or not expressions
        or len(expressions) > 64
        or len(expressions) != len(set(expressions))
        or any(not isinstance(item, str) or not item for item in expressions)
    ):
        raise ProtocolViolation("observation expression allowlist is invalid")
    if value["deployment_authority_phrase"] != "yükle":
        raise ProtocolViolation("deployment authority phrase must remain exact 'yükle'")
    return environment_digest, expressions, fingerprints


def write_deployment_launcher_config(
    root: Path,
    *,
    ide_executable: Path,
    ide_executable_sha256: str,
    script_engine: Path,
    script_engine_sha256: str,
    ide_profile: str,
    worker_script: Path,
    worker_script_sha256: str,
    target_adapter: Path,
    target_adapter_sha256: str,
    target_enrollment: Path,
    target_enrollment_sha256: str,
    replace: bool = False,
    transport_profile: str = "lan",
) -> Path:
    """Pin the target-side launcher without starting an IDE or contacting a PLC."""

    root = root.resolve()
    v2 = root / "v2"
    if not root.is_dir() or not v2.is_dir():
        raise ProtocolViolation("autonomous scratch root is not initialized")
    executable = _absolute_regular_file(ide_executable, "ide_executable")
    engine = _absolute_regular_file(script_engine, "script_engine")
    worker = _absolute_regular_file(worker_script, "worker_script")
    adapter = _absolute_regular_file(target_adapter, "target_adapter")
    enrollment_path = _absolute_regular_file(target_enrollment, "target_enrollment")
    try:
        enrollment_path.relative_to(v2)
    except ValueError as exc:
        raise ProtocolViolation("target enrollment must be contained by the autonomous v2 root") from exc
    if enrollment_path.name != "target-enrollment.json":
        raise ProtocolViolation("target enrollment must use the fixed target-enrollment.json name")
    autonomous_path = _absolute_regular_file(root / "autonomous-config.json", "autonomous_config")
    for expected, file_path, field in (
        (ide_executable_sha256, executable, "ide_executable_sha256"),
        (script_engine_sha256, engine, "script_engine_sha256"),
        (worker_script_sha256, worker, "worker_script_sha256"),
        (target_adapter_sha256, adapter, "target_adapter_sha256"),
        (target_enrollment_sha256, enrollment_path, "target_enrollment_sha256"),
    ):
        validate_digest(expected)
        if sha256_file(file_path) != expected:
            raise ProtocolViolation(f"{field} does not match file bytes")
    if ide_profile != "DIADesigner-AX 1.9":
        raise ProtocolViolation("only the frozen DIADesigner-AX 1.9 profile is supported")
    if transport_profile not in {"lan", "diacloud"}:
        raise ProtocolViolation("unsupported transport_profile")
    if transport_profile == "diacloud" and "WORKFLOW_DEADLINE_CAPABILITY = 1" not in worker.read_text(encoding="utf-8"):
        raise ProtocolViolation("deployment worker lacks workflow deadline capability")
    if target_adapter_sha256 not in APPROVED_TARGET_ADAPTER_SHA256:
        raise ProtocolViolation(
            "target adapter digest is not approved by this AXMCP release"
        )
    _validate_target_adapter_source(adapter)
    autonomous = _load_strict(autonomous_path, "autonomous-config.json")
    environment_digest, expressions, allowed_targets = _validate_autonomous_config(autonomous)
    enrollment = _load_strict(enrollment_path, "target-enrollment.json")
    fingerprint = _validate_enrollment(enrollment)
    if fingerprint not in allowed_targets:
        raise ProtocolViolation("enrolled target fingerprint is not in the host allowlist")
    config = {
        "schema_version": "2" if transport_profile == "diacloud" else "1",
        "worker_id": str(uuid.uuid4()),
        "ide_executable": str(executable),
        "ide_executable_sha256": ide_executable_sha256,
        "script_engine": str(engine),
        "script_engine_sha256": script_engine_sha256,
        "ide_profile": ide_profile,
        "worker_script": str(worker),
        "worker_script_sha256": worker_script_sha256,
        "target_adapter": str(adapter),
        "target_adapter_sha256": target_adapter_sha256,
        "autonomous_config": str(autonomous_path),
        "autonomous_config_sha256": sha256_file(autonomous_path),
        "target_enrollment": str(enrollment_path),
        "target_enrollment_sha256": target_enrollment_sha256,
        "environment_digest": environment_digest,
        "target_fingerprint": fingerprint,
        "observation_expression_allowlist": expressions,
        "heartbeat_seconds": 1,
        "lease_timeout_seconds": 10,
        "max_runtime_seconds": 660 if transport_profile == "diacloud" else 330,
        "max_requests": 20,
        "idle_exit_seconds": 5,
        "startup_timeout_seconds": 30,
    }
    if transport_profile == "diacloud":
        config["runtime_capabilities"] = ["workflow_deadline_v1", "transfer_retention_v1"]
    path = root / "deployment-launcher-config.json"
    lease = v2 / "state" / "deployment-worker.lease.json"
    if replace and lease.exists():
        raise ProtocolViolation("deployment launcher config cannot change while a lease exists")
    if replace:
        atomic_write_replace(path, canonical_json_bytes(config))
    else:
        atomic_write_exclusive(path, canonical_json_bytes(config))
    return path


class AxDeploymentSupervisor:
    """Launch one exact, separately leased target worker and nothing else."""

    def __init__(
        self,
        root: Path,
        *,
        popen: Callable[..., Any] = subprocess.Popen,
        process_alive: Callable[[int], bool | None] = _pid_alive,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.root = root.resolve()
        self.v2 = self.root / "v2"
        if not self.v2.is_dir():
            raise ProtocolViolation("autonomous v2 scratch root does not exist")
        self._popen = popen
        self._process_alive = process_alive
        self._now = now or (lambda: datetime.now(UTC))
        self._monotonic = monotonic
        self._sleep = sleeper
        self.config_path = self.root / "deployment-launcher-config.json"
        self.config = self._load_and_verify_config()
        self.config_digest = sha256_file(self.config_path)
        self.queue = FileJobQueue(self.v2)

    def _load_and_verify_config(self) -> dict[str, Any]:
        config = _load_strict(self.config_path, "deployment-launcher-config.json")
        version = config.get("schema_version")
        expected = _CONFIG_KEYS | ({"runtime_capabilities"} if version == "2" else set())
        if set(config) != expected or version not in {"1", "2"}:
            raise ProtocolViolation("deployment launcher config shape or version mismatch")
        if version == "2" and config["runtime_capabilities"] != ["workflow_deadline_v1", "transfer_retention_v1"]:
            raise ProtocolViolation("deployment launcher capabilities are invalid")
        try:
            validate_correlation_id(config["worker_id"])
        except ProtocolViolation as exc:
            raise ProtocolViolation("deployment worker_id must be a canonical UUID") from exc
        if config["ide_profile"] != "DIADesigner-AX 1.9":
            raise ProtocolViolation("deployment launcher profile is not frozen")
        for field in (
            "ide_executable",
            "script_engine",
            "worker_script",
            "target_adapter",
            "autonomous_config",
            "target_enrollment",
        ):
            file_path = _absolute_regular_file(config[field], field)
            expected = config[field + "_sha256"]
            validate_digest(expected)
            if sha256_file(file_path) != expected:
                raise ProtocolViolation(f"{field} bytes changed after launcher configuration")
        if version == "2" and "WORKFLOW_DEADLINE_CAPABILITY = 1" not in Path(config["worker_script"]).read_text(encoding="utf-8"):
            raise ProtocolViolation("pinned deployment worker lacks workflow deadline capability")
        if config["target_adapter_sha256"] not in APPROVED_TARGET_ADAPTER_SHA256:
            raise ProtocolViolation("deployment target adapter approval is absent or revoked")
        environment_digest = validate_digest(config["environment_digest"])
        fingerprint = validate_digest(config["target_fingerprint"])
        autonomous = _load_strict(Path(config["autonomous_config"]), "autonomous-config.json")
        observed_environment, expressions, targets = _validate_autonomous_config(autonomous)
        if observed_environment != environment_digest:
            raise ProtocolViolation("deployment environment digest drifted")
        if sha256_file(Path(config["autonomous_config"])) != config["autonomous_config_sha256"]:
            raise ProtocolViolation("autonomous config digest drifted")
        enrollment = _load_strict(Path(config["target_enrollment"]), "target-enrollment.json")
        enrollment_path = Path(config["target_enrollment"])
        try:
            enrollment_path.relative_to(self.v2)
        except ValueError as exc:
            raise ProtocolViolation("deployment target enrollment escaped the v2 root") from exc
        if enrollment_path.name != "target-enrollment.json":
            raise ProtocolViolation("deployment target enrollment name drifted")
        if _validate_enrollment(enrollment) != fingerprint or fingerprint not in targets:
            raise ProtocolViolation("deployment target fingerprint binding drifted")
        _validate_target_adapter_source(Path(config["target_adapter"]))
        if expressions != config["observation_expression_allowlist"]:
            raise ProtocolViolation("deployment observation allowlist drifted")
        heartbeat = _bounded_int(config["heartbeat_seconds"], "heartbeat_seconds", 1, 30)
        lease_timeout = _bounded_int(
            config["lease_timeout_seconds"], "lease_timeout_seconds", 2, 120
        )
        if heartbeat >= lease_timeout:
            raise ProtocolViolation("heartbeat must be shorter than lease timeout")
        _bounded_int(config["max_runtime_seconds"], "max_runtime_seconds", 30, 660 if version == "2" else 600)
        _bounded_int(config["max_requests"], "max_requests", 1, 100)
        _bounded_int(config["idle_exit_seconds"], "idle_exit_seconds", 1, 60)
        _bounded_int(config["startup_timeout_seconds"], "startup_timeout_seconds", 1, 60)
        return config

    @property
    def supports_workflow_deadline_v1(self) -> bool:
        return self.config["schema_version"] == "2"

    @property
    def lease_path(self) -> Path:
        return self.v2 / "state" / "deployment-worker.lease.json"

    def _request_exists(self, job_id: str, operation: str) -> bool:
        matches = []
        for directory in ("inbox", "running"):
            for path in (self.v2 / directory).glob("*.request.json"):
                try:
                    request = strict_json_loads_v2(path.read_bytes())
                except (OSError, ProtocolViolation):
                    continue
                if request.get("job_id") == job_id:
                    matches.append(request)
        return len(matches) == 1 and matches[0].get("operation") == operation

    def _archive_dead_lease(self) -> None:
        archive = self.v2 / "archive" / "leases"
        archive.mkdir(parents=True, exist_ok=True)
        destination = archive / f"{utc_now().replace(':', '').replace('-', '')}-deployment-dead.json"
        if destination.exists():
            raise ProtocolViolation("deployment dead-lease archive collision")
        os.replace(self.lease_path, destination)

    def _validate_launch_record(
        self, value: Any, job_id: str, operation: str, target_fingerprint: str
    ) -> dict[str, Any]:
        if (
            not isinstance(value, dict)
            or set(value) != _LAUNCH_KEYS
            or value.get("schema_version") != "1"
            or value.get("record_type") != "AxDeploymentWorkerLaunch"
            or value.get("job_id") != job_id
            or value.get("operation") != operation
            or value.get("target_fingerprint") != target_fingerprint
            or value.get("worker_id") != self.config["worker_id"]
            or value.get("config_digest") != self.config_digest
            or value.get("headless") is not True
            or isinstance(value.get("pid"), bool)
            or not isinstance(value.get("pid"), int)
            or value["pid"] <= 0
        ):
            raise ProtocolViolation("deployment launch record binding mismatch")
        validate_correlation_id(value["job_id"])
        validate_correlation_id(value["worker_id"])
        parse_utc(value["launched_at"])
        return value

    def _archive_launch_record(self, path: Path, job_id: str) -> None:
        directory = self.v2 / "archive" / "launches"
        directory.mkdir(parents=True, exist_ok=True)
        counter = 1
        destination = directory / f"{job_id}.{counter:04d}.deployment.json"
        while destination.exists():
            counter += 1
            destination = directory / f"{job_id}.{counter:04d}.deployment.json"
        os.replace(path, destination)

    def _await_existing_launch(
        self, record: dict[str, Any], record_path: Path
    ) -> dict[str, Any] | None:
        deadline = self._monotonic() + self.config["startup_timeout_seconds"]
        while self._monotonic() < deadline:
            if self.lease_path.is_file():
                lease = self._live_lease()
                if lease is not None:
                    if lease["pid"] != record["pid"]:
                        raise ProtocolViolation(
                            "launch record PID does not own the exact live lease"
                        )
                    if self._latest_heartbeat(lease, require_record=True) is not None:
                        return lease
            alive = self._process_alive(record["pid"])
            if alive is False:
                self._archive_launch_record(record_path, record["job_id"])
                return None
            if alive is None:
                raise ProtocolViolation("recorded deployment launch PID liveness is unknown")
            self._sleep(0.1)
        raise ProtocolViolation(
            "recorded deployment launch is live but readiness remains unproven"
        )

    def _latest_heartbeat(
        self, lease: dict[str, Any], *, require_record: bool = False
    ) -> datetime | None:
        latest = parse_utc(lease["heartbeat_at"])
        if latest > self._now():
            raise ProtocolViolation("deployment lease heartbeat is from the future")
        matched_record = False
        directory = (
            self.v2
            / "state"
            / "deployment-heartbeats"
            / self.config["worker_id"]
        )
        if not directory.is_dir():
            return None if require_record else latest
        for path in directory.glob("*.json"):
            try:
                value = strict_json_loads_v2(path.read_bytes())
                if (
                    not isinstance(value, dict)
                    or set(value) != _HEARTBEAT_KEYS
                    or value.get("protocol") != "AXQ-FILE/2"
                    or value.get("worker_id") != self.config["worker_id"]
                    or value.get("pid") != lease["pid"]
                    or value.get("process_start_fingerprint")
                    != lease["process_start_fingerprint"]
                    or value.get("target_fingerprint")
                    != self.config["target_fingerprint"]
                    or value.get("config_digest") != self.config_digest
                ):
                    continue
                created = parse_utc(value["created_at"])
                if created > self._now():
                    raise ProtocolViolation("deployment heartbeat is from the future")
            except (OSError, KeyError, ProtocolViolation):
                continue
            matched_record = True
            latest = max(latest, created)
        return latest if matched_record or not require_record else None

    def _live_lease(self) -> dict[str, Any] | None:
        if not self.lease_path.exists():
            return None
        lease = _load_strict(self.lease_path, "deployment worker lease")
        if set(lease) != _LEASE_KEYS or lease["protocol"] != "AXQ-FILE/2":
            raise ProtocolViolation("deployment worker lease shape or protocol mismatch")
        for field in ("process_start_fingerprint", "environment_digest", "target_fingerprint", "config_digest"):
            validate_digest(lease[field])
        if (
            isinstance(lease["pid"], bool)
            or not isinstance(lease["pid"], int)
            or lease["pid"] <= 0
        ):
            raise ProtocolViolation("deployment worker lease PID is invalid")
        if (
            lease["worker_id"] != self.config["worker_id"]
            or lease["environment_digest"] != self.config["environment_digest"]
            or lease["target_fingerprint"] != self.config["target_fingerprint"]
            or lease["config_digest"] != self.config_digest
        ):
            raise ProtocolViolation("foreign deployment worker owns the lease")
        heartbeat = self._latest_heartbeat(lease)
        if heartbeat is None:
            raise ProtocolViolation("deployment worker heartbeat is unavailable")
        age = (self._now() - heartbeat).total_seconds()
        alive = self._process_alive(lease["pid"])
        if alive and age <= self.config["lease_timeout_seconds"]:
            return lease
        if alive:
            raise ProtocolViolation("deployment worker PID is live but heartbeat is stale")
        if alive is False:
            self._archive_dead_lease()
            return None
        raise ProtocolViolation("deployment worker PID liveness is unknown")

    def _has_unresolved_deployment(self, exclude_job_id: str | None = None) -> bool:
        for path in (self.v2 / "results").glob("*.result.json"):
            try:
                result = strict_json_loads_v2(path.read_bytes())
            except (OSError, ProtocolViolation):
                continue
            if (
                isinstance(result, dict)
                and result.get("operation") == "deployment.execute"
                and result.get("status") == "unknown_outcome"
                and result.get("job_id") != exclude_job_id
            ):
                if self._terminal_reconciliation_exists(result["job_id"], path):
                    continue
                return True
        for directory in ("inbox", "running"):
            for path in (self.v2 / directory).glob("*.request.json"):
                try:
                    request = strict_json_loads_v2(path.read_bytes())
                except (OSError, ProtocolViolation):
                    continue
                if (
                    request.get("operation") == "deployment.execute"
                    and request.get("job_id") != exclude_job_id
                ):
                    return True
        return False

    def _registered_runtime_release(self, artifact_digest: str) -> str:
        validate_digest(artifact_digest)
        artifact_path = self.v2 / "artifacts" / f"{artifact_digest[7:]}.project"
        if not artifact_path.is_file() or sha256_file(artifact_path) != artifact_digest:
            raise ProtocolViolation("reconciliation artifact is missing or corrupt")
        matches: list[str] = []
        for manifest_path in sorted((self.v2 / "workspaces").glob("*/manifest.json")):
            manifest = _load_strict(manifest_path, "workspace artifact manifest")
            for record in manifest.get("frozen_artifacts", []):
                if (
                    isinstance(record, dict)
                    and record.get("artifact_digest") == artifact_digest
                    and record.get("environment_digest") == self.config["environment_digest"]
                    and record.get("status") == "READY_FOR_DEPLOY"
                    and record.get("path") == f"artifacts/{artifact_digest[7:]}.project"
                ):
                    release = record.get("runtime_release_digest")
                    validate_digest(release)
                    matches.append(release)
        if not matches or len(set(matches)) != 1:
            raise ProtocolViolation("reconciliation artifact release identity is ambiguous")
        return matches[0]

    def _terminal_reconciliation_exists(self, job_id: str, result_path: Path) -> bool:
        validate_correlation_id(job_id)
        directory = self.v2 / "state" / "reconciliations"
        if not directory.is_dir():
            return False
        try:
            original_request, result = load_archived_result_chain_v2(result_path, self.v2)
        except (OSError, ProtocolViolation) as exc:
            raise ProtocolViolation("original deployment result chain is invalid") from exc
        if (
            result.get("job_id") != job_id
            or result.get("operation") != "deployment.execute"
            or result.get("status") != "unknown_outcome"
            or result.get("environment_digest") != self.config["environment_digest"]
        ):
            raise ProtocolViolation("original deployment result binding mismatch")
        result_digest = sha256_file(result_path)
        evidence_descriptors = [
            item
            for item in result.get("artifacts", [])
            if isinstance(item, dict) and item.get("role") == "evidence"
        ]
        if len(evidence_descriptors) != 1:
            raise ProtocolViolation("original deployment evidence descriptor is missing")
        descriptor = evidence_descriptors[0]
        evidence_path = validate_artifact_path(descriptor.get("path"), self.v2)
        if (
            not evidence_path.is_file()
            or sha256_file(evidence_path) != descriptor.get("sha256")
        ):
            raise ProtocolViolation("original deployment evidence changed")
        evidence = validate_deployment_worker_evidence_v1(
            _load_strict(evidence_path, "original deployment evidence"), result
        )
        if (
            evidence["target_fingerprint"] != self.config["target_fingerprint"]
            or evidence["reconciliation_required"] is not True
            or evidence["outcome"] != "unknown_outcome"
        ):
            raise ProtocolViolation("original deployment evidence binding mismatch")
        candidate_digest = evidence.get("candidate_digest")
        rollback_digest = evidence.get("rollback_digest")
        candidate_release = self._registered_runtime_release(candidate_digest)
        rollback_release = self._registered_runtime_release(rollback_digest)
        marker_path = self.v2 / "state" / "side-effects" / f"{job_id}.started.json"
        marker = _load_strict(marker_path, "deployment side-effect marker")
        validate_deployment_side_effect_marker_v1(
            marker,
            job_id=job_id,
            environment_digest=self.config["environment_digest"],
        )
        provenance_head = validate_deployment_reconciliation_head_v1(
            self.v2,
            request=original_request,
            request_raw=canonical_json_bytes(original_request),
            target_fingerprint=self.config["target_fingerprint"],
        )
        journal_head_digest = provenance_head["digest"]
        terminal: list[dict[str, Any]] = []
        for path in sorted(directory.glob(f"{job_id}.*.json")):
            value = _load_strict(path, "deployment reconciliation")
            if set(value) != _RECONCILIATION_RECORD_KEYS:
                raise ProtocolViolation("deployment reconciliation shape mismatch")
            if (
                value.get("schema_version") == "1"
                and value.get("record_type") == "DeploymentReconciliation"
                and value.get("original_job_id") == job_id
                and value.get("original_result_digest") == result_digest
                and value.get("target_fingerprint") == self.config["target_fingerprint"]
                and value.get("outcome") in {"succeeded", "rolled_back"}
                and value.get("reconciliation_required") is False
            ):
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
                    or value["side_effect_marker_digest"] != sha256_file(marker_path)
                    or value["provenance_head_kind"] != provenance_head["kind"]
                    or value["journal_head_digest"] != journal_head_digest
                    or not isinstance(observed, dict)
                    or set(observed) != _RECONCILIATION_OBSERVED_KEYS
                    or observed["target_fingerprint"] != value["target_fingerprint"]
                    or observed["runtime_release_digest"] != expected_release
                    or observed["application_state"] != "run"
                    or observed["forced_values_present"] is not False
                    or observed["physical_outputs_energized"] is not False
                ):
                    raise ProtocolViolation("deployment reconciliation semantic mismatch")
                reconciliation_result_path = (
                    self.v2
                    / "results"
                    / f"{value['reconciliation_job_id']}.result.json"
                )
                try:
                    reconciliation_request, reconciliation_result = (
                        load_archived_result_chain_v2(
                            reconciliation_result_path, self.v2
                        )
                    )
                except (OSError, ProtocolViolation) as exc:
                    raise ProtocolViolation("reconciliation result chain is invalid") from exc
                if (
                    reconciliation_result.get("job_id")
                    != value["reconciliation_job_id"]
                    or reconciliation_result.get("operation") != "deployment.reconcile"
                    or reconciliation_result.get("status") != "succeeded"
                    or reconciliation_result.get("environment_digest")
                    != self.config["environment_digest"]
                ):
                    raise ProtocolViolation("reconciliation result binding mismatch")
                input_descriptors = [
                    item
                    for item in reconciliation_request["artifacts"]
                    if item["role"] == "input"
                ]
                if len(reconciliation_request["artifacts"]) != 1 or len(input_descriptors) != 1:
                    raise ProtocolViolation("reconciliation request input binding mismatch")
                reconciliation_input = _load_strict(
                    validate_artifact_path(input_descriptors[0]["path"], self.v2),
                    "reconciliation request input",
                )
                if reconciliation_input != {
                    "schema_version": "1",
                    "record_type": "DeploymentReconcileInput",
                    "original_job_id": job_id,
                    "target_fingerprint": value["target_fingerprint"],
                    "artifact_digest": value["artifact_digest"],
                    "rollback_digest": value["rollback_digest"],
                }:
                    raise ProtocolViolation("reconciliation request payload binding mismatch")
                evidence_descriptors = [
                    item
                    for item in reconciliation_result["artifacts"]
                    if item["role"] == "evidence"
                ]
                if len(reconciliation_result["artifacts"]) != 1 or len(evidence_descriptors) != 1:
                    raise ProtocolViolation("reconciliation evidence descriptor binding mismatch")
                reconciliation_evidence = validate_deployment_worker_evidence_v1(
                    _load_strict(
                        validate_artifact_path(evidence_descriptors[0]["path"], self.v2),
                        "reconciliation evidence",
                    ),
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
                terminal.append(value)
        if len(terminal) > 1:
            first = canonical_json_bytes(terminal[0])
            if any(canonical_json_bytes(value) != first for value in terminal[1:]):
                raise ProtocolViolation("conflicting deployment reconciliation evidence")
        return bool(terminal)

    def recover_inactive(self) -> dict[str, int]:
        if self._live_lease() is not None:
            return {
                "partial_rejected": 0,
                "corrupt_rejected": 0,
                "stale_rejected": 0,
                "requeued": 0,
                "blocked_for_reconciliation": 0,
                "unknown_outcome": 0,
            }
        return self.queue.recover(operations=TARGET_OPERATIONS)

    def _wait_for_ready_lease(self, process: Any) -> dict[str, Any] | None:
        deadline = self._monotonic() + self.config["startup_timeout_seconds"]
        while self._monotonic() < deadline:
            if self.lease_path.is_file():
                lease = self._live_lease()
                if lease is not None:
                    if lease["pid"] != process.pid:
                        raise ProtocolViolation(
                            "a different deployment worker acquired the startup lease"
                        )
                    if self._latest_heartbeat(lease, require_record=True) is not None:
                        return lease
            poll = getattr(process, "poll", None)
            if callable(poll) and poll() is not None:
                return None
            self._sleep(0.1)
        return None

    def ensure_running(
        self, job_id: str, operation: str, target_fingerprint: str
    ) -> dict[str, Any]:
        validate_correlation_id(job_id)
        if operation not in TARGET_OPERATIONS:
            raise ProtocolViolation("deployment supervisor accepts only exact target operations")
        validate_digest(target_fingerprint)
        if target_fingerprint != self.config["target_fingerprint"]:
            raise ProtocolViolation("deployment supervisor target fingerprint mismatch")
        self.config = self._load_and_verify_config()
        current_digest = sha256_file(self.config_path)
        if current_digest != self.config_digest:
            raise ProtocolViolation("deployment launcher config changed after supervisor creation")
        if not self._request_exists(job_id, operation):
            raise ProtocolViolation("target request is not queued with the exact job and operation")
        record_path = self.v2 / "state" / "launches" / f"{job_id}.deployment.json"
        existing_launch = None
        if record_path.is_file():
            existing_launch = self._validate_launch_record(
                _load_strict(record_path, "deployment launch record"),
                job_id,
                operation,
                target_fingerprint,
            )
        live = self._live_lease()
        if existing_launch is not None:
            if live is not None:
                if live["pid"] != existing_launch["pid"]:
                    raise ProtocolViolation(
                        "launch record PID does not own the exact live lease"
                    )
                return {
                    "launch_state": "adopted",
                    "job_id": job_id,
                    "operation": operation,
                    "target_fingerprint": target_fingerprint,
                    "worker_id": live["worker_id"],
                    "pid": live["pid"],
                    "config_digest": self.config_digest,
                }
            ready = self._await_existing_launch(existing_launch, record_path)
            if ready is not None:
                return {
                    "launch_state": "adopted",
                    "job_id": job_id,
                    "operation": operation,
                    "target_fingerprint": target_fingerprint,
                    "worker_id": ready["worker_id"],
                    "pid": ready["pid"],
                    "config_digest": self.config_digest,
                }
        if live is None:
            self.queue.recover(operations=TARGET_OPERATIONS)
        if operation != "deployment.reconcile" and self._has_unresolved_deployment(
            job_id if operation == "deployment.execute" else None
        ):
            raise ProtocolViolation(
                "an earlier deployment outcome is unresolved; reconciliation is required"
            )
        if live is not None:
            return {
                "launch_state": "adopted",
                "job_id": job_id,
                "operation": operation,
                "target_fingerprint": target_fingerprint,
                "worker_id": live["worker_id"],
                "pid": live["pid"],
                "config_digest": self.config_digest,
            }
        executable = self.config["ide_executable"]
        profile_argument = f"--profile=\"{self.config['ide_profile']}\""
        runscript_argument = f"--runscript=\"{self.config['worker_script']}\""
        command: str | list[str]
        if os.name == "nt":
            command = (
                f'"{executable}" {profile_argument} {runscript_argument} '
                "--culture=en --noUI --textPrompts"
            )
        else:
            command = [
                executable,
                profile_argument,
                runscript_argument,
                "--culture=en",
                "--noUI",
                "--textPrompts",
            ]
        environment = os.environ.copy()
        environment["AXMCP_DEPLOYMENT_ROOT"] = str(self.v2)
        environment["AXMCP_DEPLOYMENT_CONFIG"] = str(self.config_path)
        environment["AXMCP_DEPLOYMENT_CONFIG_SHA256"] = self.config_digest
        log_path = self.v2 / "evidence" / f"{job_id}.ax-deployment-worker.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            creationflags = subprocess.CREATE_NO_WINDOW
        with log_path.open("ab", buffering=0) as log_handle:
            process = self._popen(
                command,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                shell=False,
                close_fds=True,
                startupinfo=startupinfo,
                creationflags=creationflags,
            )
        if not isinstance(getattr(process, "pid", None), int) or process.pid <= 0:
            raise ProtocolViolation("deployment worker launcher returned an invalid PID")
        record = {
            "schema_version": "1",
            "record_type": "AxDeploymentWorkerLaunch",
            "job_id": job_id,
            "operation": operation,
            "target_fingerprint": target_fingerprint,
            "worker_id": self.config["worker_id"],
            "pid": process.pid,
            "launched_at": utc_now(),
            "config_digest": self.config_digest,
            "headless": True,
        }
        atomic_write_exclusive(record_path, canonical_json_bytes(record))
        ready = self._wait_for_ready_lease(process)
        if ready is None:
            poll = getattr(process, "poll", None)
            stopped = callable(poll) and poll() is not None
            return {
                "launch_state": "not_started" if stopped else "unknown",
                "job_id": job_id,
                "operation": operation,
                "target_fingerprint": target_fingerprint,
                "worker_id": self.config["worker_id"],
                "pid": process.pid,
                "config_digest": self.config_digest,
                "diagnostic": (
                    "deployment worker exited before the readiness lease"
                    if stopped
                    else "deployment worker did not prove the readiness lease before timeout"
                ),
            }
        return {
            "launch_state": "started",
            "job_id": job_id,
            "operation": operation,
            "target_fingerprint": target_fingerprint,
            "worker_id": self.config["worker_id"],
            "pid": process.pid,
            "config_digest": self.config_digest,
        }


__all__ = [
    "AxDeploymentSupervisor",
    "APPROVED_TARGET_ADAPTER_SHA256",
    "TARGET_OPERATIONS",
    "write_deployment_launcher_config",
]
