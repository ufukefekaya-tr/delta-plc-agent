from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from .constants import FILE_BRIDGE_MODE
from .records import (
    load_strict_json,
    make_request,
    request_bytes,
    validate_chain,
    validate_operation,
)
from .security import (
    ProtocolViolation,
    atomic_write_exclusive,
    sha256_file,
    utc_now,
    validate_artifact_path,
    validate_digest,
)

_CONFIG_KEYS = {"config_version", "environment_digest", "source_target", "export_path"}


def initialize_scratch(root: Path) -> Path:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    for child in ("requests", "results", "evidence", "inputs", "outputs"):
        (root / child).mkdir(exist_ok=True)
    config_path = root / "probe-config.json"
    if not config_path.exists():
        config = {
            "config_version": "1",
            "environment_digest": "sha256:" + ("0" * 64),
            "source_target": "PLC_PRG",
            "export_path": "outputs/project.export",
        }
        atomic_write_exclusive(config_path, (json.dumps(config, indent=2) + "\n").encode("utf-8"))
    return config_path


class FileBridgeBackend:
    def __init__(self, scratch_root: Path, timeout_seconds: int = 3) -> None:
        if timeout_seconds < 1 or timeout_seconds > 30:
            raise ProtocolViolation("bridge timeout must be between 1 and 30 seconds")
        self.root = scratch_root.resolve()
        if not self.root.is_dir():
            raise ProtocolViolation("scratch root does not exist")
        self.timeout_seconds = timeout_seconds

    def _load_config(self) -> dict[str, str]:
        path = self.root / "probe-config.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolViolation("invalid probe-config.json") from exc
        if not isinstance(value, dict) or set(value) != _CONFIG_KEYS:
            raise ProtocolViolation("probe config has unknown or missing fields")
        if value["config_version"] != "1":
            raise ProtocolViolation("unsupported probe config version")
        validate_digest(value["environment_digest"])
        if value["environment_digest"] == "sha256:" + ("0" * 64):
            raise ProtocolViolation("probe config environment digest is still the placeholder")
        if not isinstance(value["source_target"], str) or not value["source_target"]:
            raise ProtocolViolation("source_target must be non-empty")
        validate_artifact_path(value["export_path"], self.root)
        return value

    def execute(self, operation: str) -> dict[str, Any]:
        operation = validate_operation(operation)
        config = self._load_config()
        config_path = self.root / "probe-config.json"
        descriptor = {
            "path": "probe-config.json",
            "sha256": sha256_file(config_path),
        }
        correlation_id = str(uuid.uuid4())
        request = make_request(
            correlation_id=correlation_id,
            operation=operation,
            created_at=utc_now(),
            environment_digest_value=config["environment_digest"],
            artifacts=[descriptor],
            timeout_seconds=self.timeout_seconds,
            scratch_root=self.root,
        )
        raw_request = request_bytes(request, self.root)
        request_path = self.root / "requests" / f"{correlation_id}.request.json"
        atomic_write_exclusive(request_path, raw_request)

        result_path = self.root / "results" / f"{correlation_id}.result.json"
        evidence_path = self.root / "evidence" / f"{correlation_id}.evidence.json"
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            if result_path.exists() and evidence_path.exists():
                break
            time.sleep(0.05)
        if not result_path.exists() or not evidence_path.exists():
            return {
                "operation": operation,
                "status": "blocked",
                "mode": FILE_BRIDGE_MODE,
                "evidence_class": "STATIC_CHECKED",
                "live_ax_verified": False,
                "environment_digest": config["environment_digest"],
                "correlation_id": correlation_id,
                "request_path": f"requests/{request_path.name}",
                "diagnostics": ["Bounded timeout waiting for result and evidence."],
                "limitations": ["No vendor-side result was received."],
            }

        result, raw_result = load_strict_json(result_path)
        evidence, _ = load_strict_json(evidence_path)
        validate_chain(request, raw_request, result, raw_result, evidence, self.root)
        live_ax_verified = (
            result["status"] == "verified"
            and evidence["producer"] == "human"
            and evidence["evidence_class"]
            in {"HUMAN_IDE_COMPILED", "HUMAN_BENCH_VERIFIED", "HUMAN_FAT_ACCEPTED"}
        )
        return {
            "operation": operation,
            "status": result["status"],
            "mode": FILE_BRIDGE_MODE,
            "evidence_class": evidence["evidence_class"],
            "live_ax_verified": live_ax_verified,
            "environment_digest": result["environment_digest"],
            "correlation_id": correlation_id,
            "artifacts": result["artifacts"],
            "diagnostics": result["diagnostics"],
            "limitations": [],
        }
