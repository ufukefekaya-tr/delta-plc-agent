"""Host-pinned requests and parameters; the PLC remains owner of output logic.

The catalog is a user project artifact, never a built-in machine recipe. A lost
request/release acknowledgement consumes the command and fences the target until
read-only reconciliation proves its recorded request and edge memory released.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any

from .storage import canonical_digest, write_json


MACHINE_COMMAND_OPERATIONS = ('machine.command', 'machine.parameter.write')
MACHINE_READ_OPERATIONS = ('machine.profile', 'machine.read', 'machine.status', 'machine.observe', 'machine.reconcile')
MACHINE_OPERATIONS = MACHINE_COMMAND_OPERATIONS + MACHINE_READ_OPERATIONS
MACHINE_FIELDS = {'machine.profile': set(), 'machine.command': {'command', 'selector', 'command_id'},
                  'machine.parameter.write': {'parameter', 'value', 'command_id'},
                  'machine.read': {'device', 'count'}, 'machine.status': set(),
                  'machine.observe': set(), 'machine.reconcile': {'journal_path'}}
_CONTEXT = ('workspace_id', 'task_id', 'revision', 'context_digest', 'workspace_root', 'baseline_sha256')


def _fail(code, message):
    from .dvp_serial import DvpSerialError
    raise DvpSerialError(code, message)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:sha256:)?[a-fA-F0-9]{64}', value):
        _fail('INVALID_DIGEST', 'Exact SHA-256 required')
    return value.removeprefix('sha256:').lower()


def _absolute(value):
    if not isinstance(value, str) or not Path(value).is_absolute():
        _fail('INVALID_PROFILE_PATH', 'An explicit host absolute path is required')
    return Path(value).resolve()


def _json(path, expected=None):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        _fail('INVALID_ARTIFACT', 'Existing bounded JSON artifact required')
    data = path.read_bytes()
    if expected is not None and _sha(data) != _hash(expected):
        _fail('ARTIFACT_SHA_MISMATCH', 'Pinned artifact changed')
    def unique(items):
        value = {}
        for key, item in items:
            if key in value:
                _fail('INVALID_ARTIFACT', 'Duplicate JSON keys are forbidden')
            value[key] = item
        return value
    value = json.loads(data.decode('utf-8'), object_pairs_hook=unique)
    if not isinstance(value, dict):
        _fail('INVALID_ARTIFACT', 'JSON object required')
    return value


def _device(device):
    match = re.fullmatch(r'([MDY])(\d{1,4})', str(device))
    if not match:
        _fail('INVALID_READ', 'Typed M/D/Y device required')
    kind = match[1]
    try:
        number = int(match[2], 8 if kind == 'Y' else 10)
    except ValueError:
        _fail('INVALID_READ', 'Y addresses use octal notation')
    limit = {'M': 1536, 'D': 4096, 'Y': 256}[kind]
    if number >= limit:
        _fail('INVALID_READ', 'Device exceeds the documented range')
    return kind, number


class MachineControl:
    """One bounded operation under both transport ownership and durable admission."""

    def __init__(self, adapter):
        self.a = adapter
        self.trace = []
        self.lock_stream = None
        self.journal = None
        self.journal_path = None
        self.attempted = False
        self.strict = False
        self.guard_installed = False
        self.previous_send_guard = None

    def _context_current(self):
        root = Path(self.context['workspace_root'])
        manifest = _json(root / 'manifest.json')
        claim = manifest.pop('context_digest', None)
        if claim != canonical_digest(manifest) or claim != self.context['context_digest']:
            _fail('WORKSPACE_CONTEXT_DRIFT', 'Service workspace manifest changed')
        for key in ('workspace_id', 'task_id', 'revision', 'baseline_sha256'):
            if manifest.get(key) != self.context[key]:
                _fail('WORKSPACE_CONTEXT_DRIFT', 'Current Service context differs from workspace')
        baseline = manifest.get('baseline_file')
        if not isinstance(baseline, str) or Path(baseline).name != baseline:
            _fail('INVALID_WORKSPACE', 'Immutable baseline filename required')
        path = root / 'files' / baseline
        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            _fail('INVALID_WORKSPACE', 'Bounded immutable native baseline required')
        if _sha(path.read_bytes()) != _hash(self.context['baseline_sha256']):
            _fail('BASELINE_DRIFT', 'Imported baseline changed')

    def _lease(self, c):
        lease = _json(_absolute(c.get('port_lease_path')))
        try:
            expiry = datetime.fromisoformat(lease['expires_at'].replace('Z', '+00:00'))
        except (KeyError, ValueError, AttributeError):
            _fail('PORT_LEASE_INVALID', 'Current shared port lease expiry required')
        if (lease.get('schema_version') != 1 or lease.get('status') != 'HELD'
                or lease.get('owner') != c.get('port_lease_owner')
                or lease.get('token') != c.get('port_lease_token')
                or not c.get('port_lease_owner') or not c.get('port_lease_token')
                or lease.get('port') != c.get('port') or expiry.tzinfo is None
                or datetime.now(timezone.utc) >= expiry):
            _fail('PORT_LEASE_NOT_OWNED', 'Shared COM port is not currently owned by this enrolled client')

    def _live(self, c, *, required=False):
        try:
            proof = _json(_absolute(c.get('live_revision_evidence_path')),
                          _hash(c.get('live_revision_evidence_sha256')))
            expected = dict(contract_id=self.catalog['contract_id'], revision=self.catalog['revision'],
                            source_sha256=self.catalog['reference_source_sha256'],
                            payload_sha256=self.catalog['reference_payload_sha256'],
                            identity_sha256=self.identity_sha, usb_hwid=c['expected_hwid'],
                            station=c.get('station', 1), route=c.get('route', 'serial_ascii'), context=self.context)
            if (proof.get('schema_version') != 1 or proof.get('verified') is not True
                    or proof.get('kind') not in ('HUMAN_CONFIRMED_LIVE_REVISION',
                                                'HUMAN_CONFIRMED_COMPILE_SAVE_LOAD', 'INDEPENDENT_PROGRAM_READBACK')
                    or any(proof.get(k) != v for k, v in expected.items())
                    or not isinstance(proof.get('evidence'), str) or not proof['evidence'].strip()):
                _fail('LIVE_REVISION_UNVERIFIED', 'Current exact live revision evidence does not match enrollment')
            source = _absolute(c.get('machine_source_path'))
            if (not source.is_file() or source.stat().st_size > 2 * 1024 * 1024
                    or _sha(source.read_bytes()) != _hash(c.get('machine_source_sha256'))
                    or _hash(c.get('machine_source_sha256')) != self.catalog['reference_source_sha256']
                    or self.context['revision'] != self.catalog['revision']
                    or _hash(self.context['baseline_sha256']) != self.catalog['reference_source_sha256']):
                _fail('SOURCE_REVISION_CHANGED', 'Current source, imported baseline and contract must agree')
            return True
        except (ValueError, OSError, KeyError) as exc:
            if required:
                _fail('LIVE_REVISION_UNVERIFIED', str(exc))
            return False

    def _gates(self, *, strict=None):
        c = self.a._profile()
        if canonical_digest(c) != self.config_sha:
            _fail('MACHINE_ENROLLMENT_CHANGED', 'Current route/authority enrollment changed')
        self._lease(c)
        self._context_current()
        catalog = _json(_absolute(c.get('machine_catalog_path')), _hash(c.get('machine_catalog_sha256')))
        if catalog != self.catalog:
            _fail('CATALOG_CHANGED', 'Frozen machine catalog changed')
        if self.strict if strict is None else strict:
            permission = 'allow_machine_parameters' if self.parameter_operation else 'allow_machine_commands'
            # Only catalog-bound M/D operations may use attended testing.
            # Transfer and RUN/STOP retain their separate isolation gates.
            isolated = c.get('heaters_isolated') is True
            attended = (c.get('attended_machine_test_authorized') is True
                        and c.get('independent_power_cutoff_available') is True)
            if (any(c.get(k) is not True for k in ('machine_control_enabled', permission, 'user_present'))
                    or not (isolated or attended)):
                _fail('MACHINE_NOT_AUTHORIZED',
                      'Current command permission, operator and isolation or explicit attended cutoff required')
            self._live(c, required=True)
        elif self.recovering:
            self._live(c, required=True)
        return c

    def _context(self, args):
        context = args.get('_execution_context')
        if (not isinstance(context, dict) or any(not isinstance(context.get(k), str) or not context[k]
                                              for k in _CONTEXT)):
            _fail('EXECUTION_CONTEXT_REQUIRED', 'Service-bound workspace/revision context required')
        self.context = {k: context[k] for k in _CONTEXT}
        self.context['workspace_root'] = str(_absolute(self.context['workspace_root']))

    def _catalog(self, c):
        self.catalog = _json(_absolute(c.get('machine_catalog_path')), _hash(c.get('machine_catalog_sha256')))
        if (self.catalog.get('schema_version') != 1 or not isinstance(self.catalog.get('commands'), dict)
                or not 0 <= len(self.catalog['commands']) <= 64
                or not isinstance(self.catalog.get('read_devices'), list)
                or not 1 <= len(self.catalog['read_devices']) <= 4096
                or not isinstance(self.catalog.get('contract_id'), str) or not self.catalog['contract_id']
                or not isinstance(self.catalog.get('revision'), str) or not self.catalog['revision']):
            _fail('INVALID_MACHINE_CATALOG', 'Explicit bounded typed machine contract required')
        self.read_devices = set(self.catalog['read_devices'])
        for device in self.read_devices:
            _device(device)
        for key in ('reference_source_sha256', 'reference_payload_sha256'):
            _hash(self.catalog.get(key))
        for command, spec in self.catalog['commands'].items():
            if not isinstance(command, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', command):
                _fail('INVALID_COMMAND_CATALOG', 'Bounded profile command names required')
            args = {'selector': 1} if isinstance(spec, dict) and spec.get('selector') else {}
            self._members(command, args)
        parameters = self.catalog.get('parameters', {})
        if not isinstance(parameters, dict) or len(parameters) > 256:
            _fail('INVALID_PARAMETER_CATALOG', 'Bounded parameter descriptors required')
        for name in parameters:
            self._parameter_spec(name)

    def _session(self, op, args):
        self._context(args)
        c = self.a._profile()
        self.config_sha = canonical_digest(c)
        self._catalog(c)
        if not isinstance(c.get('expected_hwid'), str) or not c['expected_hwid']:
            _fail('TARGET_IDENTITY_REQUIRED', 'Exact USB gateway identity required')
        self.identity_sha = _hash(c.get('expected_identity_sha256'))
        self.state_dir = _absolute(c.get('machine_state_dir'))
        self.target_key = canonical_digest(dict(hwid=c['expected_hwid'], station=c.get('station', 1)))
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.fence_path = self.state_dir / ('target-' + self.target_key + '.json')
        self.strict = op in MACHINE_COMMAND_OPERATIONS
        self.parameter_operation = op == 'machine.parameter.write'
        self.recovering = op == 'machine.reconcile'
        if op == 'machine.command' and (type(c.get('pulse_ms')) is not int or not 50 <= c['pulse_ms'] <= 500):
            _fail('INVALID_PULSE_PROFILE', 'Host-enrolled pulse_ms must be 50..500')
        self._gates()

    def _lock(self):
        stream = (self.state_dir / ('target-' + self.target_key + '.lock')).open('a+b')
        try:
            if stream.tell() == 0:
                stream.write(b'\0'); stream.flush(); os.fsync(stream.fileno())
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            _fail('MACHINE_TARGET_BUSY', 'Another process owns this target transport')
        self.lock_stream = stream

    def _unlock(self):
        if self.lock_stream:
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

    def _exchange(self, request, function, *, write=False, request_member=None, parameter_spec=None):
        from .dvp_transfer import decode_response
        self._gates()
        self.a._open()
        self.a.connection.reset_input_buffer()
        self._gates()  # Last authority refresh after open/bridge/reset, before bytes.
        if request_member is not None:
            self._running()
            baseline = self._read_set([request_member['coil'], *request_member['edge_devices'],
                                       *request_member['stop_guards']])
            if any(baseline.values()):
                _fail('COMMAND_BASELINE_NOT_LOW', 'Final command/edge/stop baseline changed before ON')
            self._gates()  # Nested reads may have consumed the remaining authority.
        if parameter_spec is not None:
            self._parameter_guards(parameter_spec, self.journal['requested_value'])
            self._gates()
        if write:
            if request_member is not None:
                request_member['request_wire_attempted'] = True
            self.attempted = self.a.machine_command_attempted = True
        written = self.a.connection.write(request)
        response = b''
        if written == len(request):
            deadline = time.monotonic() + 2
            for _ in range(4):
                response = self.a._line(deadline)
                if response not in (b'\r\n', b'\n') and not (function not in (5, 6) and response == request):
                    break
        self.trace.append(dict(function=function, tx_hex=request.hex(), rx_hex=response.hex(), timestamp=_now()))
        if written != len(request):
            _fail('SERIAL_WRITE_INCOMPLETE', 'Request partly sent; no automatic replay')
        self._gates()
        return decode_response(response, self.a.config.get('station', 1), function), response

    def _read(self, device, count=1):
        from .dvp_serial import ascii_frame, parse_ascii_response
        from .dvp_transfer import _packet
        kind, number = _device(device)
        if type(count) is not int or not 1 <= count <= 64:
            _fail('INVALID_READ', 'Read count must be 1..64')
        devices = [kind + (format(number + i, 'o') if kind == 'Y' else str(number + i)) for i in range(count)]
        if any(item not in self.read_devices for item in devices):
            _fail('READ_OUTSIDE_MACHINE_CATALOG', 'Entire read must be inside explicit machine catalog')
        station = self.a.config.get('station', 1)
        function = 3 if kind == 'D' else 1
        address = {'M': 0x800, 'D': 0x1000, 'Y': 0x500}[kind] + number
        request = (ascii_frame(station, function, address, count) if kind != 'Y' else
                   _packet(station, b'\x01' + address.to_bytes(2, 'big') + count.to_bytes(2, 'big')))
        _, frame = self._exchange(request, function)
        return parse_ascii_response(frame, station, function, count)

    def _read_set(self, devices):
        result = {}
        ordered = sorted(set(devices), key=_device)
        index = 0
        while index < len(ordered):
            group = [ordered[index]]
            kind, number = _device(group[0])
            index += 1
            while index < len(ordered) and len(group) < 64 and _device(ordered[index]) == (kind, number + len(group)):
                group.append(ordered[index]); index += 1
            result.update(zip(group, self._read(group[0], len(group))))
        return result

    def _identify(self):
        from .dvp_transfer import identity_frame
        data, _ = self._exchange(identity_frame(self.a.config.get('station', 1)), 0x67)
        if len(data) != 11 or data[0] != 10 or data[5:9] != bytes.fromhex('0001000E'):
            _fail('NATIVE_MODEL_MISMATCH', 'Expected native SS2 identity shape required')
        if _sha(data[1:]) != self.identity_sha:
            _fail('TARGET_IDENTITY_MISMATCH', 'Current PLC identity differs from enrolled target')

    def _running(self):
        bits = self._read_set(['M1000', 'M1002'])
        if bits['M1000'] != 1 or bits['M1002'] != 0:
            _fail('PLC_NOT_RUNNING', 'Fresh RUN outside first scan required; MCP never changes RUN/STOP')

    def _save(self):
        self.journal['updated_at'] = _now()
        write_json(self.journal_path, self.journal, replace=True)

    def _fence(self):
        old = _json(self.fence_path) if self.fence_path.exists() else {}
        if old and old.get('journal_path') != str(self.journal_path):
            _fail('MACHINE_FENCE_CHANGED', 'Another journal owns the target uncertainty')
        devices = set(old.get('devices', []))
        if self.journal['kind'] == 'machine_parameter':
            devices.add(self.journal['device'])
        for member in self.journal['members']:
            if member['phase'] != 'unattempted':
                devices.update([member['coil'], *member['edge_devices']])
        write_json(self.fence_path, dict(schema_version=1, target_key=self.target_key,
                   journal_path=str(self.journal_path), command_id=self.journal['command_id'],
                   binding_sha256=self._journal_binding(self.journal), devices=sorted(devices)), replace=True)

    @staticmethod
    def _journal_binding(journal):
        keys = ('command_id', 'command', 'selector', 'target_key', 'catalog_sha256', 'source_sha256', 'identity_sha256', 'context')
        immutable = {k: journal.get(k) for k in keys}
        if journal['kind'] == 'machine_parameter':
            immutable.update({k: journal[k] for k in ('parameter', 'device', 'descriptor_sha256',
                                                       'requested_value', 'wire_value', 'baseline_value')})
        immutable['members'] = [{k: m[k] for k in ('coil', 'edge_devices', 'stop_guards', 'ack_devices', 'ack_expected')}
                                for m in journal['members']]
        return canonical_digest(immutable)

    def _clear_fence(self):
        fence = _json(self.fence_path)
        if fence.get('journal_path') != str(self.journal_path) or fence.get('target_key') != self.target_key:
            _fail('MACHINE_FENCE_CHANGED', 'Target uncertainty owner changed')
        self.fence_path.unlink()

    def _write(self, member, enabled):
        from .dvp_transfer import _packet
        kind, number = _device(member['coil'])
        if kind != 'M' or number >= 1000 or member['coil'] not in self.read_devices:
            _fail('INVALID_COMMAND_CATALOG', 'Only frozen normal SS2 M0..999 request coils can be pulsed')
        self._gates(strict=True)
        if enabled:
            self._running()
        member['phase'] = 'request_intent' if enabled else 'release_intent'
        self._save()  # Durable intent precedes every possible request/release byte.
        self._fence()
        station = self.a.config.get('station', 1)
        body = b'\x05' + (0x800 + number).to_bytes(2, 'big') + (b'\xff\0' if enabled else b'\0\0')
        data, _ = self._exchange(_packet(station, body), 5, write=True,
                                 request_member=member if enabled else None)
        if data != body[1:]:
            _fail('MACHINE_ACK_MISMATCH', 'Request/release ACK mismatch; ON is never replayed')
        member['phase'] = 'request_ack' if enabled else 'release_ack'
        self._save()

    def _members(self, command, args):
        spec = self.catalog['commands'].get(command)
        if not isinstance(spec, dict) or type(spec.get('is_start')) is not bool:
            _fail('INVALID_COMMAND_CATALOG', 'Exact catalog command with is_start bool required')
        selector = spec.get('selector')
        coils = spec.get('coils')
        if not isinstance(coils, list) or not 1 <= len(coils) <= 12:
            _fail('INVALID_COMMAND_CATALOG', 'Bounded command members required')
        indexes = list(range(len(coils)))
        if selector:
            value = args.get('selector')
            if type(value) is not int or not 1 <= value <= len(coils):
                _fail('INVALID_SELECTOR', 'Numeric selector is outside catalog')
            indexes = [value - 1]
        elif 'selector' in args:
            _fail('INVALID_SELECTOR', 'This profile command has no selector')
        members = []
        for i in indexes:
            ack = spec.get('ack_devices')
            if not isinstance(ack, list) or not ack or spec.get('ack_expected') not in (0, 1):
                _fail('INVALID_COMMAND_CATALOG', 'Logical status acknowledgement required')
            edge = spec.get('release_edge_devices', [])
            member = dict(coil=coils[i], edge_devices=[edge[i]] if edge else [],
                          stop_guards=spec['stop_guards'][i], ack_devices=[ack[i]] if len(coils) == len(ack) else ack,
                          ack_expected=spec['ack_expected'], phase='unattempted')
            if _device(member['coil'])[0] != 'M' or _device(member['coil'])[1] >= 1000:
                _fail('INVALID_COMMAND_CATALOG', 'SS2 system relays cannot become machine command coils')
            for device in [member['coil'], *member['edge_devices'], *member['stop_guards'], *member['ack_devices']]:
                if device not in self.read_devices or _device(device)[0] != 'M':
                    _fail('INVALID_COMMAND_CATALOG', 'Command devices must be frozen readable M bits')
            members.append(member)
        return members

    def _released(self, member):
        devices = [member['coil'], *member['edge_devices']]
        for sample in range(2):
            values = self._read_set(devices)
            if any(values.values()):
                if sample == 0:
                    time.sleep(0.05)
                    values = self._read_set(devices)
                if any(values.values()):
                    _fail('MACHINE_RELEASE_UNVERIFIED', 'Request or edge memory remains high')
            if sample == 0:
                time.sleep(0.05)
        member['release_verified'] = True
        member['phase'] = 'released'
        self._save()

    def _pulse(self, member):
        baseline = self._read_set([member['coil'], *member['edge_devices'], *member['stop_guards']])
        if any(baseline.values()):
            _fail('COMMAND_BASELINE_NOT_LOW', 'Request/edge memory/opposing stop baseline must be low')
        self._running()
        before = self._read_set(member['ack_devices'])
        member['logical_before'] = before
        request_error = None
        try:
            self._write(member, True)
            time.sleep(self.a.config['pulse_ms'] / 1000)
        except (ValueError, OSError) as exc:
            request_error = exc
        if request_error is not None and not member.get('request_wire_attempted'):
            raise request_error  # A rejected ON never needs an unsolicited OFF.
        # One permitted OFF attempt even when the ON acknowledgement was lost.
        # Revocation/target drift blocks this release; its durable fence survives.
        try:
            self._write(member, False)
            self._released(member)
        except (ValueError, OSError) as exc:
            member['release_error'] = str(exc)
            member['phase'] = 'unknown_release'
            self._save()
            raise
        if request_error is not None:
            member['request_error'] = str(request_error)
            member['phase'] = 'unknown_effect_released'
            self._save()
            raise request_error
        logical = self._read_set(member['ack_devices'])
        member['logical_after'] = logical
        member['effect_verified'] = all(value == member['ack_expected'] for value in logical.values())
        member['effect_newly_observed'] = member['effect_verified'] and any(
            before[d] != logical[d] for d in logical)
        member['phase'] = 'complete' if member['effect_verified'] else 'effect_not_observed'
        self._save()
        if not member['effect_verified']:
            _fail('COMMAND_EFFECT_NOT_OBSERVED', 'Released request did not show the expected logical state')

    def _command(self, op, args):
        if self.fence_path.exists():
            _fail('MACHINE_TARGET_UNCERTAIN', 'Target has an unresolved request; reconcile without replay')
        identifier = args.get('command_id')
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', identifier):
            _fail('INVALID_COMMAND_ID', 'Unique bounded command_id required')
        self.journal_path = self.state_dir / ('command-' + self.target_key + '-' + _sha(identifier.encode()) + '.json')
        if self.journal_path.exists():
            _fail('COMMAND_ALREADY_ADMITTED', 'Command ID is permanently consumed; do not replay')
        self.journal = dict(schema_version=1, kind='machine_command', target_key=self.target_key,
                            command_id=identifier, operation=op, command=args.get('command'), selector=args.get('selector'),
                            context=self.context, catalog_sha256=self.a.config['machine_catalog_sha256'],
                            source_sha256=self.catalog['reference_source_sha256'], identity_sha256=self.identity_sha,
                            created_at=_now(), status='admitted', members=self._members(args.get('command'), args))
        write_json(self.journal_path, self.journal)
        self._identify()
        self._running()
        try:
            for member in self.journal['members']:
                self._pulse(member)
            self.journal['status'] = 'complete'
            self._save()
            self._clear_fence()
            return self._result(True, 'ok', command_id=identifier, logical_state_verified=True)
        except (ValueError, OSError):
            if any(m.get('request_wire_attempted') and (m.get('request_error') or m.get('release_verified') is not True)
                   for m in self.journal['members']):
                self.journal['status'] = 'unknown_outcome'
            else:
                self.journal['status'] = 'released_effect_unverified' if self.attempted else 'blocked'
                if self.fence_path.exists():
                    self._clear_fence()
            self._save()
            raise

    def _result(self, ok, status, **extra):
        result = dict(ok=ok, status=status, retry_allowed=False, plc_program_changed=False,
                      plc_run_state_changed=False, trace=self.trace, **extra)
        if self.journal is not None:
            result.update(journal_path=str(self.journal_path), command_id=self.journal['command_id'],
                          members=self.journal['members'], journal_status=self.journal['status'])
            if self.journal['kind'] == 'machine_parameter':
                result['parameter_outcome'] = self.journal.get('parameter_outcome', 'unknown')
        return result

    def _parameter_spec(self, name, value=None):
        spec = self.catalog.get('parameters', {}).get(name)
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', name) or not isinstance(spec, dict):
            _fail('INVALID_PARAMETER', 'Exact profile parameter key required')
        kind, number = _device(spec.get('device'))
        signed = spec.get('signed', False)
        minimum, maximum = spec.get('min'), spec.get('max')
        if (kind != 'D' or 1000 <= number < 2000 or spec['device'] not in self.read_devices
                or type(signed) is not bool or type(minimum) is not int or type(maximum) is not int
                or not (-32768 if signed else 0) <= minimum <= maximum <= (32767 if signed else 65535)):
            _fail('INVALID_PARAMETER_CATALOG', 'Only bounded normal D0..999/D2000..4095 16-bit descriptors are writable')
        if value is not None and (type(value) is not int or not minimum <= value <= maximum):
            _fail('PARAMETER_VALUE_OUT_OF_RANGE', 'Integer storage value is outside the selected parameter bounds')
        guards, relations = spec.get('write_guards', []), spec.get('relations', [])
        if not isinstance(guards, list) or len(guards) > 32 or not isinstance(relations, list) or len(relations) > 64:
            _fail('INVALID_PARAMETER_CATALOG', 'Bounded guard/relation lists required')
        total = 0
        for guard in guards:
            if not isinstance(guard, dict) or set(guard) - {'device', 'count', 'equals'}:
                _fail('INVALID_PARAMETER_CATALOG', 'Typed exact equality guard required')
            kind, number = _device(guard.get('device'))
            count, expected = guard.get('count', 1), guard.get('equals')
            if type(count) is not int or not 1 <= count <= 64 or type(expected) is not int or not 0 <= expected <= (65535 if kind == 'D' else 1):
                _fail('INVALID_PARAMETER_CATALOG', 'Guard count/equality value is invalid')
            devices = [kind + (format(number + i, 'o') if kind == 'Y' else str(number + i)) for i in range(count)]
            if not set(devices).issubset(self.read_devices):
                _fail('INVALID_PARAMETER_CATALOG', 'Entire guard must stay in read whitelist')
            total += count
        for relation in relations:
            if (not isinstance(relation, dict) or set(relation) - {'left', 'op', 'right', 'offset'}
                    or relation.get('op') not in ('eq', 'ne', 'lt', 'lte', 'gt', 'gte')
                    or type(relation.get('offset', 0)) is not int or not -65535 <= relation.get('offset', 0) <= 65535):
                _fail('INVALID_PARAMETER_CATALOG', 'Finite typed comparison required; no formulas/scripts')
            for term in (relation.get('left'), relation.get('right')):
                if type(term) is int and -65535 <= term <= 65535 or term == 'value':
                    continue
                if not isinstance(term, str) or term not in self.read_devices:
                    _fail('INVALID_PARAMETER_CATALOG', 'Comparison operand must be value, integer or whitelisted device')
                total += 1
        if total > 256:
            _fail('INVALID_PARAMETER_CATALOG', 'Parameter precondition reads exceed the bounded limit')
        return spec

    def _parameter_guards(self, spec, value):
        for guard in spec.get('write_guards', []):
            if any(v != guard['equals'] for v in self._read(guard['device'], guard.get('count', 1))):
                _fail('PARAMETER_GUARD_NOT_MET', 'Selected parameter is locked by its current profile guard')
        relations = spec.get('relations', [])
        devices = {term for r in relations for term in (r['left'], r['right']) if isinstance(term, str) and term != 'value'}
        current = self._read_set(devices)
        def operand(term):
            if term == 'value':
                return value
            if type(term) is int:
                return term
            measured = current[term]
            signed = any(p.get('device') == term and p.get('signed') is True for p in self.catalog.get('parameters', {}).values())
            return measured - 65536 if signed and measured & 32768 else measured
        compare = {'eq': lambda a, b: a == b, 'ne': lambda a, b: a != b, 'lt': lambda a, b: a < b,
                   'lte': lambda a, b: a <= b, 'gt': lambda a, b: a > b, 'gte': lambda a, b: a >= b}
        if any(not compare[r['op']](operand(r['left']), operand(r['right']) + r.get('offset', 0)) for r in relations):
            _fail('PARAMETER_RELATION_NOT_MET', 'Candidate fails the selected profile cross-parameter relation')

    @staticmethod
    def _signed_value(raw, spec):
        return raw - 65536 if spec.get('signed') and raw & 32768 else raw

    def _parameter_write(self, args):
        from .dvp_transfer import _packet
        if self.fence_path.exists():
            _fail('MACHINE_TARGET_UNCERTAIN', 'Target has an unresolved M/D operation; reconcile without replay')
        value = args.get('value')
        if type(value) is not int:
            _fail('PARAMETER_VALUE_OUT_OF_RANGE', 'Parameter storage value must be an integer, not bool')
        spec = self._parameter_spec(args.get('parameter'), value)
        identifier = args.get('command_id')
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', identifier):
            _fail('INVALID_COMMAND_ID', 'Unique bounded command_id required')
        self.journal_path = self.state_dir / ('command-' + self.target_key + '-' + _sha(identifier.encode()) + '.json')
        if self.journal_path.exists():
            _fail('COMMAND_ALREADY_ADMITTED', 'Command ID is permanently consumed across M/D operations')
        self.journal = dict(schema_version=1, kind='machine_parameter', target_key=self.target_key,
                            command_id=identifier, operation='machine.parameter.write', parameter=args['parameter'],
                            device=spec['device'], descriptor_sha256=canonical_digest(spec), context=self.context,
                            catalog_sha256=self.a.config['machine_catalog_sha256'], source_sha256=self.catalog['reference_source_sha256'],
                            identity_sha256=self.identity_sha, requested_value=value, wire_value=value & 65535,
                            created_at=_now(), status='admitted', phase='admitted', members=[])
        write_json(self.journal_path, self.journal)
        self._identify()
        baseline = self._signed_value(self._read(spec['device'])[0], spec)
        self.journal['baseline_value'] = baseline
        if baseline == value:
            self.journal.update(status='complete', phase='already_equal', ack_verified=False, readback_verified=True,
                                parameter_outcome='already_equal')
            self._save()
            return self._result(True, 'ok', already_equal=True, value_verified=True, observed_value=baseline,
                                ack_verified=False, readback_verified=True)
        self._parameter_guards(spec, value)
        self.journal['phase'] = 'parameter_write_intent'
        self._save()
        self._fence()
        address = 0x1000 + _device(spec['device'])[1]
        body = b'\x06' + address.to_bytes(2, 'big') + (value & 65535).to_bytes(2, 'big')
        ack = False
        try:
            data, _ = self._exchange(_packet(self.a.config.get('station', 1), body), 6, write=True, parameter_spec=spec)
            if data != body[1:]:
                _fail('PARAMETER_ACK_MISMATCH', 'FC06 address/value acknowledgement does not match')
            ack = True
        except (ValueError, OSError) as exc:
            self.journal['write_error'] = str(exc)
            if not self.attempted:
                self.journal['status'] = 'blocked'
                self._save()
                self._clear_fence()
                raise
        self.journal['ack_verified'] = ack
        self.journal['status'] = 'unknown_outcome'
        self._save()
        observed = self._signed_value(self._read(spec['device'])[0], spec)
        self.journal['observed_value'] = observed
        self.journal['parameter_outcome'] = 'candidate' if observed == value else 'baseline' if observed == baseline else 'other'
        if observed != value:
            self._save()
            _fail('PARAMETER_READBACK_NOT_VERIFIED', 'Independent FC03 did not prove the requested value; no second write')
        self.journal.update(status='complete', phase='verified', readback_verified=True)
        self._save()
        self._clear_fence()
        return self._result(True, 'ok', value_verified=True, observed_value=observed,
                            ack_verified=ack, readback_verified=True, already_equal=False)

    def _parameter_reconcile(self, journal, path):
        spec = self._parameter_spec(journal.get('parameter'), journal.get('requested_value'))
        if (journal.get('device') != spec['device'] or journal.get('descriptor_sha256') != canonical_digest(spec)
                or journal.get('wire_value') != journal['requested_value'] & 65535
                or type(journal.get('baseline_value')) is not int):
            _fail('MACHINE_JOURNAL_MISMATCH', 'Recorded parameter descriptor or candidate/baseline changed')
        if self.fence_path.exists():
            fence = _json(self.fence_path)
            if (fence.get('journal_path') != str(path) or fence.get('target_key') != self.target_key
                    or fence.get('binding_sha256') != self._journal_binding(journal) or fence.get('devices') != [spec['device']]):
                _fail('MACHINE_FENCE_CHANGED', 'Recorded parameter uncertainty binding changed')
        self._identify()
        observed = []
        for sample in range(2):
            observed.append(self._signed_value(self._read(spec['device'])[0], spec))
            if sample == 0:
                time.sleep(0.05)
        classification = ('candidate' if observed == [journal['requested_value']] * 2 else
                          'baseline' if observed == [journal['baseline_value']] * 2 else 'other')
        journal['parameter_outcome'] = classification
        journal['reconciliation'] = dict(classification=classification, values=observed, context=self.context,
                                         timestamp=_now(), writes_issued=False)
        if classification == 'other':
            journal['status'] = 'unknown_outcome'
            self._save()
            _fail('PARAMETER_READBACK_NOT_VERIFIED', 'Parameter differs from both recorded candidate and baseline')
        journal['status'] = 'reconciled_' + classification
        self._save()
        if self.fence_path.exists():
            self._clear_fence()
        return self._result(True, 'ok', classification=classification, value_verified=classification == 'candidate',
                            observed_value=observed[-1], writes_issued=False)

    def _snapshot(self, args):
        definitions = self.catalog.get('observations')
        if not isinstance(definitions, dict) or not 1 <= len(definitions) <= 256:
            _fail('INVALID_OBSERVATION_CATALOG', 'Bounded profile-defined observations required')
        fields = {}
        for name, spec in definitions.items():
            if not isinstance(name, str) or not isinstance(spec, dict):
                _fail('INVALID_OBSERVATION_CATALOG', 'Named observation definitions required')
            kind, number = _device(spec.get('device'))
            count = spec.get('count', 1)
            scale = spec.get('scale', 1)
            if (type(count) is not int or not 1 <= count <= 64 or type(scale) not in (int, float)
                    or not -1000000 <= scale <= 1000000 or type(spec.get('signed', False)) is not bool
                    or not isinstance(spec.get('enum', {}), dict)):
                _fail('INVALID_OBSERVATION_CATALOG', 'Bounded count/scale/signed/enum profile fields required')
            fields[name] = [kind + (format(number + i, 'o') if kind == 'Y' else str(number + i)) for i in range(count)]
        if sum(map(len, fields.values())) > 1024:
            _fail('INVALID_OBSERVATION_CATALOG', 'Snapshot total exceeds bounded profile limit')
        self._identify()
        start = _now()
        raw = self._read_set(['M1000', 'M1002', *(d for devices in fields.values() for d in devices)])
        live = self._live(self.a.config)
        data = {}
        for name, spec in definitions.items():
            measured = [raw[d] for d in fields[name]]
            values = [(v - 65536 if spec.get('signed') and v & 32768 else v) * spec.get('scale', 1) for v in measured]
            labels = [spec.get('enum', {}).get(str(v)) if live else None for v in measured]
            data[name] = dict(raw=measured, values=values, labels=labels, unit=spec.get('unit'))
        if live:
            for name, spec in definitions.items():
                reason = spec.get('reason_field')
                if reason is None:
                    continue
                if reason not in data or len(data[name]['raw']) != len(data[reason]['raw']):
                    _fail('INVALID_OBSERVATION_CATALOG', 'Conditional reason fields must have the same count')
                for index, value in enumerate(data[name]['raw']):
                    if value == spec.get('precondition_value'):
                        data[reason]['labels'][index] = spec.get('enum', {}).get(str(value))
        finish = _now()
        return self._result(True, 'ok', timestamp=finish, began_at=start, finished_at=finish,
                            atomic=False, live_revision_verified=live, running=bool(raw['M1000']),
                            boot_scan=bool(raw['M1002']), data=data, raw=raw,
                            output_semantics='PLC Y bits; electrical feedback is not verified')

    def _reconcile(self, args):
        requested = args.get('journal_path')
        if (not isinstance(requested, str) or '..' in Path(requested).parts
                or Path(requested).is_symlink()):
            _fail('INVALID_JOURNAL_PATH', 'Traversal and journal symlink aliases are forbidden')
        path = _absolute(requested)
        if path.parent != self.state_dir or not path.name.startswith('command-' + self.target_key + '-'):
            _fail('INVALID_JOURNAL_PATH', 'Recorded shared target command journal required')
        journal = _json(path)
        if (journal.get('schema_version') != 1 or journal.get('kind') not in ('machine_command', 'machine_parameter')
                or journal.get('target_key') != self.target_key or journal.get('identity_sha256') != self.identity_sha
                or journal.get('catalog_sha256') != self.a.config['machine_catalog_sha256']
                or journal.get('source_sha256') != self.catalog['reference_source_sha256']):
            _fail('MACHINE_JOURNAL_MISMATCH', 'Journal belongs to a different target/contract')
        self.journal, self.journal_path = journal, path
        if journal['kind'] == 'machine_parameter':
            return self._parameter_reconcile(journal, path)
        original_args = {'selector': journal['selector']} if journal.get('selector') is not None else {}
        expected = self._members(journal.get('command'), original_args)
        for key in ('coil', 'edge_devices', 'stop_guards', 'ack_devices', 'ack_expected'):
            if [m.get(key) for m in journal['members']] != [m[key] for m in expected]:
                _fail('MACHINE_JOURNAL_MISMATCH', 'Recorded request mapping differs from the pinned command')
        recorded = [m for m in journal['members'] if m['phase'] != 'unattempted']
        devices = [d for m in recorded for d in [m['coil'], *m['edge_devices']]]
        if self.fence_path.exists():
            fence = _json(self.fence_path)
            allowed = {d for m in expected for d in [m['coil'], *m['edge_devices']]}
            if (fence.get('journal_path') != str(path) or fence.get('target_key') != self.target_key
                    or fence.get('binding_sha256') != self._journal_binding(journal)
                    or not isinstance(fence.get('devices'), list) or not set(fence['devices']).issubset(allowed)):
                _fail('MACHINE_FENCE_CHANGED', 'Recorded target uncertainty binding changed')
            devices = fence['devices']
        self._live(self.a.config, required=True)
        self._identify()
        observations = []
        for sample in range(2):
            self._running()
            values = self._read_set(devices)
            if any(values.values()):
                _fail('MACHINE_RELEASE_UNVERIFIED', 'Recorded request or edge memory is still high; no writes issued')
            observations.append(dict(timestamp=_now(), values=values))
            if sample == 0:
                time.sleep(0.05)
        for member in recorded:
            member['release_verified'] = True
        journal['status'] = 'reconciled_released_effect_unknown'
        journal['reconciliation'] = dict(observations=observations, context=self.context, timestamp=_now(), writes_issued=False)
        self._save()
        if self.fence_path.exists():
            self._clear_fence()
        return self._result(True, 'ok', release_verified=True, effect_verified=False, writes_issued=False)

    def invoke(self, operation: str, arguments: dict[str, Any]):
        from .dvp_serial import DvpSerialError
        try:
            if operation not in MACHINE_OPERATIONS or not isinstance(arguments, dict):
                _fail('OPERATION_UNSUPPORTED', 'Typed machine operation required')
            if set(arguments) - (MACHINE_FIELDS[operation] | {'universal_target', '_execution_context'}):
                _fail('INVALID_ARGUMENTS', 'Unknown machine operation fields')
            if operation == 'machine.profile':
                self.a._refresh_config()
                c = self.a.config
                self._context(arguments)
                self._context_current()
                self._catalog(c)
                catalog = self.catalog
                source = _absolute(c.get('machine_source_path'))
                if (not source.is_file() or source.stat().st_size > 2 * 1024 * 1024
                        or _sha(source.read_bytes()) != _hash(c.get('machine_source_sha256'))
                        or _hash(c['machine_source_sha256']) != catalog['reference_source_sha256']
                        or self.context['baseline_sha256'] != catalog['reference_source_sha256']
                        or self.context['revision'] != catalog['revision']):
                    _fail('SOURCE_REVISION_CHANGED', 'Selected source does not match its host-pinned catalog')
                return self._result(True, 'ok', contract_id=catalog.get('contract_id'), revision=catalog.get('revision'),
                                    source_sha256=catalog.get('reference_source_sha256'),
                                    payload_sha256=catalog.get('reference_payload_sha256'),
                                    commands={k: dict(selector=v.get('selector'), members=len(v.get('coils', [])),
                                                      is_start=v.get('is_start')) for k, v in catalog.get('commands', {}).items()},
                                    observations=catalog.get('observations', {}), live_revision_verified=False,
                                    parameters=catalog.get('parameters', {}),
                                    connected=self.a.connection is not None, port_opened=False,
                                    qualification='HOST_PINNED_SOURCE_CONTRACT_ONLY')
            self._session(operation, arguments)
            self._lock()
            self.previous_send_guard = getattr(self.a, 'machine_send_guard', None)
            self.a.machine_send_guard = self._gates
            self.guard_installed = True
            if operation == 'machine.parameter.write':
                return self._parameter_write(arguments)
            if operation == 'machine.command':
                return self._command(operation, arguments)
            if operation == 'machine.reconcile':
                return self._reconcile(arguments)
            if operation == 'machine.read':
                self._identify()
                values = self._read(arguments.get('device'), arguments.get('count', 1))
                return self._result(True, 'ok', timestamp=_now(), device=arguments['device'], values=values,
                                    live_revision_verified=self._live(self.a.config), interpretation='candidate_address_raw_read')
            return self._snapshot(arguments)
        except (DvpSerialError, OSError, ValueError, KeyError, TypeError) as exc:
            try:
                if self.journal is not None and self.journal.get('status') == 'admitted':
                    self.journal['status'] = 'unknown_outcome' if self.attempted else 'blocked'
                    self.journal['error'] = str(exc)
                    self._save()
                uncertain = self.journal is not None and self.journal.get('status') == 'unknown_outcome'
                return self._result(False, 'unknown_outcome' if uncertain else 'blocked',
                                    code=getattr(exc, 'code', 'MACHINE_IO_ERROR'), message=str(exc),
                                    machine_command_may_have_changed=self.attempted)
            finally:
                # This facade catches its own errors, so the adapter's outer
                # exception handler cannot release its owned COM handle for us.
                # Closing sends no packets and never clears durable uncertainty.
                self.a._close()
        finally:
            if self.guard_installed:
                self.a.machine_send_guard = self.previous_send_guard
            self._unlock()
