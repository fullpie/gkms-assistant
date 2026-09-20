from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
import secrets
import subprocess
import time
from ctypes import wintypes
from dataclasses import asdict, dataclass
from pathlib import Path
from .application_paths import game_file
from typing import Any, Mapping

from .runtime_probe import (
    PROCESS_QUERY_LIMITED_INFORMATION,
    _close,
    _open_process,
    _query_integrity,
    _query_path,
)
from .live_capture import (
    CANONICAL_HEIGHT,
    CANONICAL_WIDTH,
    LiveCapture,
    capture_live,
    enable_process_dpi_awareness,
)
from .maa_win32 import MaaWin32Session
from .capture_artifact_retention import allocate_capture_artifact


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = PROJECT_ROOT / "var" / "elevated_controller"
SESSION_PATH = RUNTIME_DIR / "session.json"
REQUEST_PATH = RUNTIME_DIR / "request.json"
RESPONSE_PATH = RUNTIME_DIR / "response.json"
TARGET_TITLE = "gakumas"
TARGET_CLASS = "UnityWndClass"
TARGET_EXECUTABLE = game_file('gakumas.exe').resolve()
DIRECT_LAUNCHER = (
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "GakumasSmartLauncher"
    / "GakumasSmartLauncher.exe"
).resolve()
WINDOWS_POWERSHELL = (
    Path(os.environ.get("WINDIR", r"C:\Windows"))
    / "System32"
    / "WindowsPowerShell"
    / "v1.0"
    / "powershell.exe"
).resolve()
MAA_RUNTIME_DIR = PROJECT_ROOT / "var" / "maa_runtime"
CAPTURE_DIR = PROJECT_ROOT / "var" / "captures"
CONTROLLER_VERSION = 46
WM_CLOSE = 0x0010
PROCESS_TERMINATE = 0x0001
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF
CLOSE_GAME_GRACE_SECONDS = 5.0
CLOSE_GAME_FORCE_WAIT_SECONDS = 5.0
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
MK_LBUTTON = 0x0001
SMTO_ABORTIFHUNG = 0x0002
ERROR_ALREADY_EXISTS = 183
INPUT_MOUSE = 0
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
GA_ROOT = 2


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("union",)
    _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]


@dataclass(frozen=True, slots=True)
class TargetIdentity:
    """The complete fixed identity bound to one elevated worker."""

    hwnd: int
    pid: int
    title: str
    class_name: str
    executable: str


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


def _current_integrity() -> str:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    value, error = _query_integrity(kernel32.GetCurrentProcess())
    if error or value is None:
        raise OSError(error, "無法查詢 controller integrity")
    return value


def _same_executable_path(actual: str) -> bool:
    """Compare a process path with the one immutable game executable path."""

    try:
        return Path(actual).resolve(strict=False) == TARGET_EXECUTABLE
    except (OSError, RuntimeError, TypeError):
        return False


def _target_identity() -> TargetIdentity:
    """Find and validate the one game window this worker is allowed to own."""

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [
        wintypes.HWND,
        wintypes.LPWSTR,
        ctypes.c_int,
    ]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [
        wintypes.HWND,
        wintypes.LPWSTR,
        ctypes.c_int,
    ]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    hwnd = user32.FindWindowW(TARGET_CLASS, TARGET_TITLE)
    if not hwnd:
        raise ProcessLookupError("找不到 gakumas / UnityWndClass 視窗")

    title_length = max(0, int(user32.GetWindowTextLengthW(hwnd)))
    title_buffer = ctypes.create_unicode_buffer(title_length + 1)
    user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))
    title = title_buffer.value

    class_buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, class_buffer, len(class_buffer))
    class_name = class_buffer.value

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        raise ProcessLookupError("無法取得遊戲 PID")

    handle, error = _open_process(pid.value, PROCESS_QUERY_LIMITED_INFORMATION)
    if handle is None:
        raise OSError(error, "無法查詢遊戲程序")
    try:
        executable, path_error = _query_path(handle)
    finally:
        _close(handle)
    if path_error or executable is None:
        raise OSError(path_error, "無法取得遊戲路徑")
    identity = TargetIdentity(
        hwnd=int(hwnd),
        pid=int(pid.value),
        title=title,
        class_name=class_name,
        executable=executable,
    )
    if identity.title != TARGET_TITLE:
        raise PermissionError(f"拒絕非白名單遊戲標題：{identity.title!r}")
    if identity.class_name != TARGET_CLASS:
        raise PermissionError(f"拒絕非白名單遊戲 class：{identity.class_name!r}")
    if not _same_executable_path(identity.executable):
        raise PermissionError(f"拒絕非白名單遊戲路徑：{executable}")
    return identity


def _target_window() -> tuple[int, int]:
    """Return the HWND/PID after validating the complete target identity."""

    identity = _target_identity()
    return identity.hwnd, identity.pid


def _require_bound_game_window(*, expected_hwnd: int, expected_pid: int) -> None:
    """Reject a command when the game restarted behind this worker."""

    try:
        current_hwnd, current_pid = _target_window()
    except ProcessLookupError as error:
        raise RuntimeError(
            "bound game window no longer exists; restart the elevated worker"
        ) from error
    if current_hwnd != expected_hwnd or current_pid != expected_pid:
        raise RuntimeError(
            "game HWND/PID changed; restart the elevated worker before input"
        )


def _target_window_or_launch_direct(
    *,
    timeout_seconds: float = 90.0,
) -> tuple[int, int]:
    """Return the game window, launching the fixed Direct helper if absent.

    The supervisor starts this worker with an already-high token.  Launching
    the one fixed helper here therefore covers game restarts without another
    UAC prompt; no executable path or command line is accepted from IPC.
    """

    try:
        return _target_window()
    except ProcessLookupError:
        pass
    if not DIRECT_LAUNCHER.is_file():
        raise FileNotFoundError(f"找不到 Gakumas Direct：{DIRECT_LAUNCHER}")
    launcher_text = str(DIRECT_LAUNCHER).replace('"', '""')
    launch_script = (
        f'$asm=[System.Reflection.Assembly]::LoadFile("{launcher_text}")\n'
        '$type=$asm.GetType("GakumasSmartLauncher.LauncherEnvironment",$true)\n'
        '$create=$type.GetMethod("CreateDefault",'
        '[System.Reflection.BindingFlags]"Public,NonPublic,Static")\n'
        '$environment=$create.Invoke($null,@())\n'
        '$start=$type.GetMethod("StartGame",'
        '[System.Reflection.BindingFlags]"Public,NonPublic,Instance")\n'
        '$null=$start.Invoke($environment,@($false))\n'
    )
    encoded_script = base64.b64encode(
        launch_script.encode("utf-16-le")
    ).decode("ascii")
    subprocess.Popen(
        [
            str(WINDOWS_POWERSHELL),
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            encoded_script,
        ],
        cwd=str(DIRECT_LAUNCHER.parent),
    )
    deadline = time.monotonic() + max(10.0, float(timeout_seconds))
    last_error: ProcessLookupError | None = None
    while time.monotonic() < deadline:
        try:
            return _target_window()
        except ProcessLookupError as error:
            last_error = error
            time.sleep(0.25)
    raise TimeoutError("Gakumas Direct 未在期限內建立遊戲視窗") from last_error


def _make_baseline_foreground_recovery(
    *,
    expected_hwnd: int,
    expected_pid: int,
) -> Any:
    """Build one verified, focus-only recovery callback for baseline exams."""

    from .window_focus import NativeFocusBackend, activate_verified_game

    focus = NativeFocusBackend()

    def recover() -> dict[str, Any]:
        # Re-check the controller's bound identity before even looking at the
        # foreground window.  A replaced HWND/PID must never be activated.
        current_hwnd, current_pid = _target_window()
        if current_hwnd != expected_hwnd or current_pid != expected_pid:
            raise RuntimeError(
                "遊戲視窗已更換；baseline foreground recovery 拒絕舊 handle"
            )
        if focus.foreground_window() == expected_hwnd:
            return {
                "attempted": False,
                "foreground": True,
                "target_hwnd": expected_hwnd,
                "target_pid": expected_pid,
            }
        identity = activate_verified_game(
            expected_hwnd=expected_hwnd,
            expected_pid=expected_pid,
            focus_backend=focus,
        )
        return {
            "attempted": True,
            "foreground": True,
            "target_hwnd": identity.hwnd,
            "target_pid": identity.pid,
        }

    return recover


def _window_geometry(hwnd: int) -> dict[str, int]:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    window_rect = wintypes.RECT()
    client_rect = wintypes.RECT()
    client_origin = POINT(0, 0)
    if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(window_rect)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(client_rect)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(client_origin)):
        raise ctypes.WinError(ctypes.get_last_error())
    return {
        "window_width": window_rect.right - window_rect.left,
        "window_height": window_rect.bottom - window_rect.top,
        "client_width": client_rect.right - client_rect.left,
        "client_height": client_rect.bottom - client_rect.top,
        "client_offset_x": client_origin.x - window_rect.left,
        "client_offset_y": client_origin.y - window_rect.top,
    }


def _send_mouse_message(hwnd: int, message: int, wparam: int, lparam: int) -> None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendMessageTimeoutW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    user32.SendMessageTimeoutW.restype = wintypes.LPARAM
    result = ctypes.c_size_t()
    if not user32.SendMessageTimeoutW(
        wintypes.HWND(hwnd),
        message,
        wparam,
        lparam,
        SMTO_ABORTIFHUNG,
        1000,
        ctypes.byref(result),
    ):
        error = ctypes.get_last_error()
        if error:
            raise OSError(error, f"SendMessageTimeoutW({message:#x}) 失敗")


def _wait_for_process_exit(handle: int, timeout_seconds: float) -> bool:
    """Wait for one already-open process handle without discovering a PID."""

    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or timeout_seconds < 0
    ):
        raise ValueError("timeout_seconds must be a non-negative number")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    timeout_ms = min(int(float(timeout_seconds) * 1000), 0xFFFFFFFF)
    result = int(kernel32.WaitForSingleObject(wintypes.HANDLE(handle), timeout_ms))
    if result == WAIT_OBJECT_0:
        return True
    if result == WAIT_TIMEOUT:
        return False
    if result == WAIT_FAILED:
        raise ctypes.WinError(ctypes.get_last_error())
    raise OSError(f"WaitForSingleObject returned unexpected result {result:#x}")


def _terminate_process(handle: int) -> None:
    """Terminate exactly the process represented by an already-open handle."""

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    if not kernel32.TerminateProcess(wintypes.HANDLE(handle), 1):
        raise ctypes.WinError(ctypes.get_last_error())


def _close_result(
    *,
    hwnd: int,
    pid: int,
    method: str,
) -> dict[str, Any]:
    """Serialize one stable close outcome for CLI and supervisor callers."""

    graceful = method == "graceful"
    return {
        "status": "closed",
        "outcome": method,
        "mode": method,
        "close_method": method,
        "closed": True,
        "graceful": graceful,
        "forced": not graceful,
        "method": "WM_CLOSE" if graceful else "TerminateProcess",
        "target_hwnd": hwnd,
        "target_pid": pid,
    }


def _close_bound_game(
    *,
    expected_hwnd: int,
    expected_pid: int,
    grace_seconds: float = CLOSE_GAME_GRACE_SECONDS,
    force_wait_seconds: float = CLOSE_GAME_FORCE_WAIT_SECONDS,
) -> dict[str, Any]:
    """Close only the worker-bound, fully validated game process.

    No caller-supplied PID, path, title, class, command line, or shell is used
    here.  The process handle is opened only after the current window identity
    matches the worker binding, and the identity is checked again immediately
    before a forced termination.
    """

    _require_bound_game_window(
        expected_hwnd=expected_hwnd,
        expected_pid=expected_pid,
    )
    handle, error = _open_process(
        expected_pid,
        PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE | SYNCHRONIZE,
    )
    if handle is None:
        raise OSError(error, "無法開啟已驗證的遊戲程序")
    try:
        # Query the path through the same handle used for any eventual force;
        # this closes the PID-reuse gap between validation and termination.
        executable, path_error = _query_path(handle)
        if path_error or executable is None:
            raise OSError(path_error, "無法取得已驗證遊戲路徑")
        if not _same_executable_path(executable):
            raise PermissionError(f"拒絕終止非白名單遊戲路徑：{executable}")

        try:
            _send_mouse_message(expected_hwnd, WM_CLOSE, 0, 0)
        except OSError:
            # A hung Unity window commonly makes SendMessageTimeout report
            # ERROR_TIMEOUT.  The process handle and full bound identity are
            # already verified; treat this as a failed graceful attempt and
            # continue to the same-handle fallback below.
            pass
        if _wait_for_process_exit(handle, grace_seconds):
            return _close_result(
                hwnd=expected_hwnd,
                pid=expected_pid,
                method="graceful",
            )

        # A graceful close may have replaced/destroyed the window while the
        # process remains alive.  Never turn that changed identity into a kill.
        _require_bound_game_window(
            expected_hwnd=expected_hwnd,
            expected_pid=expected_pid,
        )
        _terminate_process(handle)
        if not _wait_for_process_exit(handle, force_wait_seconds):
            raise TimeoutError("已驗證遊戲程序在強制關閉後仍未結束")
        return _close_result(
            hwnd=expected_hwnd,
            pid=expected_pid,
            method="forced",
        )
    finally:
        _close(handle)


def _click_once(hwnd: int, window_x: int, window_y: int) -> dict[str, int]:
    geometry = _window_geometry(hwnd)
    client_x = window_x - geometry["client_offset_x"]
    client_y = window_y - geometry["client_offset_y"]
    if not 0 <= client_x < geometry["client_width"]:
        raise ValueError("點擊 X 超出遊戲 client area")
    if not 0 <= client_y < geometry["client_height"]:
        raise ValueError("點擊 Y 超出遊戲 client area")
    lparam = (client_y & 0xFFFF) << 16 | (client_x & 0xFFFF)
    _send_mouse_message(hwnd, WM_MOUSEMOVE, 0, lparam)
    _send_mouse_message(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lparam)
    time.sleep(0.05)
    _send_mouse_message(hwnd, WM_LBUTTONUP, 0, lparam)
    return {"window_x": window_x, "window_y": window_y, "client_x": client_x, "client_y": client_y}


def _send_input_click_once(hwnd: int, window_x: int, window_y: int) -> dict[str, int | str]:
    geometry = _window_geometry(hwnd)
    client_x = window_x - geometry["client_offset_x"]
    client_y = window_y - geometry["client_offset_y"]
    if not 0 <= client_x < geometry["client_width"]:
        raise ValueError("點擊 X 超出遊戲 client area")
    if not 0 <= client_y < geometry["client_height"]:
        raise ValueError("點擊 Y 超出遊戲 client area")

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.WindowFromPoint.argtypes = [POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT

    screen_point = POINT(client_x, client_y)
    if not user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(screen_point)):
        raise ctypes.WinError(ctypes.get_last_error())
    pointed_window = user32.WindowFromPoint(screen_point)
    pointed_root = user32.GetAncestor(pointed_window, GA_ROOT)
    if int(pointed_root or 0) != hwnd:
        raise RuntimeError("目標螢幕座標目前被其他視窗遮住")

    user32.SetForegroundWindow(wintypes.HWND(hwnd))
    if int(user32.GetForegroundWindow() or 0) != hwnd:
        raise RuntimeError("無法確認 gakumas 是前景視窗")

    original_cursor = POINT()
    user32.GetCursorPos(ctypes.byref(original_cursor))
    if not user32.SetCursorPos(screen_point.x, screen_point.y):
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        down = INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dwFlags=MOUSEEVENTF_LEFTDOWN))
        up = INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dwFlags=MOUSEEVENTF_LEFTUP))
        if user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT)) != 1:
            raise ctypes.WinError(ctypes.get_last_error())
        time.sleep(0.05)
        if user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT)) != 1:
            raise ctypes.WinError(ctypes.get_last_error())
        time.sleep(0.1)
    finally:
        current_cursor = POINT()
        if user32.GetCursorPos(ctypes.byref(current_cursor)) and (
            current_cursor.x == screen_point.x and current_cursor.y == screen_point.y
        ):
            user32.SetCursorPos(original_cursor.x, original_cursor.y)
    return {
        "method": "SendInput",
        "window_x": window_x,
        "window_y": window_y,
        "client_x": client_x,
        "client_y": client_y,
        "screen_x": screen_point.x,
        "screen_y": screen_point.y,
    }


def _status(
    hwnd: int,
    target_pid: int,
    integrity: str,
    maa_session: MaaWin32Session | None = None,
) -> dict[str, Any]:
    current_hwnd, current_pid = _target_window()
    if current_hwnd != hwnd or current_pid != target_pid:
        raise RuntimeError("遊戲視窗已更換；controller 拒絕沿用舊 handle")
    status = {
        "controller_version": CONTROLLER_VERSION,
        "helper_pid": os.getpid(),
        "helper_integrity": integrity,
        "target_pid": target_pid,
        "target_hwnd": hwnd,
        "target_title": TARGET_TITLE,
        "target_class": TARGET_CLASS,
        "target_executable": str(TARGET_EXECUTABLE),
        "coordinate_space": "outer_window",
        "geometry": _window_geometry(hwnd),
        "backend": "maa_win32_background" if maa_session else "legacy_win32",
        "background_control": maa_session is not None,
        "foreground_required": maa_session is None,
    }
    if maa_session is not None:
        status["maa_connected"] = maa_session.connected
        status["maa_resolution"] = list(maa_session.resolution)
        status["maa_info"] = maa_session.info
        status["maa_capture_contract"] = {
            "artifact_width": CANONICAL_WIDTH,
            "artifact_height": CANONICAL_HEIGHT,
            "allowed_native_width": CANONICAL_WIDTH,
            "allowed_native_height_min": CANONICAL_HEIGHT - 1,
            "allowed_native_height_max": CANONICAL_HEIGHT + 1,
            "normalization": "top-left-invariant-bottom-edge-only",
            "resampling": False,
        }
    return status


def _capture_with_maa(
    hwnd: int,
    target_pid: int,
    maa_session: MaaWin32Session,
) -> dict[str, Any]:
    """Capture one verified client frame through MaaFramework PrintWindow."""

    current_hwnd, current_pid = _target_window()
    if current_hwnd != hwnd or current_pid != target_pid:
        raise RuntimeError("遊戲視窗已更換；controller 拒絕擷取舊 handle")
    image = maa_session.capture()
    height, width = int(image.shape[0]), int(image.shape[1])
    if (width, height) != (CANONICAL_WIDTH, CANONICAL_HEIGHT):
        raise RuntimeError(
            "MaaFramework session violated the canonical capture contract: "
            f"{width}x{height}"
        )
    timestamp = time.time()
    allocation = allocate_capture_artifact(PROJECT_ROOT, backend="maa", purpose="vision-input", kind="live", timestamp=timestamp)
    output = allocation.path
    assert output is not None
    maa_session.save_bgr_png(image, output)
    result = asdict(
        LiveCapture(
            hwnd=hwnd,
            pid=target_pid,
            width=width,
            height=height,
            scale_x=width / CANONICAL_WIDTH,
            scale_y=height / CANONICAL_HEIGHT,
            timestamp=timestamp,
            png_path=str(output),
            capture_method="MaaFramework(PrintWindow-background)",
        )
    )
    # The artifact is canonical, while Maa input remains in the unresampled
    # native screenshot coordinates.  Persist both facts in capture metadata.
    result["maa_capture_contract"] = maa_session.capture_contract
    result["artifact_retention"] = allocation.metadata()
    return result


def _capture_native_with_maa(
    hwnd: int,
    target_pid: int,
    maa_session: MaaWin32Session,
) -> dict[str, Any]:
    """Capture a raw portrait/landscape frame for background navigation."""

    current_hwnd, current_pid = _target_window()
    if current_hwnd != hwnd or current_pid != target_pid:
        raise RuntimeError("target window changed; controller refuses the stale handle")
    image = maa_session.capture_native()
    height, width = int(image.shape[0]), int(image.shape[1])
    timestamp = time.time()
    allocation = allocate_capture_artifact(PROJECT_ROOT, backend="maa", purpose="vision-input", kind="live-native", timestamp=timestamp)
    output = allocation.path
    assert output is not None
    maa_session.save_bgr_png(image, output)
    result = asdict(
        LiveCapture(
            hwnd=hwnd,
            pid=target_pid,
            width=width,
            height=height,
            scale_x=1.0,
            scale_y=1.0,
            timestamp=timestamp,
            png_path=str(output),
            capture_method="MaaFramework(PrintWindow-background-native)",
        )
    )
    result["maa_capture_contract"] = {
        "artifact_width": width,
        "artifact_height": height,
        "native_coordinate_width": width,
        "native_coordinate_height": height,
        "normalization": "native-identity",
        "orientation": "landscape" if width > height else "portrait",
        "origin": "top-left",
        "resampling": False,
        "detector_eligible": False,
    }
    result["artifact_retention"] = allocation.metadata()
    return result


def _recognize_audition_cards_once(
    hwnd: int,
    target_pid: int,
    maa_session: MaaWin32Session,
    *,
    capture_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the pure card recognizer on one controller-bound fresh frame."""

    if capture_metadata is None:
        capture_metadata = {}
    if not isinstance(capture_metadata, Mapping):
        raise ValueError("capture_metadata must be an object")
    current_hwnd, current_pid = _target_window()
    if current_hwnd != hwnd or current_pid != target_pid:
        raise RuntimeError("target window changed; controller refuses the stale handle")
    metadata = dict(capture_metadata)
    metadata.update({"hwnd": hwnd, "pid": target_pid})
    timestamp = time.time()
    allocation = allocate_capture_artifact(PROJECT_ROOT, backend="maa", purpose="vision-input", kind="audition-cards", timestamp=timestamp)
    output = allocation.path
    assert output is not None
    metadata["artifact_retention"] = allocation.metadata()
    return maa_session.recognize_audition_cards_once(
        capture_path=output,
        capture_metadata=metadata,
    )


def _capture_once(
    hwnd: int,
    target_pid: int,
    maa_session: MaaWin32Session | None = None,
) -> dict[str, Any]:
    """Capture exactly one verified client frame into the project var folder."""

    if maa_session is not None:
        return _capture_with_maa(hwnd, target_pid, maa_session)
    current_hwnd, current_pid = _target_window()
    if current_hwnd != hwnd or current_pid != target_pid:
        raise RuntimeError("遊戲視窗已更換；controller 拒絕擷取舊 handle")
    capture = capture_live(save_png=True)
    if capture.hwnd != hwnd or capture.pid != target_pid:
        raise RuntimeError("擷取結果不是 controller 綁定的遊戲視窗")
    return asdict(capture)


def _capture_screen_once(
    hwnd: int,
    target_pid: int,
    maa_session: MaaWin32Session | None = None,
) -> dict[str, Any]:
    """Capture one verified frame; Maa sessions do this in the background."""

    if maa_session is not None:
        return _capture_with_maa(hwnd, target_pid, maa_session)
    current_hwnd, current_pid = _target_window()
    if current_hwnd != hwnd or current_pid != target_pid:
        raise RuntimeError("target window changed; controller refuses the stale handle")
    capture = capture_live(save_png=True, capture_mode="screen")
    if capture.hwnd != hwnd or capture.pid != target_pid:
        raise RuntimeError("screen capture did not come from the bound target window")
    return asdict(capture)


def _maa_click_once(
    maa_session: MaaWin32Session,
    hwnd: int,
    window_x: int,
    window_y: int,
) -> dict[str, int | str]:
    """Translate the existing outer-window contract into a Maa client click."""

    geometry = _window_geometry(hwnd)
    client_x = window_x - geometry["client_offset_x"]
    client_y = window_y - geometry["client_offset_y"]
    if not 0 <= client_x < geometry["client_width"]:
        raise ValueError("點擊 X 超出遊戲 client area")
    if not 0 <= client_y < geometry["client_height"]:
        raise ValueError("點擊 Y 超出遊戲 client area")
    coordinate_width, coordinate_height = maa_session.coordinate_size
    portrait_geometry = (
        type(coordinate_width) is int
        and type(coordinate_height) is int
        and coordinate_width == CANONICAL_WIDTH
        and abs(coordinate_height - CANONICAL_HEIGHT) <= 1
    )
    # The final live rotates the same verified HWND to 16:9. Its Maa frame is
    # exactly 1280x720. Accept that native coordinate space for background
    # navigation while keeping capture artifacts/detectors portrait-only.
    landscape_geometry = (
        type(coordinate_width) is int
        and type(coordinate_height) is int
        and coordinate_width == CANONICAL_HEIGHT
        and coordinate_height == CANONICAL_WIDTH
    )
    if not (portrait_geometry or landscape_geometry):
        raise RuntimeError(
            "MaaFramework native coordinate geometry is outside the capture contract"
        )
    maa_x = round(client_x * coordinate_width / geometry["client_width"])
    maa_y = round(client_y * coordinate_height / geometry["client_height"])
    if not (0 <= maa_x < coordinate_width and 0 <= maa_y < coordinate_height):
        raise RuntimeError("MaaFramework coordinate mapping escaped the native canvas")
    result = dict(maa_session.click(maa_x, maa_y))
    result.update(
        {
            "window_x": window_x,
            "window_y": window_y,
            "physical_client_x": client_x,
            "physical_client_y": client_y,
            "maa_coordinate_width": coordinate_width,
            "maa_coordinate_height": coordinate_height,
        }
    )
    return result


def _response(request_id: str, *, ok: bool, **payload: Any) -> None:
    _atomic_json(
        RESPONSE_PATH,
        {"request_id": request_id, "ok": ok, "timestamp": time.time(), **payload},
    )


SUPPORTED_COMMANDS = frozenset(
    {
        "status",
        "focus_game_once",
        "capture_once",
        "capture_screen_once",
        "capture_native_once",
        "recognize_audition_cards_once",
        "recognize_nia_mirror_once",
        "recognize_nia_outer_once",
        "audit_nia_replay_controls",
        "recognize_nia_replay_skip_once",
        "open_produce_ranking",
        "open_nia_recommended_replay",
        "open_nia_pro_recommended_replay",
        "start_first_nia_pro_recommended_replay",
        "wait_global_home",
        "advance_nia_replay",
        "resume_active_produce",
        "start_nia_produce",
        "start_produce",
        "run_initial_plan1_exam",
        "run_maa_baseline_exam",
        "advance_nia_post_live",
        "flush_runtime_outer_observer_once",
        "click_once",
        "send_input_click_once",
        "send_input_scroll_once",
        "send_input_swipe_once",
        "close_game",
        # Kept as a fixed compatibility alias for older clients.  Both names
        # execute the same worker-bound close path below.
        "close_game_once",
        "stop",
    }
)


def _require_supported_command(command: object) -> str:
    if not isinstance(command, str) or command not in SUPPORTED_COMMANDS:
        raise ValueError(f"unknown controller command: {command}")
    return command


def _request_bool(request: Mapping[str, Any], name: str, *, default: bool = False) -> bool:
    """Read one opt-in boolean without accepting truthy strings or integers."""

    value = request.get(name, default)
    if type(value) is not bool:
        raise ValueError(f"{name} must be bool")
    return value


def _flush_runtime_outer_observer_once(
    request: Mapping[str, Any],
    *,
    target_pid: int,
) -> dict[str, object]:
    """Call the fixed native writer barrier for this worker's bound game PID."""

    from .telemetry_injector import (
        RUNTIME_OUTER_OBSERVER_BARRIER_MAX_TIMEOUT_MS,
        flush_runtime_outer_observer_once,
    )

    allowed = {
        "request_id",
        "token",
        "target_pid",
        "command",
        "timeout_ms",
        "expected_generation",
        "minimum_next",
    }
    unsupported = set(request) - allowed
    if unsupported:
        raise ValueError(
            "flush_runtime_outer_observer_once received unsupported fields: "
            f"{sorted(unsupported)}"
        )

    def unsigned(name: str, *, bits: int) -> int:
        value = request.get(name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} must be an integer")
        maximum = (1 << bits) - 1
        if value < 0 or value > maximum:
            raise ValueError(f"{name} must be between 0 and {maximum}")
        return value

    timeout_ms = unsigned("timeout_ms", bits=32)
    if timeout_ms < 1 or timeout_ms > RUNTIME_OUTER_OBSERVER_BARRIER_MAX_TIMEOUT_MS:
        raise ValueError(
            "timeout_ms must be between 1 and "
            f"{RUNTIME_OUTER_OBSERVER_BARRIER_MAX_TIMEOUT_MS}"
        )
    expected_generation = unsigned("expected_generation", bits=64)
    minimum_next = unsigned("minimum_next", bits=64)
    return flush_runtime_outer_observer_once(
        target_pid,
        timeout_ms=timeout_ms,
        expected_generation=expected_generation,
        minimum_next=minimum_next,
    )


def serve(*, idle_timeout: float) -> None:
    enable_process_dpi_awareness()
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    mutex = kernel32.CreateMutexW(None, False, "Local\\gkms_tool_elevated_controller_v1")
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(mutex)
        raise RuntimeError("elevated controller 已在執行")

    integrity = _current_integrity()
    if not integrity.startswith("high "):
        kernel32.CloseHandle(mutex)
        raise PermissionError(f"controller 必須以 high integrity 啟動，目前為 {integrity}")
    hwnd, target_pid = _target_window_or_launch_direct()
    maa_session = MaaWin32Session(hwnd, MAA_RUNTIME_DIR)
    maa_session.prewarm_read_only_recognizers()
    token = secrets.token_urlsafe(32)
    for stale in (REQUEST_PATH, RESPONSE_PATH):
        if stale.exists():
            stale.unlink()
    session = {
        **_status(hwnd, target_pid, integrity, maa_session),
        "token": token,
        "active": True,
        "started_at": time.time(),
        "request_path": str(REQUEST_PATH),
        "response_path": str(RESPONSE_PATH),
    }
    _atomic_json(SESSION_PATH, session)

    last_activity = time.monotonic()
    last_request_id = ""
    try:
        running = True
        while running and time.monotonic() - last_activity < idle_timeout:
            if not REQUEST_PATH.is_file():
                time.sleep(0.1)
                continue
            try:
                request = json.loads(REQUEST_PATH.read_text(encoding="utf-8"))
                request_id = str(request.get("request_id", ""))
                if not request_id or request_id == last_request_id:
                    raise ValueError("request_id 缺失或重複")
                last_request_id = request_id
                if not secrets.compare_digest(str(request.get("token", "")), token):
                    raise PermissionError("controller token 不符")
                claimed_target_pid = request.get("target_pid")
                if type(claimed_target_pid) is not int or claimed_target_pid != target_pid:
                    raise PermissionError("target_pid 不符")
                command = _require_supported_command(request.get("command"))
                if command not in {"status", "stop"}:
                    _require_bound_game_window(
                        expected_hwnd=hwnd,
                        expected_pid=target_pid,
                    )
                if command == "status":
                    _response(
                        request_id,
                        ok=True,
                        result=_status(hwnd, target_pid, integrity, maa_session),
                    )
                elif command == "focus_game_once":
                    _response(
                        request_id,
                        ok=True,
                        result=_make_baseline_foreground_recovery(
                            expected_hwnd=hwnd,
                            expected_pid=target_pid,
                        )(),
                    )
                elif command == "capture_once":
                    _response(
                        request_id,
                        ok=True,
                        result=_capture_once(hwnd, target_pid, maa_session),
                    )
                elif command == "capture_screen_once":
                    _response(
                        request_id,
                        ok=True,
                        result=_capture_screen_once(hwnd, target_pid, maa_session),
                    )
                elif command == "capture_native_once":
                    _response(
                        request_id,
                        ok=True,
                        result=_capture_native_with_maa(hwnd, target_pid, maa_session),
                    )
                elif command == "recognize_audition_cards_once":
                    raw_metadata = request.get("capture_metadata", {})
                    if not isinstance(raw_metadata, Mapping):
                        raise ValueError("capture_metadata must be an object")
                    _response(
                        request_id,
                        ok=True,
                        result=_recognize_audition_cards_once(
                            hwnd,
                            target_pid,
                            maa_session,
                            capture_metadata=raw_metadata,
                        ),
                    )
                elif command == "recognize_nia_mirror_once":
                    _response(
                        request_id,
                        ok=True,
                        result=maa_session.recognize_read_only_node(
                            "ProduceRecognitionMirror"
                        ),
                    )
                elif command == "recognize_nia_outer_once":
                    timestamp = time.time()
                    allocation = allocate_capture_artifact(PROJECT_ROOT, backend="maa", purpose="vision-input", kind="outer", timestamp=timestamp)
                    output = allocation.path
                    assert output is not None
                    _response(
                        request_id,
                        ok=True,
                        result={
                            **maa_session.recognize_nia_outer_once(
                                capture_path=output,
                                scope=str(request.get("scope", "all")),
                                timeout_seconds=float(
                                    request.get("timeout_seconds", 15.0)
                                ),
                            ),
                            "capture": {
                                "hwnd": hwnd,
                                "pid": target_pid,
                                "width": CANONICAL_WIDTH,
                                "height": CANONICAL_HEIGHT,
                                "timestamp": timestamp,
                                "png_path": str(output),
                                "capture_method": "MaaFramework(PrintWindow-background-batched-recognition)",
                                "artifact_retention": allocation.metadata(),
                            },
                        },
                    )
                elif command == "audit_nia_replay_controls":
                    _response(
                        request_id,
                        ok=True,
                        result=maa_session.audit_nia_replay_controls(),
                    )
                elif command == "recognize_nia_replay_skip_once":
                    _response(
                        request_id,
                        ok=True,
                        result=maa_session.recognize_nia_replay_skip_once(),
                    )
                elif command == "open_produce_ranking":
                    _response(
                        request_id,
                        ok=True,
                        result=maa_session.open_produce_ranking(
                            timeout_seconds=float(
                                request.get("timeout_seconds", 60.0)
                            )
                        ),
                    )
                elif command == "open_nia_recommended_replay":
                    _response(
                        request_id,
                        ok=True,
                        result=maa_session.open_nia_recommended_replay(
                            idol_card_id=str(request.get("idol_card_id", "")),
                            timeout_seconds=float(
                                request.get("timeout_seconds", 60.0)
                            ),
                            from_new_produce_home=_request_bool(
                                request, "from_new_produce_home"
                            ),
                            use_ap_drink=_request_bool(
                                request, "use_ap_drink"
                            ),
                        ),
                    )
                elif command == "open_nia_pro_recommended_replay":
                    _response(
                        request_id,
                        ok=True,
                        result=maa_session.open_nia_pro_recommended_replay(
                            idol_card_id=str(request.get("idol_card_id", "")),
                            timeout_seconds=float(
                                request.get("timeout_seconds", 60.0)
                            ),
                            from_new_produce_home=_request_bool(
                                request, "from_new_produce_home"
                            ),
                            use_ap_drink=_request_bool(
                                request, "use_ap_drink"
                            ),
                        ),
                    )
                elif command == "start_first_nia_pro_recommended_replay":
                    raw_visible_rank = request.get("visible_rank", 1)
                    if type(raw_visible_rank) is not int or not 1 <= raw_visible_rank <= 10:
                        raise ValueError(
                            "visible_rank must be an integer between 1 and 10"
                        )
                    _response(
                        request_id,
                        ok=True,
                        result=maa_session.start_first_nia_pro_recommended_replay(
                            timeout_seconds=float(
                                request.get("timeout_seconds", 30.0)
                            ),
                            visible_rank=raw_visible_rank,
                        ),
                    )
                elif command == "wait_global_home":
                    _response(
                        request_id,
                        ok=True,
                        result=maa_session.wait_global_home(
                            timeout_seconds=float(
                                request.get("timeout_seconds", 60.0)
                            )
                        ),
                    )
                elif command == "advance_nia_replay":
                    raw_target_turn = request.get("target_turn")
                    if raw_target_turn is not None and type(raw_target_turn) is not int:
                        raise ValueError("target_turn must be int or null")
                    _response(
                        request_id,
                        ok=True,
                        result=maa_session.advance_nia_replay(
                            target_turn=raw_target_turn,
                            timeout_seconds=float(
                                request.get("timeout_seconds", 30.0)
                            ),
                        ),
                    )
                elif command == "resume_active_produce":
                    _response(
                        request_id,
                        ok=True,
                        result=maa_session.resume_active_produce(
                            timeout_seconds=float(request.get("timeout_seconds", 30.0))
                        ),
                    )
                elif command == "start_nia_produce":
                    bootstrap = maa_session.start_nia_produce(
                        produce_id=str(request.get("produce_id", "")),
                        idol_card_id=str(request.get("idol_card_id", "")),
                        use_ap_drink=_request_bool(request, "use_ap_drink"),
                    )
                    _response(request_id, ok=True, result=bootstrap.to_dict())
                elif command == "start_produce":
                    bootstrap = maa_session.start_produce(
                        produce_id=str(request.get("produce_id", "")),
                        idol_card_id=str(request.get("idol_card_id", "")),
                        use_ap_drink=_request_bool(request, "use_ap_drink"),
                    )
                    _response(request_id, ok=True, result=bootstrap.to_dict())
                elif command == "run_initial_plan1_exam":
                    completed = maa_session.run_initial_plan1_exam()
                    _response(request_id, ok=True, result=completed)
                elif command == "run_maa_baseline_exam":
                    raw_monitor_source_run_id = request.get("monitor_source_run_id")
                    if raw_monitor_source_run_id is not None and (
                        not isinstance(raw_monitor_source_run_id, str)
                        or not raw_monitor_source_run_id.strip()
                    ):
                        raise ValueError(
                            "monitor_source_run_id must be non-empty text or null"
                        )
                    # The verified native recorder is append-only per game
                    # process.  Fence this one Maa exam by byte offset so the
                    # diagnostic worker can consume the exact Live stage after
                    # formal completion without scanning/guessing another run.
                    native_stage_capture = None
                    try:
                        from .native_runtime_stage_capture import (
                            begin_native_runtime_stage_capture,
                        )

                        native_stage_capture = begin_native_runtime_stage_capture(
                            target_pid
                        )
                    except (OSError, TypeError, ValueError):
                        native_stage_capture = None
                    completed = maa_session.run_maa_baseline_exam(
                        timeout_seconds=float(
                            request.get("timeout_seconds", 600.0)
                        ),
                        foreground_recovery_callback=(
                            _make_baseline_foreground_recovery(
                                expected_hwnd=hwnd,
                                expected_pid=target_pid,
                            )
                        ),
                        monitor_source_run_id=raw_monitor_source_run_id,
                    )
                    if native_stage_capture is not None:
                        try:
                            completed = dict(completed)
                            completed["native_runtime_stage_capture"] = (
                                native_stage_capture.finish().to_dict()
                            )
                        except (OSError, TypeError, ValueError):
                            # Capture is diagnostic-only.  Maa's completed
                            # receipt remains authoritative and unchanged.
                            pass
                    _response(request_id, ok=True, result=completed)
                elif command == "advance_nia_post_live":
                    completed = maa_session.advance_nia_post_live()
                    _response(request_id, ok=True, result=completed.to_dict())
                elif command == "flush_runtime_outer_observer_once":
                    _response(
                        request_id,
                        ok=True,
                        result=_flush_runtime_outer_observer_once(
                            request,
                            target_pid=target_pid,
                        ),
                    )
                elif command == "click_once":
                    click = _maa_click_once(
                        maa_session,
                        hwnd, int(request["window_x"]), int(request["window_y"])
                    )
                    _response(request_id, ok=True, result=click)
                elif command == "send_input_click_once":
                    click = _maa_click_once(
                        maa_session,
                        hwnd, int(request["window_x"]), int(request["window_y"])
                    )
                    _response(request_id, ok=True, result=click)
                elif command == "send_input_scroll_once":
                    scroll = maa_session.scroll(
                        int(request.get("delta_x", 0)),
                        int(request.get("delta_y", 0)),
                    )
                    _response(request_id, ok=True, result=scroll)
                elif command == "send_input_swipe_once":
                    swipe = maa_session.swipe(
                        int(request["x1"]),
                        int(request["y1"]),
                        int(request["x2"]),
                        int(request["y2"]),
                        int(request.get("duration_ms", 450)),
                    )
                    _response(request_id, ok=True, result=swipe)
                elif command in {"close_game", "close_game_once"}:
                    # This is intentionally separate from controller `stop`,
                    # which only ends the high worker.
                    result = _close_bound_game(
                        expected_hwnd=hwnd,
                        expected_pid=target_pid,
                    )
                    _response(
                        request_id,
                        ok=True,
                        result=result,
                    )
                    running = False
                elif command == "stop":
                    _response(request_id, ok=True, result={"stopping": True})
                    running = False
                else:
                    raise ValueError(f"不支援的白名單命令：{command}")
                last_activity = time.monotonic()
            except Exception as exc:
                request_id = str(locals().get("request", {}).get("request_id", "unknown"))
                _response(request_id, ok=False, error=f"{type(exc).__name__}: {exc}")
            finally:
                if REQUEST_PATH.exists():
                    REQUEST_PATH.unlink()
    finally:
        session["active"] = False
        session["stopped_at"] = time.time()
        _atomic_json(SESSION_PATH, session)
        kernel32.CloseHandle(mutex)


def main() -> None:
    parser = argparse.ArgumentParser(description="gakumas 高權限白名單控制器。")
    parser.add_argument("--idle-timeout", type=float, default=7200.0)
    args = parser.parse_args()
    serve(idle_timeout=max(30.0, args.idle_timeout))


if __name__ == "__main__":
    main()
