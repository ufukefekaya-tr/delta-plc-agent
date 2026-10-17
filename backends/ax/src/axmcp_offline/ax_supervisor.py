from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .protocol_v2 import validate_workspace_id
from .security import (
    ProtocolViolation,
    atomic_write_exclusive,
    atomic_write_replace,
    canonical_json_bytes,
    parse_utc,
    sha256_file,
    strict_json_loads_v2,
    utc_now,
    validate_digest,
)


_LAUNCHER_CONFIG_KEYS = {
    "schema_version",
    "ide_executable",
    "ide_executable_sha256",
    "script_engine",
    "script_engine_sha256",
    "ide_profile",
    "worker_script",
    "worker_script_sha256",
    "ladder_helper",
    "ladder_helper_sha256",
    "heartbeat_seconds",
    "lease_timeout_seconds",
    "max_request_seconds",
    "max_runtime_seconds",
    "max_requests",
    "idle_exit_seconds",
}
_LEASE_KEYS = {
    "protocol",
    "worker_id",
    "pid",
    "process_start_fingerprint",
    "started_at",
    "heartbeat_at",
    "environment_digest",
    "sequence",
}
_WORKER_OPERATIONS = (
    "source.read",
    "source.upsert.st",
    "ladder.networks.apply",
    "project.build",
)
_MAX_JOB_LAUNCH_ATTEMPTS = 2


def _absolute_regular_file(value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ProtocolViolation(f"{field} must be a non-empty absolute path")
    path = Path(value)
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ProtocolViolation(f"{field} must be an existing non-symlink regular file")
    return path.resolve()


def _bounded_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ProtocolViolation(f"{field} must be between {minimum} and {maximum}")
    return value


def _pid_alive(pid: int) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _terminate_launched_process(process: Any) -> None:
    terminate = getattr(process, "terminate", None)
    if callable(terminate):
        terminate()
        return
    pid = getattr(process, "pid", None)
    if isinstance(pid, int) and pid > 0:
        os.kill(pid, 15)


def _start_hidden_window_guard(
    process: Any, max_runtime_seconds: int, status_path: Path
) -> int | None:
    """Start an independent bounded window guard and wait for its ready record."""

    if os.name != "nt":
        return None
    if not isinstance(getattr(process, "pid", None), int) or process.pid <= 0:
        raise ProtocolViolation("AX process PID is invalid for hidden-window guard")
    status_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "axmcp_offline.window_guard",
        "--pid",
        str(process.pid),
        "--max-runtime-seconds",
        str(max_runtime_seconds + 30),
        "--status",
        str(status_path.resolve()),
    ]
    guard_startup = subprocess.STARTUPINFO()
    guard_startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    guard_startup.wShowWindow = subprocess.SW_HIDE
    guard_process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        close_fds=True,
        startupinfo=guard_startup,
        creationflags=(
            subprocess.CREATE_NO_WINDOW
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_BREAKAWAY_FROM_JOB
        ),
    )
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if status_path.is_file():
                record = strict_json_loads_v2(status_path.read_bytes())
                if (
                    record.get("record_type") == "AxWindowGuard"
                    and record.get("pid") == process.pid
                    and record.get("status") == "active"
                ):
                    return guard_process.pid
                if record.get("status") == "failed":
                    raise ProtocolViolation(
                        "hidden-window guard failed before AX launch handoff"
                    )
            if guard_process.poll() is not None:
                raise ProtocolViolation(
                    "hidden-window guard exited before AX launch handoff"
                )
            time.sleep(0.025)
        raise ProtocolViolation("hidden-window guard did not become ready")
    except Exception:
        if guard_process.poll() is None:
            guard_process.terminate()
        raise


def write_launcher_config(
    root: Path,
    *,
    ide_executable: Path,
    ide_executable_sha256: str,
    script_engine: Path,
    script_engine_sha256: str,
    ide_profile: str,
    worker_script: Path,
    worker_script_sha256: str,
    ladder_helper: Path | None = None,
    ladder_helper_sha256: str | None = None,
    replace: bool = False,
) -> Path:
    """Create the immutable identity/timing config used by the AX launcher.

    This performs no IDE or target operation.  Every executable byte identity is
    checked before the configuration becomes visible to the supervisor.
    """

    root = root.resolve()
    if not root.is_dir() or not (root / "v2").is_dir():
        raise ProtocolViolation("autonomous scratch root is not initialized")
    executable = _absolute_regular_file(str(ide_executable), "ide_executable")
    engine = _absolute_regular_file(str(script_engine), "script_engine")
    worker = _absolute_regular_file(str(worker_script), "worker_script")
    helper = _absolute_regular_file(
        str(ladder_helper or worker.with_name("ladder_import.py")), "ladder_helper"
    )
    if helper != worker.with_name("ladder_import.py"):
        raise ProtocolViolation("ladder_helper must be the fixed worker sibling")
    helper_digest = ladder_helper_sha256 or sha256_file(helper)
    for expected, path, field in (
        (ide_executable_sha256, executable, "ide_executable_sha256"),
        (script_engine_sha256, engine, "script_engine_sha256"),
        (worker_script_sha256, worker, "worker_script_sha256"),
        (helper_digest, helper, "ladder_helper_sha256"),
    ):
        validate_digest(expected)
        if sha256_file(path) != expected:
            raise ProtocolViolation(f"{field} does not match file bytes")
    if ide_profile != "DIADesigner-AX 1.9":
        raise ProtocolViolation("only the frozen DIADesigner-AX 1.9 profile is supported")
    config = {
        "schema_version": "1",
        "ide_executable": str(executable),
        "ide_executable_sha256": ide_executable_sha256,
        "script_engine": str(engine),
        "script_engine_sha256": script_engine_sha256,
        "ide_profile": ide_profile,
        "worker_script": str(worker),
        "worker_script_sha256": worker_script_sha256,
        "ladder_helper": str(helper),
        "ladder_helper_sha256": helper_digest,
        "heartbeat_seconds": 1,
        "lease_timeout_seconds": 10,
        "max_request_seconds": 300,
        "max_runtime_seconds": 330,
        "max_requests": 20,
        "idle_exit_seconds": 5,
    }
    path = root / "ax-launcher-config.json"
    if replace:
        atomic_write_replace(path, canonical_json_bytes(config))
    else:
        atomic_write_exclusive(path, canonical_json_bytes(config))
    return path


class AxWorkerSupervisor:
    """Fail-closed launcher for the bounded DIADesigner AXQ-FILE/2 worker."""

    def __init__(
        self,
        root: Path,
        *,
        popen: Callable[..., Any] = subprocess.Popen,
        process_alive: Callable[[int], bool] = _pid_alive,
        window_guard: Callable[[Any, int, Path], int | None] = _start_hidden_window_guard,
    ) -> None:
        self.root = root.resolve()
        self.v2 = self.root / "v2"
        if not self.v2.is_dir():
            raise ProtocolViolation("autonomous v2 scratch root does not exist")
        self._popen = popen
        self._process_alive = process_alive
        self._window_guard = window_guard
        self.config = self._load_and_verify_config()

    def _load_and_verify_config(self) -> dict[str, Any]:
        path = self.root / "ax-launcher-config.json"
        try:
            config = strict_json_loads_v2(path.read_bytes())
        except (OSError, ProtocolViolation) as exc:
            raise ProtocolViolation("invalid ax-launcher-config.json") from exc
        if set(config) != _LAUNCHER_CONFIG_KEYS or config["schema_version"] != "1":
            raise ProtocolViolation("AX launcher config shape or version mismatch")
        executable = _absolute_regular_file(config["ide_executable"], "ide_executable")
        engine = _absolute_regular_file(config["script_engine"], "script_engine")
        worker = _absolute_regular_file(config["worker_script"], "worker_script")
        helper = _absolute_regular_file(config["ladder_helper"], "ladder_helper")
        if helper != worker.with_name("ladder_import.py"):
            raise ProtocolViolation("ladder helper is not the fixed worker sibling")
        if config["ide_profile"] != "DIADesigner-AX 1.9":
            raise ProtocolViolation("AX launcher profile is not frozen to DIADesigner-AX 1.9")
        for expected, file_path, field in (
            (config["ide_executable_sha256"], executable, "ide_executable"),
            (config["script_engine_sha256"], engine, "script_engine"),
            (config["worker_script_sha256"], worker, "worker_script"),
            (config["ladder_helper_sha256"], helper, "ladder_helper"),
        ):
            validate_digest(expected)
            if sha256_file(file_path) != expected:
                raise ProtocolViolation(f"{field} bytes changed after launcher configuration")
        heartbeat = _bounded_int(config["heartbeat_seconds"], "heartbeat_seconds", 1, 30)
        lease = _bounded_int(config["lease_timeout_seconds"], "lease_timeout_seconds", 2, 120)
        if heartbeat >= lease:
            raise ProtocolViolation("heartbeat must be shorter than lease timeout")
        _bounded_int(config["max_request_seconds"], "max_request_seconds", 30, 300)
        _bounded_int(config["max_runtime_seconds"], "max_runtime_seconds", 30, 600)
        _bounded_int(config["max_requests"], "max_requests", 1, 100)
        _bounded_int(config["idle_exit_seconds"], "idle_exit_seconds", 1, 60)
        return config

    def _load_workspace(self, workspace_id: str) -> tuple[dict[str, Any], Path]:
        validate_workspace_id(workspace_id)
        directory = self.v2 / "workspaces" / workspace_id
        manifest_path = directory / "manifest.json"
        project_path = directory / "project.project"
        try:
            manifest = strict_json_loads_v2(manifest_path.read_bytes())
        except (OSError, ProtocolViolation) as exc:
            raise ProtocolViolation("workspace manifest is unavailable to AX supervisor") from exc
        if manifest.get("workspace_id") != workspace_id or not project_path.is_file():
            raise ProtocolViolation("workspace identity or project is invalid")
        for field in ("baseline_digest", "project_digest", "environment_digest"):
            validate_digest(manifest.get(field))
        if sha256_file(project_path) != manifest["project_digest"]:
            raise ProtocolViolation("workspace project bytes drifted before AX launch")
        return manifest, project_path

    def _write_worker_config(self, workspace_id: str) -> Path:
        manifest, _project = self._load_workspace(workspace_id)
        config = {
            "protocol": "AXQ-FILE/2",
            "config_version": "2",
            "worker_id": workspace_id,
            "environment_digest": manifest["environment_digest"],
            "workspace_project": f"workspaces/{workspace_id}/project.project",
            "initial_project_sha256": manifest["baseline_digest"],
            "ide_executable_sha256": self.config["ide_executable_sha256"],
            "script_engine_sha256": self.config["script_engine_sha256"],
            "ladder_helper_sha256": self.config["ladder_helper_sha256"],
            "ide_profile": self.config["ide_profile"],
            "allowed_operations": list(_WORKER_OPERATIONS),
            "heartbeat_seconds": self.config["heartbeat_seconds"],
            "lease_timeout_seconds": self.config["lease_timeout_seconds"],
            "max_request_seconds": self.config["max_request_seconds"],
            "max_runtime_seconds": self.config["max_runtime_seconds"],
            "max_requests": self.config["max_requests"],
            "idle_exit_seconds": self.config["idle_exit_seconds"],
        }
        path = self.v2 / "worker-config.json"
        raw = canonical_json_bytes(config)
        if path.exists() and path.read_bytes() == raw:
            return path
        lease = self.v2 / "state" / "worker.lease.json"
        if lease.exists():
            raise ProtocolViolation("worker configuration cannot change while a lease exists")
        atomic_write_replace(path, raw)
        return path

    def _live_lease(self, workspace_id: str) -> dict[str, Any] | None:
        path = self.v2 / "state" / "worker.lease.json"
        if not path.exists():
            return None
        try:
            lease = strict_json_loads_v2(path.read_bytes())
        except (OSError, ProtocolViolation) as exc:
            raise ProtocolViolation("worker lease is corrupt") from exc
        if set(lease) != _LEASE_KEYS or lease["protocol"] != "AXQ-FILE/2":
            raise ProtocolViolation("worker lease shape or protocol mismatch")
        if lease["worker_id"] != workspace_id:
            raise ProtocolViolation("another workspace owns the worker lease")
        if self._process_alive(lease["pid"]):
            return lease
        return None

    def _job_queue_state(self, job_id: str) -> str:
        """Return the durable queue state for one exact job.

        Request filenames are correlation IDs while result filenames are job
        IDs, so the two request directories must be inspected by content.  A
        malformed queue item is fail-closed rather than silently skipped.
        """

        result = self.v2 / "results" / f"{job_id}.result.json"
        if result.is_file():
            return "result"
        for directory_name in ("running", "inbox"):
            directory = self.v2 / directory_name
            for path in sorted(directory.glob("*.request.json")):
                try:
                    request = strict_json_loads_v2(path.read_bytes())
                except (OSError, ProtocolViolation) as exc:
                    raise ProtocolViolation(
                        f"{directory_name} request is unavailable or corrupt"
                    ) from exc
                if request.get("job_id") == job_id:
                    return directory_name
        return "absent"

    def _job_request_path(self, job_id: str, directory_name: str) -> Path | None:
        matches: list[Path] = []
        for path in sorted((self.v2 / directory_name).glob("*.request.json")):
            try:
                request = strict_json_loads_v2(path.read_bytes())
            except (OSError, ProtocolViolation) as exc:
                raise ProtocolViolation(
                    f"{directory_name} request is unavailable or corrupt"
                ) from exc
            if request.get("job_id") == job_id:
                matches.append(path)
        if len(matches) > 1:
            raise ProtocolViolation(f"duplicate {directory_name} state for AX job")
        return matches[0] if matches else None

    def _job_launch_records(self, workspace_id: str, job_id: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in sorted((self.v2 / "state" / "launches").glob(f"{job_id}*.json")):
            try:
                record = strict_json_loads_v2(path.read_bytes())
            except (OSError, ProtocolViolation) as exc:
                raise ProtocolViolation("AX launch record is unavailable or corrupt") from exc
            if (
                record.get("record_type") != "AxWorkerLaunch"
                or record.get("job_id") != job_id
                or record.get("worker_id") != workspace_id
            ):
                raise ProtocolViolation("AX launch record identity is invalid")
            records.append(record)
        records.sort(key=lambda item: (item.get("launched_at", ""), item.get("pid", 0)))
        return records

    def _launch_process_active(self, workspace_id: str, job_id: str) -> bool:
        records = self._job_launch_records(workspace_id, job_id)
        if not records:
            return False
        record = records[-1]
        handoff_status = record.get("handoff_status")
        if handoff_status in {
            "spawn_failed",
            "window_guard_start_failed_terminated",
            "launch_record_failed_terminated",
            "launch_record_handoff_failed_terminated",
        }:
            return False
        if handoff_status == "launch_intent":
            raise ProtocolViolation("AX launch handoff is unresolved before process identity")
        pid = record.get("pid")
        if (
            isinstance(pid, bool)
            or not isinstance(pid, int)
            or pid <= 0
        ):
            raise ProtocolViolation("AX launch record identity is invalid")
        if isinstance(handoff_status, str) and handoff_status.endswith(
            "_termination_unknown"
        ):
            return self._process_alive(pid)
        guard_relative = record.get("window_guard_status")
        if isinstance(guard_relative, str) and guard_relative:
            guard_path = (self.v2 / guard_relative).resolve()
            evidence_root = (self.v2 / "evidence").resolve()
            if guard_path.parent != evidence_root:
                raise ProtocolViolation("AX launch guard path escapes evidence root")
            try:
                guard = strict_json_loads_v2(guard_path.read_bytes())
            except (OSError, ProtocolViolation) as exc:
                raise ProtocolViolation("AX launch guard record is unavailable or corrupt") from exc
            if (
                guard.get("record_type") != "AxWindowGuard"
                or guard.get("pid") != pid
                or guard.get("status")
                not in {"starting", "active", "stopped", "failed"}
            ):
                raise ProtocolViolation("AX launch guard identity is invalid")
            if guard["status"] in {"starting", "active"}:
                age_seconds = (datetime.now(UTC) - parse_utc(guard["checked_at"])).total_seconds()
                if age_seconds < -5:
                    raise ProtocolViolation("AX launch guard timestamp is in the future")
                if age_seconds <= self.config["lease_timeout_seconds"] + 2:
                    return True
                try:
                    process_alive = self._process_alive(pid)
                except Exception as exc:
                    raise ProtocolViolation(
                        "stale AX launch guard process liveness is unknown"
                    ) from exc
                if not isinstance(process_alive, bool):
                    raise ProtocolViolation(
                        "stale AX launch guard process liveness is invalid"
                    )
                return process_alive
            return False
        return self._process_alive(pid)

    def _adopt_live_worker(
        self, workspace_id: str, job_id: str, live: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Adopt only after the live worker claims the already-published job.

        The worker may cross its bounded idle-exit threshold between request
        publication and ``ensure_running``.  Returning ``already_running`` at
        that instant strands the immutable request.  Wait at most one idle
        window for either a durable claim/result or exact lease release; the
        latter tells the caller to launch a replacement worker.
        """

        initial_state = self._job_queue_state(job_id)
        if initial_state == "result":
            return {
                "status": "already_running",
                "worker_id": workspace_id,
                "pid": live["pid"],
                "job_id": job_id,
                "launched_at": None,
            }
        if initial_state == "running":
            if self._live_lease(workspace_id) is None:
                return None
            return {
                "status": "already_running",
                "worker_id": workspace_id,
                "pid": live["pid"],
                "job_id": job_id,
                "launched_at": None,
            }
        if initial_state != "inbox":
            raise ProtocolViolation("AX job is absent during live-worker adoption")
        deadline = time.monotonic() + self.config["idle_exit_seconds"] + 1
        while time.monotonic() < deadline:
            state = self._job_queue_state(job_id)
            if state == "result":
                return {
                    "status": "already_running",
                    "worker_id": workspace_id,
                    "pid": live["pid"],
                    "job_id": job_id,
                    "launched_at": None,
                }
            if state == "running":
                if self._live_lease(workspace_id) is None:
                    return None
                return {
                    "status": "already_running",
                    "worker_id": workspace_id,
                    "pid": live["pid"],
                    "job_id": job_id,
                    "launched_at": None,
                }
            if state != "inbox":
                raise ProtocolViolation("queued AX job disappeared during worker adoption")
            if self._live_lease(workspace_id) is None:
                return None
            time.sleep(0.025)
        raise ProtocolViolation(
            "live AX worker did not claim queued job within bounded adoption window"
        )

    def recover_workspace_job(
        self, workspace_id: str, job_id: str, expected_project_digest: str
    ) -> dict[str, Any]:
        """Recover one inactive project-worker request without replaying changed bytes."""

        validate_workspace_id(workspace_id)
        validate_workspace_id(job_id)
        validate_digest(expected_project_digest)
        state = self._job_queue_state(job_id)
        if state == "result":
            return {"status": "result_ready", "job_id": job_id}
        live = self._live_lease(workspace_id)
        if live is not None:
            adopted = self._adopt_live_worker(workspace_id, job_id, live)
            if adopted is not None:
                return adopted
            state = self._job_queue_state(job_id)
        if self._launch_process_active(workspace_id, job_id):
            return {"status": "launching", "job_id": job_id}
        state = self._job_queue_state(job_id)
        if state == "result":
            return {"status": "result_ready", "job_id": job_id}
        if state == "absent":
            raise ProtocolViolation("AX workspace job is absent during recovery")
        if state == "running":
            project_path = (
                self.v2 / "workspaces" / workspace_id / "project.project"
            ).resolve()
            workspace_root = (self.v2 / "workspaces" / workspace_id).resolve()
            if project_path.parent != workspace_root or not project_path.is_file():
                raise ProtocolViolation("workspace project is unavailable during recovery")
            actual_digest = sha256_file(project_path)
            if actual_digest != expected_project_digest:
                return {
                    "status": "unknown_outcome",
                    "job_id": job_id,
                    "actual_project_digest": actual_digest,
                }
            running = self._job_request_path(job_id, "running")
            if running is None:
                raise ProtocolViolation("running AX workspace request disappeared")
            os.replace(running, self.v2 / "inbox" / running.name)
        elif state != "inbox":
            raise ProtocolViolation("AX workspace job has an invalid recovery state")
        launches = self._job_launch_records(workspace_id, job_id)
        if len(launches) >= _MAX_JOB_LAUNCH_ATTEMPTS:
            inbox = self._job_request_path(job_id, "inbox")
            if inbox is None:
                raise ProtocolViolation("queued AX workspace request disappeared")
            os.replace(inbox, self.v2 / "running" / inbox.name)
            return {
                "status": "blocked",
                "job_id": job_id,
                "actual_project_digest": expected_project_digest,
                "diagnostic": "AX worker failed to start in two bounded attempts",
            }
        return self.ensure_running(workspace_id, job_id)

    def wait_for_idle_shutdown(self, workspace_id: str) -> dict[str, Any] | None:
        """Keep the host alive until the bounded IDE and window guard stop."""

        validate_workspace_id(workspace_id)
        launches = []
        for path in (self.v2 / "state" / "launches").glob("*.json"):
            try:
                record = strict_json_loads_v2(path.read_bytes())
            except (OSError, ProtocolViolation):
                continue
            if record.get("worker_id") == workspace_id:
                launches.append((record.get("launched_at", ""), path, record))
        if not launches:
            raise ProtocolViolation("AX launch record is missing before artifact freeze")
        _started, _path, launch = max(launches, key=lambda item: (item[0], item[1].name))
        pid = launch.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            raise ProtocolViolation("AX launch PID is invalid before artifact freeze")
        guard_path = None
        if os.name == "nt":
            guard_relative = launch.get("window_guard_status")
            if not isinstance(guard_relative, str) or not guard_relative:
                raise ProtocolViolation("AX window-guard status path is missing")
            guard_path = (self.v2 / guard_relative).resolve()
            evidence_root = (self.v2 / "evidence").resolve()
            if guard_path.parent != evidence_root:
                raise ProtocolViolation("AX window-guard status path escapes evidence root")

        deadline = time.monotonic() + self.config["idle_exit_seconds"] + 15
        last_guard = None
        while time.monotonic() < deadline:
            if guard_path is not None and guard_path.is_file():
                last_guard = strict_json_loads_v2(guard_path.read_bytes())
                if (
                    last_guard.get("record_type") != "AxWindowGuard"
                    or last_guard.get("pid") != pid
                    or last_guard.get("status")
                    not in {"starting", "active", "stopped", "failed"}
                ):
                    raise ProtocolViolation("AX window-guard status is invalid")
                if last_guard["status"] == "failed":
                    raise ProtocolViolation("AX window-guard failed during IDE execution")
                if last_guard["status"] == "stopped":
                    # The independent guard uses a native process handle; its
                    # stopped state is stronger than os.kill(pid, 0), which can
                    # remain truthy for an unreaped Windows child handle.
                    return last_guard
            alive = self._process_alive(pid)
            if not alive:
                if guard_path is None:
                    return None
            time.sleep(0.05)
        raise ProtocolViolation(
            "AX IDE/window guard did not stop within the bounded artifact-freeze window"
        )

    def ensure_running(self, workspace_id: str, job_id: str) -> dict[str, Any]:
        """Start one unattended background IDE worker, or adopt the exact live lease.

        The method never waits for or fabricates an operation result.  The MCP
        host reconciles the worker's immutable result file separately.
        """

        validate_workspace_id(job_id)
        live = self._live_lease(workspace_id)
        if live is not None:
            adopted = self._adopt_live_worker(workspace_id, job_id, live)
            if adopted is not None:
                return adopted
        prior_launches = self._job_launch_records(workspace_id, job_id)
        launch_attempt = len(prior_launches) + 1
        if launch_attempt > _MAX_JOB_LAUNCH_ATTEMPTS:
            raise ProtocolViolation("AX worker launch attempts are exhausted")
        launch_label = (
            job_id if launch_attempt == 1 else f"{job_id}.attempt-{launch_attempt:02d}"
        )
        self._write_worker_config(workspace_id)
        executable = self.config["ide_executable"]
        worker = self.config["worker_script"]
        profile_argument = f"--profile=\"{self.config['ide_profile']}\""
        runscript_argument = f"--runscript=\"{worker}\""
        command: str | list[str]
        if os.name == "nt":
            # DIADesigner parses the raw Windows command line itself.  Passing
            # a list makes subprocess escape the inner profile quotes, which
            # the vendor parser then treats as a missing --profile option.
            # Delta 1.9's ``--noUI`` path leaves an OEM compiler service
            # uninitialized and every real build ends in a NullReference.  A
            # hidden full runtime executes the same bounded
            # ScriptEngine worker without user interaction and retains the
            # real compiler/configuration pipeline.
            command = (
                f'"{executable}" {profile_argument} {runscript_argument} '
                "--culture=en --textPrompts"
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
        environment["AXMCP_WORKER_ROOT"] = str(self.v2)
        environment["AXMCP_LADDER_HELPER_SHA256"] = self.config[
            "ladder_helper_sha256"
        ]
        log_path = self.v2 / "evidence" / f"{launch_label}.ax-worker.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        launched_at = utc_now()
        window_guard_status = self.v2 / "evidence" / f"{launch_label}.window-guard.json"
        record_path = self.v2 / "state" / "launches" / f"{launch_label}.json"
        record = {
            "schema_version": "1",
            "record_type": "AxWorkerLaunch",
            "job_id": job_id,
            "launch_attempt": launch_attempt,
            "worker_id": workspace_id,
            "pid": None,
            "launched_at": launched_at,
            "ide_executable_sha256": self.config["ide_executable_sha256"],
            "script_engine_sha256": self.config["script_engine_sha256"],
            "worker_script_sha256": self.config["worker_script_sha256"],
            "ide_profile": self.config["ide_profile"],
            "headless": True,
            "window_isolation": (
                "win32-continuous-hide" if os.name == "nt" else "vendor-noUI"
            ),
            "window_guard_status": (
                str(window_guard_status.relative_to(self.v2))
                if os.name == "nt"
                else None
            ),
            "window_guard_pid": None,
            "handoff_status": "launch_intent",
            "failure": None,
            "termination_error": None,
            "real_target_contacted": False,
        }
        # Publish intent before process creation. Any later disk/guard/cleanup
        # failure therefore leaves a durable fail-closed record and can never
        # produce an untracked second full-runtime IDE.
        atomic_write_exclusive(record_path, canonical_json_bytes(record))
        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
        try:
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
        except Exception as exc:
            record.update({"handoff_status": "spawn_failed", "failure": str(exc)})
            try:
                atomic_write_replace(record_path, canonical_json_bytes(record))
            except Exception as record_exc:
                raise ProtocolViolation(
                    f"AX spawn failed ({exc}); durable failure update also failed ({record_exc})"
                ) from exc
            raise

        def fail_process_handoff(phase: str, original: Exception) -> None:
            termination_error = None
            try:
                _terminate_launched_process(process)
            except Exception as exc:
                termination_error = exc
            record.update(
                {
                    "pid": process.pid,
                    "handoff_status": (
                        f"{phase}_termination_unknown"
                        if termination_error is not None
                        else f"{phase}_terminated"
                    ),
                    "failure": str(original),
                    "termination_error": (
                        str(termination_error) if termination_error is not None else None
                    ),
                }
            )
            publication_error = None
            try:
                atomic_write_replace(record_path, canonical_json_bytes(record))
            except Exception as exc:
                publication_error = exc
            if termination_error is not None or publication_error is not None:
                raise ProtocolViolation(
                    "AX %s failed (%s); termination=%s; durable update=%s"
                    % (
                        phase,
                        str(original),
                        str(termination_error) if termination_error is not None else "confirmed",
                        str(publication_error) if publication_error is not None else "recorded",
                    )
                ) from original
            raise original

        record.update({"pid": process.pid, "handoff_status": "process_started"})
        try:
            atomic_write_replace(record_path, canonical_json_bytes(record))
        except Exception as exc:
            fail_process_handoff("launch_record_failed", exc)
        window_guard_pid = None
        if os.name == "nt":
            try:
                window_guard_pid = self._window_guard(
                    process, self.config["max_runtime_seconds"], window_guard_status
                )
            except Exception as exc:
                fail_process_handoff("window_guard_start_failed", exc)
        record.update(
            {
                "window_guard_pid": window_guard_pid,
                "handoff_status": "ready",
            }
        )
        try:
            atomic_write_replace(record_path, canonical_json_bytes(record))
        except Exception as exc:
            fail_process_handoff("launch_record_handoff_failed", exc)
        return {
            "status": "launched",
            "worker_id": workspace_id,
            "pid": process.pid,
            "job_id": job_id,
            "launched_at": launched_at,
        }
