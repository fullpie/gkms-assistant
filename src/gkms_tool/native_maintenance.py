"""Session-scoped elevated game maintenance, with fixed commands and receipts.

UAC is requested only by launch_native_maintenance.ps1. This service inherits
that token and never runs a caller-supplied shell, executable or game callback.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, ExitStack
import ctypes
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import time
import uuid

from .application_paths import (app_root, native_state_root, public_installation, game_file,
    native_runtime_layout, native_bridge_path)
# CLI workers must select the same private profile as their GUI before any
# mailbox defaults are imported. Library imports never change environment.
if __name__ == "__main__" and not public_installation() and (app_root() / "devtools/runtime-profile.json").is_file():
    from .private_gui import configure_private_environment
    configure_private_environment(app_root())
ROOT = app_root()
IPC = native_state_root() / "native_maintenance"
# Compatibility constants are development-only. Public clients resolve the
# selected game at request time; the worker pins it only after its own startup.
BRIDGE = ROOT / "var/native/runtime_command_bridge/gkms_runtime_command_bridge.dll"
SCHEMA = "gkms.native-maintenance.v1"
COMMANDS = {"status", "start-game", "stop-game", "restart-game", "reload-dll", "stage-dll", "start-inspection", "stop-helper", "complete-installation"}
MUTATIONS = {"start-game", "stop-game", "restart-game", "reload-dll", "stage-dll", "start-inspection"}
CLOSED_GAME_COMMANDS = {"stage-dll", "start-inspection"}
IDLE_SECONDS = 12 * 60 * 60
SAFE_ID = re.compile(r"[a-f0-9]{32}\Z")
HASH = re.compile(r"[a-fA-F0-9]{64}\Z")
RUN_ID = re.compile(r"run-[a-f0-9]{32}\Z")
POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
FIXED_SCRIPTS = ("native_game_maintenance.ps1", "launch_gakumas_direct_cached.ps1", "stop_exact_gakumas.ps1")
FIXED_GAME_EXECUTABLE = str(game_file("gakumas.exe"))


def installed_bridge():
    if public_installation(ROOT):
        from .native_maintenance_public import public_configuration
        return public_configuration(ROOT)["bridge"]
    return native_bridge_path(ROOT) if native_runtime_layout(ROOT) == "public-installed" else BRIDGE


def expected_game_executable():
    if public_installation(ROOT):
        from .native_maintenance_public import public_configuration
        return str(public_configuration(ROOT)["executable"])
    return str(native_bridge_path(ROOT).parents[2] / "gakumas.exe") if native_runtime_layout(ROOT) == "public-installed" else FIXED_GAME_EXECUTABLE


def _physical_executor():
    """Share the tested lifecycle with a separately qualified private authority."""
    from .native_maintenance_public import PublicPhysicalExecutor
    if public_installation(ROOT):
        return PublicPhysicalExecutor(ROOT)
    if native_runtime_layout(ROOT) == "public-installed":
        from .native_maintenance_development import private_configuration
        return PublicPhysicalExecutor(ROOT, configuration_reader=private_configuration)
    return None


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path):
    path = Path(path)
    if path.stat().st_size > 65536:
        raise ValueError("maintenance message exceeds 64 KiB")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("maintenance message must be an object")
    return value


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def bounded_path(path, parent):
    value, parent = Path(path).resolve(), Path(parent).resolve()
    if value == parent or not value.is_relative_to(parent):
        raise ValueError("maintenance path is outside the designated directory")
    return value


def validate_payload(command, payload, *, root=ROOT):
    if command not in COMMANDS or not isinstance(payload, dict):
        raise ValueError("unsupported maintenance command or payload")
    if public_installation(root):
        from .native_maintenance_public import PUBLIC_COMMANDS
        if command not in PUBLIC_COMMANDS:
            raise ValueError("research inspection is not available in the public maintenance worker")
    if command == "complete-installation":
        if not public_installation(root):
            raise ValueError("Closed installation jobs require the verified public worker")
        from .native_installation_jobs import validate_payload as validate_installation
        return validate_installation(payload, root=root)
    if command == "stop-game" and native_runtime_layout(root) != "public-installed":
        raise ValueError("stop-game requires the fixed installed-runtime lifecycle")
    if command == "start-inspection" and native_runtime_layout(root) == "public-installed":
        raise ValueError("research inspection is unavailable in the installed normal runtime")
    required = {
        "status": set(), "stop-helper": set(), "start-game": {"expected_installed_sha256", "expected_run_id"},
        "start-inspection": {"expected_installed_sha256", "expected_run_id"},
        "stage-dll": {"expected_installed_sha256", "expected_run_id", "candidate_path", "candidate_sha256"},
        "restart-game": {"expected_game_pid", "expected_generation", "expected_installed_sha256", "expected_run_id"},
        "stop-game": {"expected_game_pid", "expected_generation", "expected_installed_sha256", "expected_run_id"},
        "reload-dll": {"expected_game_pid", "expected_generation", "expected_installed_sha256", "expected_run_id", "candidate_path", "candidate_sha256"},
    }[command]
    if set(payload) != required:
        raise ValueError("maintenance payload fields differ from fixed command contract")
    result = dict(payload)
    if command not in MUTATIONS:
        return result
    for name in ("expected_installed_sha256", "candidate_sha256"):
        if name in result:
            if not isinstance(result[name], str) or not HASH.fullmatch(result[name]):
                raise ValueError("invalid DLL SHA-256")
            result[name] = result[name].lower()
    run_id = result["expected_run_id"]
    if command in CLOSED_GAME_COMMANDS and run_id is not None:
        raise ValueError("closed-game inspection maintenance requires no active run")
    if run_id is not None and (not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id)):
        raise ValueError("invalid expected run identity")
    if "expected_game_pid" in result:
        if type(result["expected_game_pid"]) is not int or result["expected_game_pid"] <= 0:
            raise ValueError("invalid expected game PID")
        if not isinstance(result["expected_generation"], str) or not HASH.fullmatch(result["expected_generation"]):
            raise ValueError("invalid expected native generation")
    if command in {"reload-dll", "stage-dll"}:
        if public_installation(root):
            from .native_maintenance_public import verify_public_candidate
            result["candidate_path"] = str(verify_public_candidate(result["candidate_path"],
                result["candidate_sha256"], root=root))
            return result
        if native_runtime_layout(root) == "public-installed":
            from .native_maintenance_development import verify_development_candidate
            result["candidate_path"] = str(verify_development_candidate(result["candidate_path"],
                result["candidate_sha256"], root=root))
            return result
        candidate = bounded_path(result["candidate_path"], Path(root) / "var/native")
        if candidate.name != "gkms_runtime_command_bridge.dll" or not candidate.is_file():
            raise ValueError("candidate must be a built runtime-command bridge DLL")
        if candidate == (Path(root) / "var/native/runtime_command_bridge/gkms_runtime_command_bridge.dll").resolve():
            raise ValueError("candidate must be separate from the installed DLL")
        if sha256(candidate) != result["candidate_sha256"]:
            raise ValueError("candidate DLL changed after selection")
        result["candidate_path"] = str(candidate)
    return result


def validate_bridge_binary(path):
    import pefile
    binary = pefile.PE(str(path), fast_load=True)
    try:
        if binary.FILE_HEADER.Machine != 0x8664 or not binary.FILE_HEADER.Characteristics & 0x2000:
            raise ValueError("candidate is not an x64 DLL")
        binary.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXPORT"]])
        table = getattr(binary, "DIRECTORY_ENTRY_EXPORT", None)
        exports = {row.name for row in getattr(table, "symbols", ())}
        if not {b"GKMSRuntimeCommandBridgeProtocolVersion", b"GKMSRuntimeCommandBridgeStartOnManagedThread",
                b"GKMSRuntimeCommandBridgePumpOnManagedThread"} <= exports:
            raise ValueError("candidate lacks the runtime-command bridge exports")
    finally:
        binary.close()


def validate_quiescent_snapshot(raw, active, expected_run_id):
    """Allow idle navigation or the explicitly named paused cultivation."""
    actual_run_id = None if active is None else active.run_id
    if actual_run_id != expected_run_id:
        raise ValueError("active cultivation changed; pause and bind the current run before maintenance")
    if raw.get("busy") is not False:
        raise ValueError("game is still processing a transition")
    state, progress = raw.get("state") or {}, raw.get("progress") or {}
    if active is not None:
        if (state.get("in_progress") is not True or progress.get("produceId") != active.produce_id
                or progress.get("idolCardId") != active.idol_card_id):
            raise ValueError("native cultivation does not match the paused run")
    elif state.get("in_progress") is True:
        raise ValueError("untracked active cultivation; bind it before maintenance")
    elif raw.get("screen_type") not in {"HomeTopScreenPresenter", "TitlePresenter"}:
        pre_ap = {"ProduceTopScreenPresenter", "ProduceIdolSelectScreenPresenter",
                  "ProduceSupportCardSelectScreenPresenter", "ProduceMemorySelectScreenPresenter",
                  "ProduceStartScreenPresenter"}
        screen = raw.get("screen_type")
        ui = raw.get("ui_state") or {}
        memory = ui.get("memory_auto") or {}
        if (screen not in pre_ap or raw.get("underlying_screen_type") != screen
                or state.get("in_progress") is not False or raw.get("actions_complete") is not True
                or not raw.get("screen_instance_id")
                or not isinstance(raw.get("pointer_blocking"), dict)
                or raw["pointer_blocking"].get("input_ready") is not True
                or raw.get("blockers") not in ([], None) or not raw.get("legal_actions")
                or memory.get("phase") in {"awaiting_confirm", "running"}
                or memory.get("task_status") == "Pending"
                or (memory.get("operation_serial", 0) and memory.get("task_consumed") is not True)):
            raise ValueError("finish the current result/dialog or native task before idle maintenance")


def check_pending(root=ROOT):
    bridge = native_state_root(root) / "runtime_command_bridge"
    if list(bridge.glob("*pending*.json")):
        raise ValueError("an unresolved game input exists; maintenance did not submit or retry it")
    for folder in ("inbox", "running"):
        for path in (bridge / folder).glob("*.json"):
            request = read_json(path)
            if request.get("command") not in {"status", "read_snapshot", "read_outer_snapshot", "read_inventory", "read_loadout", "read_model_context", "read_pc_contracts"}:
                raise ValueError("a game input is queued or running")


def require_game_closed():
    """Enumerate only; an inaccessible process inventory must stop maintenance."""
    from .gui_setup.launcher_windows import WindowsPlatform
    if WindowsPlatform().processes("gakumas.exe"):
        raise ValueError("close the game before closed-game maintenance; no process was stopped")


def validate_inspection_status(status, *, launched_pid, previous_generation):
    version = status.get("pc_version") or {}
    if (status.get("pid") != launched_pid or status.get("session_generation") == previous_generation
            or not isinstance(status.get("session_generation"), str)
            or not HASH.fullmatch(status["session_generation"])):
        raise ValueError("inspection heartbeat does not belong to the newly launched process")
    if (status.get("ready") is not False or set(status.get("capabilities", ())) != {"status", "read_pc_contracts"}
            or version.get("schema") != "gkms.observed-pc-version-profile.v1"
            or version.get("recognized") is not True or version.get("read_only_inspection_allowed") is not True
            or version.get("input_qualified") is not False):
        raise ValueError("new bridge is not restricted to the recognized read-only inspection profile")


def verify_inspection_probe(client, status, job_dir):
    """Issue one empty metadata query; a pending response retains its request ID."""
    from .runtime_command_client import RuntimeCommandPending
    target = {"schema": "gkms.pc-readonly-contract-request.v1", "methods": [], "classes": []}
    try:
        result = client.execute("read_pc_contracts", target=target, timeout=15)
    except RuntimeCommandPending as pending:
        write_json(job_dir / "inspection_pending.json", pending.request.to_dict())
        # Never replace a timed-out probe with a new request.
        result = client.await_result(pending.request, timeout=15)
    result.require_ok()
    report = result.raw.get("pc_contracts") or {}
    if (result.request.session_generation != status["session_generation"]
            or report.get("schema") != "gkms.pc-readonly-contract-report.v1"
            or report.get("version") != status["pc_version"]
            or report.get("methods") != [] or report.get("classes") != [] or report.get("errors") != []
            or report.get("complete") is not True or report.get("version_stable") is not True
            or any(report.get(key) is not False for key in (
                "managed_method_invoked", "object_values_read", "game_input_submitted", "native_ABI_verified", "mutation_qualified"))):
        raise ValueError("empty read-only PC contract probe did not retain inspection-only semantics")
    write_json(job_dir / "inspection_verification.json", {
        "schema": "gkms.maintenance-readonly-inspection.v1", "status": status,
        "request": result.request.to_dict(), "response_path": str(result.path),
        "response_sha256": sha256(result.path), "methods_requested": 0, "classes_requested": 0,
        "gameplay_resumed": False, "input_qualified": False,
    })
    return result


def verify_inspection_process_path(launch, *, process_path_reader=None, expected_path=None):
    """The launcher receipt alone cannot prove the PID's actual executable."""
    if process_path_reader is None:
        from .gui_setup.launcher_windows import WindowsPlatform
        process_path_reader = WindowsPlatform().process_path
    try:
        actual = process_path_reader(launch["game_pid"])
    except Exception as error:
        raise ValueError("inspection process image path is unreadable") from error
    normalize = lambda value: ntpath.normcase(ntpath.normpath(value))
    expected = str(expected_path) if expected_path is not None else expected_game_executable()
    if (not isinstance(actual, str) or not actual or normalize(actual) != normalize(expected)):
        raise ValueError("inspection PID does not resolve to the fixed game executable")
    recorded = launch.get("game_path")
    if recorded is not None and (not isinstance(recorded, str) or not recorded or normalize(recorded) != normalize(actual)):
        raise ValueError("inspection launch receipt path differs from the actual process image")
    return {"pid": launch["game_pid"], "game_path": actual,
            "expected_game_path": expected, "actual_process_image_verified": True}


def wait_inspection_start(client, job_dir, *, previous_generation):
    launch = read_json(job_dir / "launch.json")
    if (launch.get("schema") != "gkms.gakumas-direct-cached-launch.v1" or launch.get("status") != "started"
            or type(launch.get("game_pid")) is not int or launch["game_pid"] <= 0
            or launch.get("already_running") is not False):
        raise ValueError("inspection startup lacks a fresh fixed-launcher receipt")
    deadline, last_error = time.monotonic() + 90, "new inspection heartbeat unavailable"
    status = None
    while time.monotonic() < deadline:
        try:
            observed = client.read_status()
            validate_inspection_status(observed, launched_pid=launch["game_pid"], previous_generation=previous_generation)
            status = observed
            break
        except Exception as error:
            last_error = str(error)
            time.sleep(.5)
    if status is None:
        raise RuntimeError("inspection launch returned without a restricted heartbeat: " + last_error)
    process_identity = verify_inspection_process_path(launch)
    write_json(job_dir / "inspection_process_identity.json", process_identity)
    # Outside the readiness wait: a rejected/incomplete probe is never retried.
    probe = verify_inspection_probe(client, status, job_dir)
    return {"game_pid": status["pid"], "native_generation": status["session_generation"],
            "profile_id": status["pc_version"].get("profile_id"), "input_qualified": False,
            "inspection_only": True, "contract_probe_path": str(probe.path),
            "game_path": process_identity["game_path"], "actual_process_image_verified": True,
            "installed_sha256": sha256(BRIDGE), "expected_run_id": None,
            "gameplay_resumed": False, "uac_requested_by_job": False}


@contextmanager
def single_service():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.CreateMutexW(None, False, r"Local\gkms_tool.native_maintenance.service.v1")
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if ctypes.get_last_error() == 183:
            raise RuntimeError("native maintenance helper is already running")
        yield
    finally:
        kernel.CloseHandle(handle)


def execute_job(command, payload, job_dir, runner, script_hashes, *, public_executor=None):
    """One serialized lifecycle transaction under the native input claim lock."""
    if command == "complete-installation":
        if not public_installation(ROOT):
            raise ValueError("Closed installation jobs require a public worker")
        from .native_maintenance_public import PublicPhysicalExecutor
        from .native_installation_jobs import execute_installation_job
        return execute_installation_job(payload, job_dir, ROOT, runner, script_hashes,
            public_executor or PublicPhysicalExecutor(ROOT))
    from .runtime_command_client import RuntimeCommandClient, _claim_lock
    from .runtime_outer_snapshot import RuntimeOuterReader
    from .run_identity import load_active_run
    with _claim_lock(), ExitStack() as resources:
        if public_executor is None and native_runtime_layout(ROOT) == "public-installed":
            public_executor = _physical_executor()
        if public_executor is not None:
            public_executor.validate_unchanged()
            from .native_maintenance_public import PUBLIC_COMMANDS
            if command not in PUBLIC_COMMANDS:
                raise ValueError("unsupported public maintenance operation")
        installed = public_executor.config["bridge"] if public_executor else BRIDGE
        pinned_process = None
        check_pending()
        if sha256(installed) != payload["expected_installed_sha256"]:
            raise ValueError("installed DLL changed; obtain its current hash before maintenance")
        active = load_active_run()
        if (None if active is None else active.run_id) != payload["expected_run_id"]:
            raise ValueError("active run changed before maintenance")
        if command in CLOSED_GAME_COMMANDS:
            if active is not None or payload["expected_run_id"] is not None:
                raise ValueError("closed-game inspection maintenance requires no active run")
            require_game_closed()
        before_generation = payload.get("expected_generation")
        if command == "start-inspection":
            prior_status = native_state_root(ROOT) / "runtime_command_bridge/status.json"
            if prior_status.is_file():
                old_status = read_json(prior_status)
                before_generation = old_status.get("session_generation")
                if not isinstance(before_generation, str) or not HASH.fullmatch(before_generation):
                    raise ValueError("previous bridge generation is malformed")
                write_json(job_dir / "previous_status.json", old_status)
        if command in {"restart-game", "reload-dll", "stop-game"}:
            if public_executor is not None:
                pinned_process = resources.enter_context(public_executor.pin_game(payload["expected_game_pid"]))
            client = RuntimeCommandClient()
            status = client.read_status()
            if (status["pid"] != payload["expected_game_pid"] or status["session_generation"] != before_generation):
                raise ValueError("game PID or native generation changed")
            snapshot = RuntimeOuterReader(client=client).read()
            if snapshot.session_generation != before_generation:
                raise ValueError("native generation changed during preflight")
            validate_quiescent_snapshot(snapshot.raw, active, payload["expected_run_id"])
            write_json(job_dir / "preflight.json", {"snapshot_path": str(snapshot.source_path),
                "pid": status["pid"], "generation": before_generation, "run_id": payload["expected_run_id"],
                "screen_type": snapshot.raw["screen_type"], "state": snapshot.raw.get("state")})
        check_pending()
        for name, expected in script_hashes.items():
            if sha256(runner / name) != expected:
                raise ValueError("fixed maintenance executor changed; restart the helper explicitly")
        staged = None
        if command in {"reload-dll", "stage-dll"}:
            staged = job_dir / "candidate.dll"
            shutil.copyfile(payload["candidate_path"], staged)
            if sha256(staged) != payload["candidate_sha256"]:
                raise ValueError("candidate DLL changed while staging")
            validate_bridge_binary(staged)
        action = {"operation": command, "workspace": str(ROOT), "job_directory": str(job_dir),
            "installed_sha256": payload["expected_installed_sha256"], "expected_pid": payload.get("expected_game_pid"),
            "candidate_sha256": payload.get("candidate_sha256"), "candidate_path": str(staged) if staged else None}
        action_path = job_dir / "operation.json"
        write_json(action_path, action)
        if public_executor is not None:
            resources.callback(public_executor.release_launch, job_dir)
            public_executor.execute(action, job_dir, pinned_process=pinned_process)
        else:
            with (job_dir / "executor.log").open("wb") as log:
                process = subprocess.Popen([str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    str(runner / FIXED_SCRIPTS[0]), "-OperationPath", str(action_path)], cwd=str(ROOT), shell=False,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
                write_json(job_dir / "child.json", {"pid": process.pid, "started_at": time.time()})
                # A slow process remains this same process; never create a replacement.
                code = process.wait()
            if code:
                raise RuntimeError(f"fixed maintenance executor exited {code}; inspect {job_dir / 'executor.log'}")
        if sha256(installed) != payload.get("candidate_sha256", payload["expected_installed_sha256"]):
            raise RuntimeError("installed DLL failed post-operation hash verification")
        if command == "stop-game":
            require_game_closed()
            check_pending()
            return {"installed_sha256": sha256(installed), "expected_run_id": payload["expected_run_id"],
                    "game_started": False, "game_closed": True, "gameplay_resumed": False, "uac_requested_by_job": False}
        if command == "stage-dll":
            require_game_closed()
            if sha256(job_dir / "previous_bridge.dll") != payload["expected_installed_sha256"]:
                raise RuntimeError("closed-game staging backup failed hash verification")
            return {"installed_sha256": sha256(installed), "previous_sha256": payload["expected_installed_sha256"],
                    "backup_path": str(job_dir / "previous_bridge.dll"), "expected_run_id": None,
                    "game_started": False, "gameplay_resumed": False, "uac_requested_by_job": False}
        client = RuntimeCommandClient()
        if command == "start-inspection":
            return wait_inspection_start(client, job_dir, previous_generation=before_generation)
        public_launch = read_json(job_dir / "bootstrap_launch.json") if public_executor is not None else None
        deadline, last_error = time.monotonic() + 90, ""
        while time.monotonic() < deadline:
            try:
                status = client.read_status()
                launch_binding = (public_executor.validate_native_launch(job_dir, public_launch, status.get("pid"))
                    if public_launch is not None else None)
                if status["session_generation"] == before_generation:
                    time.sleep(.25)
                    continue
                snapshot = RuntimeOuterReader(client=client, timeout=10).read()
                if snapshot.session_generation != status["session_generation"]:
                    continue
                if public_launch is not None:
                    launch_binding = public_executor.validate_native_launch(job_dir, public_launch, status["pid"],
                        previous=launch_binding)
                    public_executor.record_verified_launch(job_dir, public_launch, launch_binding)
                return {"game_pid": status["pid"], "native_generation": status["session_generation"],
                    "screen_type": snapshot.raw["screen_type"], "snapshot_path": str(snapshot.source_path),
                    "installed_sha256": sha256(installed), "expected_run_id": payload["expected_run_id"],
                    "gameplay_resumed": False, "uac_requested_by_job": False}
            except Exception as error:
                last_error = str(error)
                time.sleep(.5)
        raise RuntimeError("game launch returned but native bridge was not verified: " + last_error)


def serve(idle_seconds=IDLE_SECONDS):
    if os.name != "nt" or not ctypes.windll.shell32.IsUserAnAdmin():
        raise PermissionError("start this helper once through the UAC launcher")
    public_executor = None
    available_commands = COMMANDS - {"complete-installation", "stop-game"}
    executor_profile = "fixed-development-powershell"
    if public_installation(ROOT):
        from .native_maintenance_public import PublicPhysicalExecutor, PUBLIC_COMMANDS, validate_public_worker_identity
        validate_public_worker_identity()
        public_executor = PublicPhysicalExecutor(ROOT)
        available_commands = PUBLIC_COMMANDS
        executor_profile = "public-fixed-python"
    elif native_runtime_layout(ROOT) == "public-installed":
        from .native_maintenance_public import PUBLIC_COMMANDS
        public_executor = _physical_executor()
        available_commands = PUBLIC_COMMANDS - {"complete-installation"}
        executor_profile = "private-installed-fixed-python"
    with single_service():
        session_id, token = uuid.uuid4().hex, secrets.token_urlsafe(32)
        directory = IPC / "sessions" / session_id
        runner = directory / "runner"
        for folder in (directory / "requests", directory / "jobs", directory / "responses", runner):
            folder.mkdir(parents=True, exist_ok=False)
        if public_executor is None:
            for name in FIXED_SCRIPTS:
                shutil.copyfile(ROOT / "scripts" / name, runner / name)
            hashes = {name: sha256(runner / name) for name in FIXED_SCRIPTS}
        elif public_installation(ROOT):
            from .gui_setup.app_updates import ENTRYPOINT
            runner = ROOT
            hashes = {name: sha256(ROOT / name) for name in (ENTRYPOINT, "gkms-app-package.json")}
        else:
            runner = ROOT
            from .native_maintenance_development import worker_source_hashes
            hashes = worker_source_hashes(ROOT)
        session = {"schema": SCHEMA, "session_id": session_id, "token": token, "pid": os.getpid(),
            "active": True, "elevated": True, "started_at": time.time(), "directory": str(directory),
            "idle_timeout_seconds": idle_seconds, "commands": sorted(available_commands), "script_hashes": hashes,
            "executor_profile": executor_profile}
        write_json(IPC / "session.json", session)
        current, last_activity, last_heartbeat, active_job = None, time.monotonic(), 0., None
        active, executor = True, ThreadPoolExecutor(max_workers=1)
        try:
            while active:
                now = time.monotonic()
                if now - last_heartbeat >= 1:
                    write_json(directory / "heartbeat.json", {"session_id": session_id, "pid": os.getpid(),
                        "elevated": True, "active": True, "updated_at": time.time(), "active_job": active_job})
                    last_heartbeat = now
                if current is not None and current.done():
                    request_id, future = active_job, current
                    result = {"schema": SCHEMA, "session_id": session_id, "request_id": request_id, "finished_at": time.time()}
                    try:
                        result.update(status="completed", result=future.result())
                    except Exception as error:
                        result.update(status="failed", error=f"{type(error).__name__}: {error}")
                    write_json(directory / "responses" / (request_id + ".json"), result)
                    current, active_job, last_activity = None, None, now
                for path in sorted((directory / "requests").glob("*.json")):
                    request_id = path.stem
                    if not SAFE_ID.fullmatch(request_id):
                        path.rename(path.with_suffix(".rejected"))
                        continue
                    response_path = directory / "responses" / path.name
                    if response_path.exists() or (directory / "jobs" / request_id).exists():
                        path.unlink()
                        continue  # Same request is never executed twice.
                    try:
                        request = read_json(path)
                        if (set(request) != {"schema", "session_id", "token", "request_id", "command", "payload"}
                                or request["schema"] != SCHEMA or request["session_id"] != session_id
                                or not secrets.compare_digest(request["token"], token) or request["request_id"] != request_id):
                            raise ValueError("invalid maintenance session or request identity")
                        command = request["command"]
                        if command not in available_commands:
                            raise ValueError("command is unavailable in this maintenance session")
                        payload = validate_payload(command, request["payload"])
                        last_activity = now
                        if command == "status":
                            result = {"status": "completed", "result": {"pid": os.getpid(), "session_id": session_id,
                                "elevated": True, "active_job": active_job, "commands": sorted(available_commands),
                                "executor_profile": executor_profile,
                                "app_root": str(ROOT), "game_path": str(public_executor.config["executable"]) if public_executor else FIXED_GAME_EXECUTABLE}}
                        elif command == "stop-helper":
                            if current is not None:
                                raise RuntimeError("maintenance job is still running; wait for its recorded outcome")
                            active = False
                            result = {"status": "completed", "result": {"stopped": True, "game_closed": False}}
                        else:
                            if current is not None:
                                raise RuntimeError("another maintenance job is running")
                            job_dir = directory / "jobs" / request_id
                            job_dir.mkdir()
                            write_json(job_dir / "request.json", {"command": command, "payload": payload})
                            active_job = request_id
                            current = executor.submit(execute_job, command, payload, job_dir, runner, hashes,
                                public_executor=public_executor)
                            result = {"status": "running", "job_directory": str(job_dir)}
                    except Exception as error:
                        result = {"status": "failed", "error": f"{type(error).__name__}: {error}"}
                    write_json(response_path, {"schema": SCHEMA, "session_id": session_id, "request_id": request_id, **result})
                    path.unlink()
                if current is None and now - last_activity >= idle_seconds:
                    active = False
                time.sleep(.1)
        finally:
            executor.shutdown(wait=True)
            session.update(active=False, stopped_at=time.time())
            write_json(IPC / "session.json", session)


def submit(command, payload=None, *, request_id=None, timeout=5):
    session = read_json(IPC / "session.json")
    if session.get("schema") != SCHEMA or session.get("active") is not True:
        raise RuntimeError("native maintenance helper is unavailable; run its UAC launcher once")
    directory = bounded_path(session["directory"], IPC / "sessions")
    heartbeat = read_json(directory / "heartbeat.json")
    if heartbeat.get("session_id") != session["session_id"] or time.time() - heartbeat["updated_at"] > 5:
        raise RuntimeError("native maintenance helper heartbeat expired; verify its process before restarting")
    if not public_installation(ROOT) and native_runtime_layout(ROOT) == "public-installed":
        if command in {"reload-dll", "stage-dll"} and session.get("executor_profile") != "private-installed-fixed-python":
            raise RuntimeError("existing-helper-needs-private-migration: retain this helper; migrate once while idle before private DLL swaps")
        if session.get("commands") is not None and command not in session["commands"]:
            raise RuntimeError("the existing helper does not support this command; do not start another owner")
    payload = validate_payload(command, payload or {})
    request_id = request_id or uuid.uuid4().hex
    if not SAFE_ID.fullmatch(request_id):
        raise ValueError("invalid request ID")
    response = directory / "responses" / (request_id + ".json")
    request = {"schema": SCHEMA, "session_id": session["session_id"], "token": session["token"],
               "request_id": request_id, "command": command, "payload": payload}
    if not response.exists() and not (directory / "jobs" / request_id).exists():
        write_json(directory / "requests" / (request_id + ".json"), request)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if response.exists():
            return read_json(response)
        time.sleep(.1)
    return {"status": "pending", "request_id": request_id, "session_id": session["session_id"],
            "response_path": str(response), "retry_policy": "poll this same request; do not resubmit with a new ID"}


def public_worker_entry():
    from .native_maintenance_public import public_worker_entry as fixed_entry
    return fixed_entry()


def read_result(session_id, request_id):
    if not SAFE_ID.fullmatch(session_id or "") or not SAFE_ID.fullmatch(request_id or ""):
        raise ValueError("Exact maintenance session/request IDs are required")
    path = IPC / "sessions" / session_id / "responses" / (request_id + ".json")
    if not path.exists(): return None
    result = read_json(path)
    if (result.get("schema") != SCHEMA or result.get("session_id") != session_id
            or result.get("request_id") != request_id):
        raise ValueError("Maintenance result belongs to another request")
    return result


def ensure_public_helper(request_id):
    from .native_maintenance_public import ensure_public_helper as ensure
    return ensure(request_id, root=ROOT)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=sorted(COMMANDS | {"serve", "result", "paths"}))
    parser.add_argument("--request-id")
    parser.add_argument("--session-id")
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--candidate-sha256")
    parser.add_argument("--expected-run-id")
    parser.add_argument("--timeout", type=float, default=5)
    args = parser.parse_args()
    if args.command == "paths":
        print(json.dumps({"root": str(ROOT), "ipc": str(IPC), "native_layout": native_runtime_layout(ROOT)}))
        return
    if args.command == "serve":
        try:
            serve()
        except Exception as error:
            write_json(IPC / "startup_error.json", {"pid": os.getpid(), "at": time.time(),
                "error": f"{type(error).__name__}: {error}"})
            raise
        return
    if args.command == "result":
        session_id = args.session_id or read_json(IPC / "session.json")["session_id"]
        if not SAFE_ID.fullmatch(session_id) or not SAFE_ID.fullmatch(args.request_id or ""):
            raise ValueError("result requires valid request/session IDs")
        result = read_json(IPC / "sessions" / session_id / "responses" / (args.request_id + ".json"))
    else:
        payload = {}
        if args.command in MUTATIONS:
            payload.update(expected_installed_sha256=sha256(installed_bridge()), expected_run_id=args.expected_run_id)
            if args.command in {"restart-game", "reload-dll", "stop-game"}:
                from .runtime_command_client import RuntimeCommandClient
                status = RuntimeCommandClient().read_status()
                payload.update(expected_game_pid=status["pid"], expected_generation=status["session_generation"])
            if args.command in {"reload-dll", "stage-dll"}:
                if args.candidate is None or args.candidate_sha256 is None:
                    parser.error(args.command + " requires --candidate and --candidate-sha256")
                payload.update(candidate_path=str(args.candidate.resolve()), candidate_sha256=args.candidate_sha256)
        result = submit(args.command, payload, request_id=args.request_id, timeout=args.timeout)
    print(json.dumps(result, ensure_ascii=False))
    if result.get("status") == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
