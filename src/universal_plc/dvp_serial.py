"""Bounded DVP ASCII operations with separately enrolled native SS2 transfer.

USB CDC availability and a Modbus response do not prove exact PLC identity.
No generic register/coil writer or byte sender is exposed. Fixed M1072 RUN/STOP
requires a separate current, physically isolated and attended control session.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from typing import Any

from .models import PLCError


class DvpSerialError(PLCError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def ascii_frame(station: int, function: int, address: int, count: int) -> bytes:
    if type(station) is not int or not 1 <= station <= 247:
        raise DvpSerialError('INVALID_STATION', 'Station must be 1..247; broadcast is forbidden')
    if function not in (1, 3):
        raise DvpSerialError('READ_ONLY', 'Only documented FC01/FC03 reads are supported')
    if type(address) is not int or not 0 <= address <= 65535 or type(count) is not int or not 1 <= count <= 64:
        raise DvpSerialError('INVALID_READ', 'Bounded address and count required')
    payload = bytes([station, function]) + address.to_bytes(2, 'big') + count.to_bytes(2, 'big')
    return b':' + (payload + bytes([(-sum(payload)) & 255])).hex().upper().encode('ascii') + b'\r\n'


def parse_ascii_response(frame: bytes, station: int, function: int, count: int) -> list[int]:
    if not isinstance(frame, bytes) or len(frame) > 513 or not frame.startswith(b':') or not frame.endswith(b'\r\n'):
        raise DvpSerialError('INVALID_FRAME', 'Expected a bounded Modbus ASCII response')
    encoded = frame[1:-2]
    if len(encoded) % 2 or not re.fullmatch(rb'[0-9A-Fa-f]+', encoded):
        raise DvpSerialError('INVALID_FRAME', 'Invalid ASCII hex payload')
    data = bytes.fromhex(encoded.decode('ascii'))
    if len(data) < 4:
        raise DvpSerialError('INVALID_FRAME', 'Truncated response')
    if sum(data) & 255:
        raise DvpSerialError('LRC_ERROR', 'Response checksum does not match')
    if data[0] != station:
        raise DvpSerialError('WRONG_STATION', 'Response came from a different station')
    if data[1] == function | 0x80:
        if len(data) != 4:
            raise DvpSerialError('INVALID_FRAME', 'Malformed exception response')
        raise DvpSerialError('PLC_EXCEPTION', f'Modbus exception {data[2]}')
    if data[1] != function:
        raise DvpSerialError('WRONG_FUNCTION', 'Response function does not match request')
    expected = math.ceil(count / 8) if function == 1 else count * 2
    if data[2] != expected or len(data) != expected + 4:
        raise DvpSerialError('INVALID_LENGTH', 'Response byte count does not match the requested read')
    payload = data[3:-1]
    if function == 1:
        return [(payload[i // 8] >> (i % 8)) & 1 for i in range(count)]
    return [int.from_bytes(payload[i:i + 2], 'big') for i in range(0, len(payload), 2)]


def control_frame(station: int, running: bool) -> bytes:
    if type(station) is not int or not 1 <= station <= 247:
        raise DvpSerialError('INVALID_STATION', 'Explicit non-broadcast station required')
    if type(running) is not bool:
        raise DvpSerialError('INVALID_CONTROL', 'RUN/STOP must be a boolean')
    # Delta SS2 manual: M1072 R/W controls RUN/STOP; PDU = H0800+1072.
    payload = bytes([station, 5, 0x0C, 0x30, 0xFF if running else 0, 0])
    return b':' + (payload + bytes([(-sum(payload)) & 255])).hex().upper().encode() + b'\r\n'


READ_OPERATIONS = ('serial.ports', 'target.connect', 'target.status', 'target.read', 'target.identify', 'target.disconnect',
                   'target.observe.start', 'target.observe.read', 'target.observe.stop', 'deployment.reconcile')
TRANSFER_OPERATIONS = ('project.upload', 'deployment.prepare', 'deployment.execute', 'program.contract.qualify')
CONTROL_OPERATIONS = ('target.run', 'target.stop')


class DvpSerialAdapter:
    def __init__(self, config: dict | None = None, serial_factory=None, ports_provider=None):
        path = os.environ.get('UNIVERSAL_PLC_DVPSERIAL_CONFIG')
        self.config_path = Path(path).resolve() if config is None and path else None
        self.config = config if config is not None else json.loads(Path(path).read_text('utf-8')) if path else {}
        self.serial_factory = serial_factory
        self.ports_provider = ports_provider
        self.connection = None
        self.lock = threading.RLock()
        self.trace: list[dict] = []
        self.control_attempted = False
        self.transfer_mutation_attempted = False
        self.program_write_attempted = False
        self.machine_command_attempted = False
        self.machine_send_guard = None
        self.connection_profile = None

    def capabilities(self) -> list[dict]:
        from .dvp_controls import MACHINE_OPERATIONS, MACHINE_READ_OPERATIONS
        transfer_enabled = self.config.get('program_transfer_enabled') is True
        return [dict(operation=op, implementation='available',
                     qualification='vendor_static_native_identity' if op == 'target.identify' else 'software_read_only',
                     live_verified=False, description='Typed bounded reads; native FC67 verifies SS2 family only, not exact CPU suffix.')
                for op in READ_OPERATIONS] + [
                    dict(operation=op, implementation='available',
                         qualification='vendor_static_contract' if transfer_enabled else 'guard_only',
                         live_verified=False, program_transfer_supported=transfer_enabled,
                         description='Separately enrolled SS2 backup/prepare/execute; disabled profiles retain the transfer guard.')
                    for op in TRANSFER_OPERATIONS] + [
                    dict(operation=op, implementation='available', qualification='fixed_m1072_control',
                         live_verified=False, description='Explicit attended/isolated control session; FC05 M1072 only, fresh M1000 verification.')
                    for op in CONTROL_OPERATIONS] + [
                    dict(operation=op, implementation='available',
                         qualification=('host_pinned_source_inspection' if op == 'machine.profile' else
                                        'profile_parameter_write_readback_software_only' if op == 'machine.parameter.write' else
                                        'bounded_machine_read' if op in MACHINE_READ_OPERATIONS else 'revision_bound_pulse_software_only'),
                         live_verified=False,
                         description=('Offline source/catalog inspection; opens no port and requires no lease or live proof.' if op == 'machine.profile' else
                                      'Profile-defined normal D single FC06 plus independent FC03 readback; STOP allowed, no pulse or RUN command.' if op == 'machine.parameter.write' else
                                      'Explicit host-pinned machine catalog, shared lease and current live revision; no generic writer.'))
                    for op in MACHINE_OPERATIONS]

    def _ports(self):
        if self.ports_provider:
            return list(self.ports_provider())
        try:
            from serial.tools import list_ports
        except ImportError as exc:
            raise DvpSerialError('DEPENDENCY_MISSING', 'Install pinned pyserial==3.5') from exc
        return list(list_ports.comports())

    def _refresh_config(self):
        if self.config_path is not None:
            if not self.config_path.is_file() or self.config_path.stat().st_size > 1024 * 1024:
                raise DvpSerialError('INVALID_SESSION', 'Current session configuration is missing or unbounded')
            current = json.loads(self.config_path.read_text(encoding='utf-8'))
            if not isinstance(current, dict):
                raise DvpSerialError('INVALID_SESSION', 'Current session configuration must be an object')
            self.config = current

    def _profile(self):
        self._refresh_config()
        c = self.config
        if c.get('enabled') is not True:
            raise DvpSerialError('SESSION_NOT_ENROLLED', 'A current explicit host-local read session is required')
        try:
            expiry = datetime.fromisoformat(c['expires_at'].replace('Z', '+00:00'))
        except (KeyError, ValueError, AttributeError) as exc:
            raise DvpSerialError('INVALID_SESSION', 'An aware expires_at is required') from exc
        if expiry.tzinfo is None or datetime.now(timezone.utc) >= expiry:
            raise DvpSerialError('SESSION_EXPIRED', 'Read session has expired')
        if not re.fullmatch(r'COM[1-9][0-9]*', str(c.get('port', '')), re.I):
            raise DvpSerialError('INVALID_PORT', 'Explicit Windows COM port required')
        if c.get('baudrate', 9600) != 9600 or type(c.get('station', 1)) is not int or not 1 <= c.get('station', 1) <= 247:
            raise DvpSerialError('INVALID_PROFILE', 'Initial fixed 9600/7E1 and bounded station required')
        if c.get('route', 'serial_ascii') not in ('serial_ascii', 'directlink_cdc'):
            raise DvpSerialError('INVALID_PROFILE', 'Explicit supported serial/HMI route required')
        if type(c.get('cdc_initialize', False)) is not bool:
            raise DvpSerialError('INVALID_PROFILE', 'cdc_initialize must be a boolean')
        if c.get('cdc_initialize') is True and c.get('route') != 'directlink_cdc':
            raise DvpSerialError('INVALID_PROFILE', 'HMI initialization only applies to DirectLink CDC')
        if self.connection is not None and self.connection_profile != self._connection_binding(c):
            self._close()
            raise DvpSerialError('SESSION_PROFILE_CHANGED', 'Fresh route differs from the open connection; no request sent')
        return c

    @staticmethod
    def _connection_binding(c):
        return {k: c.get(k) for k in ('port', 'baudrate', 'station', 'route', 'expected_vid',
                                     'expected_pid', 'expected_hwid', 'cdc_initialize')}

    def _target(self, args):
        target = args.get('universal_target', {})
        if self.config.get('target_model', 'DVP14SS211R') != 'DVP14SS211R':
            raise DvpSerialError('TARGET_MISMATCH', 'This adapter is scoped to the DVP14SS211R model claim')
        if target and (str(target.get('vendor', '')).lower() != 'delta' or target.get('family') != 'DVP'
                       or target.get('model') != self.config.get('target_model', 'DVP14SS211R')):
            raise DvpSerialError('TARGET_MISMATCH', 'Target differs from enrolled Delta DVP model claim')

    def _open(self):
        c = self._profile()
        binding = self._connection_binding(c)
        if self.connection is not None:
            if self.connection_profile != binding:
                self._close()
                raise DvpSerialError('SESSION_PROFILE_CHANGED', 'Connection route changed; original port was closed without sending')
            return
        port = next((p for p in self._ports() if p.device.upper() == c['port'].upper()), None)
        if port is None:
            raise DvpSerialError('PORT_NOT_FOUND', f"{c['port']} not present")
        if getattr(port, 'vid', None) != c.get('expected_vid') or getattr(port, 'pid', None) != c.get('expected_pid'):
            raise DvpSerialError('USB_IDENTITY_MISMATCH', 'COM name does not match enrolled USB VID/PID')
        if c.get('expected_hwid') and getattr(port, 'hwid', None) != c['expected_hwid']:
            raise DvpSerialError('USB_IDENTITY_MISMATCH', 'USB gateway location/identity changed')
        if c.get('route') == 'directlink_cdc' and c.get('allow_read_probe') is not True:
            raise DvpSerialError('ROUTE_PROTOCOL_UNVERIFIED', 'DirectLink CDC is not presumed transparent Modbus')
        factory = self.serial_factory
        if factory is None:
            try:
                import serial
            except ImportError as exc:
                raise DvpSerialError('DEPENDENCY_MISSING', 'Install pinned pyserial==3.5') from exc
            factory = serial.Serial
        connection = factory(port=None, baudrate=9600, bytesize=7, parity='E', stopbits=1,
                             timeout=2, write_timeout=2)
        # Prepare modem controls before opening; do not pulse a connected device.
        connection.dtr = False
        connection.rts = False
        connection.port = c['port']
        try:
            connection.open()
        except Exception as exc:
            try:
                connection.close()
            finally:
                busy = isinstance(exc, PermissionError) or 'PermissionError(13' in str(exc)
                raise DvpSerialError('PORT_BUSY' if busy else 'PORT_OPEN_FAILED', str(exc)) from exc
        self.connection = connection
        self.connection_profile = binding
        if c.get('route') == 'directlink_cdc' and c.get('cdc_initialize') is True:
            self._bridge_init()

    def _close(self):
        if self.connection is not None:
            try:
                self.connection.close()
            finally:
                self.connection = None
                self.connection_profile = None

    def _line(self, deadline: float) -> bytes:
        data = bytearray()
        previous_timeout = self.connection.timeout
        try:
            while len(data) < 513:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                # readline can restart its timeout for every byte. A single
                # shared deadline also bounds slow junk and the echoed request.
                self.connection.timeout = remaining
                chunk = self.connection.read(1)
                if not chunk:
                    break
                data.extend(chunk)
                if data.endswith(b'\n'):
                    break
        finally:
            self.connection.timeout = previous_timeout
        return bytes(data)

    def _bridge_init(self):
        # Exact start command and shared Modbus continuation were observed in
        # this host's WPLSoft binary. No arbitrary vendor packet sender exists.
        request = b'!BYPASS2E\r\n'
        self.connection.reset_input_buffer()
        if self.machine_send_guard is not None:
            self.machine_send_guard()  # Same fresh lease/authority gate before bridge bytes.
        if self.connection.write(request) != len(request):
            raise DvpSerialError('SERIAL_WRITE_INCOMPLETE', 'HMI bridge request partly sent; no retry')
        deadline = time.monotonic() + 5
        response = bytearray()
        previous_timeout = self.connection.timeout
        try:
            while len(response) < 513:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.connection.timeout = remaining
                chunk = self.connection.read(1)
                if not chunk:
                    break
                response.extend(chunk)
                if b'BRDY' in response or b'RUN' in response:
                    break
        finally:
            self.connection.timeout = previous_timeout
        ready = b'BRDY' in response or b'RUN' in response
        self.trace.append(dict(kind='bridge_init', tx_hex=request.hex(), rx_hex=bytes(response).hex(), bridge_ready=ready))
        self.trace = self.trace[-16:]
        if not ready:
            raise DvpSerialError('CDC_BRIDGE_NOT_READY', 'No bounded HMI BRDY/RUN marker; PLC was not queried')
        # A marker is HMI readiness only. _status still requires valid PLC
        # FC01/FC03 responses; especially RUN is never PLC status here.

    def _read(self, device: str, count: int):
        self._profile()
        match = re.fullmatch(r'([MD])(\d{1,4})', str(device))
        if not match or type(count) is not int or not 1 <= count <= 64:
            raise DvpSerialError('INVALID_READ', 'Use typed M/D device and count 1..64')
        kind, number = match[1], int(match[2])
        limit = 1536 if kind == 'M' else 4096
        if number + count > limit:
            raise DvpSerialError('INVALID_READ', 'Read crosses the documented DVP range')
        self._open()
        self._profile()  # Bridge initialization may have consumed the session's remaining time.
        function = 1 if kind == 'M' else 3
        station = self.config.get('station', 1)
        request = ascii_frame(station, function, (0x800 if kind == 'M' else 0x1000) + number, count)
        self.connection.reset_input_buffer()
        written = self.connection.write(request)
        if written != len(request):
            raise DvpSerialError('SERIAL_WRITE_INCOMPLETE', 'Read request was only partly sent; request is not retried')
        deadline = time.monotonic() + 2
        for _ in range(4):
            response = self._line(deadline)
            # The HMI readiness token may be followed by a delayed CRLF.
            # Discard only empty lines or the exact query echo, bounded by the
            # same deadline; arbitrary junk still fails frame validation.
            if response not in (b'\r\n', b'\n', request):
                break
        if response in (b'\r\n', b'\n', request):
            response = b''
        self.trace.append({'device': device, 'count': count, 'tx_hex': request.hex(), 'rx_hex': response.hex()})
        self.trace = self.trace[-16:]
        if not response:
            raise DvpSerialError('NO_RESPONSE', 'No Modbus response; DirectLink CDC may require a vendor bridge envelope')
        values = parse_ascii_response(response, station, function, count)
        return values

    def _status(self):
        bits = self._read('M1000', 10)
        identity = self._read('D1001', 2)
        return dict(ok=True, status='ok', port_opened=True, endpoint_verified=True, exact_model_verified=False,
                    running=bool(bits[0]), error_codes=[name for i, name in [(4, 'SYNTAX'), (8, 'WATCHDOG'), (9, 'LOW_VOLTAGE')] if bits[i]],
                    firmware_raw=identity[0], capacity_steps=identity[1],
                    route=self.config.get('route', 'serial_ascii'),
                    note='Valid Modbus endpoint; firmware/capacity do not prove exact CPU or HMI forwarding identity.',
                    trace=list(self.trace), program_transfer_supported=self.config.get('program_transfer_enabled') is True,
                    program_transfer_live_verified=False)

    def _control(self, running: bool):
        c = self._profile()
        enrollment = json.dumps(c, sort_keys=True, separators=(',', ':'))
        if not all(c.get(key) is True for key in ('allow_run_stop', 'heaters_isolated', 'user_present')):
            raise DvpSerialError('CONTROL_NOT_AUTHORIZED', 'Current explicit RUN/STOP authority, isolated loads and attended PLC required')
        if running:
            before = self._status()
            if before['capacity_steps'] != 7920:
                raise DvpSerialError('TARGET_CAPACITY_MISMATCH', 'Expected SS2 capacity was not observed')
            if before['error_codes']:
                raise DvpSerialError('PLC_FAULT', 'PLC fault bits prevent RUN')
        self._open()
        request = control_frame(c.get('station', 1), running)
        self.connection.reset_input_buffer()
        current = self._profile()
        if not all(current.get(key) is True for key in ('allow_run_stop', 'heaters_isolated', 'user_present')):
            raise DvpSerialError('CONTROL_NOT_AUTHORIZED', 'Current physical control enrollment changed before sending')
        if json.dumps(current, sort_keys=True, separators=(',', ':')) != enrollment:
            raise DvpSerialError('CONTROL_ENROLLMENT_CHANGED', 'Control route/authority changed before sending; no command sent')
        self.control_attempted = True
        written = self.connection.write(request)
        response = self._line(time.monotonic() + 2) if written == len(request) else b''
        self.trace.append(dict(kind='control', requested_running=running, tx_hex=request.hex(), rx_hex=response.hex()))
        self.trace = self.trace[-16:]
        if written != len(request):
            raise DvpSerialError('SERIAL_WRITE_INCOMPLETE', 'Control command partly sent; never retry automatically')
        if response != request:
            raise DvpSerialError('CONTROL_ACK_MISMATCH', 'Control ACK missing or does not exactly match station/function/address/value/LRC')
        # ACK may be a serial echo; a fresh M1000 read is decisive.
        for attempt in range(3):
            if json.dumps(self._profile(), sort_keys=True, separators=(',', ':')) != enrollment:
                raise DvpSerialError('CONTROL_ENROLLMENT_CHANGED', 'Control verification profile changed; command was not replayed')
            observed = bool(self._read('M1000', 1)[0])
            if json.dumps(self._profile(), sort_keys=True, separators=(',', ':')) != enrollment:
                raise DvpSerialError('CONTROL_ENROLLMENT_CHANGED', 'Control verification profile changed during the read')
            if observed is running:
                return dict(ok=True, status='ok', running=observed, requested_running=running,
                            state_verified=True, control_address='M1072', trace=list(self.trace),
                            plc_program_changed=False, program_transfer_supported=False)
            if attempt < 2:
                time.sleep(0.1)
        raise DvpSerialError('CONTROL_STATE_NOT_OBSERVED', 'Fresh M1000 did not show requested state; command was not replayed')

    def _transfer(self, operation, args):
        if self.config.get('program_transfer_enabled') is True:
            from .dvp_transfer import ProgramTransfer
            return ProgramTransfer(self).invoke(operation, args)
        candidate = args.get('input_path') or self.config.get('candidate_path')
        claimed = args.get('expected_sha256') or self.config.get('candidate_sha256')
        measured = None
        if candidate or claimed:
            if not candidate or not claimed:
                raise DvpSerialError('CANDIDATE_REQUIRED', 'Candidate path and SHA must be provided together')
            path = Path(candidate)
            if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
                raise DvpSerialError('INVALID_CANDIDATE', 'Bounded existing DVP file required')
            measured = hashlib.sha256(path.read_bytes()).hexdigest()
            if measured != claimed.removeprefix('sha256:'):
                raise DvpSerialError('CANDIDATE_SHA_MISMATCH', 'Compiled candidate has changed')
        return dict(ok=False, status='blocked', code='PROGRAM_TRANSFER_UNAVAILABLE', operation=operation,
                    candidate_sha256=measured, program_transfer_supported=False, plc_program_changed=False,
                    message='No verified SS2 program-transfer contract. No program bytes sent; Modbus writes are not program loading.')

    def invoke(self, operation: str, arguments: dict[str, Any]):
        with self.lock:
            self.control_attempted = False
            self.transfer_mutation_attempted = False
            self.program_write_attempted = False
            self.machine_command_attempted = False
            try:
                self._refresh_config()
                if not isinstance(arguments, dict):
                    raise DvpSerialError('INVALID_ARGUMENTS', 'Arguments must be an object')
                self._target(arguments)
                if self.config.get('allowed_operations') and operation not in self.config['allowed_operations']:
                    raise DvpSerialError('OPERATION_NOT_AUTHORIZED', 'Operation is outside current enrolled scope')
                from .dvp_controls import MACHINE_OPERATIONS, MACHINE_FIELDS, MachineControl
                allowed = {'universal_target', '_execution_context'}
                if operation in MACHINE_OPERATIONS:
                    allowed |= set(MACHINE_FIELDS[operation])
                if operation == 'target.read':
                    allowed |= {'device', 'count'}
                elif operation in TRANSFER_OPERATIONS:
                    allowed |= {'input_path', 'expected_sha256', 'output_dir', 'backup_path',
                                'expected_backup_sha256', 'plan_path'}
                elif operation == 'deployment.reconcile':
                    allowed |= {'journal_path', 'expected_sha256'}
                if set(arguments) - allowed:
                    raise DvpSerialError('INVALID_ARGUMENTS', 'Unknown typed operation fields')
                if operation in MACHINE_OPERATIONS:
                    return MachineControl(self).invoke(operation, arguments)
                if operation == 'serial.ports':
                    return dict(ok=True, status='ok', ports=[dict(port=p.device, vid=getattr(p, 'vid', None),
                                pid=getattr(p, 'pid', None), description=getattr(p, 'description', '')) for p in self._ports()])
                if operation in TRANSFER_OPERATIONS:
                    return self._transfer(operation, arguments)
                if operation in CONTROL_OPERATIONS:
                    return self._control(operation == 'target.run')
                if operation == 'target.identify':
                    from .dvp_transfer import NativeProgramTransport
                    return NativeProgramTransport(self).identify()
                if operation in ('target.disconnect', 'target.observe.stop'):
                    self._close()
                    return dict(ok=True, status='ok', connected=False)
                if operation in ('target.connect', 'target.status', 'target.observe.start', 'target.observe.read'):
                    return self._status()
                if operation == 'target.read':
                    values = self._read(arguments.get('device', ''), arguments.get('count', 1))
                    return dict(ok=True, status='ok', values=values, device=arguments['device'], trace=list(self.trace))
                if operation == 'deployment.reconcile':
                    if self.config.get('program_transfer_enabled') is True:
                        from .dvp_transfer import ProgramTransfer
                        return ProgramTransfer(self).invoke(operation, arguments)
                    return dict(ok=True, status='ok', program_outcome='no_transfer_implemented', plc_program_changed=False,
                                connected=self.connection is not None)
                raise DvpSerialError('OPERATION_UNSUPPORTED', 'Operation not in the typed read-only facade')
            except DvpSerialError as exc:
                self._close()
                uncertain = self.control_attempted or self.transfer_mutation_attempted or self.machine_command_attempted
                return dict(ok=False, status='unknown_outcome' if uncertain else 'blocked',
                            retry_allowed=False, plc_run_state_unknown=self.control_attempted,
                            plc_runtime_state_unknown=self.transfer_mutation_attempted or self.machine_command_attempted,
                            code=exc.code, message=str(exc), trace=list(self.trace),
                            plc_program_changed=None if self.program_write_attempted else False,
                            program_transfer_supported=self.config.get('program_transfer_enabled') is True)
            except (OSError, ValueError) as exc:
                self._close()
                uncertain = self.control_attempted or self.transfer_mutation_attempted or self.machine_command_attempted
                return dict(ok=False, status='unknown_outcome' if uncertain else 'blocked',
                            retry_allowed=False, plc_run_state_unknown=self.control_attempted,
                            plc_runtime_state_unknown=self.transfer_mutation_attempted or self.machine_command_attempted,
                            code='SERIAL_IO_ERROR', message=str(exc), trace=list(self.trace),
                            plc_program_changed=None if self.program_write_attempted else False)
