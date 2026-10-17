"""Run a real offline file roundtrip through the Universal service; no PLC/IDE."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile

from universal_plc.service import Service
from universal_plc.storage import digest


def run_demo() -> dict:
    with tempfile.TemporaryDirectory(prefix="plcweave-demo-") as temporary:
        root = Path(temporary)
        source = root / "hello.il"
        source.write_text("LD,M0\nOUT,M1\n", encoding="utf-8")
        service = Service(root / "runtime", read_roots=[root])
        target = dict(vendor="delta", family="DVP", model="DVP14SS211R", toolchain="ispsoft")
        route = service.resolve(target)
        if route["adapter_id"] != "delta.ispsoft":
            raise RuntimeError("Unexpected demo route")
        workspace = service.workspace_create(target, task_id="offline-demo", revision="v1")
        wid, context = workspace["workspace_id"], workspace["context_digest"]
        service.import_file(wid, str(source), "hello.il", digest(source))
        evidence = []
        for operation, input_path, output_path in (
            ("dvp.il_to_mpu", "hello.il", "hello.mpu"),
            ("dvp.mpu_to_il", "hello.mpu", "roundtrip.il"),
        ):
            result = service.invoke(wid, operation, dict(input_path=input_path, output_path=output_path), context)
            record = service.evidence_read(wid, result["evidence_id"])
            if record["result"]["evidence_kind"] != "FILE_TRANSFORM" or record["result"]["vendor_compile"]:
                raise RuntimeError("Unexpected evidence class")
            evidence.append(result["evidence_id"])
        directory, _ = service.workspace(wid)
        restored = (directory / "files" / "roundtrip.il").read_text("utf-8")
        if restored.splitlines() != source.read_text("utf-8").splitlines():
            raise RuntimeError("Roundtrip changed the demo instructions")
        return dict(status="PASS", adapter=route["adapter_id"], workflow="IL -> MPU -> IL",
                    evidence_kind="FILE_TRANSFORM", evidence_records=len(evidence),
                    hardware_contacted=False, vendor_compile=False)


if __name__ == "__main__":
    print(json.dumps(run_demo(), indent=2))
