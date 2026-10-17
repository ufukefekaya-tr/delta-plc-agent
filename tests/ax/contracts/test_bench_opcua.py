from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "backends" / "ax" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from axmcp_offline.bench_opcua import (
    BenchConfig as ProfileConfig,
    BenchOpcUa,
    BenchRejected,
    BenchUnknownOutcome,
)


PREFIX = "ns=4;s=|var|SyntheticController.Application."
SIGNALS = {'mode': 'TestBindings.mode', 'reference_command': 'TestBindings.reference_command', 'reset_command': 'TestBindings.reset_command', 'start_command': 'TestBindings.start_command', 'stop_command': 'TestBindings.stop_command', 'step': 'TestBindings.step', 'running': 'TestBindings.running', 'fault': 'TestBindings.fault', 'fault_code': 'TestBindings.fault_code', 'motion_fault': 'TestBindings.motion_fault', 'admitted': 'TestBindings.admitted', 'real_standstill': 'TestBindings.real_standstill', 'motion_standstill': 'TestBindings.motion_standstill', 'automatic': 'TestBindings.automatic', 'drive_run_requests': 'TestBindings.drive_run_requests', 'drive_run_acks': 'TestBindings.drive_run_acks', 'drive_running': 'TestBindings.drive_running'}

def BenchConfig(*args, **kwargs):
    kwargs.setdefault("node_prefix", PREFIX)
    kwargs.setdefault("signals", dict(SIGNALS))
    kwargs.setdefault("drive_count", 5)
    kwargs.setdefault("idle_step", 0)
    kwargs.setdefault("automatic_step", 60)
    return ProfileConfig(*args, **kwargs)

FINGERPRINT = "sha256:" + "a" * 64
ENDPOINT = "opc.tcp://192.0.2.10:4840"


class FakeNode:
    def __init__(self, client: "FakeClient", name: str) -> None:
        self.client = client
        self.name = name

    async def read_value(self):
        if self.name == "step" and self.client.sequence:
            self.client.values["step"] = self.client.sequence.pop(0)
            self.client.values["automatic"] = self.client.values["step"] == 60
        return self.client.values[self.name]

    async def write_value(self, value: bool) -> None:
        if type(value) is not bool:
            raise AssertionError("only BOOL writes expected")
        self.client.writes.append((self.name, value))
        self.client.values[self.name] = value
        if self.name == "reference_command" and value:
            self.client.sequence = [30, 31, 33, 34, 40, 0]
        elif self.name == "start_command" and value:
            self.client.sequence = self.client.sequence_on_start or [30, 31, 33, 34, 40, 50, 55, 60]
            if self.client.fault_on_start:
                self.client.values["fault"] = True
                self.client.values["fault_code"] = 0x545A4030
            if self.client.motion_fault_on_start:
                self.client.values["motion_fault"] = True
        elif self.name == "stop_command" and value:
            self.client.sequence = []
            self.client.values["step"] = 0
            self.client.values["automatic"] = False
            self.client.values["motion_standstill"] = True
        elif self.name == "reset_command" and value:
            self.client.values["fault"] = False
            self.client.values["fault_code"] = 0
            self.client.values["motion_fault"] = False
            self.client.values["step"] = 0


class FakeClient:
    def __init__(self) -> None:
        self.values = {
            "mode": False,
            "reference_command": False,
            "reset_command": False,
            "start_command": False,
            "stop_command": False,
            "step": 0,
            "running": False,
            "fault": False,
            "fault_code": 0,
            "motion_fault": False,
            "admitted": False,
            "real_standstill": True,
            "motion_standstill": True,
            "automatic": False,
            "axis_position": 0.0,
            "axis_speed": 0.0,
            "aux_position_one": 0.0,
            "aux_position_two": 0.0,
            "aux_position_three": 0.0,
            "drive_run_requests": [False] * 5,
            "drive_run_acks": [False] * 5,
            "drive_running": [False] * 5,
        }
        self.writes: list[tuple[str, bool]] = []
        self.sequence: list[int] = []
        self.node_ids: list[str] = []
        self.fault_on_start = False
        self.motion_fault_on_start = False
        self.sequence_on_start: list[int] | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def get_node(self, node_id: str) -> FakeNode:
        self.node_ids.append(node_id)
        reverse = {PREFIX + suffix: name for name, suffix in SIGNALS.items()}
        if node_id not in reverse:
            raise AssertionError("arbitrary node requested")
        return FakeNode(self, reverse[node_id])


class BenchOpcUaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.client = FakeClient()
        self.probed: list[str] = []

        async def probe(endpoint: str) -> str:
            self.probed.append(endpoint)
            return FINGERPRINT

        self.probe = probe
        self.transport = BenchOpcUa(
            BenchConfig(
                endpoint=ENDPOINT,
                expected_ip="192.0.2.10",
                expected_fingerprint=FINGERPRINT,
                pulse_s=0.02,
                poll_s=0.02,
                stop_timeout_s=0.5,
            ),
            fingerprint_probe=self.probe,
            client_factory=lambda *_: self.client,
        )

    async def test_snapshot_is_read_only_and_uses_fixed_nodes(self) -> None:
        result = await self.transport.snapshot()
        self.assertEqual(result["step"], 0)
        self.assertEqual(result["drive_running"], [False] * 5)
        self.assertEqual(self.client.writes, [])
        self.assertEqual(set(self.client.node_ids), {PREFIX + value for value in SIGNALS.values()})
        self.assertEqual(self.probed, [ENDPOINT])

    async def test_typed_mode_reference_and_stop_pulses(self) -> None:
        await self.transport.execute("bench_mode_on")
        result = await self.transport.execute("reference_once", deadline_s=1.0)
        self.assertEqual(result["command"], "reference_once")
        self.assertTrue(any(sample["step"] == 30 for sample in result["samples"]))
        self.assertEqual(self.client.values["step"], 0)
        self.assertIn(("reference_command", True), self.client.writes)
        self.assertIn(("stop_command", True), self.client.writes)
        self.assertNotIn(("start_command", True), self.client.writes)
        await self.transport.execute("bench_mode_off")
        self.assertFalse(self.client.values["mode"])

    async def test_start_is_bounded_and_stopped_before_return(self) -> None:
        self.client.values["mode"] = True
        result = await self.transport.execute(
            "start_once", deadline_s=3.0, observe_s=0.04
        )
        self.assertTrue(any(s["step"] == 60 and s["automatic"] for s in result["samples"]))
        self.assertIn(("start_command", True), self.client.writes)
        self.assertIn(("stop_command", True), self.client.writes)
        self.assertEqual(self.client.values["step"], 0)
        self.assertFalse(self.client.values["automatic"])
        self.assertEqual(result["final"]["step"], 0)

    async def test_fault_during_start_sends_stop_before_error(self) -> None:
        self.client.values["mode"] = True
        self.client.fault_on_start = True
        with self.assertRaisesRegex(BenchRejected, "PLC bench fault"):
            await self.transport.execute("start_once", deadline_s=1.0, observe_s=0.04)
        self.assertIn(("start_command", True), self.client.writes)
        self.assertIn(("stop_command", True), self.client.writes)
        self.assertEqual(self.client.values["step"], 0)
        self.assertFalse(self.client.values["automatic"])

    async def test_motion_fault_alone_stops_before_bench_fault_propagates(self) -> None:
        self.client.values["mode"] = True
        self.client.motion_fault_on_start = True
        with self.assertRaisesRegex(BenchRejected, "PLC bench fault"):
            await self.transport.execute("start_once", deadline_s=1.0, observe_s=0.04)
        self.assertIn(("stop_command", True), self.client.writes)
        self.assertEqual(self.client.values["step"], 0)

    async def test_auto_drop_never_counts_as_continuous_observation(self) -> None:
        self.client.values["mode"] = True
        self.client.sequence_on_start = [60]

        async def drop_after_auto() -> None:
            async with asyncio.timeout(0.8):
                while self.client.values["step"] != 60:
                    await asyncio.sleep(0.005)
            await asyncio.sleep(0.04)
            self.client.values["step"] = 0
            self.client.values["automatic"] = False

        drop = asyncio.create_task(drop_after_auto())
        with self.assertRaisesRegex(BenchRejected, "automatic observation interrupted"):
            await self.transport.execute("start_once", deadline_s=1.0, observe_s=0.2)
        await drop
        self.assertIn(("stop_command", True), self.client.writes)
        self.assertFalse(self.client.values["automatic"])

    async def test_external_stop_preempts_observation_without_arbitrary_write(self) -> None:
        self.client.values["mode"] = True
        motion = asyncio.create_task(
            self.transport.execute("start_once", deadline_s=3.0, observe_s=1.0)
        )
        await asyncio.sleep(0.06)
        result = await self.transport.execute("stop")
        with self.assertRaisesRegex(BenchRejected, "external stop requested"):
            await motion
        self.assertEqual(result["final"]["step"], 0)
        self.assertFalse(self.client.values["automatic"])
        self.assertNotIn(("mode", False), self.client.writes)

    async def test_cancelled_first_of_two_stops_keeps_second_intent(self) -> None:
        await self.transport._operation_lock.acquire()
        first = asyncio.create_task(self.transport.execute("stop"))
        second = None
        try:
            async with asyncio.timeout(0.5):
                while self.transport._stop_generation < 1:
                    await asyncio.sleep(0)
            second = asyncio.create_task(self.transport.execute("stop"))
            async with asyncio.timeout(0.5):
                while self.transport._stop_generation < 2:
                    await asyncio.sleep(0)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertTrue(self.transport._stop_requested.is_set())
        finally:
            self.transport._operation_lock.release()
        self.assertEqual((await asyncio.wait_for(second, 1.0))["final"]["step"], 0)
        self.assertFalse(self.transport._stop_requested.is_set())

    async def test_cancelled_sole_queued_stop_is_serviced_by_motion_owner(self) -> None:
        self.client.values["mode"] = True
        motion = asyncio.create_task(
            self.transport.execute("start_once", deadline_s=3.0, observe_s=1.0)
        )
        async with asyncio.timeout(0.5):
            while ("start_command", True) not in self.client.writes:
                await asyncio.sleep(0)
        queued_stop = asyncio.create_task(self.transport.execute("stop"))
        async with asyncio.timeout(0.5):
            while not self.transport._stop_requested.is_set():
                await asyncio.sleep(0)
        queued_stop.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await queued_stop
        self.assertTrue(self.transport._stop_requested.is_set())
        with self.assertRaisesRegex(BenchRejected, "external stop requested"):
            await asyncio.wait_for(motion, 1.0)
        self.assertIn(("stop_command", True), self.client.writes)
        self.assertFalse(self.transport._stop_requested.is_set())

    async def test_older_stop_ack_does_not_clear_newer_intent(self) -> None:
        first_read = asyncio.Event()
        second_read = asyncio.Event()
        release_first = asyncio.Event()
        release_second = asyncio.Event()
        original_snapshot = self.transport._snapshot
        reads = 0

        async def blocked_snapshot(client):
            nonlocal reads
            reads += 1
            if reads == 1:
                first_read.set()
                await release_first.wait()
            elif reads == 2:
                second_read.set()
                await release_second.wait()
            return await original_snapshot(client)

        self.transport._snapshot = blocked_snapshot
        first = asyncio.create_task(self.transport.execute("stop"))
        try:
            await asyncio.wait_for(first_read.wait(), 0.5)
            second = asyncio.create_task(self.transport.execute("stop"))
            async with asyncio.timeout(0.5):
                while self.transport._stop_generation < 2:
                    await asyncio.sleep(0)
            release_first.set()
            await asyncio.wait_for(first, 1.0)
            await asyncio.wait_for(second_read.wait(), 0.5)
            self.assertTrue(self.transport._stop_requested.is_set())
        finally:
            release_first.set()
            release_second.set()
        await asyncio.wait_for(second, 1.0)
        self.assertFalse(self.transport._stop_requested.is_set())

    async def _assert_queued_motion_invalidated_by_stop(
        self, queued_command: str, *, cancel_stop_after_ack: bool
    ) -> None:
        self.client.values["mode"] = True
        owner_ack = asyncio.Event()
        release_owner = asyncio.Event()
        original_stop = self.transport._stop
        active = None

        async def hold_motion_owner_after_ack(client):
            snap = await original_stop(client)
            if asyncio.current_task() is active:
                owner_ack.set()
                await release_owner.wait()
            return snap

        self.transport._stop = hold_motion_owner_after_ack
        active = asyncio.create_task(
            self.transport.execute("start_once", deadline_s=3.0, observe_s=1.0)
        )
        async with asyncio.timeout(0.5):
            while ("start_command", True) not in self.client.writes:
                await asyncio.sleep(0)
        queued = asyncio.create_task(
            self.transport.execute(queued_command, deadline_s=3.0, observe_s=0.04)
        )
        await asyncio.sleep(0)  # Queue behind the active motion before Stop arrives.
        self.assertFalse(queued.done())
        stop = asyncio.create_task(self.transport.execute("stop"))
        try:
            await asyncio.wait_for(owner_ack.wait(), 1.0)
            self.assertFalse(self.transport._stop_requested.is_set())
            if cancel_stop_after_ack:
                stop.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await stop
        finally:
            release_owner.set()
        with self.assertRaisesRegex(BenchRejected, "external stop requested"):
            await asyncio.wait_for(active, 1.0)
        with self.assertRaisesRegex(BenchRejected, "external stop requested"):
            await asyncio.wait_for(queued, 1.0)
        if not cancel_stop_after_ack:
            await asyncio.wait_for(stop, 1.0)
        self.assertEqual(self.client.writes.count(("start_command", True)), 1)
        self.assertNotIn(("reference_command", True), self.client.writes)

    async def test_queued_start_before_later_stop_stays_invalid_after_ack(self) -> None:
        await self._assert_queued_motion_invalidated_by_stop(
            "start_once", cancel_stop_after_ack=False
        )

    async def test_queued_reference_before_cancelled_stop_stays_invalid_after_ack(self) -> None:
        await self._assert_queued_motion_invalidated_by_stop(
            "reference_once", cancel_stop_after_ack=True
        )

    async def test_start_queued_during_stop_stays_invalid_after_ack(self) -> None:
        self.client.values["mode"] = True
        await self.transport._operation_lock.acquire()
        try:
            stop = asyncio.create_task(self.transport.execute("stop"))
            async with asyncio.timeout(0.5):
                while not self.transport._stop_requested.is_set():
                    await asyncio.sleep(0)
            start = asyncio.create_task(
                self.transport.execute("start_once", deadline_s=3.0, observe_s=0.04)
            )
            await asyncio.sleep(0)
            self.assertFalse(start.done())
        finally:
            self.transport._operation_lock.release()
        await asyncio.wait_for(stop, 1.0)
        self.assertFalse(self.transport._stop_requested.is_set())
        with self.assertRaisesRegex(BenchRejected, "external stop requested"):
            await asyncio.wait_for(start, 1.0)
        self.assertNotIn(("start_command", True), self.client.writes)

    async def test_failed_stop_write_readback_and_timeout_latch_until_ack(self) -> None:
        self.client.values["mode"] = True
        original_write = self.transport._write
        original_snapshot = self.transport._snapshot

        for failure in ("write", "readback", "timeout"):
            with self.subTest(failure=failure):
                async def failed_write(client, key, value):
                    if key == "stop_command" and value is True:
                        raise RuntimeError("stop write failed")
                    await original_write(client, key, value)

                async def failed_snapshot(client):
                    if failure == "readback":
                        raise RuntimeError("stop readback failed")
                    snap = await original_snapshot(client)
                    return {**snap, "step": 30}

                if failure == "write":
                    self.transport._write = failed_write
                else:
                    self.transport._snapshot = failed_snapshot
                try:
                    with self.assertRaises(BenchUnknownOutcome) as outcome:
                        await self.transport.execute("stop")
                    if failure != "timeout":
                        self.assertIsInstance(outcome.exception.__cause__, RuntimeError)
                finally:
                    self.transport._write = original_write
                    self.transport._snapshot = original_snapshot

                self.assertTrue(self.transport._stop_requested.is_set())
                starts_before = self.client.writes.count(("start_command", True))
                with self.assertRaisesRegex(BenchRejected, "external stop requested"):
                    await self.transport.execute("start_once", deadline_s=1.0, observe_s=0.04)
                self.assertEqual(self.client.writes.count(("start_command", True)), starts_before)
                self.assertTrue(self.transport._stop_requested.is_set())
                await self.transport.execute("stop")
                self.assertFalse(self.transport._stop_requested.is_set())
                await self.transport.execute("start_once", deadline_s=3.0, observe_s=0.04)
                self.assertEqual(self.client.writes.count(("start_command", True)), starts_before + 1)

    async def test_failed_mode_exit_keeps_stop_intent_latched(self) -> None:
        self.client.values["mode"] = True
        original_write = self.transport._write

        async def failed_mode_off(client, key, value):
            if key == "mode" and value is False:
                raise RuntimeError("mode exit write failed")
            await original_write(client, key, value)

        self.transport._write = failed_mode_off
        with self.assertRaisesRegex(RuntimeError, "mode exit write failed"):
            await self.transport.execute("bench_mode_off")
        self.transport._write = original_write
        self.assertTrue(self.transport._stop_requested.is_set())
        await self.transport.execute("stop")
        self.assertFalse(self.transport._stop_requested.is_set())

    async def test_stop_during_preflight_never_emits_start_true(self) -> None:
        self.client.values["mode"] = True
        preflight = asyncio.Event()
        release = asyncio.Event()
        original_snapshot = self.transport._snapshot

        async def blocked_snapshot(client):
            if not preflight.is_set():
                preflight.set()
                await release.wait()
            return await original_snapshot(client)

        self.transport._snapshot = blocked_snapshot
        motion = asyncio.create_task(
            self.transport.execute("start_once", deadline_s=2.0, observe_s=0.1)
        )
        await asyncio.wait_for(preflight.wait(), 0.5)
        stop = asyncio.create_task(self.transport.execute("stop"))
        async with asyncio.timeout(0.5):
            while not self.transport._stop_requested.is_set():
                await asyncio.sleep(0)
        release.set()
        with self.assertRaisesRegex(BenchRejected, "external stop requested"):
            await motion
        await stop
        self.assertNotIn(("start_command", True), self.client.writes)
        self.assertIn(("stop_command", True), self.client.writes)

    async def test_stop_between_start_false_and_true_never_emits_start_true(self) -> None:
        self.client.values["mode"] = True
        start_false_sent = asyncio.Event()
        release = asyncio.Event()
        original_write = self.transport._write

        async def blocked_write(client, key, value):
            await original_write(client, key, value)
            if key == "start_command" and value is False and not start_false_sent.is_set():
                start_false_sent.set()
                await release.wait()

        self.transport._write = blocked_write
        motion = asyncio.create_task(
            self.transport.execute("start_once", deadline_s=2.0, observe_s=0.1)
        )
        await asyncio.wait_for(start_false_sent.wait(), 0.5)
        stop = asyncio.create_task(self.transport.execute("stop"))
        async with asyncio.timeout(0.5):
            while not self.transport._stop_requested.is_set():
                await asyncio.sleep(0)
        release.set()
        with self.assertRaisesRegex(BenchRejected, "external stop requested"):
            await motion
        await stop
        self.assertNotIn(("start_command", True), self.client.writes)
        self.assertIn(("stop_command", True), self.client.writes)

    async def test_mode_exit_preempts_observation_then_turns_mode_off(self) -> None:
        self.client.values["mode"] = True
        motion = asyncio.create_task(
            self.transport.execute("start_once", deadline_s=3.0, observe_s=1.0)
        )
        await asyncio.sleep(0.06)
        result = await self.transport.execute("bench_mode_off")
        with self.assertRaisesRegex(BenchRejected, "external stop requested"):
            await motion
        self.assertFalse(result["final"]["mode"])
        self.assertFalse(self.client.values["automatic"])

    async def test_latched_fault_uses_only_typed_reset_without_start(self) -> None:
        self.client.values["fault_code"] = 4
        self.client.values["motion_fault"] = True
        result = await self.transport.execute("reset_once", deadline_s=2.0)
        self.assertEqual(result["command"], "reset_once")
        self.assertEqual(result["final"]["fault_code"], 0)
        self.assertIn(("reset_command", True), self.client.writes)
        self.assertIn(("stop_command", True), self.client.writes)
        self.assertNotIn(("start_command", True), self.client.writes)
        self.assertFalse(self.client.values["mode"])

    async def test_wrong_target_or_unknown_command_cannot_write(self) -> None:
        with self.assertRaises(BenchRejected):
            BenchConfig(
                endpoint="opc.tcp://192.168.1.6:4840",
                expected_ip="192.0.2.10",
                expected_fingerprint=FINGERPRINT,
            )
        with self.assertRaises(BenchRejected):
            await self.transport.execute("UnboundSignals.ArbitraryOutput")  # type: ignore[arg-type]
        self.assertEqual(self.client.writes, [])

        async def wrong_probe(_: str) -> str:
            return "sha256:" + "b" * 64

        wrong = BenchOpcUa(
            self.transport.config,
            fingerprint_probe=wrong_probe,
            client_factory=lambda *_: self.client,
        )
        with self.assertRaisesRegex(BenchRejected, "fingerprint mismatch"):
            await wrong.execute("bench_mode_on")
        self.assertEqual(self.client.writes, [])

    async def test_preflight_rejects_drive_activity_before_start(self) -> None:
        self.client.values["mode"] = True
        self.client.values["drive_running"] = [True, False, False, False, False]
        with self.assertRaisesRegex(BenchRejected, "preflight"):
            await self.transport.execute("start_once", deadline_s=1.0, observe_s=0.04)
        self.assertNotIn(("start_command", True), self.client.writes)

    async def test_deadline_still_sends_stop(self) -> None:
        self.client.values["mode"] = True
        original = FakeNode.read_value

        async def frozen_step(node: FakeNode):
            if node.name == "step":
                return 30 if node.client.values["reference_command"] is False and node.client.writes else 0
            return await original(node)

        FakeNode.read_value = frozen_step
        try:
            with self.assertRaises(BenchRejected):
                await self.transport.execute("reference_once", deadline_s=1.0)
        finally:
            FakeNode.read_value = original
        self.assertIn(("stop_command", True), self.client.writes)


if __name__ == "__main__":
    unittest.main()
