"""Bounded native project creation jobs; local IDE only, never a PLC connection."""
from __future__ import annotations
import argparse
from datetime import datetime, UTC
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from .security import ProtocolViolation, atomic_write_exclusive, canonical_json_bytes, sha256_file, strict_json_loads_v2, validate_digest

_FIELDS = {"ide_executable", "ide_executable_sha256", "script_engine", "script_engine_sha256", "ide_profile", "output_root", "timeout_seconds", "device_profile"}
_ARGS = {"project_id", "controller_name", "pou_name", "task_name", "interval_ms", "declaration", "implementation"}
_JOB = re.compile(r"^create_[0-9a-f]{32}$")


def _config(config_path: Path) -> tuple[dict, dict, Path]:
    adapter = strict_json_loads_v2(config_path.read_bytes())
    config = adapter.get("project_factory")
    if not isinstance(config, dict) or set(config) != _FIELDS:
        raise ProtocolViolation("project_factory configuration fields are not exact")
    target = adapter.get("target")
    profile = config["device_profile"]
    if not isinstance(profile, dict) or set(profile) != {"profile_id", "model", "device_type", "device_id", "device_version"}:
        raise ProtocolViolation("versioned device profile required")
    normalize = lambda value: str(value).upper().replace("-", "").replace(" ", "")
    if not isinstance(target, dict) or normalize(profile["model"]) != normalize(target.get("model")):
        raise ProtocolViolation("project factory model differs from configured target")
    for key in ("ide_executable", "script_engine"):
        path = Path(config[key])
        if not path.is_absolute():
            path = config_path.parent / path
        path = path.resolve(strict=True)
        validate_digest(config[key + "_sha256"])
        if not path.is_file() or '"' in str(path) or sha256_file(path) != config[key + "_sha256"]:
            raise ProtocolViolation("project factory tool identity changed: " + key)
        config[key] = str(path)
    if not isinstance(config["ide_profile"], str) or not config["ide_profile"] or any(c in config["ide_profile"] for c in ('"', '\n', '\r', '\x00')):
        raise ProtocolViolation("invalid IDE profile")
    if type(config["timeout_seconds"]) is not int or not 10 <= config["timeout_seconds"] <= 600:
        raise ProtocolViolation("project factory timeout must be 10..600 seconds")
    root = Path(config["output_root"])
    if not root.is_absolute():
        root = config_path.parent / root
    if root.is_symlink():
        raise ProtocolViolation("project factory output root cannot be a symlink")
    root = root.resolve()
    return adapter, config, root


def _worker_file(name: str) -> Path:
    path = Path(str(files("axmcp_offline").joinpath("ax_worker", name)))
    if not path.is_file():
        path = Path(__file__).resolve().parents[2] / "adapters" / "ax_worker" / name
    if not path.is_file() or path.is_symlink():
        raise ProtocolViolation("packaged project factory is unavailable")
    return path.resolve()


def _hidden_options() -> dict:
    if os.name != "nt":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return {"startupinfo": startup, "creationflags": subprocess.CREATE_NO_WINDOW}


def submit(config_path: Path, arguments: dict) -> dict:
    _, config, root = _config(config_path)
    if set(arguments) != _ARGS:
        raise ProtocolViolation("project.create arguments are not exact")
    for key in ("project_id", "controller_name", "pou_name", "task_name"):
        if not isinstance(arguments[key], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", arguments[key]):
            raise ProtocolViolation("invalid project.create identifier: " + key)
    for key in ("declaration", "implementation"):
        if not isinstance(arguments[key], str) or len(arguments[key]) > 262144 or "\x00" in arguments[key]:
            raise ProtocolViolation("invalid project.create source")
    if type(arguments["interval_ms"]) is not int or not 1 <= arguments["interval_ms"] <= 1000:
        raise ProtocolViolation("invalid project.create task interval")
    job_id = "create_" + uuid.uuid4().hex
    job = root / job_id
    root.mkdir(parents=True, exist_ok=True)
    job.mkdir(exist_ok=False)
    create = dict(arguments, output_path=str(job / (arguments["project_id"] + ".project")), device_profile=config["device_profile"])
    request = {"job_id": job_id, "operation": "project.create", "created_at": datetime.now(UTC).isoformat(), "create": create,
               "factory_sha256": sha256_file(_worker_file("project_factory.py")), "runner_sha256": sha256_file(_worker_file("project_factory_runner.py")), "launcher": config}
    request_path = job / "request.json"
    atomic_write_exclusive(request_path, canonical_json_bytes(request))
    environment = os.environ.copy()
    # Development resolves only this self-contained backend; installed wheels do
    # not need a source checkout. Never use the historical source worktree.
    package_parent = str(Path(__file__).resolve().parent.parent)
    environment["PYTHONPATH"] = package_parent
    try:
        with (job / "supervisor.log").open("xb") as log:
            subprocess.Popen([sys.executable, "-m", "axmcp_offline.project_creation", "--request", str(request_path), "--digest", sha256_file(request_path)],
                             cwd=job, env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True, **_hidden_options())
    except Exception as exc:
        atomic_write_exclusive(job / "result.json", canonical_json_bytes({"job_id": job_id, "operation": "project.create", "request_digest": sha256_file(request_path), "status": "failed", "error": "supervisor launch failed: " + str(exc), "live_verified": False}))
        raise ProtocolViolation("project factory supervisor failed to launch") from exc
    return {"job_id": job_id, "operation": "project.create", "status": "queued", "live_verified": False, "compile_verified": False}


def status(config_path: Path, job_id: str) -> dict:
    if not isinstance(job_id, str) or not _JOB.fullmatch(job_id):
        raise ProtocolViolation("invalid project creation job id")
    _, _, root = _config(config_path)
    job = root / job_id
    if job.is_symlink() or not job.is_dir():
        raise ProtocolViolation("unknown project creation job")
    request = strict_json_loads_v2((job / "request.json").read_bytes())
    result_path = job / "result.json"
    if not result_path.exists():
        started = datetime.fromisoformat(request["created_at"])
        if (datetime.now(UTC) - started).total_seconds() > request["launcher"]["timeout_seconds"] + 45:
            return {"job_id": job_id, "operation": "project.create", "status": "unknown_outcome", "error": "supervisor terminal record missing after deadline; inspect retained scratch", "live_verified": False}
        return {"job_id": job_id, "operation": "project.create", "status": "running", "live_verified": False}
    result = strict_json_loads_v2(result_path.read_bytes())
    if result.get("job_id") != job_id:
        raise ProtocolViolation("project creation result job mismatch")
    if result.get("request_digest") != sha256_file(job / "request.json"):
        raise ProtocolViolation("project creation result request digest mismatch")
    if result.get("status") == "created":
        output = Path(request["create"]["output_path"])
        if output.parent != job or output.is_symlink() or sha256_file(output) != result.get("project_sha256"):
            raise ProtocolViolation("created project bytes drifted")
    return result


def cancel(config_path: Path, job_id: str) -> dict:
    current = status(config_path, job_id)
    if current["status"] != "running":
        return current
    _, _, root = _config(config_path)
    marker = root / job_id / "cancel.json"
    if not marker.exists():
        atomic_write_exclusive(marker, canonical_json_bytes({"job_id": job_id, "cancel_requested": True}))
    return {"job_id": job_id, "operation": "project.create", "status": "cancel_requested", "live_verified": False}


def supervise(request_path: Path, digest: str) -> None:
    validate_digest(digest)
    if sha256_file(request_path) != digest:
        raise ProtocolViolation("creation request digest mismatch")
    request = strict_json_loads_v2(request_path.read_bytes())
    config, job = request["launcher"], request_path.parent
    result_path = job / "result.json"
    runner = _worker_file("project_factory_runner.py")
    environment = os.environ.copy()
    environment.update(AXMCP_CREATE_REQUEST=str(request_path), AXMCP_CREATE_RESULT=str(job / "vendor-result.json"), AXMCP_CREATE_REQUEST_SHA256=digest)
    command = '"{0}" --profile="{1}" --runscript="{2}" --culture=en --textPrompts'.format(config["ide_executable"], config["ide_profile"], runner)
    process = None
    result = {"operation": "project.create", "job_id": request["job_id"], "request_digest": digest, "status": "failed", "live_verified": False, "compile_verified": False}
    try:
        for path, expected in ((Path(config["ide_executable"]), config["ide_executable_sha256"]), (Path(config["script_engine"]), config["script_engine_sha256"]), (runner, request["runner_sha256"]), (_worker_file("project_factory.py"), request["factory_sha256"])):
            if sha256_file(path) != expected:
                raise ProtocolViolation("creation tool identity drift before launch")
        if os.name != "nt":
            raise ProtocolViolation("native AX project factory requires the vendor Windows runtime")
        if (job / "cancel.json").exists():
            result["status"] = "cancelled"
            return
        with (job / "ide.log").open("xb") as log:
            process = subprocess.Popen(command, cwd=job, env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=log, shell=False, **_hidden_options())
            from .ax_supervisor import _start_hidden_window_guard
            _start_hidden_window_guard(process, config["timeout_seconds"], job / "window-guard.json")
            deadline = time.monotonic() + config["timeout_seconds"]
            while process.poll() is None:
                if (job / "cancel.json").exists():
                    process.terminate()
                    process.wait(timeout=10)
                    result["status"] = "cancelled"
                    return
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(command, config["timeout_seconds"])
                try:
                    process.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    pass
        vendor = strict_json_loads_v2((job / "vendor-result.json").read_bytes())
        if vendor.get("job_id") != request["job_id"] or vendor.get("request_digest") != digest:
            raise ProtocolViolation("vendor project creation result binding mismatch")
        result = vendor
    except subprocess.TimeoutExpired:
        # This process owns only an offline newly created scratch project. No
        # online/transfer worker is ever terminated by this supervisor.
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        result.update(status="unknown_outcome", error="offline IDE deadline expired; inspect retained scratch and do not overwrite")
    except Exception as exc:
        result["error"] = str(exc)
        if process is not None and process.poll() is None:
            process.terminate()
    finally:
        atomic_write_exclusive(result_path, canonical_json_bytes(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--digest", required=True)
    args = parser.parse_args()
    supervise(args.request, args.digest)
