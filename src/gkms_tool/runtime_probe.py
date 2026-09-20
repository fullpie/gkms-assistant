from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
from ctypes import wintypes
from dataclasses import asdict, dataclass
from pathlib import Path


PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
LIST_MODULES_ALL = 0x03
TOKEN_QUERY = 0x0008
TOKEN_INTEGRITY_LEVEL = 25


@dataclass(frozen=True, slots=True)
class RuntimeProbeResult:
    pid: int
    limited_query_open: bool
    vm_read_open: bool
    executable_path: str | None
    current_integrity: str | None
    target_integrity: str | None
    module_names: tuple[str, ...]
    errors: tuple[str, ...]


def _find_pid(image_name: str) -> int:
    completed = subprocess.run(
        ["tasklist", "/fo", "csv", "/nh", "/fi", f"imagename eq {image_name}"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    for line in completed.stdout.splitlines():
        if not line or line.startswith("INFO:"):
            continue
        columns = [part.strip('"') for part in line.split('","')]
        if len(columns) >= 2 and columns[0].casefold() == image_name.casefold():
            return int(columns[1].replace(",", ""))
    raise ProcessLookupError(f"找不到執行中的 {image_name}")


def _open_process(pid: int, access: int) -> tuple[int | None, int]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(access, False, pid)
    if not handle:
        return None, ctypes.get_last_error()
    return int(handle), 0


def _close(handle: int | None) -> None:
    if handle:
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(wintypes.HANDLE(handle))


def _query_path(handle: int) -> tuple[str | None, int]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    size = wintypes.DWORD(32768)
    buffer = ctypes.create_unicode_buffer(size.value)
    if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
        return None, ctypes.get_last_error()
    return buffer.value, 0


def _query_integrity(process_handle: int) -> tuple[str | None, int]:
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        wintypes.HANDLE(process_handle), TOKEN_QUERY, ctypes.byref(token)
    ):
        return None, ctypes.get_last_error()
    try:
        needed = wintypes.DWORD()
        advapi32.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(
            token, TOKEN_INTEGRITY_LEVEL, buffer, needed, ctypes.byref(needed)
        ):
            return None, ctypes.get_last_error()
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        count_pointer = advapi32.GetSidSubAuthorityCount(ctypes.c_void_p(sid_pointer))
        count = count_pointer[0]
        rid_pointer = advapi32.GetSidSubAuthority(ctypes.c_void_p(sid_pointer), count - 1)
        rid = rid_pointer[0]
        if rid >= 0x5000:
            label = "protected"
        elif rid >= 0x4000:
            label = "system"
        elif rid >= 0x3000:
            label = "high"
        elif rid >= 0x2100:
            label = "medium-plus"
        elif rid >= 0x2000:
            label = "medium"
        elif rid >= 0x1000:
            label = "low"
        else:
            label = "untrusted"
        return f"{label} (0x{rid:X})", 0
    finally:
        kernel32.CloseHandle(token)


def _enum_modules(handle: int) -> tuple[tuple[str, ...], int]:
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.EnumProcessModulesEx.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HMODULE),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,
    ]
    psapi.GetModuleBaseNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.HMODULE,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    capacity = 2048
    modules = (wintypes.HMODULE * capacity)()
    needed = wintypes.DWORD()
    if not psapi.EnumProcessModulesEx(
        handle,
        modules,
        ctypes.sizeof(modules),
        ctypes.byref(needed),
        LIST_MODULES_ALL,
    ):
        return (), ctypes.get_last_error()
    count = min(capacity, needed.value // ctypes.sizeof(wintypes.HMODULE))
    names: list[str] = []
    for module in modules[:count]:
        buffer = ctypes.create_unicode_buffer(1024)
        if psapi.GetModuleBaseNameW(handle, module, buffer, len(buffer)):
            names.append(buffer.value)
    return tuple(names), 0


def probe_runtime(pid: int) -> RuntimeProbeResult:
    errors: list[str] = []
    limited_handle, limited_error = _open_process(pid, PROCESS_QUERY_LIMITED_INFORMATION)
    executable_path: str | None = None
    target_integrity: str | None = None
    if limited_handle:
        executable_path, path_error = _query_path(limited_handle)
        if path_error:
            errors.append(f"QueryFullProcessImageNameW={path_error}")
        target_integrity, integrity_error = _query_integrity(limited_handle)
        if integrity_error:
            errors.append(f"TargetIntegrity={integrity_error}")
    else:
        errors.append(f"OpenProcess(query_limited)={limited_error}")

    vm_handle, vm_error = _open_process(pid, PROCESS_QUERY_INFORMATION | PROCESS_VM_READ)
    modules: tuple[str, ...] = ()
    if vm_handle:
        modules, module_error = _enum_modules(vm_handle)
        if module_error:
            errors.append(f"EnumProcessModulesEx={module_error}")
    else:
        errors.append(f"OpenProcess(vm_read)={vm_error}")

    _close(limited_handle)
    _close(vm_handle)
    current_process = ctypes.WinDLL("kernel32", use_last_error=True).GetCurrentProcess()
    current_integrity, current_integrity_error = _query_integrity(current_process)
    if current_integrity_error:
        errors.append(f"CurrentIntegrity={current_integrity_error}")
    return RuntimeProbeResult(
        pid=pid,
        limited_query_open=limited_handle is not None,
        vm_read_open=vm_handle is not None,
        executable_path=executable_path,
        current_integrity=current_integrity,
        target_integrity=target_integrity,
        module_names=modules,
        errors=tuple(errors),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="唯讀檢查遊戲程序是否允許查詢與 VM_READ。")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--image-name", default="gakumas.exe")
    parser.add_argument("--output", type=Path, help="將 JSON 原子寫入指定檔案。")
    args = parser.parse_args()
    pid = args.pid or _find_pid(args.image_name)
    result = probe_runtime(pid)
    payload = json.dumps(asdict(result), ensure_ascii=False, indent=2)
    if args.output is not None:
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(output)
    print(payload)


if __name__ == "__main__":
    main()
