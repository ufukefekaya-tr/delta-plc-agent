from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from axmcp_offline.bridge import initialize_scratch
from axmcp_offline.constants import ALLOWED_OPERATIONS
from axmcp_offline.records import (
    load_strict_json,
    make_request,
    request_bytes,
    validate_chain,
)
from axmcp_offline.security import atomic_write_exclusive, sha256_file, utc_now

from . import PACKAGE_ROOT

PROBE_PATH = PACKAGE_ROOT / "adapters" / "ax_probe" / "bounded_probe.py"


def _load_probe():
    spec = importlib.util.spec_from_file_location("bounded_probe_test", PROBE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load bounded probe")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Text:
    def __init__(self, text: str) -> None:
        self.text = text


class _Node:
    def __init__(self, name: str, children: list[object] | None = None) -> None:
        self.name = name
        self.type = "SIM_TYPE"
        self.guid = "SIM_GUID_" + name
        self.is_folder = bool(children)
        self._children = children or []

    def get_name(self) -> str:
        return self.name

    def get_children(self, _recursive: bool) -> list[object]:
        return list(self._children)


class FakeDevice(_Node):
    pass


class FakeLibrary(_Node):
    pass


class FakeTask(_Node):
    pass


class FakeSource(_Node):
    def __init__(self) -> None:
        super().__init__("PLC_PRG")
        self.textual_declaration = _Text("PROGRAM PLC_PRG")
        self.textual_implementation = _Text("x := x;")


class FakeProject(_Node):
    def __init__(self) -> None:
        self.source = FakeSource()
        children = [FakeDevice("Device"), FakeLibrary("Library"), FakeTask("MainTask"), self.source]
        super().__init__("ScratchProject", children)
        self.path = "scratch.project"
        self.dirty = False

    def find(self, target: str, _recursive: bool) -> list[object]:
        return [self.source] if target == "PLC_PRG" else []

    def export_native(self, _objects: list[object], path: str, _recursive: bool) -> None:
        Path(path).write_bytes(b"SIM_NATIVE_EXPORT")


class _Projects:
    def __init__(self) -> None:
        self.primary = FakeProject()


class _System:
    executable_filename = "DIADesigner-AX.exe"
    version = "SIMULATED"


class AxProbeAdapterTests(unittest.TestCase):
    def test_source_has_no_vendor_mutation_or_online_calls(self) -> None:
        source = PROBE_PATH.read_text(encoding="utf-8")
        compile(source, str(PROBE_PATH), "exec")
        for forbidden in (
            "projects.open(",
            ".save(",
            ".save_as(",
            ".build(",
            ".login(",
            ".download(",
            ".reset(",
            ".force(",
            "online.",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_fake_ax_executes_all_five_operations_as_claim_not_verified(self) -> None:
        probe = _load_probe()
        probe.projects = _Projects()
        probe.system = _System()
        for operation in ALLOWED_OPERATIONS:
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config_path = initialize_scratch(root)
                config = json.loads(config_path.read_text(encoding="utf-8"))
                config["environment_digest"] = "sha256:" + ("1" * 64)
                config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
                correlation = str(uuid.uuid4())
                request = make_request(
                    correlation,
                    operation,
                    utc_now(),
                    config["environment_digest"],
                    [{"path": "probe-config.json", "sha256": sha256_file(config_path)}],
                    30,
                    root,
                )
                raw_request = request_bytes(request, root)
                atomic_write_exclusive(
                    root / "requests" / f"{correlation}.request.json", raw_request
                )
                self.assertEqual(probe.run_once(str(root)), "unverified")
                result, raw_result = load_strict_json(
                    root / "results" / f"{correlation}.result.json"
                )
                evidence, _ = load_strict_json(
                    root / "evidence" / f"{correlation}.evidence.json"
                )
                validate_chain(request, raw_request, result, raw_result, evidence, root)
                self.assertEqual(evidence["producer"], "agent")
                self.assertEqual(evidence["evidence_class"], "CLAIM")
                self.assertEqual(result["status"], "unverified")


if __name__ == "__main__":
    unittest.main()
