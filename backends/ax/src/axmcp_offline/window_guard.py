from __future__ import annotations

import argparse
import ctypes
import os
import time
from ctypes import wintypes
from pathlib import Path

from .security import atomic_write_replace, canonical_json_bytes, utc_now


class _WindowsProcessHandle:
    """Retain one native handle so PID reuse cannot retarget the guard."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        self.kernel32.OpenProcess.restype = wintypes.HANDLE
        self.kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel32.TerminateProcess.restype = wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        access = 0x00100000 | 0x0001  # SYNCHRONIZE | PROCESS_TERMINATE
        self.handle = self.kernel32.OpenProcess(access, False, pid)
        if not self.handle:
            error = ctypes.get_last_error()
            raise OSError(error, "OpenProcess failed while binding AX guard identity")

    def is_alive(self) -> bool:
        result = self.kernel32.WaitForSingleObject(self.handle, 0)
        if result == 0x00000102:  # WAIT_TIMEOUT
            return True
        if result == 0x00000000:  # WAIT_OBJECT_0
            return False
        raise OSError(
            ctypes.get_last_error(),
            "WaitForSingleObject failed while checking AX liveness",
        )

    def terminate(self) -> None:
        if not self.is_alive():
            return
        if not self.kernel32.TerminateProcess(self.handle, 2):
            raise OSError(ctypes.get_last_error(), "TerminateProcess failed")

    def close(self) -> None:
        if self.handle:
            self.kernel32.CloseHandle(self.handle)
            self.handle = None


def _pid_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        error = ctypes.get_last_error()
        # ERROR_INVALID_PARAMETER is the documented result for a PID that no
        # longer identifies a process.  Access denial or any other native
        # failure is an unknown liveness state and must fail the guard closed.
        if error == 87:
            return False
        raise OSError(error, "OpenProcess failed while checking AX liveness")
    try:
        result = kernel32.WaitForSingleObject(handle, 0)
        if result == 0x00000102:  # WAIT_TIMEOUT
            return True
        if result == 0x00000000:  # WAIT_OBJECT_0
            return False
        raise OSError(
            ctypes.get_last_error(),
            "WaitForSingleObject failed while checking AX liveness",
        )
    finally:
        kernel32.CloseHandle(handle)


def _hide_process_windows_once(pid: int) -> int:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    enum_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [enum_callback, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindowAsync.restype = wintypes.BOOL
    hidden = 0
    callback_error: list[OSError] = []

    def hide_if_owned(window: int, _parameter: int) -> bool:
        nonlocal hidden
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(window):
            if not user32.ShowWindowAsync(window, 0):
                callback_error.append(
                    OSError(
                        ctypes.get_last_error(),
                        "ShowWindowAsync failed while enforcing hidden AX runtime",
                    )
                )
                return False
            hidden += 1
        return True

    enumerated = user32.EnumWindows(enum_callback(hide_if_owned), 0)
    if callback_error:
        raise callback_error[0]
    if not enumerated:
        error = ctypes.get_last_error()
        if error:
            raise OSError(error, "EnumWindows failed while enforcing hidden AX runtime")
    return hidden


def _terminate_pid(pid: int) -> None:
    if os.name != "nt":
        if _pid_alive(pid):
            os.kill(pid, 15)
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
    if not handle:
        raise OSError(ctypes.get_last_error(), "OpenProcess failed before termination")
    try:
        if not kernel32.TerminateProcess(handle, 2):
            raise OSError(ctypes.get_last_error(), "TerminateProcess failed")
    finally:
        kernel32.CloseHandle(handle)


def run_guard(
    pid: int,
    max_runtime_seconds: int,
    status_path: Path,
    *,
    process_alive=None,
    hide_once=None,
    terminate=None,
    monotonic=None,
    wait=None,
) -> int:
    hide_once = hide_once or _hide_process_windows_once
    monotonic = monotonic or time.monotonic
    wait = wait or time.sleep
    retained_handle = None
    started_at = utc_now()
    hidden_total = 0
    status = {
        "schema_version": "1",
        "record_type": "AxWindowGuard",
        "pid": pid,
        "status": "starting",
        "started_at": started_at,
        "checked_at": started_at,
        "windows_hidden": 0,
        "error": None,
    }

    def publish() -> None:
        atomic_write_replace(status_path, canonical_json_bytes(status))

    try:
        if os.name != "nt":
            raise RuntimeError("AX hidden-window guard is Windows-only")
        if process_alive is None:
            retained_handle = _WindowsProcessHandle(pid)
            process_alive = lambda _pid: retained_handle.is_alive()
        if terminate is None:
            terminate = (
                (lambda _pid: retained_handle.terminate())
                if retained_handle is not None
                else _terminate_pid
            )
        if pid <= 0 or not process_alive(pid):
            raise RuntimeError("AX process is not alive at window-guard start")
        hidden_total += hide_once(pid)
        status.update(
            {
                "status": "active",
                "checked_at": utc_now(),
                "windows_hidden": hidden_total,
            }
        )
        publish()
        deadline = monotonic() + max_runtime_seconds
        last_publish = 0.0
        while monotonic() < deadline:
            if not process_alive(pid):
                status.update(
                    {
                        "status": "stopped",
                        "checked_at": utc_now(),
                        "windows_hidden": hidden_total,
                    }
                )
                publish()
                return 0
            hidden_total += hide_once(pid)
            now = monotonic()
            if now - last_publish >= 1.0:
                status.update(
                    {"checked_at": utc_now(), "windows_hidden": hidden_total}
                )
                publish()
                last_publish = now
            wait(0.025)
        raise TimeoutError("AX process exceeded the bounded window-guard runtime")
    except Exception as exc:
        original_error = str(exc)
        status.update(
            {
                "status": "failed",
                "checked_at": utc_now(),
                "windows_hidden": hidden_total,
                "error": original_error,
            }
        )
        try:
            publish()
        except Exception:
            pass
        if terminate is not None:
            try:
                terminate(pid)
            except Exception as termination_error:
                status["error"] = (
                    original_error
                    + "; AX termination failed: "
                    + str(termination_error)
                )
                try:
                    publish()
                except Exception:
                    pass
        return 2
    finally:
        if retained_handle is not None:
            retained_handle.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded AX hidden-window guard")
    parser.add_argument("--pid", required=True, type=int)
    parser.add_argument("--max-runtime-seconds", required=True, type=int)
    parser.add_argument("--status", required=True, type=Path)
    args = parser.parse_args()
    if not 30 <= args.max_runtime_seconds <= 630:
        parser.error("max runtime must be between 30 and 630 seconds")
    status = args.status.resolve()
    if not status.parent.is_dir():
        parser.error("status parent must already exist")
    return run_guard(args.pid, args.max_runtime_seconds, status)


if __name__ == "__main__":
    raise SystemExit(main())
