from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

try:
    from .f00_conformance import (
        ALLOWED_OPERATIONS,
        ConformanceError,
        reject_duplicate_finals,
        sha256_digest,
        strict_load_bytes,
        validate_artifact_path,
        validate_chain,
        validate_request,
        validate_result,
    )
except ImportError:
    from f00_conformance import (  # type: ignore[no-redef]
        ALLOWED_OPERATIONS,
        ConformanceError,
        reject_duplicate_finals,
        sha256_digest,
        strict_load_bytes,
        validate_artifact_path,
        validate_chain,
        validate_request,
        validate_result,
    )


REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_ROOT = (
    REPO_ROOT
    / "backends" / "ax"
    / "qualification"
    / "f00"
    / "fixtures"
)
CHAIN_NOW = datetime(2026, 8, 31, 15, 1, tzinfo=timezone.utc)


def encode(record: dict[str, object]) -> bytes:
    return (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


class AxqFileProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.request_bytes = (FIXTURE_ROOT / "request.valid.json").read_bytes()
        cls.result_bytes = (FIXTURE_ROOT / "result.valid.json").read_bytes()
        cls.request = strict_load_bytes(cls.request_bytes)
        cls.result = strict_load_bytes(cls.result_bytes)
        cls.evidence = json.loads(
            (FIXTURE_ROOT / "evidence.valid.json").read_text(encoding="utf-8")
        )

    def test_recorded_request_result_evidence_chain_is_exact(self) -> None:
        request, result = validate_chain(
            self.request_bytes, self.result_bytes, self.evidence, now=CHAIN_NOW
        )
        self.assertEqual(request["correlation_id"], result["correlation_id"])
        self.assertEqual(request["operation"], result["operation"])
        self.assertEqual(request["environment_digest"], result["environment_digest"])
        self.assertEqual(self.evidence["producer"], "agent")
        self.assertEqual(self.evidence["evidence_class"], "STATIC_CHECKED")

    def test_manifest_locks_recorded_fixture_byte_digests(self) -> None:
        manifest = json.loads(
            (FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8")
        )
        for role, entry in manifest["chain"].items():
            with self.subTest(role=role):
                raw = (FIXTURE_ROOT / entry["path"]).read_bytes()
                self.assertEqual(sha256_digest(raw), entry["sha256"])
        self.assertEqual(self.result["request_digest"], sha256_digest(self.request_bytes))
        self.assertEqual(self.evidence["input_digest"], sha256_digest(self.request_bytes))
        self.assertEqual(self.evidence["output_digest"], sha256_digest(self.result_bytes))

    def test_strict_json_rejects_corrupt_duplicate_bom_invalid_utf8_and_trailing(self) -> None:
        invalid_inputs = {
            "corrupt": (FIXTURE_ROOT / "request.corrupt.json").read_bytes(),
            "duplicate": (FIXTURE_ROOT / "request.duplicate-key.json").read_bytes(),
            "bom": b"\xef\xbb\xbf{}",
            "invalid_utf8": b'{"value":"\xff"}',
            "trailing": b"{} trailing",
            "number": b'{"value": 1}',
            "nan": b'{"value": NaN}',
            "top_level_array": b"[]",
        }
        for name, raw in invalid_inputs.items():
            with self.subTest(case=name), self.assertRaises(ConformanceError):
                strict_load_bytes(raw)

    def test_partial_file_is_ignored_and_never_consumed(self) -> None:
        raw = (FIXTURE_ROOT / "request.valid.json.partial").read_bytes()
        with self.assertRaises(ConformanceError):
            validate_request(raw, filename="request.valid.json.partial", now=CHAIN_NOW)

    def test_request_exact_fields_protocol_uuid_digest_and_time_are_strict(self) -> None:
        mutations = {
            "unknown_field": {"extra": "forbidden"},
            "wrong_protocol": {"protocol": "AXQ-FILE/2"},
            "upper_uuid": {
                "correlation_id": "123E4567-E89B-42D3-A456-426614174000"
            },
            "bad_digest": {"environment_digest": "sha256:1234"},
            "fractional_time": {"created_at": "2026-08-31T15:00:00.1Z"},
            "non_increasing_deadline": {"deadline_at": "2026-08-31T15:00:00Z"},
        }
        for name, changes in mutations.items():
            mutated = deepcopy(self.request)
            mutated.update(changes)
            with self.subTest(case=name), self.assertRaises(ConformanceError):
                validate_request(encode(mutated), now=CHAIN_NOW)

    def test_stale_request_and_timeout_without_result_fail_closed(self) -> None:
        after_deadline = datetime(2026, 8, 31, 15, 6, tzinfo=timezone.utc)
        with self.assertRaises(ConformanceError):
            validate_request(self.request_bytes, now=after_deadline)
        result_bytes = None
        with self.assertRaises(ConformanceError):
            if result_bytes is None and after_deadline > datetime(
                2026, 8, 31, 15, 5, tzinfo=timezone.utc
            ):
                raise ConformanceError("request timed out without a final result")

    def test_stale_or_late_result_fails_closed(self) -> None:
        late = deepcopy(self.result)
        late["created_at"] = "2026-08-31T15:05:01Z"
        with self.assertRaises(ConformanceError):
            validate_result(
                encode(late), request=self.request, request_bytes=self.request_bytes
            )
        after_deadline = datetime(2026, 8, 31, 15, 6, tzinfo=timezone.utc)
        with self.assertRaises(ConformanceError):
            validate_result(
                self.result_bytes,
                request=self.request,
                request_bytes=self.request_bytes,
                now=after_deadline,
            )

    def test_result_correlation_operation_request_and_environment_mismatches_fail(self) -> None:
        mutations = {
            "correlation": {
                "correlation_id": "223e4567-e89b-42d3-a456-426614174000"
            },
            "operation": {"operation": "project.tree.read"},
            "request_digest": {"request_digest": "sha256:" + "0" * 64},
            "environment": {"environment_digest": "sha256:" + "4" * 64},
        }
        for name, changes in mutations.items():
            mutated = deepcopy(self.result)
            mutated.update(changes)
            with self.subTest(case=name), self.assertRaises(ConformanceError):
                validate_result(
                    encode(mutated),
                    request=self.request,
                    request_bytes=self.request_bytes,
                    now=CHAIN_NOW,
                )

    def test_result_exact_fields_protocol_status_digest_and_timestamp_are_strict(self) -> None:
        mutations = {
            "unknown_field": {"runtime_dispatch": "forbidden"},
            "wrong_protocol": {"protocol": "AXQ-FILE/2"},
            "unknown_status": {"status": "success"},
            "bad_digest": {"request_digest": "sha256:1234"},
            "fractional_time": {"created_at": "2026-08-31T15:00:30.1Z"},
        }
        for name, changes in mutations.items():
            mutated = deepcopy(self.result)
            mutated.update(changes)
            with self.subTest(case=name), self.assertRaises(ConformanceError):
                validate_result(
                    encode(mutated),
                    request=self.request,
                    request_bytes=self.request_bytes,
                    now=CHAIN_NOW,
                )

        missing = deepcopy(self.result)
        del missing["diagnostics"]
        with self.assertRaises(ConformanceError):
            validate_result(
                encode(missing),
                request=self.request,
                request_bytes=self.request_bytes,
                now=CHAIN_NOW,
            )

    def test_evidence_correlation_input_output_and_environment_mismatches_fail(self) -> None:
        mutations = {
            "correlation": {
                "correlation_id": "223e4567-e89b-42d3-a456-426614174000"
            },
            "input": {"input_digest": "sha256:" + "0" * 64},
            "output": {"output_digest": "sha256:" + "0" * 64},
            "environment": {"environment_digest": "sha256:" + "4" * 64},
        }
        for name, changes in mutations.items():
            evidence = deepcopy(self.evidence)
            evidence.update(changes)
            with self.subTest(case=name), self.assertRaises(ConformanceError):
                validate_chain(
                    self.request_bytes, self.result_bytes, evidence, now=CHAIN_NOW
                )

    def test_duplicate_final_create_is_replay_ambiguity(self) -> None:
        with self.assertRaises(ConformanceError):
            reject_duplicate_finals([self.result, deepcopy(self.result)])
        reject_duplicate_finals([self.request, self.result])

    def test_safe_scratch_relative_paths_are_accepted(self) -> None:
        valid = (
            "input.json",
            "inputs/source.export",
            "a1/b-2/c_3.d",
        )
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            for path in valid:
                with self.subTest(path=path):
                    self.assertEqual(validate_artifact_path(path, root), path)

    def test_absolute_drive_unc_device_backslash_and_traversal_paths_are_rejected(self) -> None:
        invalid = (
            "/etc/passwd",
            "C:/temp/file",
            "C:\\temp\\file",
            "\\\\server\\share\\file",
            "\\\\?\\C:\\device",
            "\\\\.\\PIPE\\name",
            "folder\\file",
            ".",
            "..",
            "folder/./file",
            "folder/../file",
            "%2e%2e/file",
            "folder/%2E%2E/file",
            "folder//file",
            "folder/",
            "",
        )
        for path in invalid:
            with self.subTest(path=path), self.assertRaises(ConformanceError):
                validate_artifact_path(path)

    def test_exact_five_operations_are_accepted_as_protocol_declarations(self) -> None:
        self.assertEqual(len(ALLOWED_OPERATIONS), 5)
        for operation in ALLOWED_OPERATIONS:
            request = deepcopy(self.request)
            request["operation"] = operation
            with self.subTest(operation=operation):
                validate_request(encode(request), now=CHAIN_NOW)


if __name__ == "__main__":
    unittest.main()
