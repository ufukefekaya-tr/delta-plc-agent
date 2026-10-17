from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from axmcp_offline.bridge import FileBridgeBackend, initialize_scratch
from axmcp_offline.records import load_strict_json
from axmcp_offline.security import (
    ProtocolViolation,
    atomic_write_exclusive,
    canonical_json_bytes,
    sha256_bytes,
    utc_now,
)


def _activate_config(root: Path) -> None:
    path = initialize_scratch(root)
    config = json.loads(path.read_text(encoding="utf-8"))
    config["environment_digest"] = "sha256:" + ("1" * 64)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def _respond_once(
    root: Path,
    corrupt_request_digest: bool = False,
    producer: str = "agent",
    evidence_class: str = "SIMULATED",
    result_status: str = "unverified",
    artifact_case: str | None = None,
) -> None:
    deadline = time.monotonic() + 3
    request_path: Path | None = None
    while time.monotonic() < deadline:
        requests = list((root / "requests").glob("*.request.json"))
        if requests:
            request_path = requests[0]
            break
        time.sleep(0.01)
    if request_path is None:
        return
    request, request_raw = load_strict_json(request_path)
    correlation = request["correlation_id"]
    artifacts = []
    if artifact_case is not None:
        artifact_path = f"outputs/{correlation}.payload.json"
        artifact_raw = b'{"headless":"fixture"}\n'
        if artifact_case != "missing":
            atomic_write_exclusive(root / artifact_path, artifact_raw)
        artifact_digest = (
            "sha256:" + ("0" * 64)
            if artifact_case == "digest_mismatch"
            else sha256_bytes(artifact_raw)
        )
        artifacts.append({"path": artifact_path, "sha256": artifact_digest})
    result = {
        "protocol": "AXQ-FILE/1",
        "message_type": "result",
        "correlation_id": correlation,
        "operation": request["operation"],
        "created_at": utc_now(),
        "status": result_status,
        "environment_digest": request["environment_digest"],
        "request_digest": "sha256:" + ("2" * 64)
        if corrupt_request_digest
        else sha256_bytes(request_raw),
        "artifacts": artifacts,
        "diagnostics": ["headless fake worker"],
    }
    result_raw = canonical_json_bytes(result)
    evidence = {
        "schema_version": "1",
        "record_type": "EvidenceRecord",
        "created_at": result["created_at"],
        "correlation_id": correlation,
        "producer": producer,
        "evidence_class": evidence_class,
        "input_digest": sha256_bytes(request_raw),
        "output_digest": sha256_bytes(result_raw),
        "environment_digest": request["environment_digest"],
        "diagnostics": ["headless fake worker"],
    }
    atomic_write_exclusive(root / "results" / f"{correlation}.result.json", result_raw)
    atomic_write_exclusive(
        root / "evidence" / f"{correlation}.evidence.json", canonical_json_bytes(evidence)
    )


class BridgeTests(unittest.TestCase):
    def test_bounded_timeout_returns_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _activate_config(root)
            result = FileBridgeBackend(root, timeout_seconds=1).execute("environment.probe")
            self.assertEqual(result["status"], "blocked")
            self.assertFalse(result["live_ax_verified"])
            self.assertEqual(len(list((root / "requests").glob("*.request.json"))), 1)

    def test_valid_chain_is_accepted_without_live_elevation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _activate_config(root)
            worker = threading.Thread(target=_respond_once, args=(root,), daemon=True)
            worker.start()
            result = FileBridgeBackend(root, timeout_seconds=3).execute("source.read")
            worker.join(timeout=1)
            self.assertEqual(result["status"], "unverified")
            self.assertEqual(result["evidence_class"], "SIMULATED")
            self.assertFalse(result["live_ax_verified"])

    def test_digest_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _activate_config(root)
            worker = threading.Thread(target=_respond_once, args=(root, True), daemon=True)
            worker.start()
            with self.assertRaises(ProtocolViolation):
                FileBridgeBackend(root, timeout_seconds=3).execute("project.tree.read")
            worker.join(timeout=1)

    def test_human_claim_cannot_elevate_live_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _activate_config(root)
            worker = threading.Thread(
                target=_respond_once,
                args=(root, False, "human", "CLAIM", "verified"),
                daemon=True,
            )
            worker.start()
            result = FileBridgeBackend(root, timeout_seconds=3).execute("environment.probe")
            worker.join(timeout=1)
            self.assertEqual(result["status"], "verified")
            self.assertFalse(result["live_ax_verified"])

    def test_qualified_human_evidence_can_mark_live_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _activate_config(root)
            worker = threading.Thread(
                target=_respond_once,
                args=(root, False, "human", "HUMAN_BENCH_VERIFIED", "verified"),
                daemon=True,
            )
            worker.start()
            result = FileBridgeBackend(root, timeout_seconds=3).execute("environment.probe")
            worker.join(timeout=1)
            self.assertTrue(result["live_ax_verified"])

    def test_result_artifact_digest_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _activate_config(root)
            worker = threading.Thread(
                target=_respond_once,
                args=(root, False, "agent", "SIMULATED", "unverified", "digest_mismatch"),
                daemon=True,
            )
            worker.start()
            with self.assertRaises(ProtocolViolation):
                FileBridgeBackend(root, timeout_seconds=3).execute("source.read")
            worker.join(timeout=1)

    def test_missing_result_artifact_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _activate_config(root)
            worker = threading.Thread(
                target=_respond_once,
                args=(root, False, "agent", "SIMULATED", "unverified", "missing"),
                daemon=True,
            )
            worker.start()
            with self.assertRaises(ProtocolViolation):
                FileBridgeBackend(root, timeout_seconds=3).execute("source.read")
            worker.join(timeout=1)

    def test_placeholder_environment_digest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            initialize_scratch(root)
            with self.assertRaises(ProtocolViolation):
                FileBridgeBackend(root, timeout_seconds=1).execute("environment.probe")


if __name__ == "__main__":
    unittest.main()
