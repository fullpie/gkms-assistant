"""V12 status and explicit integrated launcher for the existing project install.

The project still owns its deployed DLL, launch helper, mailbox and execution
preflight. File presence is reported separately from native/model readiness.
The supplied launcher source core is owner-queued and shares the native input
claim. Installation and package updates remain disabled for unmanaged DLLs.
"""
from __future__ import annotations

from contextlib import contextmanager
from collections import OrderedDict
import hashlib
import json
import os
from pathlib import Path
import re
import threading

from .packages import SetupError
from .project_session_results import ProjectSessionResults
from .launcher_guard import guard_project_launcher, local_blocker, runtime_claim


class ProjectSetupService:
    READ_ACTIONS = frozenset({"status", "launcher-status", "setup-preferences"})

    LAUNCHER_ACTIONS = frozenset({"launcher-launch", "launcher-sync", "launcher-repair"})

    def __init__(self, project_root: Path, *, inspection: bool = False,
                 launcher_factory=None, claim_factory=runtime_claim,
                 mutation_guard=guard_project_launcher, enable_installation=False,
                 data_dir=None, picker=None, game_running=None, public_build=False,
                 enable_developer_tools=False):
        self.root = Path(project_root).resolve()
        self.inspection = bool(inspection)
        self.public_build = bool(public_build)
        self.lock = threading.Lock()
        self.closing = False
        self.session = ProjectSessionResults()
        self.last_directory = ""
        self._owner_thread = threading.get_ident()
        self._launcher_factory = launcher_factory
        self._launcher = None
        self._launcher_lock = threading.Lock()
        self._claim_factory = claim_factory
        self._mutation_guard = mutation_guard
        self._operations = OrderedDict()
        self.app_updates = None
        self.loadout = None
        self.developer = None
        self.developer_error = None
        if enable_developer_tools and not self.public_build:
            from .developer_addon import load_developer_addon
            try:
                self.developer = load_developer_addon(self.root, public_build=self.public_build)
            except Exception as error:
                # An optional addon cannot take away ordinary cultivation or
                # setup. Its routes stay absent and the GUI reports the cause.
                self.developer_error = f'{type(error).__name__}: {error}'[:500]
        self.enable_installation = bool(enable_installation)
        from ..application_paths import state_root
        self.data_dir = Path(data_dir or state_root(self.root) / 'gui_setup')
        from .preferences import SetupPreferences
        self.preferences = SetupPreferences(self.data_dir)
        from ..application_paths import native_state_root, native_runtime_layout
        self.game_location_path = ((native_state_root(self.root) / 'gui_setup')
                                   if native_runtime_layout(self.root) == 'public-installed' else self.data_dir) / 'game-location.json'
        self.installer = None
        if self.enable_installation:
            from .managed_installation import ManagedInstallation
            self.installer = ManagedInstallation(self, data_dir=self.data_dir, picker=picker, running=game_running)

    @property
    def developer_actions(self):
        return frozenset() if self.developer is None else frozenset(self.developer.actions)

    @property
    def developer_read_actions(self):
        return frozenset() if self.developer is None else frozenset(self.developer.read_actions)

    @property
    def launcher(self):
        with self._launcher_lock:
            if self._launcher is None:
                if self._launcher_factory is None:
                    from .launcher import DirectLauncher
                    self._launcher = DirectLauncher(log_root=self.root / "var/gui_launcher/events")
                else:
                    self._launcher = self._launcher_factory()
            return self._launcher

    def _configured_game(self):
        """Read the existing fixed launcher contract without running it."""
        config = self.game_location_path
        if self.enable_installation and config.is_file():
            if config.stat().st_size > 16384:
                raise SetupError('GAME_LOCATION_INVALID')
            value = json.loads(config.read_text(encoding='utf-8'))
            if value.get('schema') != 'gkms.gui-game-location.v1' or not isinstance(value.get('game_directory'), str):
                raise SetupError('GAME_LOCATION_INVALID')
            return Path(value['game_directory']) / 'gakumas.exe'
        from ..application_paths import native_runtime_layout
        if native_runtime_layout(self.root) == 'public-installed':
            return None
        script = self.root / "scripts/launch_gakumas_direct_cached.ps1"
        if not script.is_file() or script.stat().st_size > 128 * 1024:
            return None
        text = script.read_text(encoding="utf-8-sig")
        values = re.findall(r"(?m)^\s*\$expectedGamePath\s*=\s*'([^'\r\n]+)'\s*$", text)
        return Path(values[0]) if len(values) == 1 else None

    def remember_game_directory(self, directory):
        """Persist only a target selected by an explicit, owner-guarded action."""
        if threading.get_ident() != self._owner_thread or self.inspection or self.closing:
            raise SetupError('Game location changes require an active GUI owner.')
        from .transactions import atomic_write
        atomic_write(self.game_location_path, json.dumps(
            {'schema': 'gkms.gui-game-location.v1', 'game_directory': str(Path(directory).resolve())},
            ensure_ascii=False).encode('utf-8'))
        self.last_directory = str(Path(directory).resolve())

    def _directory(self, requested):
        if not isinstance(requested, str):
            raise SetupError("遊戲路徑必須是文字。")
        game = self._configured_game()
        configured = str(game.parent) if game is not None else ""
        if self.enable_installation and requested:
            store = self.installer.store(requested)
            configured = str(store.root)
            game = store.root / 'gakumas.exe'
            self.last_directory = configured
            return game, configured
        if requested and (not configured or Path(requested).resolve() != Path(configured).resolve()):
            raise SetupError("目前沿用專案原啟動器的固定遊戲位置，尚未啟用可攜式安裝；不能在此改指另一份遊戲。")
        self.last_directory = configured
        return game, configured

    def status(self, folder=""):
        game, directory = self._directory(folder)
        if self.enable_installation:
            if directory:
                result = self.installer.status(directory)
            else:
                result = {'schema': 'gkms.installer-status.v1', 'revision': 'unselected',
                    'integration_mode': 'managed', 'project_frontend_ready': True,
                    'gameDirectory': '', 'safeToModify': False, 'operationPending': False,
                    'platformSupported': os.name == 'nt', 'readOnly': self.inspection,
                    'capabilities': ['install_translation', 'install_control', 'restore_all'],
                    'modules': {name: {'known': False, 'installed': None} for name in ('control', 'translation', 'launcher')},
                          'files': [], 'conflicts': []}
            try:
                result['bundledControl'] = self.installer.bundled_candidate()
            except (OSError, ValueError, KeyError, SetupError) as error:
                result['bundledControl'] = None
                result['bundledControlError'] = str(error)
            return result
        bridge = self.root / "var/native/runtime_command_bridge/gkms_runtime_command_bridge.dll"
        loader = None if game is None else game.parent / "version.dll"
        observed = {"game_executable_present": game is not None and game.is_file(),
                    "loader_present": loader is not None and loader.is_file(),
                    "project_bridge_present": bridge.is_file(),
                    "launcher_contract_found": game is not None}
        revision = hashlib.sha256(json.dumps(observed, sort_keys=True).encode()).hexdigest()
        return {"schema": "gkms.existing-project-setup.v1", "revision": revision,
                "integration_mode": "existing-project", "project_frontend_ready": True,
                "gameDirectory": directory, "managed_installation": False,
                "native_execution_checked": False, "model_readiness_checked": False,
                "safeToModify": False, "operationPending": False,
                "platformSupported": os.name == "nt", "readOnly": self.inspection,
                "capabilities": ["existing_project_status", "integrated_launcher"], "files": [],
                "modules": {name: {"known": False, "installed": None,
                                    "managed": False, "version": None}
                            for name in ("control", "translation", "launcher")},
                "observedArtifacts": observed,
                "note": "沿用現有專案 DLL、資料傳輸路徑與原啟動器；不是安裝器納管版本。開始培育仍由原控制器核對 DLL、模型及未決交易。",
                "disabled_reason": "目前尚未完成公眾版可攜式 DLL 封裝；安裝與還原暫不開放。"}

    def launcher_status(self, folder=""):
        game, directory = self._directory(folder)
        if game is not None:
            snapshot = self.launcher.snapshot(directory)
            blocked = local_blocker(self.root)
            busy = self.lock.locked() or snapshot.get("state") in {"starting", "monitoring", "syncing"}
            available = (not self.inspection and not self.closing and not blocked and not busy
                         and snapshot.get("platformSupported") is True)
            snapshot.update(integration_mode="existing-project", readOnly=self.inspection,
                            operationBusy=busy, projectPending=blocked is not None,
                            blockedReason=blocked, maintenance_guard_checked=False,
                            canLaunch=available and snapshot.get("canLaunch") is True,
                            canSync=available and bool(directory), canRepair=available,
                            launcher_source="provided-v12-direct-launcher-core")
            return snapshot
        return {"schema": "gkms.launcher-status.v1", "integrated": True,
                "integration_mode": "existing-project", "platformSupported": os.name == "nt",
                "state": "existing-maintenance", "gamePath": "" if game is None else str(game),
                "gameDirectory": directory, "gamePathExists": game is not None and game.is_file(),
                "gamePathSource": "existing-project-launcher-contract", "gameVersion": None,
                "readOnly": True, "operationBusy": self.lock.locked(),
                "canLaunch": False, "canSync": False, "canRepair": False,
                "reason": "尚未找到專案已核對的遊戲位置；不會猜測位置或啟動另一份遊戲。"}

    def _launcher_command(self, action, data):
        if threading.get_ident() != self._owner_thread:
            raise SetupError("Launcher commands require the GUI owner thread.")
        if self.inspection:
            raise SetupError("READ_ONLY")
        if self.closing:
            raise SetupError("ASSISTANT_CLOSING")
        if set(data) - {"operationId", "gameDirectory", "closeDmm", "forceCloseDmm"}:
            raise SetupError("INVALID_LAUNCHER_FIELDS")
        operation_id = data.get("operationId")
        if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", operation_id):
            raise SetupError("REQUEST_ID_REQUIRED")
        close_dmm, force_close = data.get("closeDmm", False), data.get("forceCloseDmm", False)
        if (type(close_dmm) is not bool or type(force_close) is not bool or force_close and not close_dmm
                or action != "launcher-sync" and (close_dmm or force_close)):
            raise SetupError("INVALID_CLOSE_OPTIONS")
        _, directory = self._directory(data.get("gameDirectory", ""))
        if not directory:
            raise SetupError("NO_GAME_LOCATION")
        key = (action, directory, close_dmm, force_close)
        if operation_id in self._operations:
            old_key, result = self._operations[operation_id]
            if key != old_key:
                raise SetupError("REQUEST_ID_REUSED")
            return {**result, "snapshot": self.launcher_status(directory), "replayed": True}
        with self.setup_guard():
            with self._claim_factory():
                self._mutation_guard(self.root)
                self._operations[operation_id] = (key, {"ok": False, "operationId": operation_id,
                                                        "errorCode": "RESULT_UNKNOWN"})
                # The supplied core reserves the same ID before native effects.
                # Its monitor thread only observes processes after dispatch.
                result = self.launcher.command(action.removeprefix("launcher-"), operation_id,
                                               directory, close_dmm=close_dmm, force_close=force_close)
                receipt = {key: value for key, value in result.items() if key != "snapshot"}
                self._operations[operation_id] = (key, receipt)
                while len(self._operations) > 256:
                    self._operations.popitem(last=False)
        return {**receipt, "snapshot": self.launcher_status(directory)}

    def command(self, action, data):
        if not isinstance(data, dict):
            raise SetupError("設定請求必須是 object。")
        folder = data.get("gameDirectory", "")
        if action == 'setup-preferences':
            if data: raise SetupError('Setup preference reads take no fields')
            return self.preferences.read(read_only=self.inspection)
        if action == 'save-setup-preferences':
            if self.inspection or self.closing:
                raise SetupError('READ_ONLY' if self.inspection else 'ASSISTANT_CLOSING')
            if threading.get_ident() != self._owner_thread:
                raise SetupError('Preference writes require the GUI owner thread')
            if set(data)!={'operationId','patch'}: raise SetupError('Unsupported setup preference request')
            return self.preferences.save(data['operationId'],data['patch'])
        if self.loadout is not None and action in self.loadout.actions:
            if self.inspection and action not in self.loadout.read_actions:
                raise SetupError('READ_ONLY')
            return self.loadout.command(action, data)
        if action in self.developer_actions:
            if self.inspection and action not in self.developer_read_actions:
                raise SetupError('READ_ONLY')
            return {'ok': True, 'result': self.developer.command(action, data)}
        if action.startswith('app-update-'):
            if self.app_updates is None:
                raise SetupError('GUI_UPDATE_SERVICE_UNAVAILABLE')
            if self.inspection and action != 'app-update-status':
                raise SetupError('READ_ONLY')
            return self.app_updates.command(action, data)
        if self.enable_installation:
            from .managed_installation import INSTALL_ACTIONS
            if action in INSTALL_ACTIONS:
                return self.installer.command(action, data)
            if action == 'release':
                from .releases import latest
                return latest(data.get('url', ''))
        if action == "status":
            return self.status(folder)
        if action == "launcher-status":
            return self.launcher_status(folder)
        if action == "detect":
            if self.enable_installation:
                if threading.get_ident() != self._owner_thread:
                    raise SetupError('Game detection/settings require the GUI owner thread.')
                if self.inspection or self.closing:
                    raise SetupError('READ_ONLY' if self.inspection else 'ASSISTANT_CLOSING')
                if self.lock.locked():
                    raise SetupError('SETUP_OPERATION_ACTIVE')
                self._mutation_guard(self.root)
                detected = self.launcher.snapshot('')
                if detected.get('gamePathExists') is not True:
                    game = self._configured_game()
                    if game is None or not game.is_file():
                        raise SetupError('NO_GAME_LOCATION')
                else:
                    game = Path(detected['gamePath'])
                status = self.status(str(game.parent))
                self.remember_game_directory(status['gameDirectory'])
                return {'ok': True, 'gameDirectory': status['gameDirectory'],
                        'source': detected.get('gamePathSource'), 'snapshot': status,
                        'launcherSnapshot': self.launcher_status(str(game.parent))}
            status = self.status("")
            return {"ok": True, "gameDirectory": status["gameDirectory"],
                    "source": "existing-project-launcher-contract", "snapshot": status,
                    "launcherSnapshot": self.launcher_status("")}
        if action in self.LAUNCHER_ACTIONS:
            return self._launcher_command(action, data)
        raise SetupError("現有專案模式未啟用這項安裝／啟動器操作。請沿用原啟動器與維護程序；不會覆寫現有 DLL 或另啟 DMM。")

    @contextmanager
    def setup_guard(self):
        if self.closing:
            raise SetupError("主程式正在安全結束。")
        if not self.lock.acquire(blocking=False):
            raise SetupError("請等待目前設定操作完成。")
        try:
            yield
        finally:
            self.lock.release()

    def close(self):
        self.closing = True
        if self.developer is not None:
            self.developer.close()
        if self.app_updates is not None:
            self.app_updates.close()
        if self.installer is not None:
            self.installer.close()
        if self._launcher is not None:
            self._launcher.close()
