"""Independent byte vectors; no PLC, IDE, DLL, or copyrighted project fixture."""
import hashlib
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from universal_plc.dvp_native import (
    CAPACITY_WORDS, DvpNativeError, active_program, program_checksum,
    read_native, repair_checksum,
)


def native_vector(payload=bytes.fromhex('e82b0fc00f00'), checksum=0x06EB, key=0xB5):
    # LD M1000, OUT S15, END. Fixed checksum is hand-summed E82B+0FC0+0F00.
    header = bytearray(256)
    header[:3] = b'W\x01\x01'
    struct.pack_into('<H', header, 0x0C, 3960)
    struct.pack_into('<HH', header, 0x4A, 5, 0xD8)
    struct.pack_into('<H', header, 0x56, checksum)
    struct.pack_into('<I', header, 0x5E, 0x014D0000 | key)
    image = payload + b'\xff\xff' * (CAPACITY_WORDS - len(payload) // 2)
    slot = bytes(value ^ key for value in image)
    password = bytes(range(8)) + b'\x00\x00'
    tables = b'\x00\x00\x04\x00' + bytes(range(4)) + b'\x10\x00' + b'\xff' * 16 + b'\x18\x00' + b'\xff' * 24
    return bytes(header) + slot + password * 2 + tables + b'\nLOCAL EDITOR COMMENTS ARE NOT PLC CODE\n'


class NativeCodecTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, data, name='candidate.dvp'):
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_end_is_inclusive_and_checksum_uses_wire_byte_order(self):
        payload = bytes.fromhex('e82b0fc00f00')
        self.assertEqual(active_program(payload + b'\x20\x00'), payload)
        self.assertEqual(program_checksum(payload), 0x06EB)
        path = self.write(native_vector(checksum=0x06EB))
        result = read_native(path, hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(result['payload_le'], payload)
        self.assertEqual(result['word_count'], 3)
        self.assertEqual(len(result['image_le']), 15840)
        self.assertTrue(result['checksum_valid'])
        self.assertEqual(result['family'], 'SS2')
        self.assertFalse(result['vendor_compile'])

    def test_nonuniform_high_key_bytes_do_not_change_d8_program(self):
        for key in (0, 0x37, 0xFF):
            path = self.write(native_vector(checksum=0x06EB, key=key), f'{key}.dvp')
            self.assertEqual(read_native(path)['payload_le'], bytes.fromhex('e82b0fc00f00'))

    def test_stale_checksum_is_explicit_research_only(self):
        path = self.write(native_vector(checksum=0x06EA))
        with self.assertRaises(DvpNativeError) as error:
            read_native(path)
        self.assertEqual(error.exception.code, 'NATIVE_CHECKSUM_MISMATCH')
        result = read_native(path, require_checksum=False)
        self.assertFalse(result['checksum_valid'])
        self.assertEqual(result['calculated_checksum'], 0x06EB)

    def test_repair_changes_only_header_and_never_claims_compile(self):
        original = native_vector(checksum=0xFFFF)
        source = self.write(original)
        output = self.root / 'repaired.dvp'
        result = repair_checksum(source, output, hashlib.sha256(original).hexdigest())
        repaired = output.read_bytes()
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual([i for i, pair in enumerate(zip(original, repaired)) if pair[0] != pair[1]], [0x56, 0x57])
        self.assertEqual(result['classification'], 'FILE_FORMAT_REPAIR')
        self.assertFalse(result['vendor_compile'])
        self.assertFalse(result['program_changed'])
        self.assertEqual(read_native(output)['payload_le'], read_native(source, require_checksum=False)['payload_le'])

    def test_repair_rejects_overwrite_input_alias_and_publish_collision(self):
        original = native_vector(checksum=0x06EB)
        source = self.write(original)
        digest = hashlib.sha256(original).hexdigest()
        for target in (source, self.write(b'KEEP', 'existing.dvp')):
            with self.assertRaises(DvpNativeError):
                repair_checksum(source, target, digest)
        self.assertEqual(source.read_bytes(), original)
        output = self.root / 'collision.dvp'
        with patch('universal_plc.delta_files.limits.os.link', side_effect=FileExistsError('collision')):
            with self.assertRaises(DvpNativeError):
                repair_checksum(source, output, digest)
        self.assertFalse(output.exists())
        self.assertFalse(list(self.root.glob('.plc-*')))

    def test_digest_is_checked_before_repair_or_parse(self):
        source = self.write(native_vector(checksum=0x06EB))
        for bad in ('0' * 64, 'invalid', None):
            with self.assertRaises(DvpNativeError):
                repair_checksum(source, self.root / 'bad.dvp', bad)
        self.assertFalse((self.root / 'bad.dvp').exists())
        with self.assertRaises(DvpNativeError):
            read_native(source, '0' * 64)

    def test_end_scan_bounds_alignment_and_unwritten_gap(self):
        for image in (b'', b'\x0f', b'\x00\x0f', b'\xff\xff\x0f\x00',
                      b'\x00\x00' * (CAPACITY_WORDS + 1), bytearray(b'\x0f\x00')):
            with self.subTest(image_type=type(image), length=len(image)):
                with self.assertRaises(DvpNativeError):
                    active_program(image)
        boundary = b'\xe8\x2b' * (CAPACITY_WORDS - 1) + b'\x0f\x00'
        self.assertEqual(active_program(boundary), boundary)
        self.assertEqual(program_checksum(b'\x0f\x00'), 0x0F00)

    def test_operand_end_and_unknown_instruction_do_not_become_success(self):
        # MOV with raw 000F in an unqualified operand, followed by a real END.
        fake_end = bytes.fromhex('28000f000080008600860f00')
        with self.assertRaises(DvpNativeError) as error:
            active_program(fake_end)
        self.assertEqual(error.exception.code, 'UNSUPPORTED_PROGRAM_OPERAND')
        # Literal K15 is encoded 800F/8000, which must be consumed as an operand.
        literal = bytes.fromhex('28000f800080008600860f00')
        self.assertEqual(active_program(literal + b'\xff\xff'), literal)
        for image in (bytes.fromhex('34120f000f00'), bytes.fromhex('280000860086')):
            with self.assertRaises(DvpNativeError):
                active_program(image)

    def test_unknown_family_version_capacity_and_table_lengths_fail_closed(self):
        vector = native_vector(checksum=0x06EB)
        for offset, value in [(0, b'X'), (0x4A, b'\x04\x00'), (0x4C, b'\xd9\x00'),
                              (0x0C, b'\x00\x01'), (0x52, b'\x02'),
                              (0x3EF6, b'\x08\x00'), (0x3EFC, b'\x08\x00')]:
            modified = bytearray(vector)
            modified[offset:offset + len(value)] = value
            with self.subTest(offset=offset):
                with self.assertRaises(DvpNativeError):
                    read_native(self.write(modified))
        with self.assertRaises(DvpNativeError):
            read_native(self.write(vector[:0x3EE0]))

    def test_protection_is_never_decoded_or_repaired(self):
        vector = native_vector(checksum=0x06EB)
        for offset, value in [(0x52, b'\x01'), (0x52, b'\x10'), (0x3EE0, b'A'),
                              (0x3EEA, b'A'), (0x3EF4, b'\x02\x00'), (0x3EF8, b'A')]:
            modified = bytearray(vector)
            modified[offset:offset + len(value)] = value
            path = self.write(modified)
            with self.subTest(offset=offset):
                with self.assertRaises(DvpNativeError) as error:
                    read_native(path, require_checksum=False)
                self.assertEqual(error.exception.code, 'PROTECTED_NATIVE_FORMAT')
                with self.assertRaises(DvpNativeError):
                    repair_checksum(path, self.root / 'forbidden.dvp', hashlib.sha256(modified).hexdigest())


if __name__ == '__main__':
    unittest.main()
