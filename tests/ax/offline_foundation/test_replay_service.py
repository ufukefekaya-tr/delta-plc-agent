from __future__ import annotations

import json
import unittest
from pathlib import Path

from axmcp_offline.constants import ALLOWED_OPERATIONS
from axmcp_offline.replay import ReplayBackend
from axmcp_offline.security import ProtocolViolation, sha256_file
from axmcp_offline.service import OfflineService

from . import REPO_ROOT


class ReplayServiceTests(unittest.TestCase):
    def test_all_five_operations_are_simulated_and_unverified(self) -> None:
        service = OfflineService(ReplayBackend.from_default())
        results = [service.execute(operation) for operation in ALLOWED_OPERATIONS]
        self.assertEqual([result["operation"] for result in results], list(ALLOWED_OPERATIONS))
        self.assertTrue(all(result["status"] == "unverified" for result in results))
        self.assertTrue(all(result["evidence_class"] == "SIMULATED" for result in results))
        self.assertTrue(all(result["live_ax_verified"] is False for result in results))

    def test_unknown_operation_is_fail_closed(self) -> None:
        service = OfflineService(ReplayBackend.from_default())
        with self.assertRaises(ProtocolViolation):
            service.execute("project.write")

    def test_fixture_manifest_hash_and_operation_order(self) -> None:
        manifest_path = REPO_ROOT / "tests" / "ax" / "fixtures" / "recorded_ax" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        bundle = REPO_ROOT / manifest["bundle_path"]
        self.assertEqual(sha256_file(bundle), "sha256:" + manifest["bundle_sha256"])
        self.assertEqual(tuple(manifest["operations"]), ALLOWED_OPERATIONS)
        self.assertEqual(manifest["classification"], "SIMULATED")
        self.assertFalse(manifest["live_ax_verified"])

    def test_environment_digest_tampering_is_rejected(self) -> None:
        bundle_path = (
            REPO_ROOT
            / "backends" / "ax"
            / "src"
            / "axmcp_offline"
            / "data"
            / "simulated_replay_bundle.json"
        )
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        bundle["environment_digest"] = "sha256:" + ("0" * 64)
        with self.assertRaises(ProtocolViolation):
            ReplayBackend(bundle)


if __name__ == "__main__":
    unittest.main()
