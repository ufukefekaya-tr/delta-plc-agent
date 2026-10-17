from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from axmcp_offline.records import (
    make_request,
    request_bytes,
    validate_artifacts,
    validate_operation,
)
from axmcp_offline.security import (
    ProtocolViolation,
    atomic_write_exclusive,
    canonical_json_bytes,
    strict_json_loads,
    validate_artifact_path,
)


class SecurityTests(unittest.TestCase):
    def test_exact_operation_allowlist(self) -> None:
        self.assertEqual(validate_operation("source.read"), "source.read")
        for denied in ("eval", "_eval", "shell", "project.write", "plc.online", "dev.flag"):
            with self.subTest(denied=denied), self.assertRaises(ProtocolViolation):
                validate_operation(denied)

    def test_path_matrix(self) -> None:
        valid = ("a.json", "outputs/project.export", "A-1/x_y.z")
        invalid = (
            "",
            "/absolute",
            "C:/drive",
            "//server/share",
            "\\\\server\\share",
            "a\\b",
            "a/../b",
            "a/./b",
            "a//b",
            "%2e%2e/escape",
            "a:b",
        )
        for value in valid:
            with self.subTest(valid=value):
                self.assertEqual(validate_artifact_path(value).as_posix(), value)
        for value in invalid:
            with self.subTest(invalid=value), self.assertRaises(ProtocolViolation):
                validate_artifact_path(value)

    def test_symlink_escape_is_rejected_without_platform_privilege(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            link = root / "link"
            link.mkdir()
            original_is_symlink = Path.is_symlink

            def controlled_is_symlink(path: Path) -> bool:
                return path == link or original_is_symlink(path)

            with patch.object(Path, "is_symlink", controlled_is_symlink):
                with self.assertRaises(ProtocolViolation):
                    validate_artifact_path("link/file.json", root)

    def test_strict_json_rejects_bom_duplicate_number_and_non_object(self) -> None:
        invalid = (
            b"\xef\xbb\xbf{}",
            b'{"a":"x","a":"y"}',
            b'{"a":1}',
            b'{"a":NaN}',
            b"[]",
            b'{"a":"x"} trailing',
        )
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ProtocolViolation):
                strict_json_loads(raw)
        self.assertEqual(strict_json_loads(b'{"a":"x","b":false}'), {"a": "x", "b": False})

    def test_atomic_publish_never_overwrites_final(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            final = Path(tmp) / "record.json"
            atomic_write_exclusive(final, b"first")
            self.assertEqual(final.read_bytes(), b"first")
            with self.assertRaises(ProtocolViolation):
                atomic_write_exclusive(final, b"second")
            self.assertEqual(final.read_bytes(), b"first")

    def test_atomic_publish_race_preserves_competing_final(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            final = Path(tmp) / "record.json"

            def competing_link(_partial: Path, destination: Path) -> None:
                Path(destination).write_bytes(b"competitor")
                raise FileExistsError("simulated competing publisher")

            with patch("axmcp_offline.security.os.link", side_effect=competing_link):
                with self.assertRaises(ProtocolViolation):
                    atomic_write_exclusive(final, b"ours")
            self.assertEqual(final.read_bytes(), b"competitor")
            self.assertFalse(final.with_name(final.name + ".partial").exists())

    def test_request_is_canonical_and_strict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "probe-config.json"
            config.write_text("{}\n", encoding="utf-8")
            request = make_request(
                str(uuid.uuid4()),
                "environment.probe",
                "2026-09-01T00:00:00Z",
                "sha256:" + ("1" * 64),
                [{"path": "probe-config.json", "sha256": "sha256:" + ("2" * 64)}],
                3,
                root,
            )
            self.assertEqual(request_bytes(request, root), canonical_json_bytes(request))
            validate_artifacts(request["artifacts"], root)


if __name__ == "__main__":
    unittest.main()
