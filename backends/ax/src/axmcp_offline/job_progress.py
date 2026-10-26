"""Read-only job progress assembled from durable queue and worker evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .protocol_v2 import load_v2, validate_deployment_journal_v1, validate_request_v2
from .security import ProtocolViolation, parse_utc, sha256_bytes, strict_json_loads_v2, utc_now


def read_job_progress(queue: Any, job_id: str) -> dict[str, Any]:
    """Read existing evidence only; never recover, launch, or contact a target."""

    status = queue.status(job_id)
    path = queue._matching_request(job_id)
    if path is None:
        raise ProtocolViolation("job request evidence is missing")
    request, request_raw = load_v2(path)
    validate_request_v2(request, queue.root)
    if request["job_id"] != job_id:
        raise ProtocolViolation("job progress request binding mismatch")
    now = datetime.now(UTC)
    observed_at = utc_now()
    created = parse_utc(request["created_at"])
    deadline = parse_utc(request["deadline_at"])
    source = "queue"
    stage = status["state"]
    last_real_progress: dict[str, str] | None = None
    uncertainty = status["state"] == "unknown_outcome"
    budget_scope = "workflow"

    if request["operation"] == "project.build":
        intent_path = queue.root / "state" / "build-transactions" / job_id / "intent.json"
        if intent_path.is_file():
            intent = strict_json_loads_v2(intent_path.read_bytes())
            if (
                not isinstance(intent, dict)
                or intent.get("job_id") != job_id
                or intent.get("operation") != "project.build"
                or intent.get("request_digest") != sha256_bytes(request_raw)
            ):
                raise ProtocolViolation("build progress intent binding mismatch")
            parse_utc(intent["created_at"])
            source = "build_transaction"
            stage = "build-transaction-started"
            last_real_progress = {
                "event": stage,
                "observed_at": intent["created_at"],
                "source": source,
            }

    if request["operation"] == "deployment.execute":
        journal_dir = queue.root / "state" / "deployments" / job_id
        if journal_dir.is_dir():
            events = sorted(journal_dir.glob("*.json"))
            if events:
                validate_deployment_journal_v1(events, job_id=job_id)
                records = [strict_json_loads_v2(item.read_bytes()) for item in events]
                latest = records[-1]
                source = "deployment_journal"
                stage = latest["event"]
                last_real_progress = {
                    "event": stage,
                    "observed_at": latest["created_at"],
                    "source": source,
                }
                pending_transfer = False
                for record in records:
                    event = record["event"]
                    if event.endswith("-full-download-intent"):
                        pending_transfer = True
                    elif event.endswith("-download-initialization-complete"):
                        pending_transfer = False
                    if event in {"candidate-watch-started", "rollback-budget-started"}:
                        candidate_deadline = record["details"].get("deadline_at")
                        if not isinstance(candidate_deadline, str):
                            raise ProtocolViolation("progress stage deadline is missing")
                        deadline = parse_utc(candidate_deadline)
                        budget_scope = "watch" if event == "candidate-watch-started" else "rollback"
                uncertainty = uncertainty or pending_transfer

    remaining = max(0, int((deadline - now).total_seconds()))
    result = {
        "schema_version": "1",
        "record_type": "JobProgress",
        "job_id": job_id,
        "operation": request["operation"],
        "status": status["state"],
        "transport_profile": request.get("transport_profile", "lan"),
        "stage": stage,
        "observed_at": observed_at,
        "elapsed_seconds": max(0, int((now - created).total_seconds())),
        "remaining_seconds": remaining,
        "budget_scope": budget_scope,
        "status_interval_seconds": 30,
        "warning": request.get("transport_profile") == "diacloud"
        and status["state"] in {"queued", "running"}
        and budget_scope == "workflow"
        and remaining <= 120,
        "last_real_progress": last_real_progress,
        "source": source,
        "uncertainty": uncertainty,
        # AX/SP18 has no verified byte-progress API in this adapter.
        "bytes_transferred": None,
        "bytes_total": None,
        "percent": None,
    }
    return validate_job_progress(result)


def validate_job_progress(value: dict[str, Any]) -> dict[str, Any]:
    expected = {
        "schema_version", "record_type", "job_id", "operation", "status",
        "transport_profile", "stage", "observed_at", "elapsed_seconds",
        "remaining_seconds", "budget_scope", "status_interval_seconds", "warning",
        "last_real_progress", "source", "uncertainty", "bytes_transferred",
        "bytes_total", "percent",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ProtocolViolation("JobProgress shape mismatch")
    if value["schema_version"] != "1" or value["record_type"] != "JobProgress":
        raise ProtocolViolation("JobProgress version mismatch")
    parse_utc(value["observed_at"])
    if value["transport_profile"] not in {"lan", "diacloud"}:
        raise ProtocolViolation("JobProgress transport profile is invalid")
    if value["budget_scope"] not in {"workflow", "watch", "rollback"}:
        raise ProtocolViolation("JobProgress budget scope is invalid")
    for field in ("elapsed_seconds", "remaining_seconds", "status_interval_seconds"):
        if isinstance(value[field], bool) or not isinstance(value[field], int) or value[field] < 0:
            raise ProtocolViolation("JobProgress duration is invalid")
    for field in ("warning", "uncertainty"):
        if not isinstance(value[field], bool):
            raise ProtocolViolation("JobProgress boolean is invalid")
    if any(value[field] is not None for field in ("bytes_transferred", "bytes_total", "percent")):
        raise ProtocolViolation("JobProgress byte/percent claims require vendor evidence")
    progress = value["last_real_progress"]
    if progress is not None:
        if not isinstance(progress, dict) or set(progress) != {"event", "observed_at", "source"}:
            raise ProtocolViolation("JobProgress real progress shape is invalid")
        parse_utc(progress["observed_at"])
    return value
