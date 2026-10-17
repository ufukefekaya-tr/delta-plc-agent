from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .storage import digest


PATTERNS = {
    "ispsoft": ["Delta Industrial Automation/ISPSoft */NewISPSoft.exe"],
    "wplsoft": ["Delta Industrial Automation/WPLSoft */WPLSoft.exe"],
    "diadesigner": ["Delta Industrial Automation/DIAStudio/DIADesigner */DIADesigner.exe",
                    "Delta Industrial Automation/DIAStudio/DIADesigner/DIADesigner.exe"],
    "diadesigner-ax": ["Delta Industrial Automation/DIAStudio/DIADesigner-AX*/CODESYS/Common/DIADesigner-AX.exe"],
}


def probe() -> dict[str, Any]:
    roots = {Path(p) for key in ("ProgramFiles", "ProgramFiles(x86)") if (p := os.environ.get(key))}
    found: dict[str, list[dict[str, Any]]] = {key: [] for key in PATTERNS}
    for tool, patterns in PATTERNS.items():
        seen = set()
        for root in roots:
            for pattern in patterns:
                for path in root.glob(pattern):
                    if path.is_file() and str(path.resolve()) not in seen:
                        seen.add(str(path.resolve()))
                        found[tool].append({"executable": str(path.resolve()), "sha256": digest(path),
                                            "version_source": "installation directory; native probe required",
                                            "directory": path.parent.name})
    return {"evidence_kind": "HOST_INVENTORY", "tools": found, "plc_contacted": False,
            "native_verified": False, "note": "Presence is not API, compiler or live qualification."}
