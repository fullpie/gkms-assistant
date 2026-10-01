"""Bring only the verified Gakumas window to the foreground.

This is a narrow companion to the elevated click controller.  It accepts no
arbitrary title, executable, or keyboard input and never opens the game process
for memory access.  The executable identity is re-verified before focus changes.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Callable, Protocol

from .live_capture import (
    NativeWin32CaptureBackend,
    WindowCaptureBackend,
    WindowIdentity,
    _verified_identity,
)


SW_RESTORE = 9


class FocusBackend(Protocol):
    def restore(self, hwnd: int) -> None: ...

    def set_foreground(self, hwnd: int) -> bool: ...

    def foreground_window(self) -> int: ...


class NativeFocusBackend:
    def __init__(self) -> None:
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.ShowWindow.restype = wintypes.BOOL
        self.user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.user32.SetForegroundWindow.restype = wintypes.BOOL
        self.user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.AttachThreadInput.argtypes = [
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.BOOL,
        ]
        self.user32.AttachThreadInput.restype = wintypes.BOOL
        self.user32.BringWindowToTop.argtypes = [wintypes.HWND]
        self.user32.BringWindowToTop.restype = wintypes.BOOL
        self.user32.SetActiveWindow.argtypes = [wintypes.HWND]
        self.user32.SetActiveWindow.restype = wintypes.HWND
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    def restore(self, hwnd: int) -> None:
        self.user32.ShowWindow(wintypes.HWND(hwnd), SW_RESTORE)

    def set_foreground(self, hwnd: int) -> bool:
        target = wintypes.HWND(hwnd)
        if self.user32.SetForegroundWindow(target):
            return self.foreground_window() == hwnd

        # Windows may reject SetForegroundWindow when this elevated worker is
        # not the process that last received user input.  Temporarily attach
        # its input queue to the current foreground and target UI threads;
        # this changes focus only and sends no keyboard/mouse/game action.
        current_thread = int(self.kernel32.GetCurrentThreadId())
        foreground = self.user32.GetForegroundWindow()
        foreground_thread = int(
            self.user32.GetWindowThreadProcessId(foreground, None)
            if foreground
            else 0
        )
        target_thread = int(self.user32.GetWindowThreadProcessId(target, None))
        attached: list[int] = []
        try:
            for thread_id in dict.fromkeys((foreground_thread, target_thread)):
                if thread_id and thread_id != current_thread and self.user32.AttachThreadInput(
                    wintypes.DWORD(current_thread),
                    wintypes.DWORD(thread_id),
                    True,
                ):
                    attached.append(thread_id)
            self.user32.BringWindowToTop(target)
            self.user32.SetActiveWindow(target)
            self.user32.SetForegroundWindow(target)
            return self.foreground_window() == hwnd
        finally:
            for thread_id in reversed(attached):
                self.user32.AttachThreadInput(
                    wintypes.DWORD(current_thread),
                    wintypes.DWORD(thread_id),
                    False,
                )

    def foreground_window(self) -> int:
        return int(self.user32.GetForegroundWindow() or 0)


def activate_verified_game(
    *,
    expected_hwnd: int,
    expected_pid: int,
    capture_backend: WindowCaptureBackend | None = None,
    focus_backend: FocusBackend | None = None,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = 1.0,
) -> WindowIdentity:
    """Focus the exact previously observed game window or fail closed."""

    identity = _verified_identity(capture_backend or NativeWin32CaptureBackend())
    if identity.hwnd != expected_hwnd or identity.pid != expected_pid:
        raise RuntimeError("game window changed before execution")
    focus = focus_backend or NativeFocusBackend()
    focus.restore(identity.hwnd)
    focus.set_foreground(identity.hwnd)
    deadline = time.monotonic() + max(0.0, timeout)
    while focus.foreground_window() != identity.hwnd:
        if time.monotonic() >= deadline:
            raise RuntimeError("verified game window could not become foreground")
        sleep(0.05)
    return identity
