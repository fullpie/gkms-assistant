"""Application assets and mutable user state have separate roots.

Development keeps its historical var directory. A public version slot uses
LocalAppData (or an explicitly configured user directory), never its own slot
for credentials, runs, logs, updater state or IPC.
"""
from __future__ import annotations

import os
import json
from pathlib import Path
import re
import sys


def _absolute(value):
    path = Path(value)
    if not path.is_absolute():
        raise ValueError('Application paths must be absolute.')
    return path.resolve()


def app_root():
    configured = os.environ.get('GKMS_APP_ROOT')
    if configured is not None:
        return _absolute(configured)
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def public_installation(root=None):
    return ((Path(root) if root is not None else app_root()) / 'gkms-app-package.json').is_file()


def state_root(root=None):
    root = Path(root).resolve() if root is not None else app_root()
    configured = os.environ.get('GKMS_USER_DATA_ROOT')
    if configured is not None:
        return _absolute(configured)
    if public_installation(root):
        base = os.environ.get('LOCALAPPDATA')
        if not base:
            raise ValueError('A public Windows installation requires LOCALAPPDATA.')
        return _absolute(base) / 'gkms-assistant'
    return root / 'var'


def native_runtime_layout(root=None):
    """The installed DLL layout is independent of the GUI's release profile."""
    root = Path(root).resolve() if root is not None else app_root()
    configured = os.environ.get('GKMS_NATIVE_RUNTIME_LAYOUT')
    if configured is not None and configured not in {'development', 'public-installed'}:
        raise ValueError('Unknown native runtime layout.')
    if public_installation(root):
        if configured == 'development':
            raise ValueError('A public installation cannot use the development native layout.')
        return 'public-installed'
    return configured or 'development'


def native_bridge_path(root=None):
    root = Path(root).resolve() if root is not None else app_root()
    if native_runtime_layout(root) == 'public-installed':
        game = game_directory(root)
        if game is None:
            raise ValueError('Select the installed game directory before native maintenance.')
        return game / 'gkms/native/gkms_runtime_command_bridge.dll'
    return root / 'var/native/runtime_command_bridge/gkms_runtime_command_bridge.dll'


def native_state_root(root=None):
    """Match public native DLL IPC, independently of GUI record overrides.

    Development retains state_root's existing explicit override behavior.
    Public DLLs resolve LOCALAPPDATA themselves, so their mailbox and recorder
    cannot follow GKMS_USER_DATA_ROOT or a particular application version slot.
    """
    root = Path(root).resolve() if root is not None else app_root()
    if native_runtime_layout(root) == 'public-installed':
        base = os.environ.get('LOCALAPPDATA')
        if not base:
            raise ValueError('A public Windows installation requires LOCALAPPDATA.')
        return _absolute(base) / 'gkms-assistant'
    return state_root(root)


def model_assets_root(root=None):
    return asset_directory('model_runtime', root)


def asset_directory(role, root=None):
    """Resolve an explicit qualified runtime package without research fallback."""
    keys = {'model_runtime': 'GKMS_PORTABLE_MODEL_ASSETS',
            'outer_runtime': 'GKMS_PORTABLE_OUTER_ASSETS',
            'loadout': 'GKMS_PORTABLE_LOADOUT_ASSETS',
            'display_labels': 'GKMS_PORTABLE_DISPLAY_LABELS'}
    if role not in keys:
        raise ValueError('Unknown runtime asset role.')
    configured = os.environ.get(keys[role])
    if configured is not None:
        return _absolute(configured)
    root = Path(root).resolve() if root is not None else app_root()
    package = root / 'assets' / role
    # A damaged public install must not fall back to private research assets.
    return package if public_installation(root) or package.exists() else None


def master_database():
    package = model_assets_root()
    return package / 'master.sqlite3' if package is not None else app_root() / 'var/master.sqlite3'


def master_directory():
    """Public outer rules use their own pinned asset package, never research YAML.

    Resolve only here; missing public resources remain visible to the AP
    preflight rather than causing GUI import failures or development fallback.
    """
    package=asset_directory('outer_runtime')
    return package/'Master' if package is not None else app_root()/'_research/gakumasu-diff'


def weekly_behavior_provider():
    package=asset_directory('outer_runtime')
    return package/'weekly_provider.json' if package is not None else app_root()/'var/models/outer_behavior_providers/weekly_v1_current_scope.json'


def game_directory(root=None):
    """Read explicit setup state; development may retain its original launcher."""
    root = Path(root).resolve() if root is not None else app_root()
    configured = os.environ.get('GKMS_GAME_DIRECTORY')
    if configured is not None:
        return _absolute(configured)
    installed_layout = native_runtime_layout(root) == 'public-installed'
    location_root = native_state_root(root) if installed_layout else state_root(root)
    config = location_root / 'gui_setup/game-location.json'
    if config.is_file():
        if config.stat().st_size > 16384:
            raise ValueError('Game location configuration is too large.')
        value = json.loads(config.read_text(encoding='utf-8'))
        if value.get('schema') != 'gkms.gui-game-location.v1' or not isinstance(value.get('game_directory'), str):
            raise ValueError('Game location configuration is invalid.')
        return _absolute(value['game_directory'])
    if not installed_layout:
        script = root / 'scripts/launch_gakumas_direct_cached.ps1'
        if script.is_file() and script.stat().st_size <= 128 * 1024:
            matches = re.findall(r"(?m)^\s*\$expectedGamePath\s*=\s*'([^'\r\n]+)'\s*$", script.read_text(encoding='utf-8-sig'))
            if len(matches) == 1:
                return _absolute(matches[0]).parent
    return None


def game_file(relative):
    """Optional legacy resource readers see missing resources before setup.

    The placeholder is never a guessed game location or launch authority;
    actual launch/install paths must be explicitly selected and validated.
    """
    return (game_directory() or state_root() / 'unconfigured-game') / relative
