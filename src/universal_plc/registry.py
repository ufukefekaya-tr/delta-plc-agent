from __future__ import annotations

from importlib.resources import files
import json
from pathlib import Path
import re
from typing import Any

from .models import PLCError, TargetProfile


class Registry:
    """A data-driven selector. A catalog match is not native/hardware qualification."""

    def __init__(self, document: dict[str, Any] | None = None):
        self.document = document or json.loads(files("universal_plc").joinpath("data/catalog.json").read_text("utf-8"))
        self.adapters = self.document["adapters"]
        self.rules = self.document["rules"]
        for rule in self.rules:
            re.compile(rule["model_pattern"])
            if any(a not in self.adapters for a in rule["adapters"]):
                raise PLCError("Catalog references an unknown adapter")

    def resolve(self, target: TargetProfile, operation: str | None = None,
                project_target: dict[str, Any] | None = None) -> dict[str, Any]:
        if project_target:
            for key in ("vendor", "family", "model", "firmware", "toolchain", "ide_version", "project_format"):
                actual = project_target.get(key)
                if actual and str(actual).casefold() != str(getattr(target, key)).casefold():
                    raise PLCError(f"Project/target conflict: {key}")
        matches = [r for r in self.rules if r["vendor"] == target.vendor
                   and r["family"] == target.family
                   and re.fullmatch(r["model_pattern"], target.model)]
        if not matches:
            raise PLCError("MODEL_NOT_CATALOGUED: provide a reviewed exact model/tool mapping; no nearest-model fallback")
        if len(matches) != 1:
            raise PLCError("AMBIGUOUS_MODEL_MAPPING: catalog rules overlap")
        rule = matches[0]
        if "exact_models" in rule and target.model not in rule["exact_models"]:
            raise PLCError("MODEL_NOT_CATALOGUED: family pattern matches but exact model has no reviewed entry")
        candidates = list(rule["adapters"])
        if target.toolchain:
            candidates = [a for a in candidates if self.adapters[a]["toolchain"] == target.toolchain]
        if target.project_format:
            candidates = [a for a in candidates if target.project_format in self.adapters[a]["formats"]]
        if operation:
            capable = [a for a in candidates if operation in self.adapters[a].get("file_operations", [])
                       or operation in self.adapters[a].get("native_operations", [])]
            if capable:
                candidates = capable
            else:
                raise PLCError(f"OPERATION_NOT_MAPPED: {operation}; no automatic conversion or vendor substitution")
        if not candidates:
            raise PLCError("INCOMPATIBLE_TOOLCHAIN_OR_FORMAT")
        selected = candidates[0] if len(candidates) == 1 else None
        return {"target": target.to_dict(), "adapter_id": selected, "candidates": candidates,
                "mapping_id": rule["id"], "mapping_evidence": rule["evidence"],
                "qualification": "catalog_mapping_only", "native_verified": False,
                "requires_selection": selected is None,
                "mapping_uncertainties": rule.get("mapping_uncertainties", []),
                "note": "Firmware, installed toolchain and operation qualification are checked separately."}

    @classmethod
    def from_path(cls, path: Path) -> Registry:
        return cls(json.loads(path.read_text("utf-8")))
