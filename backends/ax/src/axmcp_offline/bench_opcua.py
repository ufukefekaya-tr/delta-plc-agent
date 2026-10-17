"""Profile-bound OPC UA transport for an explicitly enrolled application.

The caller supplies a fresh, read-only target fingerprint probe. This module has
no generic node API and never edits or downloads a PLC project. An operation that
can move a motor owns its observe interval and sends Stop before returning.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Awaitable, Callable, Literal
from urllib.parse import urlsplit


REQUIRED_SIGNALS = frozenset(('mode', 'reference_command', 'reset_command', 'start_command', 'stop_command', 'step', 'running', 'fault', 'fault_code', 'motion_fault', 'admitted', 'real_standstill', 'motion_standstill', 'automatic', 'drive_run_requests', 'drive_run_acks', 'drive_running'))
COMMANDS = MappingProxyType({
    "bench_mode_on": ("mode", True),
    "bench_mode_off": ("mode", False),
    "reference_once": ("reference_command", True),
    "reset_once": ("reset_command", True),
    "start_once": ("start_command", True),
    "stop": ("stop_command", True),
})
BenchCommand = Literal[
    "bench_mode_on", "reset_once", "reference_once", "start_once", "stop", "bench_mode_off"
]


class BenchRejected(RuntimeError):
    """The exact target, state, or requested operation failed a closed gate."""


class BenchUnknownOutcome(BenchRejected):
    """Stop could not be confirmed; independent physical stop is required."""


@dataclass(frozen=True)
class BenchConfig:
    endpoint: str
    expected_ip: str
    expected_fingerprint: str
    node_prefix: str
    signals: dict[str, str]
    drive_count: int
    idle_step: int
    automatic_step: int
    io_timeout_s: float = 2.0
    pulse_s: float = 0.1
    poll_s: float = 0.2
    stop_timeout_s: float = 4.0

    def __post_init__(self) -> None:
        if not isinstance(self.node_prefix, str) or not re.fullmatch(r"ns=[0-9]+;s=.+[.]", self.node_prefix):
            raise BenchRejected("explicit OPC UA namespace/application prefix required")
        if not isinstance(self.signals, dict) or not REQUIRED_SIGNALS <= set(self.signals):
            raise BenchRejected("explicit signal binding profile is incomplete")
        if any(not isinstance(k, str) or not isinstance(v, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", v) for k, v in self.signals.items()):
            raise BenchRejected("invalid symbolic node binding")
        if len(set(self.signals.values())) != len(self.signals):
            raise BenchRejected("signal bindings must not alias")
        if type(self.drive_count) is not int or not 1 <= self.drive_count <= 64:
            raise BenchRejected("explicit drive count outside bounded range")
        if type(self.idle_step) is not int or type(self.automatic_step) is not int or self.idle_step == self.automatic_step:
            raise BenchRejected("explicit distinct state values required")
        object.__setattr__(self, "signals", MappingProxyType(dict(self.signals)))
        parsed = urlsplit(self.endpoint)
        try:
            expected = ipaddress.IPv4Address(self.expected_ip)
            actual = ipaddress.IPv4Address(parsed.hostname or "")
            port = parsed.port
        except (ValueError, TypeError) as exc:
            raise BenchRejected("exact IPv4 target required") from exc
        if (
            parsed.scheme != "opc.tcp"
            or actual != expected
            or port != 4840
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise BenchRejected("OPC UA endpoint differs from exact target")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.expected_fingerprint):
            raise BenchRejected("target fingerprint must be a SHA-256 digest")
        if not (0.05 <= self.io_timeout_s <= 10.0):
            raise BenchRejected("I/O timeout outside bounded range")
        if not (0.02 <= self.pulse_s <= 0.5):
            raise BenchRejected("pulse outside bounded range")
        if not (0.02 <= self.poll_s <= 1.0):
            raise BenchRejected("poll interval outside bounded range")
        if not (0.5 <= self.stop_timeout_s <= 10.0):
            raise BenchRejected("stop timeout outside bounded range")


def _default_client(endpoint: str, timeout_s: float):
    try:
        from asyncua import Client
    except ImportError as exc:
        raise BenchRejected("optional asyncua package is not installed") from exc
    return Client(endpoint, timeout=timeout_s)


class BenchOpcUa:
    """One serialized operation at a time; no public raw-node write method."""

    def __init__(
        self,
        config: BenchConfig,
        *,
        fingerprint_probe: Callable[[str], Awaitable[str]],
        client_factory: Callable[[str, float], object] = _default_client,
    ) -> None:
        if not callable(fingerprint_probe):
            raise BenchRejected("fresh live fingerprint probe required")
        self.config = config
        self._fingerprint_probe = fingerprint_probe
        self._client_factory = client_factory
        self._operation_lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
        self._stop_requested = asyncio.Event()
        self._stop_generation = 0

    async def _verified_client(self):
        try:
            actual = await asyncio.wait_for(
                self._fingerprint_probe(self.config.endpoint),
                timeout=self.config.io_timeout_s,
            )
        except Exception as exc:
            raise BenchRejected("live target fingerprint unavailable") from exc
        if actual != self.config.expected_fingerprint:
            raise BenchRejected("live target fingerprint mismatch")
        return self._client_factory(self.config.endpoint, self.config.io_timeout_s)

    async def _read(self, client, key: str):
        node = client.get_node(self.config.node_prefix + self.config.signals[key])
        return await asyncio.wait_for(node.read_value(), self.config.io_timeout_s)

    async def _write(self, client, key: str, value: bool) -> None:
        node = client.get_node(self.config.node_prefix + self.config.signals[key])
        await asyncio.wait_for(node.write_value(value), self.config.io_timeout_s)

    async def _snapshot(self, client) -> dict[str, object]:
        # A stalled telemetry node must not delay the Stop watchdog by one
        # timeout per symbol.
        values = await asyncio.wait_for(
            asyncio.gather(*(self._read(client, key) for key in self.config.signals)),
            timeout=self.config.io_timeout_s,
        )
        return dict(zip(self.config.signals, values))

    async def snapshot(self) -> dict[str, object]:
        async with self._operation_lock:
            client = await self._verified_client()
            async with client:
                return await self._snapshot(client)

    def _drives_idle(self, snapshot: dict[str, object]) -> bool:
        for key in ("drive_run_requests", "drive_run_acks", "drive_running"):
            values = snapshot[key]
            if not isinstance(values, (list, tuple)) or len(values) != self.config.drive_count:
                raise BenchRejected("drive state shape is unknown")
            if any(type(value) is not bool or value for value in values):
                return False
        return True

    def _require_idle(self, snap: dict[str, object], *, mode: bool) -> None:
        if (
            snap["mode"] is not mode
            or snap["step"] != self.config.idle_step
            or snap["fault"] is not False
            or snap["fault_code"] != 0
            or snap["motion_fault"] is not False
            or snap["motion_standstill"] is not True
            or snap["automatic"] is not False
            or not self._drives_idle(snap)
        ):
            raise BenchRejected("bench preflight is not idle and fault-free")

    async def _pulse(self, client, key: str, *, abort_on_stop: bool = False) -> None:
        # The enrolled application consumes rising edges and expects momentary inputs.
        await self._write(client, key, False)
        try:
            if abort_on_stop and self._stop_requested.is_set():
                raise BenchRejected("external stop requested")
            await self._write(client, key, True)
            await asyncio.sleep(self.config.pulse_s)
        finally:
            await self._write(client, key, False)

    async def _stop(self, client) -> dict[str, object]:
        async with self._stop_lock:
            # A motion owner's final Stop may service an external waiter that
            # was cancelled before acquiring the operation lock.
            serviced_generation = self._stop_generation
            try:
                await self._pulse(client, "stop_command")
                loop = asyncio.get_running_loop()
                deadline = loop.time() + self.config.stop_timeout_s
                while True:
                    snap = await self._snapshot(client)
                    if (
                        snap["step"] == self.config.idle_step
                        and snap["motion_standstill"] is True
                        and snap["automatic"] is False
                        and self._drives_idle(snap)
                    ):
                        if serviced_generation == self._stop_generation:
                            self._stop_requested.clear()
                        return snap
                    if loop.time() >= deadline:
                        raise BenchUnknownOutcome("stop was sent but standstill was not confirmed")
                    await asyncio.sleep(self.config.poll_s)
            except BenchUnknownOutcome:
                raise
            except Exception as exc:
                raise BenchUnknownOutcome("stop outcome could not be confirmed") from exc

    async def _watchdog(self, client, deadline_s: float, tripped: asyncio.Event) -> None:
        await asyncio.sleep(deadline_s)
        tripped.set()
        try:
            await self._stop(client)
        except Exception:
            # The owner still performs a final stop and reports unknown outcome.
            pass

    async def _reset_once(self, client, *, deadline_s: float) -> dict[str, object]:
        if not (1.0 <= deadline_s <= 15.0):
            raise BenchRejected("reset window outside bounded range")
        before = await self._snapshot(client)
        if (
            before["mode"] is not False
            or before["step"] != self.config.idle_step
            or before["automatic"] is not False
            or before["motion_standstill"] is not True
            or before["reset_command"] is not False
            or before["start_command"] is not False
            or (before["fault_code"] == 0 and before["motion_fault"] is False)
            or not self._drives_idle(before)
        ):
            raise BenchRejected("reset preflight is not stopped and idle")
        await self._write(client, "mode", True)
        try:
            await self._pulse(client, "reset_command")
            loop = asyncio.get_running_loop()
            deadline = loop.time() + deadline_s
            while loop.time() < deadline:
                if self._stop_requested.is_set():
                    raise BenchRejected("external stop requested")
                snap = await self._snapshot(client)
                if (
                    snap["step"] == self.config.idle_step
                    and snap["fault_code"] == 0
                    and snap["motion_fault"] is False
                    and snap["motion_standstill"] is True
                    and snap["automatic"] is False
                    and self._drives_idle(snap)
                ):
                    return snap
                await asyncio.sleep(self.config.poll_s)
            raise BenchRejected("existing-program reset did not clear fault")
        finally:
            try:
                await self._stop(client)
            finally:
                await self._write(client, "mode", False)

    async def _motion_once(
        self,
        client,
        key: str,
        *,
        deadline_s: float,
        observe_s: float,
    ) -> list[dict[str, object]]:
        if not (1.0 <= deadline_s <= 300.0 and 0.0 <= observe_s <= 300.0):
            raise BenchRejected("motion window outside bounded range")
        if observe_s > deadline_s:
            raise BenchRejected("observe window exceeds motion deadline")
        if self._stop_requested.is_set():
            raise BenchRejected("external stop requested")
        snap = await self._snapshot(client)
        self._require_idle(snap, mode=True)
        if snap[key] is not False:
            raise BenchRejected("momentary command is already asserted")
        samples = [snap]
        tripped = asyncio.Event()
        watchdog = asyncio.create_task(self._watchdog(client, deadline_s, tripped))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + deadline_s
        active_seen = False
        auto_seen_at: float | None = None
        try:
            if self._stop_requested.is_set():
                raise BenchRejected("external stop requested")
            await self._pulse(client, key, abort_on_stop=True)
            while loop.time() < deadline and not tripped.is_set():
                if self._stop_requested.is_set():
                    raise BenchRejected("external stop requested")
                snap = await self._snapshot(client)
                samples.append(snap)
                if self._stop_requested.is_set():
                    raise BenchRejected("external stop requested")
                if snap["fault"] is True or snap["fault_code"] != 0 or snap["motion_fault"] is True:
                    raise BenchRejected("PLC bench fault during motion")
                active_seen |= snap["step"] != self.config.idle_step
                if key == "reference_command":
                    if active_seen and snap["step"] == self.config.idle_step and snap["motion_standstill"] is True:
                        return samples
                elif snap["step"] == self.config.automatic_step and snap["automatic"] is True:
                    if auto_seen_at is None:
                        auto_seen_at = loop.time()
                    if loop.time() - auto_seen_at >= observe_s:
                        return samples
                elif auto_seen_at is not None:
                    raise BenchRejected("automatic observation interrupted; cause unverified")
                await asyncio.sleep(self.config.poll_s)
            raise BenchRejected("bench motion deadline expired before expected state")
        finally:
            watchdog.cancel()
            try:
                await watchdog
            except asyncio.CancelledError:
                pass
            try:
                await self._stop(client)
            except Exception as exc:
                raise BenchUnknownOutcome("final stop could not be confirmed") from exc

    async def execute(
        self,
        command: BenchCommand,
        *,
        deadline_s: float = 120.0,
        observe_s: float = 60.0,
    ) -> dict[str, object]:
        if command not in COMMANDS:
            raise BenchRejected("unknown or arbitrary bench command")
        motion_admission = (
            self._stop_generation, self._stop_requested.is_set()
        ) if command in {"start_once", "reference_once"} else None
        if command in {"stop", "bench_mode_off"}:
            # Signal the active motion owner before waiting for its serialized
            # client lease. That owner sends the first Stop in its finally path.
            self._stop_generation += 1
            self._stop_requested.set()
        async with self._operation_lock:
            if motion_admission is not None and (
                motion_admission[1]
                or motion_admission[0] != self._stop_generation
                or self._stop_requested.is_set()
            ):
                raise BenchRejected("external stop requested")
            client = await self._verified_client()
            async with client:
                if command == "stop":
                    return {"command": command, "final": await self._stop(client)}
                if command == "bench_mode_off":
                    try:
                        try:
                            await self._stop(client)
                        finally:
                            # Leaving bench mode is itself a Stop path in the enrolled application contract.
                            await self._write(client, "mode", False)
                        final = await self._snapshot(client)
                        if final["mode"] is not False or final["step"] != self.config.idle_step:
                            raise BenchUnknownOutcome("bench mode exit not confirmed")
                    except BaseException:
                        # A confirmed Stop does not acknowledge a failed exit.
                        self._stop_requested.set()
                        raise
                    return {"command": command, "final": final}
                if command == "bench_mode_on":
                    before = await self._snapshot(client)
                    self._require_idle(before, mode=False)
                    await self._write(client, "mode", True)
                    try:
                        final = await self._snapshot(client)
                        if final["mode"] is not True:
                            raise BenchUnknownOutcome("bench mode entry not confirmed")
                    except Exception:
                        await self._write(client, "mode", False)
                        raise
                    return {"command": command, "final": final}
                if command == "reset_once":
                    final = await self._reset_once(client, deadline_s=min(deadline_s, 15.0))
                    return {"command": command, "final": final}
                key = COMMANDS[command][0]
                samples = await self._motion_once(
                    client, key, deadline_s=deadline_s,
                    observe_s=observe_s if command == "start_once" else 0.0,
                )
                return {
                    "command": command,
                    "samples": samples,
                    "final": await self._snapshot(client),
                }
