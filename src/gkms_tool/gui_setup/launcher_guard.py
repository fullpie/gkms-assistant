"""Reuse project transaction and lifecycle exclusion for explicit UI launches."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path

from .packages import SetupError
from ..application_paths import state_root, native_state_root


def local_blocker(root):
    from ..native_maintenance import check_pending
    from ..run_identity import load_active_run
    try:
        check_pending(root=root)
    except Exception:
        return "PROJECT_INPUT_PENDING"
    try:
        if load_active_run(root=state_root(root) / "runs") is not None:
            return "ACTIVE_RUN_REQUIRES_MAINTENANCE"
    except Exception:
        return "ACTIVE_RUN_IDENTITY_UNVERIFIED"
    return None


def _process_alive(pid):
    """None means unverified; access denied must never mean stopped."""
    if type(pid) is not int or pid <= 0:
        return None
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.WaitForSingleObject.restype = ctypes.c_uint32
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        return False if ctypes.get_last_error() == 87 else None
    try:
        wait = kernel.WaitForSingleObject(handle, 0)
        return False if wait == 0 else True if wait == 0x102 else None
    finally:
        kernel.CloseHandle(handle)


def maintenance_blocker(root, *, status_client=None, process_alive=None):
    """Read status through the original authenticated client, never expose tokens."""
    from .. import native_maintenance as maintenance
    session_path = native_state_root(root) / "native_maintenance/session.json"
    if not session_path.is_file():
        return None
    try:
        # Only used to identify the existing process if its status client fails.
        # No session contents or raw error text are returned to GUI/logs.
        session = maintenance.read_json(session_path)
        if session.get("schema") != maintenance.SCHEMA:
            return "MAINTENANCE_STATE_UNVERIFIED"
        if status_client is None:
            if Path(root).resolve() != maintenance.ROOT.resolve():
                return "MAINTENANCE_STATE_UNVERIFIED"
            status_client = lambda: maintenance.submit("status", timeout=2)
        try:
            status = status_client()
        except Exception:
            status = None
        if isinstance(status, dict) and status.get("status") == "completed":
            result = status.get("result")
            if (isinstance(result, dict) and result.get("elevated") is True
                    and result.get("pid") == session.get("pid")
                    and result.get("session_id") == session.get("session_id")
                    and "active_job" in result):
                return "MAINTENANCE_JOB_ACTIVE" if result.get("active_job") else None
        alive = (process_alive or _process_alive)(session.get("pid"))
        return None if alive is False else "MAINTENANCE_STATE_UNVERIFIED"
    except Exception:
        return "MAINTENANCE_STATE_UNVERIFIED"


def guard_project_launcher(root):
    blocked = local_blocker(root) or maintenance_blocker(root)
    if blocked:
        raise SetupError(blocked)


def runtime_claim():
    from ..runtime_command_client import _claim_lock
    return _claim_lock()
