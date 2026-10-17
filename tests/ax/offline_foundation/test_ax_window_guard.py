from __future__ import annotations

import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from axmcp_offline import window_guard
from axmcp_offline.window_guard import run_guard


class AxWindowGuardTests(unittest.TestCase):
    def test_native_handle_is_retained_and_wait_failure_is_not_clean_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            status = Path(temporary) / "guard.json"

            class NativeCall:
                argtypes = None
                restype = None

                def __init__(self, values):
                    self.values = iter(values)
                    self.call_count = 0

                def __call__(self, *_args):
                    self.call_count += 1
                    return next(self.values)

            class Kernel32:
                def __init__(self) -> None:
                    self.OpenProcess = NativeCall((1,))
                    self.WaitForSingleObject = NativeCall(
                        (0x00000102, 0xFFFFFFFF, 0x00000102)
                    )
                    self.TerminateProcess = NativeCall((1,))
                    self.CloseHandle = NativeCall((1,))

            native = Kernel32()
            with mock.patch.object(window_guard.os, "name", "nt"), mock.patch.object(
                window_guard.ctypes, "WinDLL", return_value=native
            ), mock.patch.object(window_guard.ctypes, "get_last_error", return_value=5):
                result = run_guard(
                    4242,
                    30,
                    status,
                    hide_once=lambda _pid: 0,
                    monotonic=lambda: 0.0,
                    wait=lambda _seconds: None,
                )

            record = json.loads(status.read_text(encoding="utf-8"))
            self.assertEqual(result, 2)
            self.assertEqual(record["status"], "failed")
            self.assertIn("WaitForSingleObject failed", record["error"])
            self.assertEqual(native.OpenProcess.call_count, 1)
            self.assertEqual(native.TerminateProcess.call_count, 1)
            self.assertEqual(native.CloseHandle.call_count, 1)

    def test_termination_failure_is_recorded_in_durable_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            status = Path(temporary) / "guard.json"

            def fail_hide(_pid: int) -> int:
                raise OSError("hide failed")

            def fail_terminate(_pid: int) -> None:
                raise OSError("terminate failed")

            result = run_guard(
                4242,
                30,
                status,
                process_alive=lambda _pid: True,
                hide_once=fail_hide,
                terminate=fail_terminate,
                monotonic=lambda: 0.0,
                wait=lambda _seconds: None,
            )
            record = json.loads(status.read_text(encoding="utf-8"))
            self.assertEqual(result, 2)
            self.assertEqual(record["status"], "failed")
            self.assertIn("hide failed", record["error"])
            self.assertIn("AX termination failed", record["error"])
            self.assertIn("terminate failed", record["error"])

    def test_async_hide_failure_is_durable_and_terminates_ax(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            status = Path(temporary) / "guard.json"
            calls = 0
            terminated: list[int] = []

            def hide(pid: int) -> int:
                nonlocal calls
                self.assertEqual(pid, 4242)
                calls += 1
                if calls == 2:
                    raise OSError("simulated async hide failure")
                return 0

            result = run_guard(
                4242,
                30,
                status,
                process_alive=lambda _pid: True,
                hide_once=hide,
                terminate=terminated.append,
                monotonic=lambda: 0.0,
                wait=lambda _seconds: None,
            )
            record = json.loads(status.read_text(encoding="utf-8"))
            self.assertEqual(result, 2)
            self.assertEqual(record["status"], "failed")
            self.assertIn("async hide failure", record["error"])
            self.assertEqual(terminated, [4242])

    def test_deadline_is_durable_and_terminates_ax(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            status = Path(temporary) / "guard.json"
            terminated: list[int] = []
            ticks = iter((0.0, 31.0))
            result = run_guard(
                4242,
                30,
                status,
                process_alive=lambda _pid: True,
                hide_once=lambda _pid: 0,
                terminate=terminated.append,
                monotonic=lambda: next(ticks),
                wait=lambda _seconds: None,
            )
            record = json.loads(status.read_text(encoding="utf-8"))
            self.assertEqual(result, 2)
            self.assertEqual(record["status"], "failed")
            self.assertIn("bounded window-guard runtime", record["error"])
            self.assertEqual(terminated, [4242])


if __name__ == "__main__":
    unittest.main()
