"""Fixed public maintenance executor; no caller-provided program or shell.

The existing native_maintenance.execute_job owns transaction exclusion and
pre/post native checks. This module only owns exact process/file lifecycle and
the existing Python DirectLauncher entry under the already-elevated worker.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes as W
import hashlib
import json
import ntpath
import os
from pathlib import Path
import shutil
import sys
import uuid
import re
import time

PUBLIC_COMMANDS = frozenset({"status", "stop-helper", "start-game", "stop-game", "restart-game", "reload-dll", "stage-dll", "complete-installation"})
BRIDGE_ROLE = "gkms/native/gkms_runtime_command_bridge.dll"


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _require(value, message):
    if not value:
        raise ValueError(message)


def _same_path(left, right):
    return ntpath.normcase(ntpath.normpath(str(left))) == ntpath.normcase(ntpath.normpath(str(right)))


def public_configuration(root):
    from .application_paths import game_directory, native_state_root, public_installation
    from .gui_setup.app_updates import verify_slot
    root = Path(root).resolve()
    _require(public_installation(root), "A verified public application slot is required")
    manifest = verify_slot(root)
    game = game_directory(root)
    _require(game is not None and game.is_absolute(), "Select the game directory before public maintenance")
    executable = game / "gakumas.exe"
    _require(executable.is_file(), "The selected game executable is unavailable")
    return {"root": root, "game": game, "executable": executable,
        "bridge": game / BRIDGE_ROLE, "state": native_state_root(root), "manifest": manifest,
        "manifest_sha256": digest(root / "gkms-app-package.json")}


def verify_public_candidate(path, expected_sha256, *, root):
    """Only the normal bridge in this verified slot's control package is eligible."""
    from .gui_setup.packages import control_package
    config = public_configuration(root)
    base = config["state"] / "native_maintenance/candidates"
    candidate = Path(path).resolve()
    _require(candidate.is_relative_to(base.resolve()) and candidate != base.resolve()
        and candidate.name == "gkms_runtime_command_bridge.dll", "Public candidate is outside its fixed bridge role")
    _require(candidate.is_file() and digest(candidate) == expected_sha256, "Public candidate changed after selection")
    package = control_package((config["root"] / "assets/control/gkms-control.zip").read_bytes(),
        digest(config["game"] / "GameAssembly.dll"))
    _require(package.metadata.get("buildProfile") == "public" and BRIDGE_ROLE in package.files,
        "A formal public control package is required")
    _require(hashlib.sha256(package.files[BRIDGE_ROLE]).hexdigest() == expected_sha256,
        "Public DLL candidate is not the current slot's verified normal bridge")
    # A bridge-only swap cannot silently combine incompatible component versions.
    for role, raw in package.files.items():
        if role == BRIDGE_ROLE:
            continue
        installed = config["game"] / role
        _require(installed.is_file() and digest(installed) == hashlib.sha256(raw).hexdigest(),
            "Public component set requires a complete managed installation before a bridge-only swap")
    return candidate


class ExactGameProcess:
    """Pin one kernel process object across preflight and termination."""
    def __init__(self, pid, expected_path):
        if os.name != "nt":
            raise OSError("Windows process handles are required")
        self.expected_path = Path(expected_path)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        k = self.kernel
        k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]; k.OpenProcess.restype = W.HANDLE
        k.CloseHandle.argtypes = [W.HANDLE]; k.CloseHandle.restype = W.BOOL
        k.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD)]
        k.QueryFullProcessImageNameW.restype = W.BOOL
        k.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]; k.WaitForSingleObject.restype = W.DWORD
        k.TerminateProcess.argtypes = [W.HANDLE, W.UINT]; k.TerminateProcess.restype = W.BOOL
        self.handle = k.OpenProcess(0x1000 | 0x100000 | 0x1, False, pid)
        if not self.handle:
            raise ValueError("Exact game process cannot be pinned; no process was stopped")
        try:
            self.validate()
        except Exception:
            self.close(); raise

    def validate(self):
        count = W.DWORD(32768); path = ctypes.create_unicode_buffer(count.value)
        _require(bool(self.kernel.QueryFullProcessImageNameW(self.handle, 0, path, ctypes.byref(count)))
            and _same_path(path.value, self.expected_path), "Pinned process image differs from the selected game")
        _require(self.kernel.WaitForSingleObject(self.handle, 0) == 0x102,
            "Pinned game process ended during maintenance preflight")

    def stop(self):
        self.validate()
        _require(bool(self.kernel.TerminateProcess(self.handle, 0)), "Exact game termination failed")
        _require(self.kernel.WaitForSingleObject(self.handle, 30000) == 0,
            "Exact game termination is unconfirmed; no replacement was started")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle); self.handle = None

    def __enter__(self): return self
    def __exit__(self, *_): self.close()


class _LaunchPlatform:
    def __init__(self, platform, filetime_clock):
        self.platform, self.launched_pid = platform, None
        self.filetime_clock = filetime_clock
        self.witness = None
        self.lower_filetime = self.upper_filetime = None

    def __getattr__(self, name): return getattr(self.platform, name)

    def launch(self, executable, arguments, directory, elevate):
        # This call inherits the worker's token. Never request a nested UAC.
        self.lower_filetime = self.filetime_clock()
        self.witness = self.platform.launch_tracked(executable, arguments, directory, False)
        self.upper_filetime = self.filetime_clock()
        self.launched_pid = self.witness.pid
        return self.launched_pid


class PublicPhysicalExecutor:
    def __init__(self, root, *, platform=None, launcher_factory=None, configuration_reader=None, process_factory=None,
                 filetime_clock=None):
        from .gui_setup.launcher_windows import WindowsPlatform
        from .gui_setup.launcher import DirectLauncher
        self._configuration_reader = configuration_reader or public_configuration
        self.config = self._configuration_reader(root)
        self.platform = platform or WindowsPlatform()
        self.launcher_factory = launcher_factory or DirectLauncher
        self.process_factory = process_factory or ExactGameProcess
        self.filetime_clock = filetime_clock or (lambda: time.time_ns() // 100 + 116444736000000000)
        self._launches = {}

    def validate_unchanged(self):
        current = self._configuration_reader(self.config["root"])
        for name in ("root", "game", "executable", "bridge", "state", "manifest_sha256"):
            _require(current[name] == self.config[name], "Public maintenance application or game location changed")

    def pin_game(self, pid):
        self.validate_unchanged()
        return self.process_factory(pid, self.config["executable"])

    def _closed(self):
        _require(self.platform.processes("gakumas.exe") == [], "A game process exists; no additional process was started")

    def release_launch(self, job_dir):
        owned = self._launches.pop(str(Path(job_dir).resolve()), None)
        if owned is not None:
            owned["witness"].close()

    def _process_identity(self, value):
        _require(isinstance(value, dict) and type(value.get("pid")) is int and value["pid"] > 0,
            "Game launch process identity is unconfirmed; do not retry")
        _require(value.get("path") and _same_path(value["path"], self.config["executable"]),
            "New game process path is unconfirmed; do not retry")
        _require(type(value.get("parent_pid")) is int and value["parent_pid"] >= 0
            and type(value.get("created_filetime")) is int and value["created_filetime"] > 0
            and type(value.get("exit_filetime")) is int and value["exit_filetime"] >= 0
            and type(value.get("alive")) is bool
            and ((value["alive"] and value["exit_filetime"] == 0)
                 or (not value["alive"] and value["exit_filetime"] >= value["created_filetime"])),
            "Game process lifetime is unconfirmed; do not retry")
        return dict(value)

    def validate_native_launch(self, job_dir, bootstrap, native_pid, *, previous=None):
        """Bind readiness to the held launch handle or its sole direct child.

        A path/name match alone is never sufficient. A child's birth must fall
        within the exact original process's lifetime, preventing PID reuse from
        turning a later unrelated process into a valid descendant.
        """
        self.validate_unchanged()
        job = Path(job_dir).resolve()
        owned = self._launches.get(str(job))
        _require(owned is not None and owned["receipt"] == bootstrap,
            "Original launch process handle or receipt is unavailable; do not relaunch")
        _require(type(native_pid) is int and native_pid > 0, "Native process PID is unverified")
        original = bootstrap["bootstrap_identity"]
        parent = self._process_identity(owned["witness"].snapshot())
        _require(all(parent[key] == original[key] for key in ("pid", "parent_pid", "path", "created_filetime")),
            "Original launch process identity changed")
        now = self.filetime_clock()
        if native_pid == parent["pid"]:
            _require(parent["alive"], "Original launched process exited before native readiness")
            current, kind = parent, "original-launched-process"
        else:
            _require(not parent["alive"] and parent["exit_filetime"] <= now,
                "Bootstrap must have a confirmed exit before accepting its child")
            children = []
            seen = set()
            for row in self.platform.processes("gakumas.exe"):
                pid = row.get("pid")
                _require(type(pid) is int and pid > 0 and pid not in seen, "Game process inventory is ambiguous")
                seen.add(pid)
                observed = self._process_identity(self.platform.process_identity(pid))
                _require(observed["pid"] == pid, "Game process inventory identity changed")
                if not observed["alive"]:
                    continue
                _require(observed["parent_pid"] == parent["pid"]
                    and bootstrap["launch_filetime_lower"] <= parent["created_filetime"]
                    <= observed["created_filetime"] <= parent["exit_filetime"] <= now,
                    "New game process does not belong to this exact bootstrap lifetime")
                children.append(observed)
            _require(len(children) == 1 and children[0]["pid"] == native_pid,
                "Native PID is not the unique verified direct child of this launch")
            current, kind = children[0], "verified-direct-child"
        if previous is not None:
            _require(previous.get("kind") == kind and previous.get("process_identity") == current,
                "Native process changed while its generation and snapshot were being verified")
        return {"schema": "gkms.public-launch-process-binding.v1", "kind": kind,
            "job_id": job.name, "process_identity": current, "bootstrap_identity": parent,
            "launch_filetime_lower": bootstrap["launch_filetime_lower"],
            "launch_filetime_upper": bootstrap["launch_filetime_upper"],
            "original_launch_handle_retained": True, "input_submitted": False}

    def record_verified_launch(self, job_dir, bootstrap, binding):
        from .native_maintenance import write_json
        job = Path(job_dir).resolve()
        source = job / "bootstrap_launch.json"
        _require(not (job / "launch.json").exists(), "Verified launch receipt already exists")
        _require(json.loads(source.read_bytes()) == bootstrap, "Bootstrap launch evidence changed")
        result = {**bootstrap, "schema": "gkms.gakumas-direct-cached-launch.v1",
            "game_pid": binding["process_identity"]["pid"], "game_path": binding["process_identity"]["path"],
            "bootstrap_pid": bootstrap["bootstrap_identity"]["pid"], "process_binding": binding,
            "bootstrap_reference": {"path": str(source), "sha256": digest(source)}}
        write_json(job / "launch.json", result)
        return result

    def execute(self, action, job_dir, *, pinned_process=None):
        from .native_maintenance import write_json
        self.validate_unchanged()
        job = Path(job_dir).resolve()
        sessions = self.config["state"] / "native_maintenance/sessions"
        _require(job.parent.name == "jobs" and job.parents[2] == sessions.resolve()
            and re.fullmatch("[a-f0-9]{32}", job.name) and re.fullmatch("[a-f0-9]{32}", job.parents[1].name),
            "Public job is outside its exact maintenance session/request role")
        _require(not any((job / name).exists() for name in ("executor_result.json", "launch.json", "bootstrap_launch.json")),
            "Public physical job already has an outcome; do not execute it again")
        operation = action["operation"]
        _require(operation in {"start-game", "stop-game", "restart-game", "reload-dll", "stage-dll"}, "Unsupported public physical operation")
        installed = self.config["bridge"]
        _require(digest(installed) == action["installed_sha256"], "Installed bridge changed before the physical operation")
        candidate_bytes = None
        backup = job / "previous_bridge.dll"
        if operation in {"reload-dll", "stage-dll"}:
            candidate = job / "candidate.dll"
            _require(Path(action["candidate_path"]).resolve() == candidate and candidate.is_file(),
                "Staged public bridge path differs")
            candidate_bytes = candidate.read_bytes()
            _require(hashlib.sha256(candidate_bytes).hexdigest() == action["candidate_sha256"],
                "Staged public bridge identity differs")
            _require(not backup.exists(), "Maintenance backup already exists; do not repeat a physical job")
        if operation in {"restart-game", "reload-dll", "stop-game"}:
            _require(pinned_process is not None, "A pinned exact game process is required")
            pinned_process.stop()
        else:
            _require(pinned_process is None, "Unexpected game process pin")
        self._closed()
        if operation in {"reload-dll", "stage-dll"}:
            _require(digest(installed) == action["installed_sha256"], "Installed bridge changed while stopping the game")
            shutil.copyfile(installed, backup)
            _require(digest(backup) == action["installed_sha256"], "Public bridge backup is incomplete")
            temporary = installed.with_name("bridge-swap-" + uuid.uuid4().hex + ".tmp")
            try:
                temporary.write_bytes(candidate_bytes)
                _require(digest(temporary) == action["candidate_sha256"], "Copied public bridge hash differs")
                self._closed()
                os.replace(temporary, installed)
            finally:
                temporary.unlink(missing_ok=True)
        if operation in {"stage-dll", "stop-game"}:
            self._closed()
        else:
            launch_platform = _LaunchPlatform(self.platform, self.filetime_clock)
            retained = False
            launcher = self.launcher_factory(platform=launch_platform, monitor_thread=False,
                log_root=job / "launcher")
            try:
                result = launcher.command("launch", "maintenance-" + job.name, directory=str(self.config["game"]))
                _require(result.get("ok") is True and result.get("submitted") is True
                    and result.get("alreadyRunning") is False, "Python DirectLauncher did not submit a fresh game start")
                pid = launch_platform.launched_pid
                _require(type(pid) is int and pid > 0, "Game launch process identity is unconfirmed; do not retry")
                identity = self._process_identity(launch_platform.witness.snapshot())
                _require(identity["pid"] == pid
                    and launch_platform.lower_filetime <= identity["created_filetime"] <= launch_platform.upper_filetime,
                    "Original game process birth is outside this launch request")
                receipt = {"schema": "gkms.gakumas-direct-cached-bootstrap.v1", "status": "started",
                    "game_pid": pid, "game_path": identity["path"], "already_running": False,
                    "bootstrap_identity": identity, "launch_filetime_lower": launch_platform.lower_filetime,
                    "launch_filetime_upper": launch_platform.upper_filetime,
                    "launcher_core": "existing-Python-DirectLauncher", "nested_UAC_requested": False}
                write_json(job / "bootstrap_launch.json", receipt)
                self._launches[str(job)] = {"witness": launch_platform.witness, "receipt": receipt}
                retained = True
            finally:
                if launch_platform.witness is not None and not retained:
                    launch_platform.witness.close()
                launcher.close()
        result = {"schema": "gkms.native-maintenance-executor.v1", "operation": operation,
            "installed_sha256": digest(installed), "uac_requested": False, "game_started": operation not in {"stage-dll", "stop-game"},
            "inspection_only": False, "executor": "public-fixed-python-lifecycle"}
        write_json(job / "executor_result.json", result)
        return result


def validate_public_worker_identity():
    from .application_paths import app_root
    from .gui_setup.app_updates import ENTRYPOINT
    _require(os.name == "nt" and getattr(sys, "frozen", False), "Public maintenance requires its fixed packaged executable")
    config = public_configuration(app_root())
    _require(Path(sys.executable).resolve() == config["root"] / ENTRYPOINT,
        "Public maintenance worker executable differs from its verified slot")
    if not ctypes.windll.shell32.IsUserAnAdmin():
        raise PermissionError("Public maintenance worker must inherit the explicitly approved UAC token")
    return config


def public_worker_entry():
    validate_public_worker_identity()
    from .native_maintenance import serve
    serve()
    return 0


def prepare_public_bridge_candidate(root):
    """Stage only the selected slot's fixed bridge role; no elevated operation."""
    from .gui_setup.packages import control_package
    config = public_configuration(root)
    package = control_package((config["root"] / "assets/control/gkms-control.zip").read_bytes(),
        digest(config["game"] / "GameAssembly.dll"))
    _require(package.metadata.get("buildProfile") == "public", "Only the formal public component package is eligible")
    raw = package.files[BRIDGE_ROLE]
    expected = hashlib.sha256(raw).hexdigest()
    target = config["state"] / "native_maintenance/candidates" / expected / "gkms_runtime_command_bridge.dll"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        temporary = target.with_suffix("." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_bytes(raw)
            _require(digest(temporary) == expected, "Public bridge staging hash differs")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    verify_public_candidate(target, expected, root=root)
    return {"candidate_path": str(target), "candidate_sha256": expected,
        "role": "command-bridge", "game_IO": False, "elevation_requested": False}


def _maintenance_mutex_active():
    _require(os.name == "nt", "Windows maintenance mutex inspection is required")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenMutexW.argtypes = [W.DWORD, W.BOOL, W.LPCWSTR]; kernel.OpenMutexW.restype = W.HANDLE
    kernel.CloseHandle.argtypes = [W.HANDLE]; kernel.CloseHandle.restype = W.BOOL
    handle = kernel.OpenMutexW(0x100000, False, r"Local\gkms_tool.native_maintenance.service.v1")
    if handle:
        kernel.CloseHandle(handle); return True
    _require(ctypes.get_last_error() == 2, "Maintenance owner mutex is unreadable")
    return False


def _ensure_public_helper_locked(request_id, *, root=None, status_client=None, process_alive=None,
                                platform=None, mutex_active=None, configuration_reader=None):
    """Explicit GUI action: reuse a live owner or request exactly one fixed UAC.

    A timeout is not a stopped process. Cancelled or uncertain launch IDs never
    cause a second prompt; a fresh user action must supply a new request ID.
    """
    from .application_paths import app_root
    from .gui_setup.app_updates import ENTRYPOINT
    from .gui_setup.launcher_guard import _process_alive
    from .gui_setup.launcher_windows import WindowsPlatform, NativeError
    from . import native_maintenance as maintenance
    _require(isinstance(request_id, str) and re.fullmatch("[a-f0-9]{32}", request_id), "Fixed helper request ID required")
    config = (configuration_reader or public_configuration)(app_root() if root is None else root)
    maintenance.check_pending(root=config["root"])
    ipc = config["state"] / "native_maintenance"
    current = ipc / "session.json"
    if current.is_file():
        session = maintenance.read_json(current)
        _require(session.get("schema") == maintenance.SCHEMA and type(session.get("pid")) is int
            and isinstance(session.get("session_id"), str), "Existing maintenance identity is unreadable")
        try:
            response = (status_client or (lambda: maintenance.submit("status", timeout=2)))()
        except Exception:
            response = None
        result = response.get("result", {}) if isinstance(response, dict) else {}
        if (isinstance(response, dict) and response.get("status") == "completed" and result.get("elevated") is True
                and result.get("pid") == session["pid"] and result.get("session_id") == session["session_id"]):
            _require(result.get("executor_profile") == "public-fixed-python"
                and _same_path(result.get("app_root", ""), config["root"])
                and _same_path(result.get("game_path", ""), config["executable"]),
                "Existing elevated helper belongs to another slot or game configuration")
            return {"status": "ready", "reused": True, "helper_pid": result["pid"],
                "session_id": result["session_id"], "active_job": result.get("active_job"), "uac_requested": False}
        _require((process_alive or _process_alive)(session["pid"]) is False,
            "Existing helper is not confirmed stopped; preserve its owner and wait")
    launch_file = ipc / "helper_launches" / (request_id + ".json")
    if launch_file.exists():
        previous = maintenance.read_json(launch_file)
        _require(previous.get("schema") == "gkms.public-helper-launch.v1" and previous.get("request_id") == request_id,
            "Original helper launch identity is invalid")
        pid = previous.get("helper_pid")
        if previous.get("status") == "pending" and type(pid) is int and (process_alive or _process_alive)(pid) is False:
            previous.update(status="failed", error_code="HELPER_EXITED_BEFORE_SESSION")
            maintenance.write_json(launch_file, previous)
        return previous
    _require((mutex_active or _maintenance_mutex_active)() is False,
        "Another maintenance owner exists; do not start a competing helper")
    executable = config["root"] / ENTRYPOINT
    _require(executable.is_file(), "Fixed public maintenance executable is unavailable")
    result = {"schema": "gkms.public-helper-launch.v1", "request_id": request_id,
        "status": "unknown", "uac_requested": True, "helper_pid": None,
        "retry_policy": "check this original request; a cancelled prompt requires a new explicit user action"}
    maintenance.write_json(launch_file, result)
    try:
        pid = (platform or WindowsPlatform()).launch(str(executable), "--maintenance-worker", str(config["root"]), True)
        result.update(status="pending" if type(pid) is int and pid > 0 else "unknown", helper_pid=pid)
    except NativeError as error:
        result.update(status="cancelled" if error.code == "UAC_CANCELLED" else "unknown", error_code=error.code)
    except Exception:
        result.update(status="unknown", error_code="HELPER_LAUNCH_UNCONFIRMED")
    maintenance.write_json(launch_file, result)
    return result


def ensure_public_helper(request_id, *, root=None, status_client=None, process_alive=None,
                         platform=None, mutex_active=None, configuration_reader=None, claim_lock=None):
    """Serialize only helper bootstrap; never hold the game-input lock for UAC."""
    if claim_lock is None:
        from .controller_client import _serialized_controller_request
        claim_lock = lambda: _serialized_controller_request(2.0,
            mutex_name=r"Local\gkms_tool.native_maintenance.bootstrap.v1")
    with claim_lock():
        return _ensure_public_helper_locked(request_id, root=root, status_client=status_client,
            process_alive=process_alive, platform=platform, mutex_active=mutex_active,
            configuration_reader=configuration_reader)
