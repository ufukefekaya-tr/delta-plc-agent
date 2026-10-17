"""Software-only Modbus ASCII and bounded DVP observation contracts.

The serial fixtures never enumerate or open a physical USB/PLC endpoint.
"""
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from universal_plc.dvp_serial import (
    DvpSerialAdapter, DvpSerialError, ascii_frame, parse_ascii_response,
)


TARGET = dict(vendor="delta", family="DVP", model="DVP14SS211R", toolchain="dvpserial")


def reply(function, payload, station=1):
    """Make test responses independently from the production frame encoder."""
    body = bytes([station, function, len(payload)]) + payload
    check = bytes([(-sum(body)) & 0xFF])
    return b":" + (body + check).hex().upper().encode("ascii") + b"\r\n"


def register_reply(values):
    return reply(3, b"".join(value.to_bytes(2, "big") for value in values))


class PortFixture:
    device = "COM6"
    vid = 0x05DD
    pid = 0x0100
    hwid = "USB VID:PID=05DD:0100 SER=fixture-only"
    description = "Fake Delta CDC (no hardware)"


class SerialFixture:
    def __init__(self, responses, fail_open=False):
        self.responses = list(responses)
        self.writes = []
        self.events = []
        self.port = None
        self.dtr = True
        self.rts = True
        self.fail_open = fail_open
        self.closed = False
        self.factory_kwargs = None
        self.pending = b""
        self.timeout = 2

    def factory(self, **kwargs):
        self.factory_kwargs = kwargs
        self.events.append(("construct", dict(kwargs)))
        return self

    def open(self):
        self.events.append(("open", self.port, self.dtr, self.rts))
        if self.fail_open:
            if isinstance(self.fail_open, Exception):
                raise self.fail_open
            raise OSError("fixture open failed")

    def reset_input_buffer(self):
        self.events.append(("reset_input_buffer",))
        self.pending = b""

    def write(self, value):
        self.writes.append(value)
        return len(value)

    def read(self, size=1):
        self.events.append(("read", size))
        if not self.pending and self.responses:
            self.pending = self.responses.pop(0)
        chunk, self.pending = self.pending[:size], self.pending[size:]
        return chunk

    def close(self):
        self.closed = True
        self.events.append(("close",))


class FramingTests(unittest.TestCase):
    def test_published_request_vectors_and_lrc(self):
        self.assertEqual(ascii_frame(1, 1, 0x0BE8, 10), b":01010BE8000A01\r\n")
        self.assertEqual(ascii_frame(1, 3, 0x1064, 12), b":01031064000C7C\r\n")
        self.assertEqual(ascii_frame(1, 3, 0x13E9, 2), b":010313E90002FE\r\n")

    def test_register_response_keeps_unsigned_wire_values(self):
        self.assertEqual(parse_ascii_response(b":0103041234FFFFB4\r\n", 1, 3, 2), [0x1234, 65535])

    def test_bits_use_lsb_order_and_requested_count(self):
        self.assertEqual(parse_ascii_response(reply(1, bytes([0x81, 0x01])), 1, 1, 10),
                         [1, 0, 0, 0, 0, 0, 0, 1, 1, 0])

    def test_bad_lrc_rejected(self):
        with self.assertRaises(DvpSerialError):
            parse_ascii_response(b":0103041234FFFFB5\r\n", 1, 3, 2)

    def test_wrong_station_rejected(self):
        with self.assertRaises(DvpSerialError):
            parse_ascii_response(reply(3, b"\x00\x01", station=2), 1, 3, 1)

    def test_wrong_function_rejected(self):
        with self.assertRaises(DvpSerialError):
            parse_ascii_response(reply(1, b"\x01"), 1, 3, 1)

    def test_wrong_byte_count_or_extra_data_rejected(self):
        for frame in (register_reply([1]), register_reply([1, 2, 3]), reply(1, b"\x01")):
            with self.subTest(frame=frame), self.assertRaises(DvpSerialError):
                parse_ascii_response(frame, 1, 3, 2)

    def test_truncation_non_hex_and_multiple_frames_rejected(self):
        valid = register_reply([1])
        for frame in (b"", valid[:-1], valid[1:], b":0103ZZ0000\r\n", valid + valid):
            with self.subTest(frame=frame), self.assertRaises(DvpSerialError):
                parse_ascii_response(frame, 1, 3, 1)

    def test_modbus_exception_is_not_a_data_response(self):
        with self.assertRaises(DvpSerialError) as caught:
            parse_ascii_response(b":0183027A\r\n", 1, 3, 1)
        self.assertTrue(caught.exception.code)

    def test_encoder_rejects_write_functions_and_out_of_range_fields(self):
        for arguments in ((1, 5, 0, 1), (1, 6, 0, 1), (1, 15, 0, 1), (1, 16, 0, 1),
                          (0, 3, 0, 1), (248, 3, 0, 1), (1, 3, -1, 1), (1, 3, 65536, 1),
                          (1, 3, 0, 0), (1, 3, 0, 65536)):
            with self.subTest(arguments=arguments), self.assertRaises(DvpSerialError):
                ascii_frame(*arguments)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(enabled=True, port="COM6", baudrate=9600, station=1,
                           expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                           expected_vid=0x05DD, expected_pid=0x0100,
                           route="directlink_cdc", allow_read_probe=True)

    def adapter(self, fixture=None, ports=None, config=None):
        fixture = fixture or SerialFixture([])
        return DvpSerialAdapter(config=self.config if config is None else config,
                                serial_factory=fixture.factory,
                                ports_provider=lambda: [PortFixture()] if ports is None else ports)

    def invoke(self, adapter, operation, **arguments):
        return adapter.invoke(operation, dict(universal_target=TARGET, **arguments))

    def assert_blocked(self, result):
        self.assertIs(result["ok"], False)
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["code"])

    def test_connect_reads_vendor_status_without_project_sensor_assumptions(self):
        fixture = SerialFixture([reply(1, bytes([0xFF, 0x03])),
                                 register_reply([0, 0])])
        result = self.invoke(self.adapter(fixture), "target.connect")
        self.assertIs(result["ok"], True)
        self.assertEqual(result["status"], "ok")
        self.assertIs(result["running"], True)
        self.assertNotIn("temperature_raw", result)
        self.assertNotIn("temperature_c", result)
        self.assertNotIn(ascii_frame(1, 3, 0x1064, 12), fixture.writes)
        self.assertIsInstance(result["error_codes"], list)
        self.assertIs(result["endpoint_verified"], True)
        self.assertIs(result["exact_model_verified"], False)
        self.assertEqual(fixture.writes, [b":01010BE8000A01\r\n", b":010313E90002FE\r\n"])

    def test_stop_status_and_fault_bits_are_distinct_from_connection_failure(self):
        for bits, running, errors in ((b"\x00\x00", False, []),
                                      (b"\x11\x03", True, ["SYNTAX", "WATCHDOG", "LOW_VOLTAGE"])):
            fixture = SerialFixture([reply(1, bits), register_reply([0, 0])])
            with self.subTest(bits=bits):
                result = self.invoke(self.adapter(fixture), "target.status")
                self.assertTrue(result["ok"])
                self.assertIs(result["running"], running)
                self.assertEqual(result["error_codes"], errors)

    def test_dtr_rts_disabled_before_port_open_and_reads_bounded(self):
        fixture = SerialFixture([reply(1, b"\xFF\x03"), register_reply([0, 0])])
        result = self.invoke(self.adapter(fixture), "target.connect")
        self.assertTrue(result["ok"])
        self.assertEqual(fixture.events[1], ("open", "COM6", False, False))
        self.assertIsNone(fixture.factory_kwargs["port"])
        self.assertEqual(fixture.factory_kwargs["bytesize"], 7)
        self.assertEqual(fixture.factory_kwargs["parity"], "E")
        self.assertEqual(fixture.factory_kwargs["stopbits"], 1)
        self.assertGreater(fixture.factory_kwargs["timeout"], 0)
        self.assertGreater(fixture.factory_kwargs["write_timeout"], 0)
        self.assertTrue(all(event[1] == 1 for event in fixture.events if event[0] == "read"))

    def test_bounded_typed_register_read(self):
        fixture = SerialFixture([register_reply([123] * 12)])
        result = self.invoke(self.adapter(fixture), "target.read", device="D100", count=12)
        self.assertTrue(result["ok"])
        self.assertEqual(fixture.writes, [b":01031064000C7C\r\n"])

    def test_fast_echo_is_skipped_without_resending_the_request(self):
        request = ascii_frame(1, 3, 0x1064, 1)
        fixture = SerialFixture([request, register_reply([123])])
        result = self.invoke(self.adapter(fixture), "target.read", device="D100", count=1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["values"], [123])
        self.assertEqual(fixture.writes, [request])

    def test_delayed_blank_crlf_is_skipped_within_the_same_read_budget(self):
        clock = [100.0]
        fixture = SerialFixture([b"\r\n", register_reply([123])])
        ordinary_read = fixture.read
        def delayed_read(size=1):
            clock[0] += 0.1
            return ordinary_read(size)
        fixture.read = delayed_read
        with patch("universal_plc.dvp_serial.time.monotonic", side_effect=lambda: clock[0]):
            result = self.invoke(self.adapter(fixture), "target.read", device="D100", count=1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["values"], [123])
        self.assertEqual(len(fixture.writes), 1)
        self.assertLess(clock[0] - 100.0, 2)

    def test_incomplete_read_request_is_not_retried(self):
        fixture = SerialFixture([])
        ordinary_write = fixture.write
        def partial_write(request):
            ordinary_write(request)
            return len(request) - 1
        fixture.write = partial_write
        result = self.invoke(self.adapter(fixture), "target.read", device="D100", count=1)
        self.assert_blocked(result)
        self.assertEqual(result["code"], "SERIAL_WRITE_INCOMPLETE")
        self.assertEqual(len(fixture.writes), 1)
        self.assertTrue(fixture.closed)

    def test_expired_disabled_or_unapproved_profile_never_opens_port(self):
        profiles = [dict(self.config, enabled=False), dict(self.config, allow_read_probe=False),
                    dict(self.config, expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()),
                    dict(self.config, expires_at="2030-01-01T00:00:00")]
        for profile in profiles:
            fixture = SerialFixture([])
            with self.subTest(profile=profile):
                self.assert_blocked(self.invoke(self.adapter(fixture, config=profile), "target.connect"))
                self.assertFalse(fixture.events)

    def test_missing_or_wrong_usb_port_never_opens(self):
        wrong_port = PortFixture()
        wrong_port.vid = 0x1A86
        for ports in ([], [wrong_port]):
            fixture = SerialFixture([])
            with self.subTest(ports=ports):
                self.assert_blocked(self.invoke(self.adapter(fixture, ports=ports), "target.connect"))
                self.assertFalse(fixture.events)

    def test_busy_port_and_empty_reply_return_blocked(self):
        for fixture in (SerialFixture([], fail_open=True), SerialFixture([b""])):
            with self.subTest(fixture=fixture):
                with patch("universal_plc.dvp_serial.time.monotonic", side_effect=iter(range(100)).__next__):
                    self.assert_blocked(self.invoke(self.adapter(fixture), "target.connect"))
                self.assertTrue(fixture.closed)

    def test_permission_busy_and_other_open_failures_have_distinct_codes(self):
        for error, expected in ((PermissionError("fixture access denied"), "PORT_BUSY"),
                                (OSError("fixture disconnected"), "PORT_OPEN_FAILED")):
            fixture = SerialFixture([], fail_open=error)
            with self.subTest(error=error):
                result = self.invoke(self.adapter(fixture), "target.connect")
                self.assert_blocked(result)
                self.assertEqual(result["code"], expected)
                self.assertFalse(fixture.writes)
                self.assertTrue(fixture.closed)

    def test_slow_junk_and_echo_share_a_bounded_absolute_read_deadline(self):
        for response, increment in ((b"x" * 10000, 0.75),
                                    (ascii_frame(1, 3, 0x1064, 1) + register_reply([1]), 0.1)):
            clock = [100.0]
            fixture = SerialFixture([response])
            ordinary_read = fixture.read
            def delayed_read(size=1):
                clock[0] += increment
                return ordinary_read(size)
            fixture.read = delayed_read
            with self.subTest(increment=increment):
                with patch("universal_plc.dvp_serial.time.monotonic", side_effect=lambda: clock[0]):
                    result = self.invoke(self.adapter(fixture), "target.read", device="D100", count=1)
                self.assert_blocked(result)
                self.assertLessEqual(len([e for e in fixture.events if e[0] == "read"]), 22)
                self.assertEqual(len(fixture.writes), 1)
                self.assertTrue(fixture.closed)

    def test_invalid_serial_profile_types_rejected_before_open(self):
        for station in (True, "1", 1.5, 0, 248):
            fixture = SerialFixture([])
            with self.subTest(station=station):
                result = self.invoke(self.adapter(fixture, config=dict(self.config, station=station)), "target.connect")
                self.assert_blocked(result)
                self.assertEqual(result["code"], "INVALID_PROFILE")
                self.assertFalse(fixture.events)

    def test_cdc_bridge_initialization_is_opt_in_and_sent_once(self):
        for acknowledgement in (b"BRDY\r\n", b"RUN\r\n"):
            status_replies = [reply(1, b"\x00\x00"), register_reply([0, 0])]
            fixture = SerialFixture([acknowledgement] + status_replies + status_replies)
            adapter = self.adapter(fixture, config=dict(self.config, cdc_initialize=True))
            with self.subTest(acknowledgement=acknowledgement):
                for operation in ("target.connect", "target.status"):
                    result = self.invoke(adapter, operation)
                    self.assertTrue(result["ok"])
                    self.assertIs(result["trace"][0]["bridge_ready"], True)
                    # HMI bridge RUN readiness is independent from PLC run state.
                    self.assertIs(result["running"], False)
                    self.assertEqual(result["trace"][0]["kind"], "bridge_init")
                self.assertEqual(fixture.writes.count(b"!BYPASS2E\r\n"), 1)
                self.assertEqual(len(fixture.writes), 5)

    def test_cdc_missing_or_slow_ack_stops_before_any_plc_read(self):
        for acknowledgement in (b"", b"j" * 10000):
            clock = [100.0]
            fixture = SerialFixture([acknowledgement])
            ordinary_read = fixture.read
            def delayed_read(size=1):
                clock[0] += 1
                return ordinary_read(size)
            fixture.read = delayed_read
            with self.subTest(acknowledgement_length=len(acknowledgement)):
                with patch("universal_plc.dvp_serial.time.monotonic", side_effect=lambda: clock[0]):
                    result = self.invoke(self.adapter(fixture, config=dict(self.config, cdc_initialize=True)),
                                         "target.connect")
                self.assert_blocked(result)
                self.assertEqual(result["code"], "CDC_BRIDGE_NOT_READY")
                self.assertEqual(fixture.writes, [b"!BYPASS2E\r\n"])
                self.assertTrue(fixture.closed)
                self.assertLessEqual(len([e for e in fixture.events if e[0] == "read"]), 6)

    def test_partial_bridge_initialization_is_not_retried(self):
        fixture = SerialFixture([])
        ordinary_write = fixture.write
        def partial_write(request):
            ordinary_write(request)
            return len(request) - 1
        fixture.write = partial_write
        result = self.invoke(self.adapter(fixture, config=dict(self.config, cdc_initialize=True)), "target.connect")
        self.assert_blocked(result)
        self.assertEqual(fixture.writes, [b"!BYPASS2E\r\n"])
        self.assertTrue(fixture.closed)

    def test_session_expiring_after_bridge_ack_prevents_first_plc_query(self):
        profile = dict(self.config, cdc_initialize=True)
        fixture = SerialFixture([b"BRDY", reply(1, b"\x00\x00")])
        ordinary_read = fixture.read
        received = bytearray()
        def expire_after_ack(size=1):
            chunk = ordinary_read(size)
            received.extend(chunk)
            if b"BRDY" in received:
                profile["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            return chunk
        fixture.read = expire_after_ack
        result = self.invoke(self.adapter(fixture, config=profile), "target.connect")
        self.assert_blocked(result)
        self.assertEqual(result["code"], "SESSION_EXPIRED")
        self.assertEqual(fixture.writes, [b"!BYPASS2E\r\n"])
        self.assertTrue(fixture.closed)

    def test_bridge_initialization_requires_the_exact_opt_in_route(self):
        for profile in (dict(self.config, cdc_initialize=True, route="serial_ascii"),
                        dict(self.config, cdc_initialize=True, allow_read_probe=False)):
            fixture = SerialFixture([])
            with self.subTest(profile=profile):
                result = self.invoke(self.adapter(fixture, config=profile), "target.connect")
                self.assert_blocked(result)
                self.assertFalse(fixture.writes)

    def test_expiry_after_connect_closes_connection_before_another_request(self):
        fixture = SerialFixture([reply(1, b"\x00\x00"), register_reply([0, 0])])
        adapter = self.adapter(fixture)
        self.assertTrue(self.invoke(adapter, "target.connect")["ok"])
        adapter.config["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.assert_blocked(self.invoke(adapter, "target.status"))
        self.assertEqual(len(fixture.writes), 2)
        self.assertTrue(fixture.closed)

    def test_wrong_target_is_rejected_before_port_open(self):
        for target in (dict(TARGET, model="DVP12SE11R"), dict(TARGET, vendor="other"),
                       dict(TARGET, family="AS")):
            fixture = SerialFixture([])
            with self.subTest(target=target):
                result = self.adapter(fixture).invoke("target.connect", {"universal_target": target})
                self.assert_blocked(result)
                self.assertFalse(fixture.events)

    def test_arbitrary_write_fields_and_out_of_range_reads_rejected(self):
        arguments = [dict(device="D100", count=0), dict(device="D100", count=65),
                     dict(device="D4095", count=2), dict(device="M1535", count=2),
                     dict(device="D-1", count=1), dict(device="D100", count=1, value=42),
                     dict(device="D100", count=1, raw_frame=":010600000001F8\r\n")]
        for args in arguments:
            fixture = SerialFixture([])
            with self.subTest(args=args):
                self.assert_blocked(self.invoke(self.adapter(fixture), "target.read", **args))
                self.assertFalse(fixture.writes)

    def test_program_transfer_and_plc_control_never_write_packets(self):
        for operation in ("deployment.prepare", "deployment.execute", "project.upload"):
            fixture = SerialFixture([])
            with self.subTest(operation=operation):
                result = self.invoke(self.adapter(fixture), operation)
                self.assert_blocked(result)
                self.assertEqual(result["code"], "PROGRAM_TRANSFER_UNAVAILABLE")
                self.assertFalse(fixture.events)
        for operation in ("target.download", "target.run", "target.stop", "target.force", "target.write"):
            fixture = SerialFixture([])
            with self.subTest(operation=operation):
                self.assert_blocked(self.invoke(self.adapter(fixture), operation))
                self.assertFalse(fixture.writes)

    def test_candidate_digest_is_checked_without_opening_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "fixture.dvp"
            candidate.write_bytes(b"uncompiled software fixture")
            actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
            for claimed, expected in ((actual, "PROGRAM_TRANSFER_UNAVAILABLE"),
                                      ("0" * 64, "CANDIDATE_SHA_MISMATCH")):
                fixture = SerialFixture([])
                with self.subTest(claimed=claimed):
                    result = self.invoke(self.adapter(fixture), "deployment.prepare",
                                         input_path=str(candidate), expected_sha256=claimed)
                    self.assert_blocked(result)
                    self.assertEqual(result["code"], expected)
                    self.assertFalse(fixture.events)


if __name__ == "__main__":
    unittest.main()
