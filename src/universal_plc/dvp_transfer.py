"""Pinned WPLSoft SS2 CDC program contract, distinct from generic D registers.

Program mode and transfer require current physical enrollment and an independently
qualified private-64 ACK contract. Static research alone cannot enable hardware.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from datetime import datetime, timezone
from typing import Any
import uuid

from .delta_files.limits import atomic_write, read_bytes
from .dvp_native import PROGRAM_BYTES, active_program, read_native


def _packet(station: int, body: bytes) -> bytes:
    if type(station) is not int or not 1 <= station <= 247:
        raise ValueError('Non-broadcast station 1..247 required')
    data = bytes([station]) + body
    return b':' + (data + bytes([(-sum(data)) & 255])).hex().upper().encode() + b'\r\n'


def identity_frame(station: int) -> bytes:
    return _packet(station, bytes.fromhex('6700100005'))


def program_mode_frame(station: int, enabled: bool) -> bytes:
    if type(enabled) is not bool:
        raise ValueError('Program mode requires boolean')
    return _packet(station, bytes([0x64, 1, 0x19, 1, int(enabled)]))


def program_read_frame(station: int, offset_words: int, count: int) -> bytes:
    if type(offset_words) is not int or type(count) is not int or not 0 <= offset_words < 7920 or not 1 <= count <= 16 or offset_words + count > 7920:
        raise ValueError('Bounded SS2 program read required')
    return _packet(station, b'\x03' + (0x2000 + offset_words).to_bytes(2, 'big') + count.to_bytes(2, 'big'))


def decode_response(frame: bytes, station: int, function: int) -> bytes:
    from .dvp_serial import DvpSerialError
    if not isinstance(frame, bytes) or not 11 <= len(frame) <= 513 or not frame.startswith(b':') or not frame.endswith(b'\r\n'):
        raise DvpSerialError('INVALID_FRAME', 'Bounded native ASCII response required')
    encoded = frame[1:-2]
    if len(encoded) % 2 or not re.fullmatch(rb'[0-9a-fA-F]+', encoded):
        raise DvpSerialError('INVALID_FRAME', 'Invalid native response hexadecimal payload')
    data = bytes.fromhex(encoded.decode())
    if sum(data) & 255:
        raise DvpSerialError('LRC_ERROR', 'Native response LRC mismatch')
    if data[0] != station:
        raise DvpSerialError('WRONG_STATION', 'Native response station mismatch')
    if data[1] == function | 0x80:
        raise DvpSerialError('PLC_EXCEPTION', f'Native function {function:02X} exception {data[2]}')
    if data[1] != function:
        raise DvpSerialError('WRONG_FUNCTION', 'Native response function mismatch')
    return data[2:-1]


class NativeProgramTransport:
    def __init__(self, adapter):
        self.adapter = adapter
        self.trace: list[dict[str, Any]] = []
        self.authority_guard = None

    def exchange(self, request: bytes, function: int, *, mutating: bool = False) -> bytes:
        from .dvp_serial import DvpSerialError
        a = self.adapter
        a._profile()
        a._open()
        a._profile()
        a.connection.reset_input_buffer()
        if mutating:
            if not callable(self.authority_guard):
                _fail('PROGRAM_AUTHORITY_GUARD_REQUIRED', 'Native mutation requires the full current transfer authority guard')
            self.authority_guard()  # Last metadata refresh, immediately BEFORE native bytes.
        written = a.connection.write(request)
        response = b''
        if written == len(request):
            deadline = time.monotonic() + 2
            for _ in range(4):
                response = a._line(deadline)
                if response not in (b'\r\n', b'\n') and not (function in (1, 3, 0x67) and response == request):
                    break
            if response in (b'\r\n', b'\n') or (function in (1, 3, 0x67) and response == request):
                response = b''
        self.trace.append({'function': function, 'tx_hex': request.hex(), 'rx_hex': response.hex()})
        if written != len(request):
            raise DvpSerialError('SERIAL_WRITE_INCOMPLETE', 'Native request partly sent; not automatically replayed')
        return decode_response(response, a.config.get('station', 1), function)

    def identify(self) -> dict[str, Any]:
        from .dvp_serial import DvpSerialError
        a = self.adapter
        data = self.exchange(identity_frame(a.config.get('station', 1)), 0x67)
        if len(data) != 11 or data[0] != 10:
            raise DvpSerialError('INVALID_LENGTH', 'FC67 identity must contain five big-endian words')
        words = [int.from_bytes(data[i:i+2], 'big') for i in range(1, 11, 2)]
        ss2 = words[2] == 1 and words[3] == 0x000E
        if not ss2:
            raise DvpSerialError('NATIVE_MODEL_MISMATCH', 'FC67 native SS2 marker/model code was not observed')
        return {'ok': True, 'status': 'ok', 'family_verified': True, 'native_family': 'SS2',
                'identity_words': words, 'exact_model_verified': False,
                'declared_model': a.config.get('target_model'), 'capacity_words': 7920,
                'station': a.config.get('station', 1), 'usb_hwid': a.config.get('expected_hwid'),
                'trace': list(self.trace), 'identity_sha256': hashlib.sha256(data[1:]).hexdigest()}

    def mode(self, enabled: bool):
        # This stricter echo contract is a hypothesis until verified_contract is
        # enrolled from real entry/exit responses. Vendor static checks were lax.
        expected = bytes([1, 0x19, 1, int(enabled)])
        data = self.exchange(program_mode_frame(self.adapter.config.get('station', 1), enabled), 0x64, mutating=True)
        if data != expected:
            _fail('PROGRAM_MODE_ACK_MISMATCH', 'Private-64 mode ACK does not match the enrolled strict echo shape')

    def id_status(self) -> int:
        data = self.exchange(_packet(self.adapter.config.get('station', 1), bytes.fromhex('64012A0100')), 0x64)
        if len(data) != 4 or data[:3] != bytes.fromhex('012A01') or data[3] not in (0, 255):
            _fail('PLC_ID_ACK_MISMATCH', 'Private-2A status shape is not qualified')
        return data[3]

    def read_device(self, device: str, count: int) -> list[int]:
        return _device_read(self.adapter, self.trace, device, count)

    def preflight(self) -> dict:
        running = self.read_device('M1000', 1)[0]
        protected = self.read_device('M1069', 1)[0]
        if running:
            _fail('PLC_NOT_STOPPED', 'Program transfer requires freshly observed STOP; no automatic STOP/RUN')
        if protected:
            _fail('PLC_PROGRAM_PROTECTED', 'M1069 program protection is active')
        enabled = self.id_status() != 0
        if enabled:
            _fail('PLC_ID_PROTECTED', 'PLC ID authentication is outside this transfer contract')
        return dict(running=False, protected=False, plc_id_enabled=False)

    def read_descriptor(self) -> list[int]:
        data = self.exchange(_packet(self.adapter.config.get('station', 1), bytes.fromhex('0315800002')), 3)
        if len(data) != 5 or data[0] != 4:
            _fail('INVALID_LENGTH', 'Initial-value descriptor must contain two words')
        return [int.from_bytes(data[i:i + 2], 'big') for i in (1, 3)]

    def read_parameters(self, specs: list[dict]) -> list[dict]:
        return [dict(device=s['device'], count=s['count'], values=self.read_device(s['device'], s['count']))
                for s in specs]

    def read_image(self) -> bytes:
        image = bytearray()
        for offset in range(0, 7920, 16):
            data = self.exchange(program_read_frame(self.adapter.config.get('station', 1), offset, 16), 3)
            if len(data) != 33 or data[0] != 32:
                _fail('INVALID_LENGTH', 'Program read must return exactly sixteen raw little-endian words')
            image.extend(data[1:])  # Vendor _memcpy, never generic D-register byte swapping.
        return bytes(image)

    def write_program(self, offset_words: int, payload_le: bytes):
        if (type(offset_words) is not int or not isinstance(payload_le, bytes)
                or not 2 <= len(payload_le) <= 32 or len(payload_le) % 2
                or not 0 <= offset_words < 7920 or offset_words + len(payload_le) // 2 > 7920):
            _fail('INVALID_PROGRAM_WRITE', 'Only bounded sixteen-word native program chunks are supported')
        address = (0x2000 + offset_words).to_bytes(2, 'big')
        quantity = (len(payload_le) // 2).to_bytes(2, 'big')
        request = _packet(self.adapter.config.get('station', 1), b'\x10' + address + quantity
                          + bytes([len(payload_le)]) + payload_le)
        if self.exchange(request, 0x10, mutating=True) != address + quantity:
            _fail('PROGRAM_WRITE_ACK_MISMATCH', 'Program chunk ACK does not match address/quantity')

    def write_descriptor(self):
        body = bytes.fromhex('101580000204FFFFFFFF')
        if self.exchange(_packet(self.adapter.config.get('station', 1), body), 0x10, mutating=True) != body[1:5]:
            _fail('DESCRIPTOR_ACK_MISMATCH', 'Initial-value descriptor ACK mismatch')

    def finalize(self) -> dict:
        body = bytes.fromhex('050C31FF00')
        if self.exchange(_packet(self.adapter.config.get('station', 1), body), 5, mutating=True) != body[1:]:
            _fail('FINALIZE_ACK_MISMATCH', 'M1073 ACK mismatch; never automatically replayed')
        syntax = self.read_device('D1004', 1)[0]
        if syntax != 0:
            _fail('PLC_SYNTAX_FAULT', f'D1004 reports syntax error {syntax}')
        return dict(syntax_code=syntax)


def _fail(code: str, message: str):
    from .dvp_serial import DvpSerialError
    raise DvpSerialError(code, message)


def _device_read(adapter, trace: list[dict], device: str, count: int) -> list[int]:
    before = getattr(adapter, 'trace', [])
    previous = id(before[-1]) if before else None
    try:
        values = adapter._read(device, count)
    finally:
        after = getattr(adapter, 'trace', [])
        if after and id(after[-1]) != previous:
            trace.append(dict(after[-1], kind='device_read'))
    maximum = 1 if device.startswith('M') else 65535
    if (not isinstance(values, list) or len(values) != count
            or any(type(value) is not int or not 0 <= value <= maximum for value in values)):
        _fail('INVALID_DEVICE_READ', 'Typed device read did not return the requested bounded values')
    return values


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def _digest(value: Any) -> str:
    return _sha(_json_bytes(value))


def _hash_claim(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'(?:sha256:)?[0-9a-fA-F]{64}', value):
        _fail('INVALID_DIGEST', 'An exact SHA-256 digest is required')
    return value.removeprefix('sha256:').lower()


def _read_json(path: Path, expected: str | None = None) -> tuple[dict, str]:
    data = read_bytes(path, 2 * 1024 * 1024)
    measured = _sha(data)
    if expected is not None and measured != _hash_claim(expected):
        _fail('ARTIFACT_SHA_MISMATCH', 'Pinned artifact bytes changed')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                _fail('INVALID_ARTIFACT', 'Duplicate JSON keys are forbidden')
            value[key] = item
        return value
    value = json.loads(data.decode('utf-8'), object_pairs_hook=unique)
    if not isinstance(value, dict):
        _fail('INVALID_ARTIFACT', 'Artifact must be a JSON object')
    return value, measured


class ProgramTransfer:
    """One typed transaction, immutable plan, durable journal and shared target lease.

    No RTC, runtime-parameter, password, PLC-ID or upload-disable writes. The fixed
    D1408/D1409 descriptor is native initial-value metadata, explicitly reset only
    after existing and candidate initial-value data are confirmed absent.
    Initial-value blocks are unsupported. An ACK never substitutes for full fresh
    bytecode/tail/descriptor/parameter/STOP verification. Failed mutations consume
    their admission permanently and require explicit read-only reconciliation.
    """
    _FIELDS = {
        'program.contract.qualify': {'output_dir'},
        'project.upload': {'output_dir'},
        'deployment.prepare': {'input_path', 'expected_sha256', 'backup_path', 'expected_backup_sha256'},
        'deployment.execute': {'plan_path', 'expected_sha256'},
        'deployment.reconcile': {'journal_path', 'expected_sha256'},
    }

    def __init__(self, adapter):
        self.a = adapter
        self.t = NativeProgramTransport(adapter)
        self.t.authority_guard = self._current
        self.journal = None
        self.journal_path = None
        self.mutation_attempted = False
        self.program_write_attempted = False
        self.journal_owned = False
        self.contract_valid = False
        self.qualifying = False
        self.recovering = False
        self.lease_path = None
        self.lease_token = None
        self.lock_stream = None

    def _inside(self, value: str | Path) -> Path:
        if not isinstance(value, (str, Path)) or not str(value):
            _fail('INVALID_ARTIFACT_PATH', 'An explicit workspace artifact path is required')
        requested = Path(value)
        path = (requested if requested.is_absolute() else self.files / requested).resolve()
        if not path.is_relative_to(self.files) or path == self.files:
            _fail('ARTIFACT_OUTSIDE_WORKSPACE', 'Transfer artifacts must stay beneath workspace/files')
        return path

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.files).as_posix()

    def _approval(self) -> tuple[dict, str]:
        c = self.a.config
        path = c.get('approved_manifest_path')
        if not isinstance(path, str) or not Path(path).is_absolute():
            _fail('APPROVED_SOURCE_REQUIRED', 'Current approved manifest must be explicitly enrolled')
        manifest, digest = _read_json(Path(path), _hash_claim(c.get('approved_manifest_sha256')))
        if (manifest.get('schema_version') != 1 or manifest.get('approved') is not True
                or manifest.get('revision') != self.context['revision']
                or not isinstance(manifest.get('approval_evidence'), str) or not manifest['approval_evidence'].strip()):
            _fail('APPROVED_SOURCE_REQUIRED', 'Current revision and human approval evidence are required')
        source = manifest.get('source_path')
        if not isinstance(source, str) or not Path(source).is_absolute():
            _fail('APPROVED_SOURCE_REQUIRED', 'Approval must name the original current source file')
        source_sha = _hash_claim(manifest.get('source_sha256'))
        # Recovery classifies the interrupted transaction's immutable candidate;
        # a producer changing its external file must not prevent mode closure.
        # Manifest/context/identity bindings remain pinned, and recovery has no
        # program writer. Every other operation rehashes the original source.
        if not self.recovering and _sha(read_bytes(source)) != source_sha:
            _fail('APPROVED_SOURCE_CHANGED', 'Original approved source changed; stale imported copies cannot be transferred')
        _hash_claim(manifest.get('payload_sha256'))
        return manifest, digest

    def _session(self, args: dict, physical: bool):
        context = args.get('_execution_context')
        required = {'workspace_id', 'task_id', 'revision', 'context_digest', 'workspace_root', 'baseline_sha256'}
        if not isinstance(context, dict) or not required.issubset(context):
            _fail('EXECUTION_CONTEXT_REQUIRED', 'Service-bound workspace/task/revision/context is required')
        if any(not isinstance(context[k], str) or not context[k].strip() for k in required):
            _fail('EXECUTION_CONTEXT_REQUIRED', 'Execution context fields must be nonempty strings')
        self.context = {k: context[k] for k in sorted(required)}
        root = Path(context['workspace_root'])
        if not root.is_absolute() or not (root / 'files').is_dir():
            _fail('INVALID_WORKSPACE', 'Existing absolute workspace/files directory required')
        self.files = (root / 'files').resolve()
        self.context['workspace_root'] = str(root.resolve())
        c = self.a._profile()
        if c.get('program_transfer_enabled') is not True or c.get('allow_program_transfer') is not True:
            _fail('PROGRAM_TRANSFER_NOT_AUTHORIZED', 'Current program-transfer enrollment is required')
        flags = ('heaters_isolated', 'user_present') if self.qualifying else ('heaters_isolated', 'user_present', 'verified_contract')
        if physical and any(c.get(k) is not True for k in flags):
            _fail('PROGRAM_TRANSFER_NOT_AUTHORIZED', 'Current attendance/isolation and qualified private-64 ACK contract required')
        if not isinstance(c.get('expected_hwid'), str) or not c['expected_hwid']:
            _fail('TARGET_IDENTITY_REQUIRED', 'Exact gateway hardware identity must be enrolled')
        self.identity_sha = _hash_claim(c.get('expected_identity_sha256'))
        self.specs = c.get('parameter_reads')
        if (not isinstance(self.specs, list) or not 1 <= len(self.specs) <= 32
                or any(not isinstance(s, dict) or set(s) != {'device', 'count'}
                       or not re.fullmatch(r'D\d{1,4}', str(s.get('device', '')))
                       or type(s.get('count')) is not int or not 1 <= s['count'] <= 64
                       or int(s['device'][1:]) + s['count'] > 4096 for s in self.specs)
                or sum(s['count'] for s in self.specs) > 256):
            _fail('INVALID_PARAMETER_PROFILE', 'Bounded explicit preserved D-register ranges required')
        self.specs = json.loads(json.dumps(self.specs))
        self.approval, self.approval_sha = self._approval()
        if self.operation == 'deployment.execute' and self.approval.get('program_download_authorized') is False:
            _fail('PROGRAM_DOWNLOAD_NOT_AUTHORIZED', 'Current source approval explicitly denies PLC program download')
        self.config_sha = _digest(c)
        if _hash_claim(self.context['baseline_sha256']) != _hash_claim(self.approval['source_sha256']):
            _fail('REVISION_MISMATCH', 'Workspace baseline differs from current approved source')
        directory = c.get('target_lease_dir')
        if not isinstance(directory, str) or not Path(directory).is_absolute():
            _fail('TARGET_LEASE_REQUIRED', 'Explicit shared absolute target lease directory required')
        self.lease_dir = Path(directory).resolve()
        self.lease_dir.mkdir(parents=True, exist_ok=True)
        self.target_key = _digest(dict(hwid=c['expected_hwid'], station=c.get('station', 1)))
        if physical and not self.qualifying:
            self._contract()

    def _contract(self):
        c = self.a.config
        proof, _ = _read_json(self._inside(c.get('contract_path')), _hash_claim(c.get('contract_sha256')))
        if (proof.get('schema_version') != 1 or proof.get('kind') != 'ss2_program_contract'
                or proof.get('qualified') is not True
                or proof.get('qualification_level') != 'ACK_SHAPE_ONLY'
                or proof.get('mode_effect_verified') is not False or proof.get('program_transfer_verified') is not False
                or proof.get('context') != self.context or proof.get('approval_sha256') != self.approval_sha
                or proof.get('identity_sha256') != self.identity_sha or proof.get('target_key') != self.target_key
                or proof.get('usb_hwid') != c['expected_hwid'] or proof.get('station') != c.get('station', 1)
                or proof.get('entry_ack') != '01190101' or proof.get('exit_ack') != '01190100'
                or proof.get('protocol_sha256') != _sha(Path(__file__).read_bytes())
                or proof.get('parser_sha256') != _parser_sha()):
            _fail('CONTRACT_PROOF_MISMATCH', 'Qualified private-64 contract belongs to different code/identity/context/revision')
        self.contract_valid = True

    def _current(self):
        c = self.a._profile()
        flags = ('program_transfer_enabled', 'allow_program_transfer', 'heaters_isolated', 'user_present')
        if not self.qualifying:
            flags += ('verified_contract',)
        if _digest(c) != self.config_sha or any(c.get(k) is not True for k in flags):
            _fail('ENROLLMENT_CHANGED', 'Current program-transfer enrollment changed')
        _, digest = self._approval()
        if digest != self.approval_sha:
            _fail('APPROVAL_CHANGED', 'Current approved revision changed')
        if not self.qualifying:
            self._contract()

    def _new_dir(self, base: str, kind: str) -> Path:
        parent = self._inside(base)
        parent.mkdir(parents=True, exist_ok=True)
        path = parent / (kind + '-' + uuid.uuid4().hex)
        path.mkdir()
        return path

    def _binding(self) -> dict:
        return dict(context=self.context, config_sha256=self.config_sha,
                    approval_sha256=self.approval_sha, identity_sha256=self.identity_sha,
                    parameter_reads=self.specs, target_key=self.target_key)

    def _bound(self, value: dict, kind: str, *, renewal: bool = False):
        if value.get('schema_version') != 1 or value.get('kind') != kind:
            _fail('INVALID_ARTIFACT', 'Wrong transfer artifact schema/kind')
        binding = self._binding()
        if any(value.get(k) != v for k, v in binding.items() if not (renewal and k == 'config_sha256')):
            _fail('TRANSFER_BINDING_MISMATCH', 'Artifact belongs to a different target/context/revision/enrollment')

    def _write_journal(self, value: dict, *, new: bool = False):
        data = _json_bytes(value)
        if new:
            atomic_write(self.journal_path, data)
        else:
            path = self._inside(self.journal_path)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.journal-', delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        self.journal = value

    def _new_journal(self, directory: Path, operation: str, **details):
        self.journal_path = directory / 'journal.json'
        value = dict(schema_version=1, kind='ss2_deployment_journal', **self._binding(),
                     execution_id=uuid.uuid4().hex, operation=operation, created_at=_now(),
                     state='admitted', mutation_attempted=False, events=[], **details)
        self._write_journal(value, new=True)
        self.journal_owned = True

    def _event(self, action: str, *, mutation: bool = False, **details):
        value = json.loads(json.dumps(self.journal))
        value['events'].append(dict(action=action, at=_now(), **details))
        value['state'] = action
        value['mutation_attempted'] = value['mutation_attempted'] or mutation
        writing = action in ('program_write_intent', 'descriptor_write_intent')
        value['program_write_attempted'] = value.get('program_write_attempted', False) or writing
        self._write_journal(value)  # Durable BEFORE the physical attempt.
        self.mutation_attempted = value['mutation_attempted']
        self.program_write_attempted = value['program_write_attempted']
        self.a.transfer_mutation_attempted = self.mutation_attempted
        self.a.program_write_attempted = self.program_write_attempted

    def _os_lock(self):
        if self.lock_stream is not None:
            return
        stream = (self.lease_dir / ('target-' + self.target_key + '.lock')).open('a+b')
        try:
            if stream.tell() == 0:
                stream.write(b'\x00')
                stream.flush()
                os.fsync(stream.fileno())
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            _fail('TARGET_BUSY', 'Another live process holds this target; reconciliation cannot join it')
        self.lock_stream = stream

    def _unlock(self):
        if self.lock_stream is not None:
            stream, self.lock_stream = self.lock_stream, None
            try:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()

    def _lease(self, *, recovery: bool = False):
        self._os_lock()
        path = self.lease_dir / ('target-' + self.target_key + '.json')
        token = uuid.uuid4().hex
        value = dict(token=token, journal_path=str(self.journal_path), identity_sha256=self.identity_sha,
                     target_key=self.target_key, context=self.context, created_at=_now())
        try:
            atomic_write(path, _json_bytes(value))
        except (FileExistsError, ValueError):
            old, _ = _read_json(path)
            if not recovery or any(old.get(k) != value[k] for k in ('journal_path', 'identity_sha256', 'target_key', 'context')):
                _fail('TARGET_ALREADY_ADMITTED', 'Another transaction or unresolved result owns the target lease')
            token = old['token']
        self.lease_path, self.lease_token = path, token
        self.journal_owned = True

    def _release(self):
        if self.lease_path is not None:
            value, _ = _read_json(self.lease_path)
            if value.get('token') != self.lease_token:
                _fail('TARGET_LEASE_CHANGED', 'Target lease ownership changed')
            self.lease_path.unlink()
            self.lease_path = None

    def _identify_preflight(self) -> dict:
        self._current()
        identity = self.t.identify()
        if (identity.get('identity_sha256') != self.identity_sha
                or identity.get('native_family') != 'SS2' or identity.get('capacity_words') != 7920):
            _fail('TARGET_IDENTITY_MISMATCH', 'Current native target differs from the enrolled SS2 identity')
        preflight = self.t.preflight()
        if any(preflight.get(k) is not False for k in ('running', 'protected', 'plc_id_enabled')):
            _fail('PLC_PREFLIGHT_FAILED', 'Fresh STOP and unprotected/no-ID state required')
        return identity

    @staticmethod
    def _descriptor(value):
        if value != [65535, 65535]:
            _fail('INITIAL_VALUE_UNSUPPORTED', 'Existing initial-value blocks cannot be silently relocated or erased')

    def _check_parameters(self, values: list[dict]) -> list[dict]:
        if (not isinstance(values, list) or len(values) != len(self.specs)
                or any(not isinstance(v, dict) or v.get('device') != s['device'] or v.get('count') != s['count']
                       or not isinstance(v.get('values'), list) or len(v['values']) != s['count']
                       or any(type(n) is not int or not 0 <= n <= 65535 for n in v['values'])
                       for v, s in zip(values, self.specs))):
            _fail('PARAMETER_READ_MISMATCH', 'Preserved parameter read shape is invalid')
        return values

    def _params(self) -> list[dict]:
        return self._check_parameters(self.t.read_parameters(self.specs))

    def _mode(self, enabled: bool):
        self._current()
        self._event('mode_enter_intent' if enabled else 'mode_exit_intent', mutation=True)
        self.t.mode(enabled)
        self._event('mode_enter_ack' if enabled else 'mode_exit_ack')

    def _image(self) -> bytes:
        value = self.t.read_image()
        if not isinstance(value, bytes) or len(value) != PROGRAM_BYTES:
            _fail('INVALID_PROGRAM_IMAGE', 'Independent full 7920-word raw image is required')
        return value

    def _stopped(self):
        if _device_read(self.a, self.t.trace, 'M1000', 1) != [0]:
            _fail('PLC_NOT_STOPPED', 'Fresh STOP verification failed')

    def _load_backup(self, path: Path, expected: str, *, renewal: bool = False) -> tuple[dict, bytes, str]:
        backup, digest = _read_json(path, expected)
        self._bound(backup, 'ss2_raw_backup', renewal=renewal)
        self._descriptor(backup.get('initial_value_descriptor'))
        self._check_parameters(backup.get('parameters'))
        image = read_bytes(self._inside(backup.get('program_image_path')), PROGRAM_BYTES)
        if len(image) != PROGRAM_BYTES or _sha(image) != backup.get('image_sha256'):
            _fail('BACKUP_IMAGE_MISMATCH', 'Full raw backup changed or was truncated')
        return backup, image, digest

    def _candidate(self, path: Path, expected: str) -> dict:
        native = read_native(path, _hash_claim(expected))
        if (native['source_sha256'] != _hash_claim(self.approval['source_sha256'])
                or native['payload_sha256'] != _hash_claim(self.approval['payload_sha256'])):
            _fail('APPROVED_SOURCE_MISMATCH', 'Candidate is not the current approved native program')
        # The supported transfer deliberately writes no appended initialization blob.
        data = read_bytes(path)
        marker = b'<TypeBegin>\nFunction=Initial Value\n<TypeEnd>\n<DataBegin>\n'
        if data.count(marker) != 1 or not data.split(marker, 1)[1].startswith(b'<DataEnd>'):
            _fail('INITIAL_VALUE_UNSUPPORTED', 'Candidate initial-value data is unsupported')
        if active_program(native['image_le']) != native['payload_le']:
            _fail('CANDIDATE_PROGRAM_MISMATCH', 'Candidate instruction boundary differs')
        return native

    def _qualify(self, args: dict) -> dict:
        directory = self._new_dir(args.get('output_dir', 'contracts'), 'qualify')
        self._new_journal(directory, 'program.contract.qualify')
        self._lease()
        self._identify_preflight()
        self._mode(True)
        self._mode(False)
        self._stopped()
        proof = dict(schema_version=1, kind='ss2_program_contract', qualified=True,
                     qualification_level='ACK_SHAPE_ONLY', mode_effect_verified=False, program_transfer_verified=False,
                     context=self.context, approval_sha256=self.approval_sha, identity_sha256=self.identity_sha,
                     target_key=self.target_key, usb_hwid=self.a.config['expected_hwid'],
                     station=self.a.config.get('station', 1), created_at=_now(),
                     entry_ack='01190101', exit_ack='01190100',
                     parser_sha256=_parser_sha(), protocol_sha256=_sha(Path(__file__).read_bytes()),
                     trace=list(getattr(self.t, 'trace', [])), stop_verified=True,
                     program_read=False, program_write=False, rtc_write=False)
        path = directory / 'contract.json'
        data = _json_bytes(proof)
        atomic_write(path, data)
        self._event('contract_qualified', contract_path=self._relative(path), contract_sha256=_sha(data))
        self._release()
        return dict(ok=True, status='ok', contract_path=self._relative(path), contract_sha256=_sha(data),
                    qualification_level='ACK_SHAPE_ONLY', mode_effect_verified=False, program_transfer_verified=False,
                    qualified=True, plc_program_changed=False, stop_verified=True, **self._journal_result())

    def _upload(self, args: dict) -> dict:
        directory = self._new_dir(args.get('output_dir', 'backups'), 'upload')
        self._new_journal(directory, 'project.upload')
        self._lease()
        identity = self._identify_preflight()
        descriptor = self.t.read_descriptor()
        self._descriptor(descriptor)
        parameters = self._params()
        self._event('backup_preflight', identity=identity, initial_value_descriptor=descriptor, parameters=parameters)
        self._mode(True)
        image = self._image()
        raw_path = directory / 'program-image.le.bin'
        atomic_write(raw_path, image)
        self._event('raw_backup_saved', program_image_path=self._relative(raw_path), image_sha256=_sha(image))
        self._mode(False)
        self._stopped()
        if self.t.read_descriptor() != descriptor or self._params() != parameters:
            _fail('BACKUP_STATE_CHANGED', 'Descriptor or preserved parameters changed during backup')
        backup = dict(schema_version=1, kind='ss2_raw_backup', **self._binding(), created_at=_now(),
                      program_image_path=self._relative(raw_path), image_sha256=_sha(image),
                      initial_value_descriptor=descriptor, parameters=parameters,
                      identity=identity, stop_verified=True, native_container_reconstructed=False)
        manifest_path = directory / 'backup.json'
        data = _json_bytes(backup)
        atomic_write(manifest_path, data)
        self._event('backup_verified', backup_path=self._relative(manifest_path), backup_sha256=_sha(data))
        self._release()
        return dict(ok=True, status='ok', manifest_path=self._relative(manifest_path), manifest_sha256=_sha(data),
                    backup_path=self._relative(manifest_path), backup_sha256=_sha(data),
                    raw_words=7920, stop_verified=True, native_container_reconstructed=False,
                    plc_program_changed=False, **self._journal_result())

    def _prepare(self, args: dict) -> dict:
        path = self._inside(args.get('input_path'))
        candidate = self._candidate(path, args.get('expected_sha256'))
        backup_path = self._inside(args.get('backup_path'))
        backup, image, backup_sha = self._load_backup(backup_path, args.get('expected_backup_sha256'))
        if not backup.get('stop_verified'):
            _fail('BACKUP_UNVERIFIED', 'Fresh complete STOP backup is required')
        directory = self._new_dir('plans', 'plan')
        plan = dict(schema_version=1, kind='ss2_deployment_plan', **self._binding(), created_at=_now(),
                    candidate_path=self._relative(path), source_sha256=candidate['source_sha256'],
                    payload_sha256=candidate['payload_sha256'], payload_bytes=len(candidate['payload_le']),
                    backup_path=self._relative(backup_path), backup_sha256=backup_sha,
                    expected_image_sha256=_sha(candidate['payload_le'] + image[len(candidate['payload_le']):]),
                    tail_sha256=_sha(image[len(candidate['payload_le']):]), no_automatic_run=True)
        plan_path = directory / 'plan.json'
        data = _json_bytes(plan)
        atomic_write(plan_path, data)
        return dict(ok=True, status='ok', plan_path=self._relative(plan_path), plan_sha256=_sha(data),
                    candidate_sha256=candidate['source_sha256'], payload_sha256=candidate['payload_sha256'],
                    word_count=candidate['word_count'], plc_program_changed=False, vendor_compile=False)

    def _load_plan(self, args: dict, *, renewal: bool = False):
        path = self._inside(args['plan_path'])
        plan, plan_sha = _read_json(path, args['expected_sha256'])
        self._bound(plan, 'ss2_deployment_plan', renewal=renewal)
        candidate = self._candidate(self._inside(plan['candidate_path']), plan['source_sha256'])
        backup, image, _ = self._load_backup(self._inside(plan['backup_path']), plan['backup_sha256'], renewal=renewal)
        expected = candidate['payload_le'] + image[len(candidate['payload_le']):]
        if (plan.get('payload_sha256') != candidate['payload_sha256']
                or plan.get('expected_image_sha256') != _sha(expected)
                or plan.get('payload_bytes') != len(candidate['payload_le'])
                or plan.get('tail_sha256') != _sha(image[len(candidate['payload_le']):])):
            _fail('PLAN_PROGRAM_MISMATCH', 'Immutable plan no longer matches candidate and preserved backup tail')
        return path, plan, plan_sha, candidate, backup, image, expected

    def _execute(self, args: dict) -> dict:
        path, plan, plan_sha, candidate, backup, old_image, expected = self._load_plan(args)
        directory = self._new_dir('executions', 'execute')
        self._new_journal(directory, 'deployment.execute', plan_path=self._relative(path), plan_sha256=plan_sha)
        admission_dir = self._inside('admissions')
        admission_dir.mkdir(exist_ok=True)
        try:
            atomic_write(admission_dir / (plan_sha + '.json'), _json_bytes(dict(
                journal_path=self._relative(self.journal_path), execution_id=self.journal['execution_id'], plan_sha256=plan_sha)))
        except (FileExistsError, ValueError):
            _fail('PLAN_ALREADY_ADMITTED', 'Plan is consumed by an earlier process; reconcile its journal, never replay')
        self._lease()
        self._identify_preflight()
        self._descriptor(self.t.read_descriptor())
        if self._params() != backup['parameters']:
            _fail('BASELINE_PARAMETERS_CHANGED', 'Preserved parameters differ from prepared backup')
        self._mode(True)
        if self._image() != old_image:
            _fail('BASELINE_PROGRAM_CHANGED', 'Current full PLC image differs from prepared backup; no program bytes written')
        payload = candidate['payload_le']
        for offset in range(0, len(payload), 32):
            self._current()
            chunk = payload[offset:offset + 32]
            self._event('program_write_intent', mutation=True, offset_words=offset // 2, bytes=len(chunk), sha256=_sha(chunk))
            self.t.write_program(offset // 2, chunk)
            self._event('program_write_ack', offset_words=offset // 2)
        self._current()
        self._event('descriptor_write_intent', mutation=True)
        self.t.write_descriptor()
        self._current()
        self._event('syntax_finalize_intent', mutation=True)
        finalized = self.t.finalize()
        if finalized.get('syntax_code') != 0:
            _fail('PLC_SYNTAX_FAULT', 'Finalization did not independently observe D1004=0')
        # Proven native upload/readback enters with 1 and exits with 0. Download
        # has no separately proved exit; this is a new verification phase.
        self._mode(True)
        observed = self._image()
        readback_path = directory / 'readback-image.le.bin'
        atomic_write(readback_path, observed)
        self._mode(False)
        self._stopped()
        self._descriptor(self.t.read_descriptor())
        if observed != expected or active_program(observed) != payload:
            _fail('PROGRAM_READBACK_MISMATCH', 'Complete candidate image or preserved tail differs after transfer')
        if self._params() != backup['parameters']:
            _fail('PARAMETERS_CHANGED', 'Preserved parameters changed during transfer')
        self._event('verified', readback_image_path=self._relative(readback_path), image_sha256=_sha(observed),
                    stop_verified=True, parameters_preserved=True, readback_verified=True)
        self._release()
        return dict(ok=True, status='ok', readback_verified=True, program_verified=True, stop_verified=True,
                    parameters_preserved=True, tail_preserved=True, candidate_sha256=candidate['source_sha256'],
                    payload_sha256=candidate['payload_sha256'], plc_program_changed=observed != old_image,
                    retry_allowed=False, **self._journal_result())

    def _reconcile(self, args: dict) -> dict:
        self.journal_path = self._inside(args.get('journal_path'))
        journal, _ = _read_json(self.journal_path, args.get('expected_sha256'))
        self._bound(journal, 'ss2_deployment_journal', renewal=True)
        self.journal = journal
        self._lease(recovery=True)
        self.mutation_attempted = journal.get('mutation_attempted') is True
        self.program_write_attempted = journal.get('program_write_attempted') is True
        if not journal.get('plan_path'):
            return self._reconcile_mode_only()
        _, _, _, candidate, backup, old_image, expected = self._load_plan(
            dict(plan_path=journal['plan_path'], expected_sha256=journal['plan_sha256']), renewal=True)
        self._identify_preflight()
        self._mode(True)
        observed = self._image()
        self._mode(False)
        self._stopped()
        descriptor = self.t.read_descriptor()
        parameters = self._params()
        syntax = _device_read(self.a, self.t.trace, 'D1004', 1)
        exact_data = descriptor == [65535, 65535] and parameters == backup['parameters'] and syntax == [0]
        outcome = 'candidate_current' if observed == expected and exact_data else (
            'backup_current' if observed == old_image and exact_data else 'partial_or_unknown')
        state = {'candidate_current': 'reconciled_candidate', 'backup_current': 'reconciled_backup'}.get(outcome, 'unknown_outcome')
        self._event(state, program_outcome=outcome, observed_image_sha256=_sha(observed),
                    stop_verified=True, parameters_preserved=parameters == backup['parameters'])
        if outcome != 'partial_or_unknown':
            self._release()
        return dict(ok=outcome != 'partial_or_unknown', status=state, state=state, program_outcome=outcome,
                    readback_verified=outcome == 'candidate_current', program_verified=outcome == 'candidate_current',
                    stop_verified=True, retry_allowed=False, plc_program_changed=None, **self._journal_result())

    def _reconcile_mode_only(self) -> dict:
        self._identify_preflight()
        self._mode(True)
        image = self._image()
        path = self.journal_path.parent / ('recovery-' + uuid.uuid4().hex + '.le.bin')
        atomic_write(path, image)
        self._mode(False)
        self._stopped()
        descriptor = self.t.read_descriptor()
        parameters = self._params()
        self._event('mode_closed', recovery_image_path=self._relative(path), image_sha256=_sha(image),
                    initial_value_descriptor=descriptor, parameters=parameters, stop_verified=True,
                    program_outcome='observed_only')
        self._release()
        return dict(ok=True, status='reconciled_mode', state='mode_closed', program_outcome='observed_only',
                    readback_verified=False, program_verified=False, stop_verified=True,
                    plc_program_changed=False, runtime_state_unknown=False, retry_allowed=False,
                    **self._journal_result())

    def _journal_result(self) -> dict:
        if self.journal_path is None or not self.journal_path.is_file():
            return {}
        try:
            digest = _sha(read_bytes(self.journal_path))
        except (OSError, ValueError):
            return dict(journal_path=self._relative(self.journal_path), journal_sha256=None, journal_unreadable=True)
        return dict(journal_path=self._relative(self.journal_path), journal_sha256=digest)

    def invoke(self, operation: str, args: dict) -> dict:
        self.operation = operation
        # The same helper object may be reused; uncertainty belongs to the durable
        # transaction being invoked, never an earlier successful backup operation.
        self.journal = None
        self.journal_path = None
        self.journal_owned = False
        self.mutation_attempted = False
        self.program_write_attempted = False
        self.lease_path = None
        self.lease_token = None
        self.contract_valid = False
        self.qualifying = False
        self.recovering = operation == 'deployment.reconcile'
        self.a.transfer_mutation_attempted = False
        self.a.program_write_attempted = False
        try:
            if (operation not in self._FIELDS or not isinstance(args, dict)
                    or set(args) - (self._FIELDS.get(operation, set()) | {'_execution_context', 'universal_target'})):
                _fail('INVALID_ARGUMENTS', 'Unknown typed transfer operation or fields')
            self.qualifying = operation == 'program.contract.qualify'
            # A failed qualification must be recoverable before a qualified proof
            # exists. Only its exact identity/context-bound journal admits this.
            if operation == 'deployment.reconcile' and isinstance(args.get('_execution_context'), dict):
                root = Path(args['_execution_context'].get('workspace_root', '')) / 'files'
                requested = Path(args.get('journal_path', ''))
                path = (requested if requested.is_absolute() else root / requested).resolve()
                if path.is_relative_to(root.resolve()):
                    pending, _ = _read_json(path, args.get('expected_sha256'))
                    self.qualifying = pending.get('operation') == 'program.contract.qualify'
            self._session(args, physical=operation != 'deployment.prepare')
            result = {'program.contract.qualify': self._qualify, 'project.upload': self._upload, 'deployment.prepare': self._prepare,
                      'deployment.execute': self._execute, 'deployment.reconcile': self._reconcile}[operation](args)
            result['trace'] = list(getattr(self.t, 'trace', []))
            result['read_trace'] = list(getattr(self.a, 'trace', []))
            return result
        except Exception as exc:
            unknown = self.mutation_attempted
            if self.journal is not None and self.journal_owned:
                try:
                    self._event('unknown_outcome' if unknown else 'blocked', code=getattr(exc, 'code', 'TRANSFER_ERROR'), message=str(exc))
                except (OSError, ValueError):
                    pass  # Earlier fsynced intent remains authoritative.
            if not unknown and self.lease_path is not None:
                try:
                    self._release()
                except (OSError, ValueError):
                    pass
            try:
                self.a._close()
            except (OSError, ValueError):
                pass
            return dict(ok=False, status='unknown_outcome' if unknown else 'blocked', state='unknown_outcome' if unknown else 'blocked',
                        code=getattr(exc, 'code', 'TRANSFER_ERROR'), message=str(exc), retry_allowed=False,
                        plc_program_changed=None if self.program_write_attempted else False,
                        runtime_state_unknown=unknown, requires_contract_qualification=not self.contract_valid,
                        trace=list(getattr(self.t, 'trace', [])), read_trace=list(getattr(self.a, 'trace', [])),
                        **self._journal_result())
        finally:
            self._unlock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parser_sha() -> str:
    from . import dvp_native
    return _sha(Path(dvp_native.__file__).read_bytes())
