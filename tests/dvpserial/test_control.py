"""Fixed RUN/STOP contracts with a stateful software PLC, never physical I/O."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from universal_plc.dvp_serial import DvpSerialAdapter, DvpSerialError, control_frame

try:
    from mcp import Client
except ImportError:
    Client = None


TARGET = dict(vendor="delta", family="DVP", model="DVP14SS211R", toolchain="dvpserial")
RUN = b":01050C30FF00BF\r\n"
STOP = b":01050C300000BE\r\n"


class StatefulSerial:
    def __init__(self, *, change_state=True, ack="valid", capacity=7920, fault_mask=0):
        self.change_state = change_state
        self.ack = ack
        self.capacity = capacity
        self.fault_mask = fault_mask
        self.running = False
        self.writes = []
        self.pending = b""
        self.timeout = 2
        self.port = None
        self.dtr = self.rts = True
        self.closed = False

    def factory(self, **kwargs):
        self.timeout = kwargs["timeout"]
        self.assert_constructor_port = kwargs["port"]
        return self

    def open(self):
        assert self.assert_constructor_port is None
        assert self.port == "COM6" and self.dtr is False and self.rts is False

    def reset_input_buffer(self):
        self.pending = b""

    def write(self, frame):
        self.writes.append(frame)
        wire = bytes.fromhex(frame[1:-2].decode("ascii"))
        station, function = wire[:2]
        address = int.from_bytes(wire[2:4], "big")
        count_or_value = int.from_bytes(wire[4:6], "big")
        if function == 5:
            assert address == 0x0C30 and count_or_value in (0xFF00, 0)
            if self.change_state:
                self.running = count_or_value == 0xFF00
            if self.ack == "valid":
                self.pending = frame
            elif self.ack == "bad_lrc":
                self.pending = frame[:-4] + b"00\r\n"
            if self.ack == "partial_write":
                return len(frame) - 1
        elif function in (1, 3):
            if function == 1:
                bits = int(self.running) | self.fault_mask
                payload = bits.to_bytes((count_or_value + 7) // 8, "little")
            elif address == 0x13E9:
                payload = b"\x88\x40" + self.capacity.to_bytes(2, "big")
            else:
                payload = bytes(count_or_value * 2)
            body = bytes([station, function, len(payload)]) + payload
            self.pending = b":" + (body + bytes([(-sum(body)) & 255])).hex().upper().encode("ascii") + b"\r\n"
        else:
            raise AssertionError("Unexpected PLC write function")
        return len(frame)

    def read(self, size):
        assert size == 1
        chunk, self.pending = self.pending[:size], self.pending[size:]
        return chunk

    def close(self):
        self.closed = True

    def controls(self):
        return [frame for frame in self.writes if int(frame[3:5], 16) == 5]


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(enabled=True, port="COM6", baudrate=9600, station=1,
                           expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                           expected_vid=0x05DD, expected_pid=0x0100,
                           route="directlink_cdc", allow_read_probe=True,
                           allow_run_stop=True, heaters_isolated=True, user_present=True)

    def adapter(self, fixture=None, config=None):
        fixture = fixture or StatefulSerial()
        ports = lambda: [SimpleNamespace(device="COM6", vid=0x05DD, pid=0x0100,
                                         description="Software fixture", hwid="FAKE")]
        return DvpSerialAdapter(config=self.config if config is None else config,
                                serial_factory=fixture.factory, ports_provider=ports)

    def call(self, adapter, operation, **arguments):
        return adapter.invoke(operation, dict(universal_target=TARGET, **arguments))

    def test_fixed_vendor_run_stop_vectors_and_strict_boolean(self):
        self.assertEqual(control_frame(1, True), RUN)
        self.assertEqual(control_frame(1, False), STOP)
        for running in (1, 0, "RUN", None):
            with self.subTest(running=running), self.assertRaises(DvpSerialError):
                control_frame(1, running)
        for station in (0, 248, True):
            with self.subTest(station=station), self.assertRaises(DvpSerialError):
                control_frame(station, True)

    def test_all_current_control_flags_required_before_any_transport(self):
        for flag in ("allow_run_stop", "heaters_isolated", "user_present"):
            fixture = StatefulSerial()
            profile = dict(self.config)
            profile.pop(flag)
            with self.subTest(flag=flag):
                result = self.call(self.adapter(fixture, profile), "target.run")
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["code"], "CONTROL_NOT_AUTHORIZED")
                self.assertFalse(fixture.writes)

    def test_run_then_read_then_stop_uses_one_fixed_command_each(self):
        fixture = StatefulSerial()
        adapter = self.adapter(fixture)
        ran = self.call(adapter, "target.run")
        self.assertTrue(ran["ok"])
        self.assertIs(ran["running"], True)
        read = self.call(adapter, "target.status")
        self.assertIs(read["running"], True)
        stopped = self.call(adapter, "target.stop")
        self.assertTrue(stopped["ok"])
        self.assertIs(stopped["running"], False)
        self.assertEqual(fixture.controls(), [RUN, STOP])

    def test_ack_echo_alone_does_not_prove_run_state(self):
        fixture = StatefulSerial(change_state=False)
        result = self.call(self.adapter(fixture), "target.run")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unknown_outcome")
        self.assertIs(result["retry_allowed"], False)
        self.assertIs(result["plc_run_state_unknown"], True)
        self.assertEqual(fixture.controls(), [RUN])

    def test_corrupt_missing_or_partial_control_ack_does_not_resend(self):
        for ack in ("bad_lrc", "missing", "partial_write"):
            fixture = StatefulSerial(change_state=False, ack=ack)
            with self.subTest(ack=ack):
                result = self.call(self.adapter(fixture), "target.run")
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "unknown_outcome")
                self.assertIs(result["retry_allowed"], False)
                self.assertIs(result["plc_run_state_unknown"], True)
                self.assertEqual(fixture.controls(), [RUN])

    def test_stop_cleanup_does_not_wait_for_capacity_or_fault_precheck(self):
        fixture = StatefulSerial(capacity=8000, fault_mask=1 << 4)
        fixture.running = True
        result = self.call(self.adapter(fixture), "target.stop")
        self.assertTrue(result["ok"])
        self.assertIs(result["running"], False)
        self.assertEqual(fixture.writes[0], STOP)
        self.assertEqual(fixture.controls(), [STOP])
        self.assertFalse(any(int(frame[3:5], 16) == 3 for frame in fixture.writes))

    def test_run_requires_expected_capacity_and_no_error_flags(self):
        for fixture in (StatefulSerial(capacity=8000), StatefulSerial(fault_mask=1 << 4)):
            with self.subTest(capacity=fixture.capacity, fault_mask=fixture.fault_mask):
                result = self.call(self.adapter(fixture), "target.run")
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "blocked")
                self.assertFalse(fixture.controls())

    def test_arbitrary_control_fields_rejected_before_any_transport(self):
        for arguments in (dict(device="M0"), dict(running=True), dict(raw_frame=RUN.decode()), dict(value=0xFF00)):
            fixture = StatefulSerial()
            with self.subTest(arguments=arguments):
                result = self.call(self.adapter(fixture), "target.run", **arguments)
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "blocked")
                self.assertFalse(fixture.writes)

    @unittest.skipIf(Client is None, "mcp SDK2.1.1 required")
    def test_public_mcp_run_read_stop_with_software_fixture(self):
        from universal_plc.adapters import AdapterManager
        from universal_plc.mcp_server import create_server
        from universal_plc.service import Service

        fixture = StatefulSerial()
        manager = AdapterManager()
        manager.register("delta.dvpserial", self.adapter(fixture))
        with tempfile.TemporaryDirectory() as directory:
            service = Service(Path(directory), adapters=manager)
            async def scenario():
                async with Client(create_server(service), read_timeout_seconds=10) as client:
                    response = await client.call_tool("workspace.create", {"target": TARGET})
                    self.assertFalse(response.is_error)
                    workspace = response.structured_content
                    for operation, expected_running in (("target.run", True), ("target.status", True), ("target.stop", False)):
                        returned = await client.call_tool("adapter.invoke", dict(
                            workspace_id=workspace["workspace_id"], operation=operation, arguments={},
                            expected_context_digest=workspace["context_digest"]))
                        self.assertFalse(returned.is_error)
                        result = returned.structured_content["result"]
                        self.assertTrue(result["ok"])
                        self.assertIs(result["running"], expected_running)
            asyncio.run(scenario())
        self.assertEqual(fixture.controls(), [RUN, STOP])


if __name__ == "__main__":
    unittest.main()
