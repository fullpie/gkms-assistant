"""Read-only capture of the verified Gakumas PC client window.

This module intentionally contains no input APIs.  It is suitable for a
capture/recognition loop, but cannot click, focus, or otherwise control the
game.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import struct
import time
import zlib
from ctypes import wintypes
from dataclasses import asdict, dataclass
from pathlib import Path
from .application_paths import game_file
from typing import Protocol


TARGET_TITLE = "gakumas"
TARGET_CLASS = "UnityWndClass"
TARGET_EXECUTABLE = game_file('gakumas.exe')
CANONICAL_WIDTH = 720
CANONICAL_HEIGHT = 1280
PW_CLIENTONLY = 0x00000001
PW_RENDERFULLCONTENT = 0x00000002
BI_RGB = 0
DIB_RGB_COLORS = 0
GA_ROOT = 2
SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
BLACKNESS = 0x00000042
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
ERROR_ACCESS_DENIED = 5


class CaptureError(RuntimeError):
    """A capture failure that never attempts a fallback capture mechanism."""


def enable_process_dpi_awareness() -> None:
    """Disable DPI virtualization before reading window/client coordinates.

    Without this, a 720x1280 game client can be reported as 540x960 on a
    133% scaled display.  PrintWindow then receives a too-small bitmap and
    silently crops the right and bottom quarter of the game UI.
    """

    if os.name != "nt":
        return
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    setter = getattr(user32, "SetProcessDpiAwarenessContext", None)
    if setter is not None:
        setter.argtypes = [ctypes.c_void_p]
        setter.restype = wintypes.BOOL
        ctypes.set_last_error(0)
        if setter(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
            return
        if ctypes.get_last_error() == ERROR_ACCESS_DENIED:
            # The host may already have selected an equally suitable context.
            return
    legacy = getattr(user32, "SetProcessDPIAware", None)
    if legacy is not None:
        legacy.restype = wintypes.BOOL
        legacy()


@dataclass(frozen=True, slots=True)
class WindowIdentity:
    hwnd: int
    pid: int
    title: str
    class_name: str
    executable: str


@dataclass(frozen=True, slots=True)
class LiveCapture:
    hwnd: int
    pid: int
    width: int
    height: int
    scale_x: float
    scale_y: float
    timestamp: float
    png_path: str | None
    capture_method: str


class WindowCaptureBackend(Protocol):
    def find_window(self) -> WindowIdentity | None: ...

    def print_window_client(self, hwnd: int) -> tuple[int, int, bytes, str]: ...

    def screen_capture_client(self, hwnd: int) -> tuple[int, int, bytes, str]: ...


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class NativeWin32CaptureBackend:
    """Minimal GDI/PrintWindow backend.  No screen-copy fallback is permitted."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("live capture requires Windows")
        enable_process_dpi_awareness()
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        self.user32.FindWindowW.restype = wintypes.HWND
        self.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self.user32.GetWindowTextLengthW.restype = ctypes.c_int
        self.user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        self.user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
        self.user32.PrintWindow.restype = wintypes.BOOL
        self.user32.GetDC.argtypes = [wintypes.HWND]
        self.user32.GetDC.restype = wintypes.HDC
        self.user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        self.user32.ReleaseDC.restype = ctypes.c_int
        self.user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user32.IsWindowVisible.restype = wintypes.BOOL
        self.user32.IsIconic.argtypes = [wintypes.HWND]
        self.user32.IsIconic.restype = wintypes.BOOL
        self.user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.user32.SetForegroundWindow.restype = wintypes.BOOL
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.WindowFromPoint.argtypes = [wintypes.POINT]
        self.user32.WindowFromPoint.restype = wintypes.HWND
        self.user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user32.GetAncestor.restype = wintypes.HWND
        self.gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        self.gdi32.CreateCompatibleDC.restype = wintypes.HDC
        self.gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        self.gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
        self.gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        self.gdi32.SelectObject.restype = wintypes.HGDIOBJ
        self.gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        self.gdi32.DeleteObject.restype = wintypes.BOOL
        self.gdi32.DeleteDC.argtypes = [wintypes.HDC]
        self.gdi32.DeleteDC.restype = wintypes.BOOL
        self.gdi32.PatBlt.argtypes = [
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.DWORD,
        ]
        self.gdi32.PatBlt.restype = wintypes.BOOL
        self.gdi32.BitBlt.argtypes = [
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.DWORD,
        ]
        self.gdi32.BitBlt.restype = wintypes.BOOL
        self.gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), wintypes.UINT]
        self.gdi32.GetDIBits.restype = ctypes.c_int
        self.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel32.OpenProcess.restype = wintypes.HANDLE
        self.kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL

    def find_window(self) -> WindowIdentity | None:
        hwnd = self.user32.FindWindowW(TARGET_CLASS, TARGET_TITLE)
        if not hwnd:
            return None
        title = self._window_text(hwnd)
        class_name = self._class_name(hwnd)
        pid = wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            raise CaptureError("GetWindowThreadProcessId returned no process id")
        return WindowIdentity(int(hwnd), pid.value, title, class_name, self._process_path(pid.value))

    def _window_text(self, hwnd: int) -> str:
        size = self.user32.GetWindowTextLengthW(hwnd) + 1
        buffer = ctypes.create_unicode_buffer(max(1, size))
        self.user32.GetWindowTextW(hwnd, buffer, len(buffer))
        return buffer.value

    def _class_name(self, hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        self.user32.GetClassNameW(hwnd, buffer, len(buffer))
        return buffer.value

    def _process_path(self, pid: int) -> str:
        handle = self.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            raise CaptureError(f"OpenProcess(query_limited) failed: {ctypes.get_last_error()}")
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not self.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                raise CaptureError(f"QueryFullProcessImageNameW failed: {ctypes.get_last_error()}")
            return buffer.value
        finally:
            self.kernel32.CloseHandle(handle)

    def print_window_client(self, hwnd: int) -> tuple[int, int, bytes, str]:
        rect = wintypes.RECT()
        if not self.user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            raise CaptureError(f"GetClientRect failed: {ctypes.get_last_error()}")
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width <= 0 or height <= 0:
            raise CaptureError(f"invalid client size: {width}x{height}")
        screen_dc = self.user32.GetDC(wintypes.HWND(hwnd))
        if not screen_dc:
            raise CaptureError(f"GetDC failed: {ctypes.get_last_error()}")
        memory_dc = bitmap = previous = None
        captured: tuple[int, int, bytes, str] | None = None
        try:
            memory_dc = self.gdi32.CreateCompatibleDC(screen_dc)
            bitmap = self.gdi32.CreateCompatibleBitmap(screen_dc, width, height)
            if not memory_dc or not bitmap:
                raise CaptureError(f"GDI bitmap setup failed: {ctypes.get_last_error()}")
            previous = self.gdi32.SelectObject(memory_dc, bitmap)
            for flags in (
                PW_CLIENTONLY | PW_RENDERFULLCONTENT,
                PW_CLIENTONLY,
            ):
                self.gdi32.PatBlt(memory_dc, 0, 0, width, height, BLACKNESS)
                if not self.user32.PrintWindow(wintypes.HWND(hwnd), memory_dc, flags):
                    continue
                pixels = self._dibits(memory_dc, bitmap, width, height)
                if not _frame_is_blank(pixels):
                    captured = (width, height, pixels, f"PrintWindow({flags})")
                    break
        finally:
            if memory_dc and previous:
                self.gdi32.SelectObject(memory_dc, previous)
            if bitmap:
                self.gdi32.DeleteObject(bitmap)
            if memory_dc:
                self.gdi32.DeleteDC(memory_dc)
            self.user32.ReleaseDC(wintypes.HWND(hwnd), screen_dc)
        if captured is not None:
            return captured
        return self.screen_capture_client(hwnd)

    def _dibits(
        self, memory_dc: int, bitmap: int, width: int, height: int
    ) -> bytes:
        info = BITMAPINFO()
        info.bmiHeader = BITMAPINFOHEADER(
            ctypes.sizeof(BITMAPINFOHEADER),
            width,
            -height,
            1,
            32,
            BI_RGB,
            0,
            0,
            0,
            0,
            0,
        )
        pixels = ctypes.create_string_buffer(width * height * 4)
        rows = self.gdi32.GetDIBits(
            memory_dc,
            bitmap,
            0,
            height,
            pixels,
            ctypes.byref(info),
            DIB_RGB_COLORS,
        )
        if rows != height:
            raise CaptureError(f"GetDIBits returned {rows} of {height} rows")
        return pixels.raw

    def screen_capture_client(self, hwnd: int) -> tuple[int, int, bytes, str]:
        rect = wintypes.RECT()
        if not self.user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            raise CaptureError(f"GetClientRect failed: {ctypes.get_last_error()}")
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width <= 0 or height <= 0:
            raise CaptureError(f"invalid client size: {width}x{height}")
        if not self.user32.IsWindowVisible(wintypes.HWND(hwnd)):
            raise CaptureError("BitBlt fallback refused: target window is not visible")
        if self.user32.IsIconic(wintypes.HWND(hwnd)):
            raise CaptureError("BitBlt fallback refused: target window is minimized")
        origin = wintypes.POINT(0, 0)
        if not self.user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(origin)):
            raise CaptureError(f"ClientToScreen failed: {ctypes.get_last_error()}")
        sample_x = tuple(
            sorted({2, width // 4, width // 2, width * 3 // 4, max(2, width - 3)})
        )
        sample_y = tuple(
            sorted(
                {
                    2,
                    height // 6,
                    height // 3,
                    height // 2,
                    height * 2 // 3,
                    height * 5 // 6,
                    max(2, height - 3),
                }
            )
        )
        for client_x in sample_x:
            for client_y in sample_y:
                point = wintypes.POINT(origin.x + client_x, origin.y + client_y)
                pointed = self.user32.WindowFromPoint(point)
                root = self.user32.GetAncestor(pointed, GA_ROOT)
                if int(root or 0) != hwnd:
                    raise CaptureError(
                        "BitBlt fallback refused: target client is occluded"
                    )

        source_dc = self.user32.GetDC(None)
        if not source_dc:
            raise CaptureError(f"GetDC(desktop) failed: {ctypes.get_last_error()}")
        memory_dc = bitmap = previous = None
        try:
            memory_dc = self.gdi32.CreateCompatibleDC(source_dc)
            bitmap = self.gdi32.CreateCompatibleBitmap(source_dc, width, height)
            if not memory_dc or not bitmap:
                raise CaptureError(f"GDI fallback setup failed: {ctypes.get_last_error()}")
            previous = self.gdi32.SelectObject(memory_dc, bitmap)
            if not self.gdi32.BitBlt(
                memory_dc,
                0,
                0,
                width,
                height,
                source_dc,
                origin.x,
                origin.y,
                SRCCOPY | CAPTUREBLT,
            ):
                raise CaptureError(f"BitBlt failed: {ctypes.get_last_error()}")
            pixels = self._dibits(memory_dc, bitmap, width, height)
            if _frame_is_blank(pixels):
                raise CaptureError("BitBlt returned a blank frame")
            return width, height, pixels, "BitBlt(verified-visible)"
        finally:
            if memory_dc and previous:
                self.gdi32.SelectObject(memory_dc, previous)
            if bitmap:
                self.gdi32.DeleteObject(bitmap)
            if memory_dc:
                self.gdi32.DeleteDC(memory_dc)
            self.user32.ReleaseDC(None, source_dc)


def _verified_identity(backend: WindowCaptureBackend) -> WindowIdentity:
    identity = backend.find_window()
    if identity is None:
        raise CaptureError("verified gakumas window not found")
    expected = TARGET_EXECUTABLE.resolve(strict=False)
    actual = Path(identity.executable).resolve(strict=False)
    failures = []
    if identity.title != TARGET_TITLE:
        failures.append(f"title={identity.title!r}")
    if identity.class_name != TARGET_CLASS:
        failures.append(f"class={identity.class_name!r}")
    if actual != expected:
        failures.append(f"exe={identity.executable!r}")
    if failures:
        raise CaptureError("window identity mismatch: " + ", ".join(failures))
    return identity


def _frame_is_blank(bgra: bytes) -> bool:
    """Reject uniform PrintWindow placeholders before they reach recognition."""

    if not bgra:
        return True
    minimum = 255
    maximum = 0
    step = max(4, (len(bgra) // 4096) // 4 * 4)
    for offset in range(0, len(bgra), step):
        for channel in bgra[offset : offset + 3]:
            minimum = min(minimum, channel)
            maximum = max(maximum, channel)
            if maximum - minimum > 8:
                return False
    return True


def _png_from_bgra(width: int, height: int, bgra: bytes) -> bytes:
    if len(bgra) != width * height * 4:
        raise ValueError("unexpected BGRA buffer length")
    rows = bytearray()
    for offset in range(0, len(bgra), width * 4):
        rows.append(0)
        row = bgra[offset : offset + width * 4]
        for index in range(0, len(row), 4):
            blue, green, red, _alpha = row[index : index + 4]
            rows.extend((red, green, blue, 255))
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + chunk(b"IEND", b"")


def capture_live(
    *,
    save_png: bool = False,
    backend: WindowCaptureBackend | None = None,
    capture_mode: str = "print-window",
) -> LiveCapture:
    backend = backend or NativeWin32CaptureBackend()
    identity = _verified_identity(backend)
    if capture_mode == "print-window":
        width, height, bgra, capture_method = backend.print_window_client(identity.hwnd)
    elif capture_mode == "screen":
        width, height, bgra, capture_method = backend.screen_capture_client(identity.hwnd)
    else:
        raise ValueError(f"unsupported capture mode: {capture_mode}")
    timestamp = time.time()
    png_path: str | None = None
    if save_png:
        from .capture_artifact_retention import allocate_capture_artifact

        allocation = allocate_capture_artifact(Path(__file__).resolve().parents[2], backend="maa", purpose="manual", timestamp=timestamp)
        output = allocation.path
        assert output is not None
        output.write_bytes(_png_from_bgra(width, height, bgra))
        png_path = str(output)
    return LiveCapture(
        identity.hwnd,
        identity.pid,
        width,
        height,
        width / CANONICAL_WIDTH,
        height / CANONICAL_HEIGHT,
        timestamp,
        png_path,
        capture_method,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only verified Gakumas client capture")
    parser.add_argument("--save-png", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asdict(capture_live(save_png=args.save_png)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
