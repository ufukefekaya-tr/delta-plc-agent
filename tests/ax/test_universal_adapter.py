from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backends/ax/src"))
from axmcp_offline.constants import PUBLIC_OPERATIONS
from axmcp_offline.security import ProtocolViolation
from axmcp_offline.universal_adapter import AxAdapter
from axmcp_offline.bench_opcua import BenchConfig, BenchRejected, REQUIRED_SIGNALS

TARGET = {"vendor": "Delta", "family": "AX", "model": "AX-308", "toolchain": "DIADesigner-AX", "ide_version": "1.9.0.9194", "project_format": ".project"}


class UniversalAdapterTests(unittest.TestCase):
    def test_new_operations_dispatch_through_universal_adapter(self):
        adapter = AxAdapter()
        host = Mock()
        adapter._service = Mock(autonomous=host)
        cases = {
            "job.progress": ("job_progress", {"job_id": "job-fixture"}),
            "revision.apply": ("revision_apply", {"workspace_id": "ws", "expected_revision": 0,
                              "idempotency_key": "unit", "st_changes": []}),
            "candidate.snapshot": ("candidate_snapshot", {"workspace_id": "ws", "expected_revision": 0}),
        }
        for operation, (method, arguments) in cases.items():
            with self.subTest(operation=operation):
                getattr(host, method).return_value = {"routed": operation}
                self.assertEqual(adapter.invoke(operation, arguments), {"routed": operation})
                getattr(host, method).assert_called_once_with(**arguments)

    def test_evidence_helpers_work_with_real_host_and_no_target_connection(self):
        from tests.ax.offline_foundation.test_autonomous_host import _activate_root
        from axmcp_offline.service import OfflineService
        from tests.ax.offline_foundation.test_operator_evidence import sample
        with tempfile.TemporaryDirectory() as tmp:
            adapter = AxAdapter()
            adapter._service = OfflineService(_activate_root(Path(tmp)))
            result = adapter.invoke("operator.profile", {})
            self.assertEqual(result["profile_kind"], "SYNTHETIC_REFERENCE_ONLY")
            self.assertFalse(result["runtime_dispatch"])
            result = adapter.invoke("operator.evidence.check", {
                "profile_id": "example-six-drive-v1", "kind": "watch", "evidence": sample("watch")})
            self.assertTrue(result["evidence_matches"])
            self.assertFalse(result["live_ax_verified"])
            with self.assertRaises(ProtocolViolation):
                adapter.invoke("job.progress", {"job_id": "missing"})

    def test_capabilities_are_lazy_and_preserve_all_operations(self):
        adapter = AxAdapter("does-not-exist.json")
        records = adapter.capabilities()
        self.assertEqual({r["operation"] for r in records if r["implementation"] == "available"}, set(PUBLIC_OPERATIONS) | {"project.create"})
        self.assertTrue(all(r["live_verified"] is False for r in records))

    def test_explicit_target_replay_never_claims_live_or_allows_model_substitution(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "adapter.json"
            path.write_text(json.dumps({"mode": "recorded_replay", "target": TARGET}), encoding="utf-8")
            adapter = AxAdapter(path)
            result = adapter.invoke("environment.probe", {"universal_target": TARGET})
            self.assertEqual(result["evidence_class"], "SIMULATED")
            self.assertFalse(result["live_ax_verified"])
            for field, value in (("model", "AX-8"), ("family", "AS"), ("ide_version", "9.0"), ("firmware", "changed")):
                changed = dict(TARGET, **{field: value})
                if field == "firmware":
                    continue  # Unpinned optional firmware is not a compatibility claim.
                with self.subTest(field=field), self.assertRaises(ProtocolViolation):
                    adapter.invoke("environment.probe", {"universal_target": changed})

    def test_unknown_dispatch_and_extra_args_rejected(self):
        adapter = AxAdapter()
        with self.assertRaises(ProtocolViolation):
            adapter.invoke("__class__", {})
        with self.assertRaises(ProtocolViolation):
            adapter.invoke("environment.probe", {"shell": "echo"})
        with self.assertRaises(ProtocolViolation):
            adapter.invoke("environment.probe", {"universal_target": TARGET})

    def test_bench_profile_is_frozen_and_has_no_default_node_binding(self):
        bindings = {key: "UnitTest." + key for key in REQUIRED_SIGNALS}
        kwargs = dict(endpoint="opc.tcp://192.0.2.10:4840", expected_ip="192.0.2.10", expected_fingerprint="sha256:" + "a"*64,
                      node_prefix="ns=7;s=Application.", signals=bindings, drive_count=2, idle_step=10, automatic_step=20)
        config = BenchConfig(**kwargs)
        bindings["stop_command"] = "Changed.Stop"
        self.assertEqual(config.signals["stop_command"], "UnitTest.stop_command")
        for mutation in ({"signals": {}}, {"drive_count": 0}, {"automatic_step": 10}, {"node_prefix": ""}):
            with self.subTest(mutation=mutation), self.assertRaises(BenchRejected):
                BenchConfig(**dict(kwargs, **mutation))

    def test_runtime_contains_no_original_project_dependencies(self):
        for directory in (ROOT / "backends/ax/src", ROOT / "backends/ax/adapters"):
            for path in directory.rglob("*.py"):
                text = path.read_text(encoding="utf-8").lower()
                for marker in ("private_machine_project", "customer_recipe", "gvl_customer", "c:\\users\\private_user"):
                    with self.subTest(path=str(path), marker=marker):
                        self.assertNotIn(marker, text)


if __name__ == "__main__":
    unittest.main()
