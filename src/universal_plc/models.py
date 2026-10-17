from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any


class PLCError(ValueError):
    """A bounded, actionable rejection; never a successful PLC result."""


@dataclass(frozen=True)
class TargetProfile:
    vendor: str
    family: str
    model: str
    firmware: str | None = None
    toolchain: str | None = None
    ide_version: str | None = None
    project_format: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TargetProfile:
        if not isinstance(value, dict):
            raise PLCError("target must be an object")
        unknown = set(value) - set(cls.__dataclass_fields__)
        if unknown:
            raise PLCError(f"Unknown target fields: {sorted(unknown)}")
        for key in ("vendor", "family", "model"):
            if not isinstance(value.get(key), str) or not value[key].strip():
                raise PLCError(f"Exact target {key} is required")
        for key, item in value.items():
            if item is not None and not isinstance(item, str):
                raise PLCError(f"target.{key} must be text")
        normalized = {k: v.strip() if isinstance(v, str) else v for k, v in value.items()}
        normalized["vendor"] = normalized["vendor"].casefold()
        normalized["family"] = normalized["family"].upper()
        normalized["model"] = normalized["model"].upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9_. -]{1,79}", normalized["model"]):
            raise PLCError("Invalid model identifier")
        if normalized.get("toolchain"):
            normalized["toolchain"] = normalized["toolchain"].casefold()
        if normalized.get("project_format"):
            normalized["project_format"] = "." + normalized["project_format"].lstrip(".").lower()
        return cls(**normalized)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
