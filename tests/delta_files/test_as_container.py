"""Neutral regression tests for the project-local AS228 pointer-layout codec.

Fixtures are software-only containers, never vendor-compiled machine projects.
"""
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

from universal_plc.delta_files import DeltaFileAdapter, FileTransformError, containers

TARGET = dict(vendor="delta", family="AS", model="AS228T-A", toolchain="ispsoft")
SOURCE = """<PROJECT>
Active_CodePage=1254
Prj_ISPVer=3.19 ,2024/1/30,2024
Prj_DT=AS228T
Prj_C_SourceCodeLocked=FALSE
</PROJECT>
<POU>
P_Name=Neutral
P_Lang=2
P_type=0
<LOCAL_VAR>
(* Sayaç değişkeni *)
</LOCAL_VAR>
<IL_ST_CODE>
Value := Value + 1;
</IL_ST_CODE>
</POU>
""".replace("\n", "\r\n")


def fixture(source: str = SOURCE, prefix: bytes = b"COMPILED_PREFIX_SENTINEL" * 3) -> bytes:
    raw = source.encode("cp1254")
    compress = zlib.compressobj(9, wbits=-15)
    payload = compress.compress(raw) + compress.flush()
    header = bytearray(0xa4)
    struct.pack_into("<II", header, 0x0c, 1020, 3010)
    struct.pack_into("<I", header, 0x7c, len(raw))
    struct.pack_into("<I", header, 0x94, len(payload) + 6)
    struct.pack_into("<I", header, 0x98, len(header) + len(prefix))
    struct.pack_into("<I", header, 0x9c, len(prefix))
    return bytes(header) + prefix + struct.pack("<HI", 8, zlib.crc32(raw) & 0xffffffff) + payload


class ASContainerTests(unittest.TestCase):
    def test_decode_and_source_identity(self):
        original = fixture()
        parsed = containers.decode(original)
        self.assertEqual(parsed.source, SOURCE.encode("cp1254"))
        self.assertEqual(containers.inspect(parsed)["project_model"], "AS228T")

    def test_rebuild_preserves_compiled_prefix(self):
        original = fixture()
        parsed = containers.decode(original)
        changed = parsed.rebuild(parsed.source.replace(b"Value + 1", b"Value + 2"))
        start = struct.unpack_from("<I", original, 0x98)[0]
        self.assertEqual(original[0xa4:start], changed[0xa4:start])
        self.assertIn(b"Value + 2", containers.decode(changed).source)

    def test_opaque_prefix_marker_never_overrides_explicit_pointer(self):
        for prefix in (b"\x08\x00opaque-prefix" * 4,
                       b"xx\x08\x00opaque-prefix" * 4):
            with self.subTest(prefix=prefix):
                original = fixture(prefix=prefix)
                parsed = containers.decode(original)
                self.assertEqual(parsed.source, SOURCE.encode("cp1254"))
                changed = parsed.rebuild(parsed.source.replace(b"Value + 1", b"Value + 2"))
                pointer = struct.unpack_from("<I", original, 0x98)[0]
                self.assertEqual(changed[0xa4:pointer], prefix)
                self.assertIn(b"Value + 2", containers.decode(changed).source)

    def test_pointer_and_lengths_and_crc_and_tail_rejected(self):
        original = fixture()
        payload = struct.unpack_from("<I", original, 0x98)[0]
        cases = []
        for offset, number in ((0x98, len(original) + 1), (0x9c, 1),
                               (0x10, 999), (0x94, 1), (0x7c, 1)):
            value = bytearray(original)
            struct.pack_into("<I", value, offset, number)
            cases.append(bytes(value))
        crc = bytearray(original)
        crc[payload + 2] ^= 1
        cases.extend((bytes(crc), original[:-1], original + b"junk", original[:120]))
        for value in cases:
            with self.subTest(bytes=len(value)), self.assertRaises(FileTransformError):
                containers.decode(value)

    def test_protection_and_other_family_rejected(self):
        for source in (SOURCE.replace("Locked=FALSE", "Locked=TRUE"),
                       SOURCE.replace("Prj_DT=AS228T", "Prj_DT=SE")):
            with self.assertRaises(FileTransformError):
                containers.decode(fixture(source))

    def test_variant_match_and_conflict_and_turkish_patch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "neutral.isp"
            original = fixture()
            path.write_bytes(original)
            adapter = DeltaFileAdapter("ispsoft")
            info = adapter.invoke("isp.inspect", dict(input_path=str(path), universal_target=TARGET))
            self.assertEqual(info["target_match"]["status"], "metadata_variant_match")
            self.assertFalse(info["vendor_compile"])
            with self.assertRaises(FileTransformError):
                adapter.invoke("isp.inspect", dict(input_path=str(path),
                    universal_target={**TARGET, "model": "AS228R-A"}))
            code = root / "neutral.st"
            code.write_text("(* Ölçüm: ışık *)\nValue := 7;\n", encoding="utf-8-sig")
            output = root / "patched.isp"
            adapter.invoke("isp.patch_st", dict(input_path=str(path), output_path=str(output),
                pou_name="Neutral", code_path=str(code), universal_target=TARGET))
            decoded = containers.read_container(output).source.decode("cp1254")
            self.assertIn("Ölçüm: ışık", decoded)
            self.assertIn("Value := 7;", decoded)
            self.assertEqual(path.read_bytes(), original)

    def test_native_build_remains_unavailable(self):
        capabilities = {x["operation"]: x for x in DeltaFileAdapter("ispsoft").capabilities()}
        self.assertEqual(capabilities["project.build"]["implementation"], "not_implemented")


if __name__ == "__main__":
    unittest.main(verbosity=2)
