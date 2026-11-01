"""One offline native revision through public MCP; print the file before building.

Uses an already identity-pinned private root. No online/deployment operations.
The request JSON supplies baseline/environment digests, idempotency_key and the
typed ST batch or qualified same-interface single-contact LD revision. Optional request_received_unix
must be an observed timestamp, not a benchmark or estimated time. Replay requires
the ORIGINAL workspace_id, expected_revision, key and unchanged payload. Omitted
workspace/revision fields are first-call conveniences, NOT automatic recovery.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time

from mcp import Client, StdioServerParameters
from axmcp_offline.ax_supervisor import AxWorkerSupervisor
from axmcp_offline.autonomous import AutonomousHost

IMPORT_ROOT = Path(__file__).resolve().parent.parent
TERMINAL = {"succeeded", "failed", "blocked", "cancelled", "unknown_outcome"}


def emit(event: str, **values: object) -> None:
    print(json.dumps({"event": event, "unix": time.time(), **values},
                     ensure_ascii=False), flush=True)


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def offline_root(root: Path) -> dict:
    config = json.loads((root / "autonomous-config.json").read_text(encoding="utf-8"))
    if config.get("target_allowlist") != []:
        raise ValueError("Revision runner requires an empty target allowlist")
    return config


def client_parameters(root: Path) -> StdioServerParameters:
    offline_root(root)
    env = {**os.environ, "PYTHONPATH": str(IMPORT_ROOT),
           "AXMCP_MODE": "autonomous_file_bridge",
           "AXMCP_SCRATCH_ROOT": str(root),
           "AXMCP_AUTOSTART_WORKER": "1",
           "AXMCP_AUTOSTART_DEPLOYMENT_WORKER": "0"}
    return StdioServerParameters(command=sys.executable,
                                args=["-m", "axmcp_offline", "serve"],
                                cwd=root, env=env)


async def call(client: Client, operation: str, arguments: dict) -> dict:
    allowed = {"workspace.create", "workspace.status", "revision.apply",
               "candidate.snapshot", "job.status", "project.build",
               "artifact.freeze", "ladder.networks.apply"}
    if operation not in allowed:
        raise ValueError("Runner operation is not offline allowlisted")
    result = await client.call_tool(operation, arguments)
    if result.is_error:
        raise RuntimeError(f"{operation}: {result.content}")
    value = result.structured_content
    if not isinstance(value, dict):
        raise RuntimeError(f"{operation}: missing structured result")
    return value


async def terminal(client: Client, queued: dict, timeout: float) -> dict:
    job = queued["job_id"]
    deadline, previous, last_report = time.monotonic() + timeout, None, 0.0
    while True:
        result = await call(client, "job.status", {"job_id": job})
        status = result["status"]
        if status != previous or time.monotonic() - last_report >= 30:
            emit("job", job_id=job, status=status)
            previous, last_report = status, time.monotonic()
        if status in TERMINAL:
            if status != "succeeded":
                raise RuntimeError(json.dumps(result, ensure_ascii=False))
            return result
        if time.monotonic() >= deadline:
            # A timeout is not proof of cancellation. Do not resubmit this job.
            raise TimeoutError(f"Job {job} outcome unresolved; inspect same job.status")
        await asyncio.sleep(0.5)


async def revision(client: Client, root: Path, request: dict,
                   timeout: float = 300) -> dict:
    """Patch-ready → verified candidate; caller records pre-patch work separately."""
    started = time.monotonic()
    request_time = request.get("request_received_unix")
    if request_time is not None and (
        isinstance(request_time, bool) or not isinstance(request_time, (int, float))
        or not math.isfinite(request_time)
        or request_time > time.time() or request_time <= 0
    ):
        raise ValueError("Invalid observed request timestamp")
    config = offline_root(root)
    if config["environment_digest"] != request["environment_digest"]:
        raise ValueError("Request environment does not match private root")
    workspace_id = request.get("workspace_id")
    if workspace_id is None:
        workspace = await call(client, "workspace.create", {
            "baseline_sha256": request["baseline_sha256"],
            "environment_digest": request["environment_digest"]})
        workspace_id = workspace["workspace_id"]
        emit("workspace_created", workspace_id=workspace_id, revision=workspace["revision"])
    else:
        workspace = await call(client, "workspace.status", {"workspace_id": workspace_id})
    if workspace["baseline_project_digest"] != request["baseline_sha256"]:
        raise ValueError("Workspace baseline differs from request")
    expected = request.get("expected_revision", workspace["revision"])
    emit("revision_bound", workspace_id=workspace_id, expected_revision=expected,
         idempotency_key=request["idempotency_key"])
    queued = await call(client, "revision.apply", {
        "workspace_id": workspace_id, "expected_revision": expected,
        "idempotency_key": request["idempotency_key"],
        "st_changes": request.get("st_changes", []),
        "ladder_changes": request.get("ladder_changes", [])})
    outcome = await terminal(client, queued, timeout)
    candidate = await call(client, "candidate.snapshot", {
        "workspace_id": workspace_id, "expected_revision": expected + 1})
    native = (root / "v2" / candidate["path"]).resolve()
    candidate_root = (root / "v2" / "native_candidates").resolve()
    if native.parent != candidate_root or digest(native) != candidate["project_digest"]:
        raise RuntimeError("Published native path/digest mismatch")
    if candidate["status"] != "NATIVE_SAVED_UNCOMPILED" or candidate["deployable"] is not False:
        raise RuntimeError("Uncompiled candidate status contract mismatch")
    seconds = time.monotonic() - started
    result = {"candidate": candidate, "native_path": str(native),
              "workspace_id": workspace_id, "revision": expected + 1,
              "tool_seconds": seconds,
              "request_to_native_seconds": None if request_time is None else time.time() - request_time,
              "request_timing_status": "NOT_MEASURED" if request_time is None else "OBSERVED",
              "worker_launch": queued.get("worker"), "job": outcome,
              "real_target_contacted": False}
    emit("native_ready", path=str(native), project_digest=candidate["project_digest"],
         status=candidate["status"], tool_seconds=seconds,
         request_to_native_seconds=result["request_to_native_seconds"])
    return result


async def run(args: argparse.Namespace) -> dict:
    request = json.loads(args.request.read_text(encoding="utf-8"))
    process_start = time.monotonic()
    result = {"status": "FAILED", "real_target_contacted": False}
    async with Client(client_parameters(args.root), read_timeout_seconds=600) as client:
        workspace_id = request.get("workspace_id")
        try:
            if workspace_id is None:
                workspace = await call(client, "workspace.create", {
                    "baseline_sha256": request["baseline_sha256"],
                    "environment_digest": request["environment_digest"]})
                workspace_id = workspace["workspace_id"]
                request["workspace_id"] = workspace_id
                emit("workspace_created", workspace_id=workspace_id, revision=workspace["revision"])
            result = await revision(client, args.root, request, args.timeout)
            result["process_to_native_seconds"] = time.monotonic() - process_start
            result["status"] = "COMPLETED"
            if args.build:
                start = time.monotonic()
                try:
                    # A new transaction carries project bytes, not a verified
                    # compiler cache. Never advertise incremental eligibility
                    # based only on the caller's switch or a Git commit.
                    result["build_mode"] = "rebuild"
                    result["incremental_cache"] = "NOT_QUALIFIED"
                    queued = await call(client, "project.build", {
                        "workspace_id": workspace_id,
                        "expected_revision": result["revision"],
                        "mode": "rebuild",
                        "max_attempts": 1})
                    result["build"] = await terminal(client, queued, args.timeout)
                    result["compiled_artifact"] = await call(client, "artifact.freeze", {
                        "workspace_id": workspace_id, "expected_revision": result["revision"]})
                    if digest(Path(result["native_path"])) != result["candidate"]["project_digest"]:
                        raise RuntimeError("Build modified previously shared candidate")
                finally:
                    result["build_seconds"] = time.monotonic() - start
                emit("build_finished", seconds=result["build_seconds"],
                     artifact=result["compiled_artifact"])
        except Exception as exc:
            result.update(status="FAILED", failure=str(exc), workspace_id=workspace_id)
        finally:
            if workspace_id:
                try:
                    result["shutdown"] = await close_workspace(args.root, workspace_id)
                except Exception as exc:
                    result.update(status="FAILED", shutdown_error=str(exc))
    return result


async def close_workspace(root: Path, workspace_id: str) -> dict:
    launches = root / "v2" / "state" / "launches"
    if not any(json.loads(path.read_text(encoding="utf-8")).get("worker_id") == workspace_id
               for path in launches.glob("*.json")):
        return {"status": "not_started"}
    supervisor = AxWorkerSupervisor(root)
    # Pending/unknown jobs are not force-cancelled. Keep the client/process group
    # alive until this exact worker and its independent guard have closed.
    def close_fenced():
        # Same lock order as artifact.freeze; exclude enqueue and replacement
        # launch until exact worker and independent guard closure are confirmed.
        with AutonomousHost(root)._workspace_fence(workspace_id), supervisor.launcher_fence():
            requested = supervisor.request_shutdown(workspace_id)
            guard = supervisor.wait_for_idle_shutdown(workspace_id)
            return {"request": requested, "guard": guard}
    return await asyncio.to_thread(close_fenced)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--build", choices=("incremental", "rebuild"))
    args = parser.parse_args()
    args.root = args.root.resolve()
    if not 1 <= args.timeout <= 600 or args.result.exists():
        parser.error("Timeout must be 1..600 and result must be a new path")
    try:
        result = asyncio.run(run(args))
    except Exception as exc:
        emit("failed", error=str(exc), real_target_contacted=False)
        return 1
    args.result.parent.mkdir(parents=True, exist_ok=True)
    with args.result.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    return 0 if result.get("status") == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
