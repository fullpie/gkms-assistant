"""Windows x64 loader for the read-only gakumas telemetry observer DLL."""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import secrets
import struct
import time
from typing import Final

import pefile


PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_OBSERVER_DLL: Final = (
    PROJECT_ROOT / "var" / "native" / "gkms_telemetry_observer.dll"
)
DEFAULT_TELEMETRY_DIR: Final = PROJECT_ROOT / "var" / "telemetry"
DEFAULT_RUNTIME_OUTER_OBSERVER_DLL: Final = (
    PROJECT_ROOT
    / "var"
    / "native"
    / "runtime_outer_observer"
    / "gkms_runtime_outer_observer.dll"
)
# v2 adds synchronous, fully copied ExamSequence hand_before/hand_after
# snapshots to ExecuteCardCommandImpl data.  The managed-thread lifecycle and
# the outer gkms.telemetry.v1 journal schema remain unchanged.
SUPPORTED_OBSERVER_PROTOCOL: Final = 2
SUPPORTED_OBSERVER_SHA256: Final = (
    "f09d33a8ef91c76928bb74743dec99df20e40edee8981e683bd2b54147bb0270"
)

# This observer intentionally targets one audited PC 3.3.0 build.  A game
# update changes managed ABI/layout and must be audited before hooks are armed.
SUPPORTED_TARGET_SHA256: Final = {
    "gakumas.exe": "81899faa1be2f78ec35a9aa2698ec1aefd6f09f6905349bc2e68fd875c195639",
    "GameAssembly.dll": "7d948e06cf15633a9650c3933a1bc29977b4eef4314af09ad974ae500f1cd7ef",
    "global-metadata.dat": "9a6bf153c0c42a2768e619cc7d96fa79fcc1d341e9cf9bcbdc8d6bca48812668",
}
REQUIRED_HOOK_TOKENS: Final = {
    "ExamSequence.ConsumeCardCost": 0x06004E54,
    "ExamSequence.ExecuteCardCommandImpl": 0x06004E4E,
    "CardCreateIdEffectExecutor.ExecuteEffect": 0x060045B3,
    "CardCreateSearchEffectExecutor.ExecuteEffect": 0x060045B8,
    "ExamParameterModel.set_Phase": 0x06004C56,
    "ExamParameterModel.AddUserPlayLogDetailLine": 0x06004D35,
    "ExamCommandStack.RemoveCurrentCommand": 0x060015D3,
}

# Offline-only action-state candidate for the currently installed PC pair.
# These values are separate from the canonical injector pins above; the
# candidate is never accepted by the loader merely because it was compiled.
PC_CURRENT_CANDIDATE_GAMEASSEMBLY_SHA256: Final = (
    "7D948E06CF15633A9650C3933A1BC29977B4EEF4314AF09AD974AE500F1CD7EF"
)
PC_CURRENT_CANDIDATE_METADATA_SHA256: Final = (
    "9A6BF153C0C42A2768E619CC7D96FA79FCC1D341E9CF9BCBDC8D6BCA48812668"
)
PC_CURRENT_CANDIDATE_OBSERVER_PROTOCOL: Final = 3
PC_CURRENT_CANDIDATE_OBSERVER_PATH: Final = (
    PROJECT_ROOT / "var" / "native" / "observer_candidate" /
    "gkms_telemetry_observer_pc_candidate.dll"
)
PC_CURRENT_CANDIDATE_OBSERVER_SHA256: Final = (
    "24D21FDD98B359A2ED4C9E1858D08CC50C1FD818C5AB0E6DA89F5A93401619A0"
)
PC_CURRENT_CANDIDATE_HOOK_TOKENS: Final = {
    "ExamSequence.ExecuteCardCommandImpl": 0x06004E4E,
    "ExamCommandStack.RemoveCurrentCommand": 0x060015D3,
    "ExamParameterModel.RemoveDrink": 0x06004D2B,
    "ExamParameterModel.set_Phase": 0x06004C56,
    "ExamSequence.ExecuteCommandList": 0x06004E51,
    "ExamPlayCommand.CreateUseDrinkCommand": 0x06004DD0,
    "ExamPlayCommand.CreateTurnEndCommand": 0x06004DD9,
}
PC_CURRENT_CANDIDATE_HOOK_PARAMETER_COUNTS: Final = {
    "ExamSequence.ExecuteCardCommandImpl": 4,
    "ExamCommandStack.RemoveCurrentCommand": 0,
    "ExamParameterModel.RemoveDrink": 1,
    "ExamParameterModel.set_Phase": 1,
    "ExamSequence.ExecuteCommandList": 0,
    "ExamPlayCommand.CreateUseDrinkCommand": 3,
    "ExamPlayCommand.CreateTurnEndCommand": 1,
}

# The outer observer is loaded only by the existing Localify managed-thread
# bootstrap.  This module never loads it remotely; the fixed hash merely binds
# a control call to the exact already-loaded image.  Update this one pin after
# rebuilding the native FlushBarrier export and before deployment.
SUPPORTED_RUNTIME_OUTER_OBSERVER_SHA256: Final = (
    "92bd7647300560b54ecc937f21ca62e5bc47e99a8a0b260da5c575a878b14a0c"
)
SUPPORTED_RUNTIME_OUTER_OBSERVER_PROTOCOL: Final = 1
RUNTIME_OUTER_OBSERVER_MODULE_NAME: Final = "gkms_runtime_outer_observer.dll"
RUNTIME_OUTER_OBSERVER_PROTOCOL_EXPORT: Final = (
    b"GKMSRuntimeOuterObserverProtocolVersion"
)
RUNTIME_OUTER_OBSERVER_START_EXPORT: Final = (
    b"GKMSRuntimeOuterObserverStartOnManagedThread"
)
RUNTIME_OUTER_OBSERVER_FLUSH_EXPORT: Final = (
    b"GKMSRuntimeOuterObserverFlushBarrier"
)
RUNTIME_OUTER_OBSERVER_BARRIER_ABI_VERSION: Final = 2
RUNTIME_OUTER_OBSERVER_BARRIER_MAX_TIMEOUT_MS: Final = 30_000
RUNTIME_OUTER_OBSERVER_PROTOCOL_TIMEOUT_MS: Final = 2_000
RUNTIME_OUTER_OBSERVER_REMOTE_WAIT_MARGIN_MS: Final = 2_000
RUNTIME_OUTER_OBSERVER_BARRIER_OBJECT_PREFIX: Final = (
    r"Local\gkms.runtime_outer_observer"
)

# Fixed native ABI, little-endian Windows x64.  The final seven Q fields are
# generation, target, flushed, physical watermark, two drop counters and the
# request nonce.  Keep the explicit size assertion beside the format so a
# field-count edit cannot silently change the remote allocation contract.
_RUNTIME_OUTER_OBSERVER_BARRIER_STRUCT: Final = struct.Struct(
    "<IIIIQQIIQQQQQQQ"
)
RUNTIME_OUTER_OBSERVER_BARRIER_REQUEST_SIZE: Final = 96
assert (
    _RUNTIME_OUTER_OBSERVER_BARRIER_STRUCT.size
    == RUNTIME_OUTER_OBSERVER_BARRIER_REQUEST_SIZE
)
_RUNTIME_OUTER_OBSERVER_BARRIER_STATUS_SENTINEL: Final = 0xFFFFFFFF
RUNTIME_OUTER_OBSERVER_BARRIER_STATUS_NAMES: Final = {
    0: "ok",
    1: "invalid-pointer-or-seh",
    2: "invalid-abi",
    3: "writer-unavailable-or-stopping",
    4: "timeout",
    5: "queue-drop-before-target",
    6: "writer-failure",
    7: "generation-mismatch",
    8: "internal-error",
}
RUNTIME_OUTER_OBSERVER_WRITER_ERROR_NAMES: Final = {
    0: "none",
    1: "serialize-exception",
    2: "write-or-flush-failed",
    3: "sequence-invariant-failed",
}


PROCESS_CREATE_THREAD = 0x0002
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF
INFINITE = 0xFFFFFFFF
FILE_MAP_WRITE = 0x0002
FILE_MAP_READ = 0x0004
EVENT_MODIFY_STATE = 0x0002
SYNCHRONIZE = 0x00100000
WH_GETMESSAGE = 3
WM_NULL = 0x0000


class RemoteThreadStateUnknown(TimeoutError):
    """The remote thread did not reach a confirmed terminal state."""


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_ubyte)),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", wintypes.WCHAR * 256),
        ("szExePath", wintypes.WCHAR * 260),
    ]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Module32FirstW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(MODULEENTRY32W),
    ]
    kernel32.Module32FirstW.restype = wintypes.BOOL
    kernel32.Module32NextW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(MODULEENTRY32W),
    ]
    kernel32.Module32NextW.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.VirtualAllocEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_size_t,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel32.VirtualAllocEx.restype = ctypes.c_void_p
    kernel32.VirtualFreeEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_size_t,
        wintypes.DWORD,
    ]
    kernel32.VirtualFreeEx.restype = wintypes.BOOL
    kernel32.WriteProcessMemory.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.WriteProcessMemory.restype = wintypes.BOOL
    kernel32.ReadProcessMemory.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.ReadProcessMemory.restype = wintypes.BOOL
    kernel32.CreateRemoteThread.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.CreateRemoteThread.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeThread.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeThread.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetProcAddress.argtypes = [wintypes.HMODULE, ctypes.c_char_p]
    kernel32.GetProcAddress.restype = ctypes.c_void_p
    kernel32.OpenFileMappingW.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.OpenFileMappingW.restype = wintypes.HANDLE
    kernel32.MapViewOfFile.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_size_t,
    ]
    kernel32.MapViewOfFile.restype = ctypes.c_void_p
    kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
    kernel32.UnmapViewOfFile.restype = wintypes.BOOL
    kernel32.OpenEventW.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    kernel32.SetEvent.restype = wintypes.BOOL
    return kernel32


def _user32():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.SetWindowsHookExW.argtypes = [
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.HMODULE,
        wintypes.DWORD,
    ]
    user32.SetWindowsHookExW.restype = wintypes.HANDLE
    user32.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
    user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.PostMessageW.restype = wintypes.BOOL
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
    return user32


def enumerate_modules(process_id: int) -> tuple[dict[str, object], ...]:
    if process_id <= 0:
        raise ValueError("process_id must be positive")
    kernel32 = _kernel32()
    snapshot = kernel32.CreateToolhelp32Snapshot(
        TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32,
        process_id,
    )
    if snapshot == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    result: list[dict[str, object]] = []
    try:
        entry = MODULEENTRY32W()
        entry.dwSize = ctypes.sizeof(MODULEENTRY32W)
        ok = kernel32.Module32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            result.append(
                {
                    "name": str(entry.szModule),
                    "path": str(entry.szExePath),
                    "base": ctypes.cast(
                        entry.modBaseAddr,
                        ctypes.c_void_p,
                    ).value,
                    "size": int(entry.modBaseSize),
                }
            )
            ok = kernel32.Module32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return tuple(result)


def _remote_kernel_export(
    process_id: int,
    modules: tuple[dict[str, object], ...],
    export_name: bytes,
) -> int:
    kernel32 = _kernel32()
    local_base = int(kernel32.GetModuleHandleW("kernel32.dll"))
    local_export = int(kernel32.GetProcAddress(local_base, export_name))
    remote = next(
        (
            module
            for module in modules
            if str(module["name"]).casefold() == "kernel32.dll"
        ),
        None,
    )
    if remote is None:
        raise RuntimeError(f"kernel32.dll is absent from process {process_id}")
    return int(remote["base"]) + (local_export - local_base)


def _pe_export_rva(path: Path, export_name: bytes) -> int:
    image = pefile.PE(str(path), fast_load=True)
    try:
        image.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXPORT"]]
        )
        directory = getattr(image, "DIRECTORY_ENTRY_EXPORT", None)
        if directory is None:
            raise RuntimeError(f"DLL has no export directory: {path}")
        for symbol in directory.symbols:
            if symbol.name == export_name:
                return int(symbol.address)
    finally:
        image.close()
    raise RuntimeError(f"DLL export is unavailable: {export_name.decode('ascii')}")


def _runtime_outer_observer_binary_contract(path: Path) -> dict[str, object]:
    """Pin the exact already-loaded outer observer and its remote-call ABI."""

    resolved = path.resolve()
    expected_path = DEFAULT_RUNTIME_OUTER_OBSERVER_DLL.resolve()
    if resolved != expected_path:
        raise RuntimeError(
            f"only the fixed runtime outer observer may be called: {resolved}"
        )
    digest = _sha256(resolved)
    if digest.casefold() != SUPPORTED_RUNTIME_OUTER_OBSERVER_SHA256.casefold():
        raise RuntimeError(
            "runtime outer observer build differs from the audited binary: "
            f"expected {SUPPORTED_RUNTIME_OUTER_OBSERVER_SHA256}, got {digest}"
        )

    image = pefile.PE(str(resolved), fast_load=True)
    try:
        if image.FILE_HEADER.Machine != pefile.MACHINE_TYPE["IMAGE_FILE_MACHINE_AMD64"]:
            raise RuntimeError("runtime outer observer is not an AMD64 image")
        if image.OPTIONAL_HEADER.Magic != pefile.OPTIONAL_HEADER_MAGIC_PE_PLUS:
            raise RuntimeError("runtime outer observer is not a PE32+ image")
        image.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXPORT"]]
        )
        directory = getattr(image, "DIRECTORY_ENTRY_EXPORT", None)
        if directory is None:
            raise RuntimeError("runtime outer observer has no export directory")
        symbols = {
            symbol.name: symbol
            for symbol in directory.symbols
            if symbol.name is not None
        }
        expected_exports = {
            RUNTIME_OUTER_OBSERVER_PROTOCOL_EXPORT,
            RUNTIME_OUTER_OBSERVER_START_EXPORT,
            RUNTIME_OUTER_OBSERVER_FLUSH_EXPORT,
        }
        if set(symbols) != expected_exports:
            rendered = sorted(
                name.decode("ascii", errors="replace") for name in symbols
            )
            raise RuntimeError(
                f"runtime outer observer exports differ: {rendered}"
            )
        image_size = int(image.OPTIONAL_HEADER.SizeOfImage)
        if image_size <= 0:
            raise RuntimeError("runtime outer observer image size is invalid")
        exports: dict[bytes, int] = {}
        for name in expected_exports:
            symbol = symbols[name]
            if getattr(symbol, "forwarder", None) is not None:
                raise RuntimeError(
                    "runtime outer observer export must not be forwarded: "
                    + name.decode("ascii")
                )
            rva = int(symbol.address)
            if rva <= 0 or rva >= image_size:
                raise RuntimeError(
                    "runtime outer observer export RVA is outside the image: "
                    + name.decode("ascii")
                )
            exports[name] = rva
        return {
            "path": str(resolved),
            "sha256": digest.casefold(),
            "image_size": image_size,
            "exports": exports,
        }
    finally:
        image.close()


def _loaded_runtime_outer_observer_module(
    process_id: int,
    modules: tuple[dict[str, object], ...],
) -> dict[str, object]:
    named = tuple(
        module
        for module in modules
        if str(module.get("name", "")).casefold()
        == RUNTIME_OUTER_OBSERVER_MODULE_NAME.casefold()
    )
    if len(named) != 1:
        raise RuntimeError(
            "runtime outer observer must be loaded exactly once in process "
            f"{process_id}; found {len(named)}"
        )
    module = dict(named[0])
    raw_path = module.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise RuntimeError("runtime outer observer module path is absent")
    try:
        module_path = Path(raw_path).resolve()
    except (OSError, RuntimeError) as error:
        raise RuntimeError("runtime outer observer module path is invalid") from error
    if module_path != DEFAULT_RUNTIME_OUTER_OBSERVER_DLL.resolve():
        raise RuntimeError(
            f"runtime outer observer was loaded from an unexpected path: {module_path}"
        )
    base = module.get("base")
    size = module.get("size")
    if (
        isinstance(base, bool)
        or not isinstance(base, int)
        or base <= 0
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size <= 0
    ):
        raise RuntimeError("runtime outer observer module base/size is invalid")
    module["path"] = str(module_path)
    return module


def _runtime_outer_observer_uint(value: object, name: str, *, bits: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    maximum = (1 << bits) - 1
    if value < 0 or value > maximum:
        raise ValueError(f"{name} must be between 0 and {maximum}")
    return value


def pack_runtime_outer_observer_barrier_request(
    *,
    timeout_ms: int,
    expected_generation: int,
    minimum_next: int,
    request_nonce: int,
) -> bytes:
    timeout = _runtime_outer_observer_uint(timeout_ms, "timeout_ms", bits=32)
    if timeout < 1 or timeout > RUNTIME_OUTER_OBSERVER_BARRIER_MAX_TIMEOUT_MS:
        raise ValueError(
            "timeout_ms must be between 1 and "
            f"{RUNTIME_OUTER_OBSERVER_BARRIER_MAX_TIMEOUT_MS}"
        )
    generation = _runtime_outer_observer_uint(
        expected_generation, "expected_generation", bits=64
    )
    minimum = _runtime_outer_observer_uint(minimum_next, "minimum_next", bits=64)
    nonce = _runtime_outer_observer_uint(request_nonce, "request_nonce", bits=64)
    if nonce == 0:
        raise ValueError("request_nonce must be non-zero")
    return _RUNTIME_OUTER_OBSERVER_BARRIER_STRUCT.pack(
        RUNTIME_OUTER_OBSERVER_BARRIER_REQUEST_SIZE,
        RUNTIME_OUTER_OBSERVER_BARRIER_ABI_VERSION,
        timeout,
        0,
        generation,
        minimum,
        _RUNTIME_OUTER_OBSERVER_BARRIER_STATUS_SENTINEL,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        nonce,
    )


def unpack_runtime_outer_observer_barrier_request(data: bytes) -> dict[str, int]:
    if not isinstance(data, bytes):
        raise TypeError("runtime outer observer barrier request must be bytes")
    if len(data) != RUNTIME_OUTER_OBSERVER_BARRIER_REQUEST_SIZE:
        raise ValueError(
            "runtime outer observer barrier request must contain exactly "
            f"{RUNTIME_OUTER_OBSERVER_BARRIER_REQUEST_SIZE} bytes"
        )
    values = _RUNTIME_OUTER_OBSERVER_BARRIER_STRUCT.unpack(data)
    names = (
        "size",
        "version",
        "timeout_ms",
        "flags",
        "expected_writer_generation",
        "minimum_next_enqueue_sequence",
        "status",
        "writer_error",
        "writer_generation",
        "target_next_enqueue_sequence",
        "flushed_next_enqueue_sequence",
        "watermark_next_physical_order",
        "dropped_rows_at_target",
        "dropped_rows_now",
        "request_nonce",
    )
    return dict(zip(names, values, strict=True))


def _observer_binary_contract(path: Path) -> tuple[str, int]:
    if path != DEFAULT_OBSERVER_DLL.resolve():
        raise RuntimeError(f"only the fixed telemetry observer may be loaded: {path}")
    digest = _sha256(path)
    if digest != SUPPORTED_OBSERVER_SHA256:
        raise RuntimeError(
            "telemetry observer build differs from the audited binary: "
            f"expected {SUPPORTED_OBSERVER_SHA256}, got {digest}"
        )
    image = pefile.PE(str(path), fast_load=True)
    try:
        if image.FILE_HEADER.Machine != pefile.MACHINE_TYPE["IMAGE_FILE_MACHINE_AMD64"]:
            raise RuntimeError("telemetry observer is not an AMD64 image")
        if image.OPTIONAL_HEADER.Magic != pefile.OPTIONAL_HEADER_MAGIC_PE_PLUS:
            raise RuntimeError("telemetry observer is not a PE32+ image")
        image.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXPORT"]]
        )
        directory = getattr(image, "DIRECTORY_ENTRY_EXPORT", None)
        names = set() if directory is None else {
            symbol.name for symbol in directory.symbols if symbol.name is not None
        }
        expected = {
            b"GKMSObserverProtocolVersion",
            b"GKMSObserverStartOnManagedThread",
        }
        if names != expected:
            raise RuntimeError(f"telemetry observer exports differ: {sorted(names)}")
        start = next(
            symbol for symbol in directory.symbols
            if symbol.name == b"GKMSObserverStartOnManagedThread"
        )
        return digest, int(start.address)
    finally:
        image.close()


def _run_remote_thread(
    kernel32,
    process: int,
    start_address: int,
    parameter: int,
    *,
    timeout_ms: int,
    operation: str,
) -> int:
    thread_id = wintypes.DWORD()
    thread = kernel32.CreateRemoteThread(
        process,
        None,
        0,
        start_address,
        parameter,
        0,
        ctypes.byref(thread_id),
    )
    if not thread:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        wait_result = kernel32.WaitForSingleObject(thread, timeout_ms)
        if wait_result != WAIT_OBJECT_0:
            raise RemoteThreadStateUnknown(
                f"remote {operation} state is unknown after wait result "
                f"0x{wait_result:08x}"
            )
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeThread(thread, ctypes.byref(exit_code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(exit_code.value)
    finally:
        kernel32.CloseHandle(thread)


def _runtime_outer_observer_result_base(
    *,
    process_id: int,
    binary: dict[str, object],
) -> dict[str, object]:
    return {
        "schema": "gkms.runtime-outer-observer.flush-barrier-result.v1",
        "target_pid": process_id,
        "expected_binary_path": binary["path"],
        "expected_binary_sha256": binary["sha256"],
        "expected_binary_image_size": int(binary["image_size"]),
        "loaded_module_verified": False,
        "observer_protocol": SUPPORTED_RUNTIME_OUTER_OBSERVER_PROTOCOL,
        "barrier_abi_version": RUNTIME_OUTER_OBSERVER_BARRIER_ABI_VERSION,
        "transport": "pid-scoped-named-ipc",
        # A barrier is never authority to replay the Maa action that preceded
        # it.  Even success means only that one native prefix is durable.
        "original_action_retry_allowed": False,
    }


def runtime_outer_observer_barrier_object_names(
    process_id: int,
) -> dict[str, str]:
    if isinstance(process_id, bool) or not isinstance(process_id, int):
        raise TypeError("process_id must be an integer")
    if process_id <= 0:
        raise ValueError("process_id must be positive")
    base = (
        f"{RUNTIME_OUTER_OBSERVER_BARRIER_OBJECT_PREFIX}."
        f"{process_id}.barrier"
    )
    return {
        "base": base,
        "mapping": f"{base}.mapping",
        "request": f"{base}.request",
        "response": f"{base}.response",
    }


def _runtime_outer_observer_known_result(
    *,
    base: dict[str, object],
    exit_code: int,
    submitted: dict[str, int],
    request: dict[str, int],
) -> dict[str, object]:
    errors: list[str] = []
    status = request["status"]
    if exit_code not in RUNTIME_OUTER_OBSERVER_BARRIER_STATUS_NAMES:
        errors.append(f"unknown-exit-code:{exit_code}")
    if status != exit_code:
        errors.append(f"status-exit-mismatch:{status}!={exit_code}")
    if request["size"] != RUNTIME_OUTER_OBSERVER_BARRIER_REQUEST_SIZE:
        errors.append("size-changed")
    if request["version"] != RUNTIME_OUTER_OBSERVER_BARRIER_ABI_VERSION:
        errors.append("version-changed")
    if request["flags"] != 0:
        errors.append("flags-changed")
    if request["request_nonce"] <= 0:
        errors.append("request-nonce-not-positive")
    for name in (
        "size",
        "version",
        "timeout_ms",
        "flags",
        "expected_writer_generation",
        "minimum_next_enqueue_sequence",
        "request_nonce",
    ):
        if request[name] != submitted[name]:
            errors.append(f"input-changed:{name}")
    if request["writer_error"] not in RUNTIME_OUTER_OBSERVER_WRITER_ERROR_NAMES:
        errors.append(f"unknown-writer-error:{request['writer_error']}")

    if exit_code == 0:
        if request["writer_generation"] <= 0:
            errors.append("generation-not-positive")
        expected = request["expected_writer_generation"]
        if expected and request["writer_generation"] != expected:
            errors.append("generation-differs-from-request")
        if (
            request["target_next_enqueue_sequence"]
            < request["minimum_next_enqueue_sequence"]
        ):
            errors.append("target-before-minimum")
        if (
            request["flushed_next_enqueue_sequence"]
            < request["target_next_enqueue_sequence"]
        ):
            errors.append("flushed-before-target")
        if (
            request["watermark_next_physical_order"]
            != request["target_next_enqueue_sequence"]
        ):
            errors.append("physical-watermark-differs-from-target")
        if request["dropped_rows_at_target"] != 0:
            errors.append("observer-row-drop-before-target")
        if request["writer_error"] != 0:
            errors.append("writer-error-on-success")

    native_status = RUNTIME_OUTER_OBSERVER_BARRIER_STATUS_NAMES.get(
        exit_code, "unknown-native-status"
    )
    timeout_phase = None
    if exit_code == 4:
        timeout_phase = (
            "waiting-for-minimum-enqueue"
            if request["target_next_enqueue_sequence"]
            < request["minimum_next_enqueue_sequence"]
            else "waiting-for-flush"
        )
    settled = exit_code == 0 and not errors
    return {
        **base,
        "status": native_status if not errors else "contract-violation",
        "native_status": native_status,
        "native_status_code": exit_code,
        "writer_error": RUNTIME_OUTER_OBSERVER_WRITER_ERROR_NAMES.get(
            request["writer_error"], "unknown-writer-error"
        ),
        "writer_error_code": request["writer_error"],
        "timeout_phase": timeout_phase,
        "barrier_invoked": True,
        "transport_terminal": True,
        "request_mapping_released": True,
        # Compatibility only.  Named IPC has no remote thread/allocation;
        # canonical consumers must use the transport-neutral fields above.
        "remote_thread_terminal": True,
        "remote_buffer_released": True,
        "legacy_remote_thread_fields_deprecated": True,
        "barrier_settled": settled,
        "exact_prefix_available": settled,
        "observer_stream_healthy_now": request["dropped_rows_now"] == 0,
        "contract_errors": errors,
        "request": request,
    }


def flush_runtime_outer_observer_once(
    process_id: int,
    *,
    timeout_ms: int,
    expected_generation: int,
    minimum_next: int,
) -> dict[str, object]:
    """Flush one prefix through the observer's PID-scoped named IPC.

    The observer owns one fixed 96-byte mapping and two auto-reset events.  The
    client sends exactly one request and performs one bounded response wait;
    timeout/unknown state is never retried by this function.
    """

    if isinstance(process_id, bool) or not isinstance(process_id, int):
        raise TypeError("process_id must be an integer")
    if process_id <= 0:
        raise ValueError("process_id must be positive")
    request_nonce = secrets.randbits(64)
    while request_nonce == 0:
        request_nonce = secrets.randbits(64)
    request_bytes = pack_runtime_outer_observer_barrier_request(
        timeout_ms=timeout_ms,
        expected_generation=expected_generation,
        minimum_next=minimum_next,
        request_nonce=request_nonce,
    )
    submitted_request = unpack_runtime_outer_observer_barrier_request(request_bytes)
    binary = _runtime_outer_observer_binary_contract(
        DEFAULT_RUNTIME_OUTER_OBSERVER_DLL
    )
    result_base = _runtime_outer_observer_result_base(
        process_id=process_id,
        binary=binary,
    )
    kernel32 = _kernel32()
    names = runtime_outer_observer_barrier_object_names(process_id)
    mapping = kernel32.OpenFileMappingW(
        FILE_MAP_READ | FILE_MAP_WRITE,
        False,
        names["mapping"],
    )
    if not mapping:
        raise ctypes.WinError(ctypes.get_last_error())
    request_event = None
    response_event = None
    view = None
    try:
        request_event = kernel32.OpenEventW(
            EVENT_MODIFY_STATE,
            False,
            names["request"],
        )
        if not request_event:
            raise ctypes.WinError(ctypes.get_last_error())
        response_event = kernel32.OpenEventW(
            SYNCHRONIZE,
            False,
            names["response"],
        )
        if not response_event:
            raise ctypes.WinError(ctypes.get_last_error())
        view = kernel32.MapViewOfFile(
            mapping,
            FILE_MAP_READ | FILE_MAP_WRITE,
            0,
            0,
            RUNTIME_OUTER_OBSERVER_BARRIER_REQUEST_SIZE,
        )
        if not view:
            raise ctypes.WinError(ctypes.get_last_error())

        stale = kernel32.WaitForSingleObject(response_event, 0)
        if stale == WAIT_OBJECT_0:
            # Auto-reset consumed the stale signal.  The session is ambiguous;
            # fail this logical attempt before writing or signalling a request.
            raise RuntimeError(
                "runtime outer observer IPC contained a stale response signal"
            )
        if stale == WAIT_FAILED:
            raise ctypes.WinError(ctypes.get_last_error())
        if stale != WAIT_TIMEOUT:
            raise RuntimeError(
                f"unexpected stale-response wait result: 0x{stale:08x}"
            )

        ctypes.memmove(view, request_bytes, len(request_bytes))
        if not kernel32.SetEvent(request_event):
            raise ctypes.WinError(ctypes.get_last_error())

        wait_result = kernel32.WaitForSingleObject(
            response_event,
            int(timeout_ms) + RUNTIME_OUTER_OBSERVER_REMOTE_WAIT_MARGIN_MS,
        )
        if wait_result == WAIT_TIMEOUT:
            raise RemoteThreadStateUnknown(
                "runtime outer observer IPC response state is unknown after timeout"
            )
        if wait_result == WAIT_FAILED:
            raise ctypes.WinError(ctypes.get_last_error())
        if wait_result != WAIT_OBJECT_0:
            raise RuntimeError(
                f"unexpected barrier response wait result: 0x{wait_result:08x}"
            )

        response = unpack_runtime_outer_observer_barrier_request(
            ctypes.string_at(
                view,
                RUNTIME_OUTER_OBSERVER_BARRIER_REQUEST_SIZE,
            )
        )
        return _runtime_outer_observer_known_result(
            base=result_base,
            exit_code=int(response["status"]),
            submitted=submitted_request,
            request=response,
        )
    finally:
        unmap_error: int | None = None
        if view is not None and not kernel32.UnmapViewOfFile(view):
            unmap_error = ctypes.get_last_error()
        for handle in (response_event, request_event, mapping):
            if handle is not None:
                kernel32.CloseHandle(handle)
        if unmap_error is not None:
            raise ctypes.WinError(unmap_error)


def _target_fingerprint(
    process_id: int,
    modules: tuple[dict[str, object], ...],
) -> dict[str, object]:
    executable = next(
        (
            module
            for module in modules
            if str(module["name"]).casefold() == "gakumas.exe"
        ),
        None,
    )
    game_assembly = next(
        (
            module
            for module in modules
            if str(module["name"]).casefold() == "gameassembly.dll"
        ),
        None,
    )
    if executable is None or game_assembly is None:
        raise RuntimeError(f"process {process_id} is not the loaded gakumas target")
    executable_path = Path(str(executable["path"])).resolve()
    game_assembly_path = Path(str(game_assembly["path"])).resolve()
    metadata_path = (
        game_assembly_path.parent
        / "gakumas_Data"
        / "il2cpp_data"
        / "Metadata"
        / "global-metadata.dat"
    ).resolve()
    for source in (executable_path, game_assembly_path, metadata_path):
        if not source.is_file():
            raise RuntimeError(f"target fingerprint source is unavailable: {source}")
    result = {
        "process_id": process_id,
        "executable": executable,
        "executable_sha256": _sha256(executable_path),
        "game_assembly": game_assembly,
        "game_assembly_sha256": _sha256(game_assembly_path),
        "global_metadata_path": str(metadata_path),
        "global_metadata_sha256": _sha256(metadata_path),
    }
    actual = {
        "gakumas.exe": result["executable_sha256"],
        "GameAssembly.dll": result["game_assembly_sha256"],
        "global-metadata.dat": result["global_metadata_sha256"],
    }
    mismatches = {
        name: {"expected": expected, "actual": actual[name]}
        for name, expected in SUPPORTED_TARGET_SHA256.items()
        if actual[name] != expected
    }
    if mismatches:
        raise RuntimeError(
            "unsupported gakumas build; observer was not loaded: "
            + json.dumps(mismatches, ensure_ascii=False, sort_keys=True)
        )
    return result


def _validate_ready_event(
    event: dict[str, object],
    *,
    process_id: int | None = None,
    observer_base: int | None = None,
    game_assembly_base: int | None = None,
) -> None:
    if event.get("schema") != "gkms.telemetry.v1":
        raise RuntimeError(f"unexpected observer schema: {event.get('schema')}")
    if event.get("dropped_events") != 0:
        raise RuntimeError("observer had already dropped telemetry before ready")
    if event.get("call_id") != 0 or event.get("parent_call_id") != 0:
        raise RuntimeError("observer ready event appeared inside a managed hook")
    data = event.get("data")
    if not isinstance(data, dict) or data.get("enable_status") != "MH_OK":
        raise RuntimeError(f"observer did not enable hooks exactly: {event}")
    module = data.get("module")
    if not isinstance(module, dict) or module.get("valid") is not True:
        raise RuntimeError(f"observer module fingerprint is absent: {event}")
    if module.get("observer_protocol") != SUPPORTED_OBSERVER_PROTOCOL:
        raise RuntimeError(f"observer protocol fingerprint differs: {module}")
    if process_id is not None and module.get("pid") != process_id:
        raise RuntimeError(f"observer ready PID differs from target: {module}")
    pointer_expectations = {
        "observer_base": observer_base,
        "game_assembly_base": game_assembly_base,
    }
    for name, expected in pointer_expectations.items():
        if expected is None:
            continue
        try:
            actual = int(str(module.get(name)), 0)
        except (TypeError, ValueError) as error:
            raise RuntimeError(f"observer pointer fingerprint is malformed: {module}") from error
        if actual != expected:
            raise RuntimeError(
                f"observer {name} differs: expected 0x{expected:x}, got 0x{actual:x}"
            )
    resolution = data.get("resolution")
    hooks = resolution.get("hooks") if isinstance(resolution, dict) else None
    if not isinstance(hooks, list):
        raise RuntimeError(f"observer hook resolution is absent: {event}")
    by_name: dict[str, dict[str, object]] = {}
    for raw in hooks:
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
            raise RuntimeError(f"observer hook resolution is malformed: {event}")
        name = str(raw["name"])
        if name in by_name:
            raise RuntimeError(f"observer reported a duplicate hook: {name}")
        by_name[name] = raw
    if set(by_name) != set(REQUIRED_HOOK_TOKENS):
        raise RuntimeError(
            "observer hook set differs from the audited protocol: "
            f"{sorted(by_name)}"
        )
    for name, token in REQUIRED_HOOK_TOKENS.items():
        hook = by_name[name]
        if hook.get("installed") is not True or int(hook.get("actual_token", 0)) != token:
            raise RuntimeError(f"observer hook did not resolve exactly: {name}: {hook}")


def _observer_ready(
    process_id: int,
    *,
    timeout_seconds: float,
    not_before_unix_time_ms: int | None = None,
    observer_base: int | None = None,
    game_assembly_base: int | None = None,
) -> dict[str, object]:
    path = DEFAULT_TELEMETRY_DIR / f"gakumas_telemetry_{process_id}.jsonl"
    deadline = time.monotonic() + timeout_seconds
    last_error = None
    while time.monotonic() < deadline:
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8")
                raw_lines = text.splitlines(keepends=True)
                rows = []
                for index, original in enumerate(raw_lines):
                    line = original.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        if index == len(raw_lines) - 1 and not original.endswith(("\n", "\r")):
                            break
                        raise
            except (OSError, json.JSONDecodeError) as error:
                last_error = error
            else:
                for row in reversed(rows):
                    if not isinstance(row, dict):
                        continue
                    timestamp = row.get("unix_time_ms")
                    if (
                        not_before_unix_time_ms is not None
                        and (not isinstance(timestamp, int) or timestamp < not_before_unix_time_ms)
                    ):
                        continue
                    if row.get("kind") == "observer_ready":
                        _validate_ready_event(
                            row,
                            process_id=process_id,
                            observer_base=observer_base,
                            game_assembly_base=game_assembly_base,
                        )
                        return {"path": str(path), "event": row}
                    if row.get("kind") == "observer_error":
                        raise RuntimeError(f"observer bootstrap failed: {row}")
        time.sleep(0.05)
    raise TimeoutError(
        "observer ready event was not observed"
        + ("" if last_error is None else f": {last_error}")
    )


def _filesystem_target_fingerprint(
    process_id: int,
    executable_path: str | Path,
) -> dict[str, object]:
    executable = Path(executable_path).resolve()
    game_assembly = executable.parent / "GameAssembly.dll"
    modules = (
        {
            "name": "gakumas.exe",
            "path": str(executable),
            "base": 0,
            "size": executable.stat().st_size if executable.is_file() else 0,
        },
        {
            "name": "GameAssembly.dll",
            "path": str(game_assembly),
            "base": 0,
            "size": game_assembly.stat().st_size if game_assembly.is_file() else 0,
        },
    )
    return _target_fingerprint(process_id, modules)


def inject_observer_via_window_hook(
    process_id: int,
    window_handle: int,
    target_executable: str | Path,
    dll_path: str | Path = DEFAULT_OBSERVER_DLL,
    *,
    ready_timeout_seconds: float = 15.0,
) -> dict[str, object]:
    """Ask Windows to load the fixed observer on one bound GUI thread."""

    raise RuntimeError(
        "external observer injection is retired; load it only from an "
        "already-registered IL2CPP managed callback"
    )


def inject_observer_dll(
    process_id: int,
    dll_path: str | Path = DEFAULT_OBSERVER_DLL,
    *,
    ready_timeout_seconds: float = 15.0,
) -> dict[str, object]:
    """Load one exact observer DLL into an existing x64 process."""

    raise RuntimeError(
        "remote observer injection is retired; load it only from an "
        "already-registered IL2CPP managed callback"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Inject the read-only telemetry DLL")
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--ready-timeout", type=float, default=15.0)
    arguments = parser.parse_args()
    print(
        json.dumps(
            inject_observer_dll(
                arguments.pid,
                DEFAULT_OBSERVER_DLL,
                ready_timeout_seconds=arguments.ready_timeout,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
