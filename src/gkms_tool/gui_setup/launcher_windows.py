"""Native Windows API boundary for the source-integrated launcher.

No DirectLauncher EXE, .NET reflection, subprocess shell, or PowerShell bridge.
Only current-user DPAPI, process inspection and explicit game/DMM actions.
See docs/LAUNCHER_INTEGRATION.md for the audited upstream and API references.
"""
from __future__ import annotations
from contextlib import contextmanager
import ctypes as C
from ctypes import wintypes as W
import io
import os
from pathlib import Path
import time

class NativeError(Exception):
    """A stable, non-sensitive error code; never include command arguments."""
    def __init__(self, code: str, *, api=None, winerror=None, ntstatus=None):
        self.code = code
        self.api, self.winerror, self.ntstatus = api, winerror, ntstatus
        super().__init__(code)


class _OwnedProcessWitness:
    """Read-only identity of one retained kernel process object, not a PID lookup."""

    def __init__(self, kernel, handle, pid):
        if not handle or type(pid) is not int or pid <= 0:
            raise NativeError('PROCESS_IDENTITY_UNAVAILABLE')
        self._kernel, self._handle, self.pid = kernel, handle, pid
        self._verified_alive_identity = None

    def snapshot(self):
        if self._handle is None:
            raise NativeError('PROCESS_WITNESS_CLOSED')
        k, handle = self._kernel, self._handle
        k.GetProcessId.argtypes = [W.HANDLE]; k.GetProcessId.restype = W.DWORD
        k.GetProcessTimes.argtypes = [W.HANDLE] + [C.POINTER(W.FILETIME)] * 4
        k.GetProcessTimes.restype = W.BOOL
        k.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)]
        k.QueryFullProcessImageNameW.restype = W.BOOL
        k.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]; k.WaitForSingleObject.restype = W.DWORD
        before = k.WaitForSingleObject(handle, 0)
        if before not in (0, 0x102):
            raise NativeError('PROCESS_STATUS_UNKNOWN')
        if int(k.GetProcessId(handle)) != self.pid:
            raise NativeError('PROCESS_IDENTITY_CHANGED')
        count = W.DWORD(32768); image = C.create_unicode_buffer(count.value)
        path_ok = bool(k.QueryFullProcessImageNameW(handle, 0, image, C.byref(count)))
        path_error = C.get_last_error() if not path_ok else None
        observed_path = image.value if path_ok and image.value else None
        times = [W.FILETIME() for _ in range(4)]
        if not k.GetProcessTimes(handle, *[C.byref(value) for value in times]):
            raise NativeError('PROCESS_TIMES_UNVERIFIED', api='GetProcessTimes', winerror=C.get_last_error())
        class Basic(C.Structure):
            _fields_ = [('reserved', C.c_void_p), ('peb', C.c_void_p),
                        ('reserved2', C.c_void_p * 2), ('pid', C.c_size_t), ('parent', C.c_size_t)]
        native = C.WinDLL('ntdll', use_last_error=True)
        native.NtQueryInformationProcess.argtypes = [W.HANDLE, W.ULONG, C.c_void_p, W.ULONG, C.c_void_p]
        native.NtQueryInformationProcess.restype = C.c_long
        basic = Basic()
        parent_status = native.NtQueryInformationProcess(handle, 0, C.byref(basic), C.sizeof(basic), None)
        if parent_status != 0:
            raise NativeError('PROCESS_PARENT_UNVERIFIED', api='NtQueryInformationProcess', ntstatus=int(parent_status))
        after = k.WaitForSingleObject(handle, 0)
        created = int(times[0].dwHighDateTime) << 32 | int(times[0].dwLowDateTime)
        # GetProcessTimes documents lpExitTime as undefined while alive. The
        # wait result owns liveness; read an exit timestamp only after signaled.
        exited = (int(times[1].dwHighDateTime) << 32 | int(times[1].dwLowDateTime)) if after == 0 else 0
        if (int(basic.pid) != self.pid or before != after or created <= 0
                or (after == 0 and exited < created)):
            raise NativeError('PROCESS_IDENTITY_CHANGED')
        identity = (self.pid, int(basic.parent), created)
        previous = self._verified_alive_identity
        if previous is not None and (identity != previous[:3]
                or (observed_path is not None and observed_path != previous[3])):
            raise NativeError('PROCESS_IDENTITY_CHANGED')
        path_source = 'queried'
        if observed_path is None:
            # Windows can return ERROR_GEN_FAILURE for an exited process image
            # even while its original handle and lifetime remain queryable.
            # Only this same witness's fully verified ALIVE image may survive
            # that exit. No PID cache/configured path or live-child fallback.
            if after != 0 or previous is None:
                raise NativeError('PROCESS_PATH_UNVERIFIED', api='QueryFullProcessImageNameW', winerror=path_error)
            observed_path = previous[3]
            path_source = 'retained-verified-same-handle'
        if after == 0x102:
            self._verified_alive_identity = (*identity, observed_path)
        result = {'pid': self.pid, 'path': observed_path, 'created_filetime': created,
                'exit_filetime': exited, 'alive': after == 0x102, 'parent_pid': int(basic.parent),
                'path_source': path_source}
        if path_source != 'queried':
            result['path_query_winerror'] = path_error
        return result

    def close(self):
        if self._handle is not None:
            close = self._kernel.CloseHandle
            close.argtypes = [W.HANDLE]; close.restype = W.BOOL
            close(self._handle)
            self._handle = None
            self._verified_alive_identity = None

    def __enter__(self): return self
    def __exit__(self, *_): self.close()


class WindowsPlatform:
    supported = os.name == 'nt'

    def _require(self):
        if not self.supported:
            raise NativeError('WINDOWS_REQUIRED')

    @staticmethod
    def exists(path):
        return Path(path).is_file()

    def read_lines(self, path):
        """DMM may still own/write the log. Share read/write/delete like C#."""
        self._require()
        import msvcrt
        k = C.WinDLL('kernel32', use_last_error=True)
        f = k.CreateFileW
        f.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE]
        f.restype = W.HANDLE
        handle = f(str(path), 0x80000000, 7, None, 3, 0x80, None)
        if handle == C.c_void_p(-1).value:
            raise NativeError('LOG_UNREADABLE')
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except Exception:
            close = k.CloseHandle; close.argtypes = [W.HANDLE]; close.restype = W.BOOL
            close(handle)
            raise NativeError('LOG_UNREADABLE') from None
        # fd now owns handle. Avoid unlimited allocation for a damaged line.
        with os.fdopen(fd, 'rb') as stream:
            reader = io.TextIOWrapper(stream, encoding='utf-8-sig', errors='replace')
            while True:
                line = reader.readline(1024 * 1024 + 1)
                if not line:
                    break
                if len(line) > 1024 * 1024:
                    while line and not line.endswith('\n'):
                        line = reader.readline(1024 * 1024 + 1)
                    continue
                yield line.rstrip('\r\n')

    @contextmanager
    def action_lock(self):
        """Share the original launcher's user-session mutex, never open a second owner."""
        self._require()
        k = C.WinDLL('kernel32', use_last_error=True)
        k.CreateMutexW.argtypes = [C.c_void_p, W.BOOL, W.LPCWSTR]; k.CreateMutexW.restype = W.HANDLE
        k.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]; k.WaitForSingleObject.restype = W.DWORD
        k.ReleaseMutex.argtypes = [W.HANDLE]; k.ReleaseMutex.restype = W.BOOL
        k.CloseHandle.argtypes = [W.HANDLE]; k.CloseHandle.restype = W.BOOL
        handle = k.CreateMutexW(None, False, 'Local\\GakumasSmartLauncher.Gui')
        if not handle:
            raise NativeError('LAUNCHER_BUSY')
        owned = False
        try:
            owned = k.WaitForSingleObject(handle, 0) in (0, 0x80)
            if not owned:
                raise NativeError('LAUNCHER_BUSY')
            yield
        finally:
            if owned:
                k.ReleaseMutex(handle)
            k.CloseHandle(handle)

    def _crypt(self, data: bytes, entropy: bytes, decrypt: bool) -> bytes:
        self._require()
        class Blob(C.Structure):
            _fields_ = [('cbData', W.DWORD), ('pbData', C.POINTER(C.c_ubyte))]
        def blob(value):
            buf = (C.c_ubyte * max(1, len(value)))()
            if value:
                C.memmove(buf, value, len(value))
            return Blob(len(value), buf), buf
        original, ob = blob(data); extra, eb = blob(entropy); output = Blob()
        crypt = C.WinDLL('crypt32', use_last_error=True)
        kernel = C.WinDLL('kernel32', use_last_error=True)
        kernel.LocalFree.argtypes = [C.c_void_p]; kernel.LocalFree.restype = C.c_void_p
        fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
        fn.argtypes = [C.POINTER(Blob), C.c_void_p, C.POINTER(Blob), C.c_void_p,
                       C.c_void_p, W.DWORD, C.POINTER(Blob)]
        fn.restype = W.BOOL
        try:
            # No CRYPTPROTECT_LOCAL_MACHINE: preserve CurrentUser semantics.
            if not fn(C.byref(original), None, C.byref(extra), None, None, 1, C.byref(output)):
                raise NativeError('CACHE_UNREADABLE' if decrypt else 'CACHE_SAVE_FAILED')
            return C.string_at(output.pbData, output.cbData)
        finally:
            C.memset(ob, 0, len(ob)); C.memset(eb, 0, len(eb))
            if output.pbData:
                C.memset(output.pbData, 0, output.cbData)
                kernel.LocalFree(output.pbData)

    def protect(self, data: bytes, entropy: bytes) -> bytes:
        return self._crypt(data, entropy, False)

    def unprotect(self, data: bytes, entropy: bytes) -> bytes:
        return self._crypt(data, entropy, True)

    def processes(self, name: str) -> list[dict]:
        self._require()
        k = C.WinDLL('kernel32', use_last_error=True)
        class Entry(C.Structure):
            _fields_ = [('dwSize', W.DWORD), ('cntUsage', W.DWORD), ('pid', W.DWORD),
                        ('heap', C.c_size_t), ('module', W.DWORD), ('threads', W.DWORD),
                        ('parent', W.DWORD), ('priority', W.LONG), ('flags', W.DWORD),
                        ('name', W.WCHAR * 260)]
        k.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
        k.CreateToolhelp32Snapshot.restype = W.HANDLE
        k.Process32FirstW.argtypes = [W.HANDLE, C.POINTER(Entry)]; k.Process32FirstW.restype = W.BOOL
        k.Process32NextW.argtypes = [W.HANDLE, C.POINTER(Entry)]; k.Process32NextW.restype = W.BOOL
        k.CloseHandle.argtypes = [W.HANDLE]; k.CloseHandle.restype = W.BOOL
        handle = k.CreateToolhelp32Snapshot(2, 0)
        if handle == C.c_void_p(-1).value:
            raise NativeError('PROCESS_STATUS_UNKNOWN')
        result = []
        try:
            entry = Entry(); entry.dwSize = C.sizeof(entry)
            more = k.Process32FirstW(handle, C.byref(entry))
            if not more and C.get_last_error() != 18:
                raise NativeError('PROCESS_STATUS_UNKNOWN')
            while more:
                if entry.name.casefold() == name.casefold():
                    result.append({'pid': int(entry.pid), 'path': self.process_path(int(entry.pid))})
                more = k.Process32NextW(handle, C.byref(entry))
            if C.get_last_error() not in (0, 18):
                raise NativeError('PROCESS_STATUS_UNKNOWN')
            return result
        finally:
            k.CloseHandle(handle)

    def process_path(self, pid: int) -> str | None:
        self._require()
        k = C.WinDLL('kernel32', use_last_error=True)
        k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]; k.OpenProcess.restype = W.HANDLE
        k.CloseHandle.argtypes = [W.HANDLE]; k.CloseHandle.restype = W.BOOL
        k.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)]
        k.QueryFullProcessImageNameW.restype = W.BOOL
        h = k.OpenProcess(0x1000, False, pid)
        if not h:
            return None
        try:
            size = W.DWORD(32768); buf = C.create_unicode_buffer(size.value)
            return buf.value if k.QueryFullProcessImageNameW(h, 0, buf, C.byref(size)) else None
        finally:
            k.CloseHandle(h)

    def launch(self, exe: str, args: str, directory: str, elevate: bool) -> int | None:
        self._require()
        shell = C.WinDLL('shell32', use_last_error=True)
        shell.IsUserAnAdmin.argtypes = []; shell.IsUserAnAdmin.restype = W.BOOL
        request_elevation = elevate and not bool(shell.IsUserAnAdmin())
        return self._shell_execute(exe, args, directory, 'runas' if request_elevation else 'open')

    def launch_tracked(self, exe: str, args: str, directory: str, elevate: bool):
        """Retain ShellExecuteEx's original process handle for its caller to close."""
        self._require()
        shell = C.WinDLL('shell32', use_last_error=True)
        shell.IsUserAnAdmin.argtypes = []; shell.IsUserAnAdmin.restype = W.BOOL
        request_elevation = elevate and not bool(shell.IsUserAnAdmin())
        return self._shell_execute(exe, args, directory, 'runas' if request_elevation else 'open', tracked=True)

    def process_identity(self, pid: int) -> dict:
        """Inspect one PID through a read-only pinned handle; never infer absence."""
        self._require()
        if type(pid) is not int or not 0 < pid <= 0xffffffff:
            raise NativeError('PROCESS_IDENTITY_UNAVAILABLE')
        k = C.WinDLL('kernel32', use_last_error=True)
        k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]; k.OpenProcess.restype = W.HANDLE
        k.GetProcessId.argtypes = [W.HANDLE]; k.GetProcessId.restype = W.DWORD
        k.CloseHandle.argtypes = [W.HANDLE]; k.CloseHandle.restype = W.BOOL
        # Query information + limited query + synchronize, with no terminate,
        # write-memory, thread-creation or other process-control rights.
        handle = k.OpenProcess(0x400 | 0x1000 | 0x100000, False, pid)
        if not handle:
            raise NativeError('PROCESS_IDENTITY_UNAVAILABLE')
        try:
            if int(k.GetProcessId(handle)) != pid:
                raise NativeError('PROCESS_IDENTITY_CHANGED')
            return _OwnedProcessWitness(k, handle, pid).snapshot()
        finally:
            k.CloseHandle(handle)

    def repair(self):
        self._require()
        self._shell_execute('dmmgameplayer://play/GCL/gakumas/cl/win', None, None, 'open')

    def _shell_execute(self, file, args, directory, verb, *, tracked=False):
        # Only launch()/repair() call this. Browser cannot specify a verb or command.
        class Info(C.Structure):
            _fields_ = [('cbSize', W.DWORD), ('fMask', W.ULONG), ('hwnd', W.HWND),
                        ('lpVerb', W.LPCWSTR), ('lpFile', W.LPCWSTR), ('lpParameters', W.LPCWSTR),
                        ('lpDirectory', W.LPCWSTR), ('nShow', C.c_int), ('hInstApp', W.HINSTANCE),
                        ('lpIDList', C.c_void_p), ('lpClass', W.LPCWSTR), ('hkeyClass', W.HKEY),
                        ('dwHotKey', W.DWORD), ('hIcon', W.HANDLE), ('hProcess', W.HANDLE)]
        shell = C.WinDLL('shell32', use_last_error=True); k = C.WinDLL('kernel32', use_last_error=True)
        ole = C.WinDLL('ole32', use_last_error=True)
        ole.CoInitializeEx.argtypes = [C.c_void_p, W.DWORD]; ole.CoInitializeEx.restype = W.LONG
        ole.CoUninitialize.argtypes = []; ole.CoUninitialize.restype = None
        co = ole.CoInitializeEx(None, 2)  # paired even on S_FALSE; not on changed mode
        shell.ShellExecuteExW.argtypes = [C.POINTER(Info)]; shell.ShellExecuteExW.restype = W.BOOL
        k.GetProcessId.argtypes = [W.HANDLE]; k.GetProcessId.restype = W.DWORD
        k.CloseHandle.argtypes = [W.HANDLE]; k.CloseHandle.restype = W.BOOL
        info = Info(); info.cbSize = C.sizeof(info)
        info.fMask = 0x40 | 0x100 | 0x400  # process handle, wait for shell dispatch, no error UI
        info.lpVerb = verb; info.lpFile = file; info.lpParameters = args
        info.lpDirectory = directory; info.nShow = 1
        try:
            if not shell.ShellExecuteExW(C.byref(info)):
                raise NativeError('UAC_CANCELLED' if C.get_last_error() == 1223 else 'LAUNCH_FAILED')
            if tracked:
                pid = int(k.GetProcessId(info.hProcess)) if info.hProcess else 0
                witness = _OwnedProcessWitness(k, info.hProcess, pid)
                info.hProcess = None  # Transfer ownership; never reopen by PID.
                try:
                    # Establish actual identity before DirectLauncher's audit,
                    # cache/log reads and process-status projection can outlive
                    # a short bootstrap. Later checks still use this same handle.
                    witness.snapshot()
                except Exception:
                    witness.close()
                    raise  # Launch may already have happened; never retry it.
                return witness
            return int(k.GetProcessId(info.hProcess)) or None if info.hProcess else None
        finally:
            if info.hProcess:
                k.CloseHandle(info.hProcess)
            if co in (0, 1):
                ole.CoUninitialize()

    def close_dmm(self, force: bool = False):
        """Only after explicit sync confirmation AND newly committed credentials.

        Graceful WM_CLOSE first. Force termination is a separately consented option.
        Validate each image again on an open process handle before terminating it.
        """
        self._require()
        initial = self.processes('DMMGamePlayer.exe'); pids = {p['pid'] for p in initial}
        u = C.WinDLL('user32', use_last_error=True)
        CALLBACK = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        u.EnumWindows.argtypes = [CALLBACK, W.LPARAM]; u.EnumWindows.restype = W.BOOL
        u.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]; u.GetWindowThreadProcessId.restype = W.DWORD
        u.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]; u.PostMessageW.restype = W.BOOL
        @CALLBACK
        def close_window(hwnd, _):
            pid = W.DWORD(); u.GetWindowThreadProcessId(hwnd, C.byref(pid))
            if pid.value in pids:
                u.PostMessageW(hwnd, 0x10, 0, 0)  # WM_CLOSE
            return True
        u.EnumWindows(close_window, 0)
        if initial:
            time.sleep(.6)
        if force:
            import ntpath
            k = C.WinDLL('kernel32', use_last_error=True)
            k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]; k.OpenProcess.restype = W.HANDLE
            k.CloseHandle.argtypes = [W.HANDLE]; k.CloseHandle.restype = W.BOOL
            k.QueryFullProcessImageNameW.argtypes = [W.HANDLE,W.DWORD,W.LPWSTR,C.POINTER(W.DWORD)]
            k.QueryFullProcessImageNameW.restype = W.BOOL
            k.TerminateProcess.argtypes = [W.HANDLE, W.UINT]; k.TerminateProcess.restype = W.BOOL
            k.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]; k.WaitForSingleObject.restype = W.DWORD
            for process in self.processes('DMMGamePlayer.exe'):
                # No killing processes that appeared after the confirmed close began.
                old = next((p for p in initial if p['pid'] == process['pid']), None)
                if not old or not old['path']:
                    continue
                h = k.OpenProcess(0x1000 | 0x1 | 0x100000, False, process['pid'])
                if not h:
                    continue
                try:
                    size = W.DWORD(32768); buf = C.create_unicode_buffer(size.value)
                    if not k.QueryFullProcessImageNameW(h, 0, buf, C.byref(size)):
                        continue
                    if ntpath.normcase(buf.value) != ntpath.normcase(old['path']):
                        continue
                    if ntpath.basename(buf.value).casefold() != 'dmmgameplayer.exe':
                        continue
                    k.TerminateProcess(h, 0); k.WaitForSingleObject(h, 1000)
                finally:
                    k.CloseHandle(h)
        remaining = len(self.processes('DMMGamePlayer.exe'))
        return {'found': len(initial), 'closed': max(0, len(initial)-remaining), 'remaining': remaining}
