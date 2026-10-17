from __future__ import annotations

import copy
import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from .constants import ALLOWED_OPERATIONS, REPLAY_MODE
from .records import environment_digest, validate_capability_result, validate_operation
from .security import ProtocolViolation

_BUNDLE_KEYS = {
    "bundle_version",
    "evidence_class",
    "live_ax_verified",
    "environment",
    "environment_digest",
    "operations",
}
_OPERATION_KEYS = {"capability", "payload"}


class ReplayBackend:
    def __init__(self, bundle: dict[str, Any]) -> None:
        if not isinstance(bundle, dict) or set(bundle) != _BUNDLE_KEYS:
            raise ProtocolViolation("replay bundle has unknown or missing fields")
        if bundle["bundle_version"] != "1":
            raise ProtocolViolation("unsupported replay bundle version")
        if bundle["evidence_class"] != "SIMULATED" or bundle["live_ax_verified"] is not False:
            raise ProtocolViolation("built-in replay must be explicitly simulated and not live verified")
        actual_environment_digest = environment_digest(bundle["environment"])
        if bundle["environment_digest"] != actual_environment_digest:
            raise ProtocolViolation("replay environment digest mismatch")
        operations = bundle["operations"]
        if not isinstance(operations, dict) or tuple(operations) != ALLOWED_OPERATIONS:
            raise ProtocolViolation("replay operation registry must be the exact ordered allowlist")
        for operation, entry in operations.items():
            validate_operation(operation)
            if not isinstance(entry, dict) or set(entry) != _OPERATION_KEYS:
                raise ProtocolViolation(f"invalid replay entry for {operation}")
            capability = validate_capability_result(entry["capability"])
            if capability["operation"] != operation:
                raise ProtocolViolation("replay capability operation mismatch")
            if capability["environment_digest"] != actual_environment_digest:
                raise ProtocolViolation("replay capability environment mismatch")
            if capability["status"] != "unverified" or capability["mode"] != REPLAY_MODE:
                raise ProtocolViolation("simulated replay capability must remain unverified")
        self._bundle = copy.deepcopy(bundle)

    @classmethod
    def from_path(cls, path: Path) -> "ReplayBackend":
        try:
            bundle = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolViolation("unable to load replay bundle") from exc
        return cls(bundle)

    @classmethod
    def from_default(cls) -> "ReplayBackend":
        path = files("axmcp_offline").joinpath("data/simulated_replay_bundle.json")
        try:
            bundle = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolViolation("unable to load built-in replay bundle") from exc
        return cls(bundle)

    @property
    def environment_digest(self) -> str:
        return self._bundle["environment_digest"]

    def execute(self, operation: str) -> dict[str, Any]:
        operation = validate_operation(operation)
        entry = copy.deepcopy(self._bundle["operations"][operation])
        return {
            "operation": operation,
            "status": "unverified",
            "mode": REPLAY_MODE,
            "evidence_class": "SIMULATED",
            "live_ax_verified": False,
            "environment_digest": self.environment_digest,
            "capability": entry["capability"],
            "payload": entry["payload"],
            "limitations": [
                "Recorded replay proves package behavior only.",
                "No vendor IDE, exact AX runtime or PLC was contacted.",
            ],
        }
