"""Shared public path setup; no GUI, model, game or native-maintenance imports."""
from __future__ import annotations

import os
from pathlib import Path


def configure_public_environment(slot, *, app_home=None, user_data=None, environment=None):
    environment = os.environ if environment is None else environment
    slot = Path(slot).resolve()
    if not (slot / 'gkms-app-package.json').is_file():
        raise ValueError('The public GUI requires its verified application package.')
    home = Path(app_home or environment.get('GKMS_APP_HOME') or slot.parent.parent).resolve()
    if slot.parent != home / 'versions':
        raise ValueError('The GUI version slot does not belong to its managed installation.')
    configured_user = user_data or environment.get('GKMS_USER_DATA_ROOT')
    if configured_user:
        user = Path(configured_user)
        if not user.is_absolute():
            raise ValueError('User data directory must be absolute.')
        user = user.resolve()
    else:
        base = environment.get('LOCALAPPDATA')
        if not base:
            raise ValueError('LOCALAPPDATA is required for the public GUI.')
        user = Path(base).resolve() / 'gkms-assistant'
    if user == slot or user.is_relative_to(slot):
        raise ValueError('User data must be outside the immutable application slot.')
    environment.update(GKMS_APP_ROOT=str(slot), GKMS_APP_HOME=str(home), GKMS_USER_DATA_ROOT=str(user),
                       GKMS_PORTABLE_MODEL_ASSETS=str(slot / 'assets/model_runtime'), GKMS_INPUT_BACKEND='dll',
                       PYTHONDONTWRITEBYTECODE='1')
    return {'app_root': slot, 'app_home': home, 'user_data_root': user}

