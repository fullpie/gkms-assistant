"""The owned Live loading button and its original native WaitPress completion."""
from __future__ import annotations

from collections.abc import Mapping
import re

from .runtime_command_client import RuntimeCommandError

ACTION = 'live.loading_continue'
SCHEMA = 'gkms.live-loading-observation.v1'


def loading_target(raw):
    if raw.get('screen_type') != 'LiveScenePresenter':
        return None
    actions = raw.get('legal_actions', [])
    choices = [a for a in actions if a.get('action_id') == ACTION]
    if not choices:
        return None
    ui = raw.get('ui_state') or {}
    observed = ui.get('loading') or {}
    if (len(actions) != 1 or len(choices) != 1 or raw.get('busy') is not False
            or raw.get('actions_complete') is not True or ui.get('phase') != 'before_live'
            or observed.get('schema') != SCHEMA or observed.get('ready') is not True
            or observed.get('owner_bound') is not True or observed.get('read_errors') != []
            or observed.get('loading_class') != 'Campus.InGame.LiveLoading'
            or observed.get('loading_active') is not True or observed.get('hiding') is not False
            or observed.get('now_loading_active') is not False or observed.get('tap_to_start_active') is not True
            or observed.get('no_active_layer') is not True
            or (observed.get('pointer_blocking') or {}).get('input_ready') is not True
            or (raw.get('pointer_blocking') or {}).get('input_ready') is not True):
        raise RuntimeCommandError('native Live loading has no qualified current pressed waiter')
    target = choices[0].get('target')
    progress = raw.get('progress') or {}
    if (not isinstance(target, Mapping) or target.get('action_id') != ACTION
            or target != observed.get('target')
            or any(not target.get(key) or target[key] != progress.get(source) for key, source in
                   (('produce_id', 'produceId'), ('idol_card_id', 'idolCardId'), ('character_id', 'characterId')))
            or not re.fullmatch('[a-f0-9]{64}', str(target.get('scope_fingerprint', '')))):
        raise RuntimeCommandError('native Live loading target differs from its current produce')
    for key in ('live_presenter_id', 'fixed_id', 'manager_id', 'loading_id', 'param_id', 'manager_param_id'):
        if not isinstance(target.get(key), str) or target[key] in ('', '0x0') or target[key] != observed.get(key):
            raise RuntimeCommandError('native Live loading owner differs')
    button = observed.get('button') or {}
    if (any(button.get(key) is not True for key in ('active', 'enabled', 'has_callback'))
            or button.get('disabled') is not False
            or any(not target.get(key) or target[key] == '0x0' or target[key] != button.get(key)
                   for key in ('button_id', 'pressed_callback_id'))):
        raise RuntimeCommandError('native Live loading pressed button differs')
    for name in ('root', 'button_root'):
        canvas = (observed.get('canvas') or {}).get(name) or {}
        if any(canvas.get(key) is not True for key in ('active', 'interactable', 'blocks_raycasts')):
            raise RuntimeCommandError('native Live loading canvas is not actionable')
    waiters = observed.get('waiters')
    if (not isinstance(waiters, list) or len(waiters) != 1 or not isinstance(waiters[0], Mapping)
            or type(waiters[0].get('status')) is not int or waiters[0]['status'] != 0
            or any(not target.get(key) or target[key] == '0x0' or target[key] != waiters[0].get(key)
                   for key in ('closure_id', 'source_id'))):
        raise RuntimeCommandError('native Live loading has no unique original Pending WaitPress source')
    return target


def loading_press_receipt(before, after, target):
    if target.get('action_id') != ACTION:
        return None
    if loading_target(before) != target:
        raise RuntimeCommandError('pending loading press differs from its original qualified target')
    witness = after.get('live_loading_receipt')
    if (not isinstance(witness, Mapping)
            or witness.get('target') != target or witness.get('handler_submitted') is not True
            or witness.get('handler_returned') is not True or witness.get('error') not in (None, '')
            or after.get('surface') == 'error'
            or any((after.get('progress') or {}).get(key) != (before.get('progress') or {}).get(key)
                   for key in ('produceId', 'idolCardId', 'characterId'))):
        return None
    status = witness.get('status')
    if type(status) is not int or status not in (1, 2, 3):
        return None
    return {'source': 'native-LiveLoading-WaitPress', 'status': 'settled' if status == 1 else 'failed',
            'completion': 'original pressed waiter succeeded' if status == 1 else 'original pressed waiter faulted or cancelled',
            'target': dict(target), 'native_waiter_status': status,
            'cultivation_complete': False, 'server_transaction_claimed': False}
