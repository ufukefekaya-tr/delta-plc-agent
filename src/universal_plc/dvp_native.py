"""Offline bytecode extraction for the observed unprotected WPLSoft D8/SS2 format.

Qualified against WPLSoft.exe SHA-256
f0c002d6dc7fab80ea942498eac9ceec19aaef037c4d3e7e418a7f64df5f42c1:
loader 0x5D9024, XOR transform 0x4A909C, END scanner 0x52F218,
checksum 0x72C784 and saver 0x5E85E0. Instruction lengths below cross-check
opcode table 0xA37278 against length table 0xA36D06. No executable is invoked.

These functions do not compile instructions or validate ladder/comment agreement.
PLC program reads contain bytecode, not the original native editor container.
Do not transplant uploaded code into an unrelated grid and call it a native backup.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import struct
from typing import Any

from .delta_files.limits import FileTransformError, atomic_write, read_bytes
from .models import PLCError


CAPACITY_WORDS = 7920
PROGRAM_OFFSET = 0x100
PROGRAM_BYTES = CAPACITY_WORDS * 2
PROGRAM_END = PROGRAM_OFFSET + PROGRAM_BYTES
CHECKSUM_OFFSET = 0x56
# Two ten-byte password containers, encrypted count, then 4/16/24-byte tables.
MIN_NATIVE_BYTES = PROGRAM_END + 72
# Only independently observed instruction shapes are admitted. Table values are
# byte lengths, including the opcode: MOV=10, FROM=18, DADD=26, GPWM=14.
_OPERANDS = {
    0x0007: (1, 2), 0x0028: (2, 2), 0x0030: (3, 2),
    0x0038: (3, 2), 0x0039: (3, 4), 0x003A: (3, 2), 0x003B: (3, 4),
    0x003C: (3, 2), 0x003D: (3, 4), 0x003E: (3, 2), 0x003F: (3, 4),
    0x00AC: (4, 2), 0x00AE: (4, 2), 0x0140: (3, 2),
    0x01D0: (2, 2), 0x01D2: (2, 2), 0x01D4: (2, 2),
    0x01DA: (2, 2), 0x01DC: (2, 2), 0x01E0: (2, 2),
    0x01E2: (2, 2), 0x01E4: (2, 2), 0x01EA: (2, 2), 0x01EC: (2, 2),
}
_BIT_OPCODES = {0x2000, 0x3000, 0x4000, 0x5000, 0x6000, 0x7000,
                0xA000, 0xC000, 0xD000}


class DvpNativeError(PLCError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def active_program(image_le: bytes) -> bytes:
    """Return bytes through END at a validated instruction boundary.

    Accept complete slots or bounded read segments accumulated from address 2000.
    Bytes after END are irrelevant to active program equality, but must be retained
    separately in a full raw backup. Unknown instructions, indirect operands and
    unqualified operand encodings fail closed; no semantic compiler is provided.
    This prevents an operand containing 000F being mistaken for END by the native
    vendor's simpler word scanner. Full raw backup need not call this function.
    """
    if not isinstance(image_le, bytes) or not 2 <= len(image_le) <= PROGRAM_BYTES or len(image_le) % 2:
        raise DvpNativeError('INVALID_PROGRAM_IMAGE', 'Expected 1..7920 little-endian 16-bit words')
    words = struct.unpack('<' + 'H' * (len(image_le) // 2), image_le)
    index = 0
    while index < len(words):
        word = words[index]
        if word == 0x000F:
            return image_le[:2 * (index + 1)]
        if word == 0xFFFF:
            raise DvpNativeError('INVALID_PROGRAM_IMAGE', 'Unwritten program word occurs before END')
        if word == 0x000E:
            index += 1
            continue
        if word & 0xF000 in _BIT_OPCODES:
            device = word & 0x0FFF
            if not (device < 0x600 or device >= 0x800):
                raise DvpNativeError('UNSUPPORTED_PROGRAM_OPERAND', 'Unknown bit-device encoding')
            index += 1
            continue
        if word not in _OPERANDS:
            raise DvpNativeError('UNSUPPORTED_PROGRAM_OPCODE', f'Unqualified opcode {word:04X} at word {index}')
        count, width = _OPERANDS[word]
        end = index + 1 + count * width
        if end > len(words):
            raise DvpNativeError('TRUNCATED_PROGRAM_INSTRUCTION', f'Truncated instruction at word {index}')
        for start in range(index + 1, end, width):
            a, b = words[start:start + 2]
            prefixes = (a & 0xFF00, b & 0xFF00)
            if prefixes not in {(0x8600, 0x8600), (0x8400, 0x8000),
                                (0x8000, 0x8000), (0x8200, 0x8000)}:
                raise DvpNativeError('UNSUPPORTED_PROGRAM_OPERAND', f'Unqualified operand at word {start}')
            if prefixes == (0x8600, 0x8600) and a & 1:
                raise DvpNativeError('UNSUPPORTED_PROGRAM_OPERAND', 'Unaligned D-register encoding')
            if width == 4 and words[start + 2:start + 4] != (0x8000, 0x8000):
                raise DvpNativeError('UNSUPPORTED_PROGRAM_OPERAND', 'Unqualified wide or indirect operand')
        index = end
    raise DvpNativeError('PROGRAM_END_MISSING', 'No aligned END within the bounded program image')


def program_checksum(payload_le: bytes) -> int:
    """Vendor additive checksum: byte-swapped words through END, modulo 65536."""
    payload = active_program(payload_le)
    return sum(struct.unpack('>' + 'H' * (len(payload) // 2), payload)) & 0xFFFF


def _read(path: str | Path) -> bytes:
    path = Path(path)
    if path.suffix.lower() != '.dvp':
        raise DvpNativeError('INVALID_NATIVE_FILE', 'An explicit .dvp path is required')
    try:
        return read_bytes(path)
    except (OSError, FileTransformError) as exc:
        raise DvpNativeError('INVALID_NATIVE_FILE', str(exc)) from exc


def _parse(data: bytes, expected_sha256: str | None, require_checksum: bool) -> dict[str, Any]:
    digest = hashlib.sha256(data).hexdigest()
    if expected_sha256 is not None:
        if not isinstance(expected_sha256, str) or not re.fullmatch('[0-9a-fA-F]{64}', expected_sha256):
            raise DvpNativeError('INVALID_NATIVE_DIGEST', 'Expected SHA-256 must contain exactly 64 hex characters')
        if digest != expected_sha256.lower():
            raise DvpNativeError('NATIVE_SHA_MISMATCH', 'Native file differs from the pinned source')
    if type(require_checksum) is not bool:
        raise DvpNativeError('INVALID_NATIVE_OPTIONS', 'require_checksum must be a boolean')
    if len(data) < MIN_NATIVE_BYTES or data[:3] != b'W\x01\x01':
        raise DvpNativeError('INVALID_NATIVE_FORMAT', 'Missing observed WPLSoft header or truncated SS2 container')
    family_code, version = struct.unpack_from('<HH', data, 0x4A)
    if family_code != 5 or version != 0xD8:
        raise DvpNativeError('UNSUPPORTED_NATIVE_FORMAT', 'Only observed D8 format, family 5 (SS2), is qualified')
    if struct.unpack_from('<H', data, 0x0C)[0] * 4 != PROGRAM_BYTES:
        raise DvpNativeError('UNSUPPORTED_NATIVE_CAPACITY', 'SS2 container must declare exactly 7920 words')
    # D8 transform repeats the low byte of the dword at 5E; the other three bytes
    # are preserved, never treated as firmware or a code integrity checksum.
    key = struct.unpack_from('<I', data, 0x5E)[0]
    flags = data[0x52]
    if flags & ~0x11:
        raise DvpNativeError('UNSUPPORTED_NATIVE_FLAGS', 'Unknown native protection flags')
    tail = data[PROGRAM_END:MIN_NATIVE_BYTES]
    blank_password = bytes(range(8)) + b'\x00\x00'
    protected = bool(flags or tail[:10] != blank_password or tail[10:20] != blank_password
                     or struct.unpack_from('<H', tail, 20)[0]
                     or tail[24:28] != bytes(range(4)))
    if protected:
        raise DvpNativeError('PROTECTED_NATIVE_FORMAT', 'Protected/native encrypted variants are outside this codec')
    if (struct.unpack_from('<H', tail, 22)[0] != 4
            or struct.unpack_from('<H', tail, 28)[0] != 16
            or struct.unpack_from('<H', tail, 46)[0] != 24):
        raise DvpNativeError('UNSUPPORTED_NATIVE_TABLES', 'Unexpected D8 native auxiliary field lengths')
    image = bytes(value ^ (key & 0xFF) for value in data[PROGRAM_OFFSET:PROGRAM_END])
    payload = active_program(image)
    stored = struct.unpack_from('<H', data, CHECKSUM_OFFSET)[0]
    calculated = program_checksum(payload)
    valid = stored == calculated
    if require_checksum and not valid:
        raise DvpNativeError('NATIVE_CHECKSUM_MISMATCH',
                             f'Native stored checksum {stored:04X} differs from program {calculated:04X}; not a verified saved candidate')
    return dict(source_sha256=digest, family='SS2', family_code=family_code, version=version,
                capacity_words=CAPACITY_WORDS, image_le=image, payload_le=payload,
                word_count=len(payload) // 2, stored_checksum=stored,
                calculated_checksum=calculated, checksum_valid=valid, protected=False,
                image_sha256=hashlib.sha256(image).hexdigest(),
                payload_sha256=hashlib.sha256(payload).hexdigest(),
                native_auxiliary_sha256=hashlib.sha256(data[PROGRAM_END:]).hexdigest(),
                vendor_compile=False, live_execution=False)


def read_native(path: str | Path, expected_sha256: str | None = None, *,
                require_checksum: bool = True) -> dict[str, Any]:
    """Extract a pinned payload; invalid checksums fail unless explicitly researching.

    A valid native checksum is a file integrity condition, not compiler evidence,
    instruction semantics, exact CPU identity, or permission to transfer the file.
    """
    return _parse(_read(path), expected_sha256, require_checksum)


def repair_checksum(source_path: str | Path, output_path: str | Path,
                    expected_sha256: str) -> dict[str, Any]:
    """Publish a new checksum-repaired copy; preserve every other native byte.

    The operation is FILE_FORMAT_REPAIR, never vendor compilation. Source bytes,
    comments, grid, program, encryption key and auxiliary fields remain unchanged.
    Existing outputs and source-path aliases are rejected atomically.
    """
    if expected_sha256 is None:
        raise DvpNativeError('INVALID_NATIVE_DIGEST', 'Checksum repair requires a pinned source SHA-256')
    original = _read(source_path)
    before = _parse(original, expected_sha256, False)
    repaired = bytearray(original)
    struct.pack_into('<H', repaired, CHECKSUM_OFFSET, before['calculated_checksum'])
    data = bytes(repaired)
    measured = hashlib.sha256(data).hexdigest()
    _parse(data, measured, True)
    if Path(output_path).suffix.lower() != '.dvp':
        raise DvpNativeError('INVALID_NATIVE_FILE', 'Repair output must use a new .dvp path')
    try:
        target = atomic_write(output_path, data, (Path(source_path),))
    except (OSError, FileTransformError) as exc:
        raise DvpNativeError('NATIVE_OUTPUT_REJECTED', str(exc)) from exc
    after = read_native(target, measured)
    return dict(classification='FILE_FORMAT_REPAIR', source_sha256=before['source_sha256'],
                output_path=str(target), output_sha256=after['source_sha256'],
                changed_offsets=[i for i in range(CHECKSUM_OFFSET, CHECKSUM_OFFSET + 2)
                                 if original[i] != data[i]],
                stored_checksum_before=before['stored_checksum'],
                stored_checksum_after=after['stored_checksum'],
                payload_sha256=after['payload_sha256'], word_count=after['word_count'],
                checksum_valid=True, program_changed=False, vendor_compile=False,
                live_execution=False, native_metadata_preserved=True)
