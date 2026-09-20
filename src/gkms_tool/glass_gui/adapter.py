"""Glass presentation adapter over the current GkmsApp control owner.

All methods below must run on Tk's owner thread. The HTTP server never accesses
Tk widgets, opens a capture worker, or dispatches a game action itself.
"""
from __future__ import annotations

import ast
import hashlib
import json
import math
import threading
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Mapping

BASE_COMMIT = '90c770767894e3d6d5a9ce98993426b3861f13c1'
GUI_BLOB = 'befe9ec0037d9dee043706fe193d9ed78031fb30'
# The current callback maps any non-Master label to Pro. Do not pretend that
# adding a frontend option alone implements another mode in this backend.
MODES = {'produce-004': 'N.I.A. Pro', 'produce-005': 'N.I.A. Master'}
TOOLS = ('_run_live_worker',)
APP_CALLBACKS = (
    '_on_console_profile_changed', '_on_console_mode_changed',
    '_start_console_autopilot', '_stop_console_autopilot',
    '_start_console_batch', '_stop_console_batch',
    '_refresh_cultivation_overview', '_refresh_model_evaluation_page',
    '_activate_console_bundle', '_save_console_preferences', *TOOLS,
)
LAUNCHER_CALLBACKS = frozenset(('launcher-launch','launcher-sync','launcher-repair'))
INSTALL_CALLBACKS = frozenset(('detect', 'choose-folder', 'choose-control', 'install-control', 'install-translation', 'restore-all', 'recover',
                               'preview-install-control', 'preview-install-translation', 'preview-restore-all', 'preview-recover'))
UPDATE_CALLBACKS = frozenset(('app-update-check', 'app-update-download', 'app-update-apply', 'app-update-rollback'))
LOADOUT_CALLBACKS = frozenset(('loadout.status', 'loadout.refresh', 'loadout.read', 'loadout.recommend', 'loadout.constraints', 'loadout.apply'))
PREFERENCE_CALLBACKS = frozenset(('save-setup-preferences',))
SETUP_CALLBACKS = LAUNCHER_CALLBACKS | INSTALL_CALLBACKS | UPDATE_CALLBACKS | LOADOUT_CALLBACKS | PREFERENCE_CALLBACKS
COMMANDS = frozenset((*[name for name in APP_CALLBACKS if name != '_save_console_preferences'],
                      'set_target_cycles', 'set_policy_variant', 'show_legacy', 'shutdown', *SETUP_CALLBACKS))
READ_CALLBACKS = frozenset(('_refresh_cultivation_overview', '_refresh_model_evaluation_page', 'app-update-check', 'app-update-download', 'loadout.status')) | PREFERENCE_CALLBACKS
CONTROL_CALLBACKS = frozenset(('_start_console_autopilot', '_stop_console_autopilot',
                              '_start_console_batch', '_stop_console_batch',
                              '_activate_console_bundle', *TOOLS, *(SETUP_CALLBACKS - {'loadout.status'} - PREFERENCE_CALLBACKS)))
PUBLIC_COMMANDS = COMMANDS - {'show_legacy', '_activate_console_bundle', '_run_live_worker', '_refresh_model_evaluation_page'}
BUSY_PHASES = frozenset(('running', 'cancelling'))
_DLL_STARTUP_NOTICE = '正在檢查 DLL 連線與遊戲場景。'


def check_source(project_root: Path, *, allow_drift: bool = False) -> dict[str, Any]:
    source = project_root / 'src' / 'gkms_tool' / 'gui.py'
    raw = source.read_bytes().replace(b'\r\n', b'\n')
    blob = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
    tree = ast.parse(raw.decode('utf-8-sig'))
    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'GkmsApp'), None)
    methods = set() if cls is None else {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    missing = sorted(set(APP_CALLBACKS) - methods)
    if missing:
        raise RuntimeError('GUI 接口不相容，缺少：' + ', '.join(missing))
    return {'commit_baseline': BASE_COMMIT, 'gui_blob': blob, 'source_matches': blob == GUI_BLOB,
            'adapter_version': 'gkms.glass-current-app.v1',
            'compatibility_note': '已接目前專案控制器；開始與停止共用既有 DLL 培育流程。'}


def plain(value: Any) -> Any:
    """Copy display data only; unknown and zero remain different."""
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return None if isinstance(value, float) and not math.isfinite(value) else value
    return str(value)


class GkmsAdapter:
    def __init__(self, app: Any, *, enable_control: bool = False,
                 compatibility: Mapping[str, Any] | None = None, public_build=False) -> None:
        self.app = app
        self._owner_thread = threading.get_ident()
        self.enable_control = bool(enable_control)
        self.public_build = bool(public_build)
        self.compatibility = dict(compatibility or {})
        self.close_requested = False
        self.errors: list[str] = []
        self.setup_service = None
        missing = [name for name in APP_CALLBACKS if not callable(getattr(app, name, None))]
        if missing:
            raise RuntimeError('執行期接口缺少：' + ', '.join(missing))

    def attach_setup_service(self, service):
        if threading.get_ident() != self._owner_thread or self.setup_service is not None:
            raise RuntimeError('GUI presentation service requires the single owner')
        from ..gui_setup.project_session import attach_controller
        self.setup_service = service
        detach = attach_controller(self.app, service.session)
        def close():
            detach()
            self.setup_service = None
        return close

    def var(self, name: str, default: Any = None) -> Any:
        obj = getattr(self.app, name, None)
        return default if obj is None else obj.get()

    def widget_text(self, name: str) -> str:
        obj = getattr(self.app, name, None)
        return '' if obj is None else str(obj.get('1.0', 'end-1c'))

    def tree_rows(self, name: str) -> list[list[Any]]:
        obj = getattr(self.app, name, None)
        if obj is None:
            return []
        return [list(obj.item(i, 'values')) for i in obj.get_children()]

    def live_view(self) -> Any:
        return self.app._console_live_controller.view

    def is_busy(self) -> bool:
        phase = str(getattr(self.live_view(), 'phase', 'idle'))
        batch = self.app._console_batch_controller.view
        library = getattr(self.app, 'card_library_panel', None)
        return (phase in BUSY_PHASES or bool(getattr(self.app, '_live_busy', False))
                or bool(getattr(batch, 'running', False)) or bool(getattr(library, 'game_input_pending', False))
                or bool(self.setup_service is not None and self.setup_service.lock.locked()))

    def has_pending_native_transaction(self):
        from ..runtime_command_client import DEFAULT_BRIDGE_ROOT
        # A saved unknown transaction is separate from an active GUI worker.
        # It blocks new selections/start, but must not prevent safe GUI exit.
        return any(DEFAULT_BRIDGE_ROOT.glob('*pending*.json'))

    def profiles(self) -> list[dict[str, str]]:
        result = []
        for p in self.app.console_idol_profiles:
            label = self.app.profile_label_by_id.get(p.id, p.label)
            result.append({'id': p.id, 'label': label,
                           'flow': self.app._flow_label(p.exam_effect_type),
                           'plan_type': p.plan_type, 'exam_effect_type': p.exam_effect_type})
        return result

    def model_start_blocker(self, descriptor, idol_card_id, mode_id, target_cycles=None):
        if not descriptor or descriptor.get('available') is not True:
            return (descriptor or {}).get('reason') or '選擇的模型檔案尚未就緒。'
        if descriptor.get('live_ready') is not True and not descriptor.get('can_request_preflight'):
            return descriptor.get('start_block_reason') or '新版模型的 DLL 接線仍待驗證。'
        profile = next((p for p in self.profiles() if p['id']==idol_card_id), {})
        from ..local_flow_trial import resolve_live_flow_scope
        try:
            resolve_live_flow_scope(variant_id=descriptor.get('id'), produce_id=mode_id,
                idol_card_id=idol_card_id, plan_type=profile.get('plan_type'),
                exam_effect_type=profile.get('exam_effect_type'),
                target_cycles=self.target_cycles() if target_cycles is None else target_cycles)
        except (OSError, TypeError, ValueError) as error:
            return str(error)
        return None

    def _display_run_identity(self, monitor, live_state, phase, cached):
        """Read the current owner's published identity; never refresh/run a job."""
        update = monitor.get('state_update')
        update = update if isinstance(update, Mapping) else {}
        native_observation = isinstance(live_state, Mapping) and bool(live_state) and (
            monitor.get('source') == 'dll' or update.get('source') == 'dll' or
            (monitor.get('schema') == 'gkms.live-monitor.v1' and update.get('kind') == 'exact-transition'))
        if isinstance(live_state, Mapping) and live_state.get('in_progress') is False:
            return None, 'idle', native_observation
        if not native_observation:
            return cached, 'saved' if cached else 'unknown', False
        controller = self.app._console_live_controller
        progress = getattr(controller, '_progress', None)
        progress = progress if isinstance(progress, Mapping) else {}
        def text(value):
            return value if isinstance(value, str) and value.strip() and len(value) <= 256 else None
        run_ids = {value for value in (text(monitor.get('source_run_id')), text(progress.get('source_run_id'))) if value}
        request = controller.request
        requested_run = text(getattr(request, 'expected_run_id', None))
        if len(run_ids) > 1 or (run_ids and requested_run and requested_run not in run_ids):
            return None, 'identity-conflict', True
        if not run_ids:
            # A newly started worker may observe week 1 before its run ID is
            # published. Do not borrow a prior saved run or invent this ID.
            return None, 'awaiting-run-id' if live_state.get('in_progress') is True else 'unknown', True
        run_id = next(iter(run_ids))
        idol = text(getattr(request, 'idol_card_id', None))
        produce = text(getattr(request, 'produce_id', None))
        observed_idol = text(live_state.get('idol_card_id'))
        observed_produce = text(live_state.get('produce_id'))
        if (observed_idol and idol and observed_idol != idol) or (observed_produce and produce and observed_produce != produce):
            return None, 'identity-conflict', True
        idol, produce = observed_idol or idol, observed_produce or produce
        name = getattr(self.app, 'profile_label_by_id', {}).get(idol)
        return {'run_id': run_id, 'idol_card_id': idol, 'idol_name': name,
                'produce_id': produce, 'identity_source': 'owned-autopilot-progress'}, 'observed', True

    def _retained_terminal_display(self, live, batch):
        if str(getattr(live, 'phase', 'idle')) != 'idle' or getattr(batch, 'running', False):
            return None
        current = getattr(live, 'monitor_snapshot', None)
        if isinstance(current, Mapping) and isinstance(current.get('state'), Mapping) and current['state'].get('in_progress') is False:
            return None
        session = getattr(self.setup_service, 'session', None)
        read = getattr(session, 'terminal_display', None)
        value = read() if callable(read) else None
        if not isinstance(value, Mapping) or value.get('phase') not in ('completed', 'cancelled', 'failed', 'blocked'):
            return None
        active = getattr(self.app, '_active_run', None)
        actual_id = getattr(active, 'run_id', None)
        if actual_id and value.get('run_id') and actual_id != value['run_id']:
            return None
        return value

    @staticmethod
    def _display_action(action, stop_reason):
        if (isinstance(action, str) and '完成本次演出' in action and
                isinstance(stop_reason, str) and 'policy-no-decision:' in stop_reason):
            return action.replace('完成本次演出', '演出策略已中斷')
        return action

    def snapshot(self) -> dict[str, Any]:
        if threading.get_ident() != self._owner_thread:
            raise RuntimeError("Tk snapshot 必須在建立 adapter 的執行緒執行。")
        app = self.app
        live = self.live_view()
        batch = app._console_batch_controller.view
        retained = self._retained_terminal_display(live, batch)
        phase = str(retained['phase'] if retained else getattr(live, 'phase', 'idle'))
        selected = app._selected_profile()
        overview = getattr(app, '_cultivation_overview', None)
        overview_dict = plain(overview) if overview is not None else {}
        raw = getattr(live, 'monitor_snapshot', None)
        monitor = raw if isinstance(raw, Mapping) else {}
        if retained:
            monitor = deepcopy(retained.get('monitor_snapshot') or {})
            if retained.get('run_id'):
                monitor['source_run_id'] = retained['run_id']
        live_state = monitor.get('state')
        has_live_state = isinstance(live_state, Mapping) and bool(live_state)
        state = dict(live_state) if has_live_state else {
            key: overview_dict.get(key) for key in
            ('week', 'stamina', 'max_stamina', 'produce_points', 'vocal', 'dance', 'visual')
        }
        if not has_live_state and not overview_dict.get('run_id'):
            state = None
        home_idle = has_live_state and live_state.get('in_progress') is False
        if home_idle:
            state = {'in_progress': False}
        update = monitor.get('state_update')
        capture = monitor.get('capture')
        timestamp = update.get('timestamp') if isinstance(update, Mapping) else (
            capture.get('timestamp') if isinstance(capture, Mapping) else None)
        # Never make a saved run-shadow look freshly observed.
        if not has_live_state or retained:
            timestamp = None
        cards = monitor.get('cards') if has_live_state else None
        drinks = monitor.get('drinks') if has_live_state else None
        legal = monitor.get('legal_actions') if has_live_state else None
        if home_idle:
            cards, drinks, legal = [], [], []
        if cards is None and state is not None:
            # Existing Treeview fallback is only an observed display projection.
            # Do not turn its fully_supported or OCR labels into a legal flag.
            display = self.tree_rows('live_card_tree')
            if display:
                cards = [{'card_id': row[0], 'display_name': str(row[2]),
                          'upgrade_display': row[1], 'availability_display': row[3],
                          'legal': None, 'source': 'existing-gui-display'}
                         for row in display if len(row) >= 4]
        recommendations = [plain(r) for r in getattr(app, '_recommendation_rows', ())]
        if home_idle:
            recommendations = []
        bundles = getattr(app, '_console_bundle_views', {})
        selected_bundle_id = str(self.var('console_bundle_var', '') or '')
        selected_bundle = bundles.get(selected_bundle_id)
        source_label = ('自動培育既有觀測' if has_live_state else
                        '已保存場次 · 非即時' if state else '尚無遊戲狀態')
        if retained:
            source_label = '上次停止前的觀測 · 非即時'
        status = self.var('live_game_status_var', '遊戲：等待狀態')
        active = getattr(app, '_active_run', None)
        active_dict = None if active is None or home_idle else {
            'run_id': getattr(active, 'run_id', None),
            'idol_card_id': getattr(active, 'idol_card_id', None),
            'idol_name': overview_dict.get('idol_name'),
            'produce_id': getattr(active, 'produce_id', None),
        }
        active_dict, run_identity_state, native_observation = self._display_run_identity(monitor, live_state, phase, active_dict)
        if retained and not home_idle and retained.get('run_id'):
            active_dict = {key: retained.get(key) for key in ('run_id', 'idol_card_id', 'idol_name', 'produce_id')}
            active_dict['identity_source'] = 'last-controller-terminal-view'
            run_identity_state = 'retained-terminal'
        note = self.var('console_selection_note_var')
        note_kind = None
        if active_dict and run_identity_state in ('observed', 'retained-terminal'):
            same_selection = (selected is not None and selected.id == active_dict['idol_card_id'] and
                              self.var('console_mode_var', 'produce-004') == active_dict['produce_id'])
            note_kind = 'current-selection' if same_selection else 'next-selection'
            note = ('上方角色與模式與目前培育一致。' if same_selection else
                    '上方設定將套用於下一場；目前培育維持原本的角色與模式。')
            if retained:
                note_kind = 'retained-run'
                note = '目前顯示上次停止的培育紀錄；實際狀態將在接續時更新。'
        elif run_identity_state in ('awaiting-run-id', 'identity-conflict'):
            note_kind = 'awaiting-identity'
            note = '培育進行中；目前場次身分仍在更新。'
        elif native_observation and home_idle:
            note_kind = 'waiting-update' if phase in BUSY_PHASES else 'next-selection-idle'
            note = '正在等待培育流程更新。' if phase in BUSY_PHASES else '目前沒有進行中的培育；上方設定將用於下一場。'
        blockers = list(retained.get('blockers', ()) if retained else getattr(live, 'blockers', ()) or ())
        startup_notes = []
        if native_observation and (phase in BUSY_PHASES or retained):
            startup_notes = [item for item in blockers if item == _DLL_STARTUP_NOTICE]
            blockers = [item for item in blockers if item != _DLL_STARTUP_NOTICE]
        from ..gui_exam_models import get_gui_exam_policy_variants, current_policy_selection_state
        policy_variants = list(get_gui_exam_policy_variants())
        from ..local_flow_trial import flow_coverage
        selected_flow_coverage = flow_coverage(
            getattr(selected, 'plan_type', None), getattr(selected, 'exam_effect_type', None),
            self.var('console_mode_var', 'produce-004'))
        for variant in policy_variants:
            variant['inference_scope'] = {'modes': list(MODES), 'flow_count': 6,
                                          'trained_flow_count': 1, 'automatic_fallback': False}
        variant_id = self.var('exam_policy_variant_var', 'baseline')
        pending_native = self.has_pending_native_transaction()
        policy_selection = current_policy_selection_state(variant_id, running=self.is_busy(),
            pending_transaction=pending_native)
        selected_variant_id = policy_selection['active_variant_id'] or variant_id
        selected_policy = next((row for row in policy_variants if row['id'] == selected_variant_id), None)
        start_reason = self.model_start_blocker(selected_policy, None if selected is None else selected.id,
            self.var('console_mode_var','produce-004'))
        if pending_native:
            start_reason = '尚有未完成的遊戲操作；需先核對原操作結果，不能重新開始或切換模型。'
        elif policy_selection['reason_kind'] == 'active-run-unavailable' or (
                policy_selection['active_run_id'] is not None and policy_selection['active_variant_id'] is None):
            start_reason = '目前場次的原模型無法確認；保留場次，暫停開始。'
        # Running provenance must be supplied by the executing worker. A
        # selection or an old bundle label is never substituted for it.
        actual_policy = monitor.get('exam_policy')
        if not isinstance(actual_policy, Mapping) or actual_policy.get('loaded') is not True:
            actual_policy = {}
        model_display_source = 'current-worker-observation' if actual_policy else None
        model_observed_at = None
        session = getattr(self.setup_service, 'session', None)
        model_conflict = (run_identity_state == 'identity-conflict' or
            (actual_policy and (not active_dict or actual_policy.get('run_id') != active_dict.get('run_id'))))
        if home_idle or model_conflict:
            forget = getattr(session, 'forget_model_display', None)
            if callable(forget):
                forget()
            actual_policy = {}
            model_display_source = None
        elif not actual_policy and 'exam_policy' not in monitor and active_dict and run_identity_state in ('observed', 'retained-terminal'):
            read_model = getattr(session, 'model_display', None)
            remembered = read_model(active_dict.get('run_id')) if callable(read_model) else None
            if isinstance(remembered, Mapping):
                actual_policy = remembered['policy']
                model_display_source = remembered['display_source']
                model_observed_at = remembered['observed_at']
        stop_reason = retained.get('stop_reason') if retained else getattr(live, 'stop_reason_text', None)
        last_action = retained.get('last_action') if retained else getattr(live, 'recent_action_text', None)
        raw_logs = self.widget_text('live_log_text').splitlines()[-80:] + self.errors[-10:]
        from ..app_version import info as application_info
        updates = getattr(self.setup_service, 'app_updates', None)
        loadout_service = getattr(self.setup_service, 'loadout', None)
        return plain({
            'schema': 'gkms.glass-view.v1',
            'presentation_version': application_info()['display_version'],
            'application': application_info(),
            'app_update': None if updates is None else updates.snapshot(),
            'loadout': None if loadout_service is None else loadout_service.snapshot(),
            'developer': (None if self.public_build else
                          {'available': False, 'error': self.setup_service.developer_error}
                          if getattr(self.setup_service, 'developer_error', None) else
                          None if getattr(self.setup_service, 'developer', None) is None
                          else self.setup_service.developer.snapshot()),
            'bridge': {'connected': True, 'control_enabled': self.enable_control, **self.compatibility},
            'catalog': {'profiles': self.profiles(),
                        'modes': [{'id': key, 'label': value} for key, value in MODES.items()],
                        'bundles': [{'id': key, 'label': key} for key in bundles],
                        'policy_variants': policy_variants},
            'selection': {'idol_card_id': '' if selected is None else selected.id,
                          'mode_id': self.var('console_mode_var', 'produce-004'),
                          'target_cycles': self.target_cycles(),
                          'bundle_id': selected_bundle_id,
                          'policy_variant_id': selected_variant_id,
                          'loadout_summary': '沿用帳號目前編成；下方配裝區可推薦、鎖定及套用卡片',
                          'availability': self.var('profile_availability_var'),
                          'note': note, 'note_kind': note_kind},
            'active_run': active_dict,
            'live': {'phase': phase, 'busy': self.is_busy(), 'page': monitor.get('page'),
                     'pending_transaction': pending_native,
                     'run_identity_state': run_identity_state,
                     'display_source': 'retained-terminal-view' if retained else 'current-controller-view',
                     'stage': self.var('live_stage_status_var', '—'),
                     'game_status': str(status).removeprefix('遊戲：'),
                     'start_enabled': self.start_enabled(live) and start_reason is None,
                     'start_reason': start_reason,
                     'start_note': (selected_policy or {}).get('preflight_note'),
                     'state': state, 'cards': cards, 'drinks': drinks, 'legal_actions': legal,
                     'state_timestamp': timestamp, 'source_label': source_label,
                     'last_action': None if home_idle else self._display_action(last_action, stop_reason),
                     'stop_reason': stop_reason,
                     'diff_summary': getattr(live, 'diff_summary_text', None),
                     'blockers': blockers, 'initial_readiness_notes': startup_notes,
                     'recommendations': recommendations},
            # Existing policy labels are configured/selected metadata, not proof
            # that the running worker has loaded the same manifest.
            'policy': {'selected_flow_coverage': selected_flow_coverage,
                       'selected_label': self.var('live_model_status_var', '尚未載入'),
                       'selection_locked': policy_selection['locked'],
                       'selection_lock_reason': policy_selection['reason'],
                       'selection_lock_kind': policy_selection['reason_kind'],
                       'selection_run_id': policy_selection['active_run_id'],
                       'configured_owner_label': self.var('live_formal_policy_var'),
                       'actual_variant_id': actual_policy.get('variant_id'),
                       'actual_model_label': actual_policy.get('label'),
                       'actual_model_sha256': actual_policy.get('model_sha256'),
                       'actual_run_id': actual_policy.get('run_id'),
                       'actual_model_display_source': model_display_source,
                       'actual_model_observed_at': model_observed_at,
                       'note': '顯示既有 GUI 的設定策略；當步實際動作以執行紀錄為準。',
                       'request_bundle_path': str(getattr(app._console_live_controller.request,
                                                         'plan2_policy_bundle_path', '') or '')},
            'batch': {key: getattr(batch, key, None) for key in
                      ('target_cycles', 'started_cycles', 'completed_cycles', 'success_count', 'failure_count',
                       'running', 'stop_requested', 'message')},
            'session': None if self.setup_service is None else self.setup_service.session.snapshot(),
            'assistant': {'closing': self.close_requested},
            'history': {'summary': self.widget_text('run_shadow_summary_text'),
                        'deck': self.tree_rows('run_deck_tree'),
                        'records': self.tree_rows('run_history_tree'),
                        'inventory': overview_dict.get('inventory', [])},
            'models': {'rows': self.tree_rows('console_model_tree'),
                       'detail': self.widget_text('console_model_detail'),
                       'status': self.var('console_bundle_status_var'),
                       'selected_view': plain(selected_bundle)},
            'training': {'rows': self.tree_rows('training_data_tree'),
                         'note': self.widget_text('training_status_text')},
            'logs': raw_logs,
            'display_logs': [self._display_action(line, line) for line in raw_logs],
            'status_message': self.var('status_var'),
        })

    def target_cycles(self):
        try:
            value = int(self.var('home_cycle_target_var', '1'))
            return value if 1 <= value <= 999 else None
        except (ValueError, TypeError):
            return None

    def start_enabled(self, live):
        button = getattr(self.app, 'live_start_auto_button', None)
        return (str(button.cget('state')) != 'disabled' if button is not None else
                bool(getattr(live, 'start_enabled', False))) and not self.is_busy()

    @contextmanager
    def capture_notifications(self):
        """Return callback errors to the visible web UI, not a hidden Tk modal."""
        from tkinter import messagebox
        names = ('showerror', 'showwarning', 'showinfo')
        original = {name: getattr(messagebox, name) for name in names}
        messages, failures = [], []
        def notify(name):
            def record(title=None, message=None, **_kwargs):
                value = f'{title}: {message}'
                messages.append(value)
                if name == 'showerror':
                    failures.append(value)
            return record
        for name in names:
            setattr(messagebox, name, notify(name))
        try:
            yield
        finally:
            for name, function in original.items():
                setattr(messagebox, name, function)
            self.errors.extend(messages)
            self.errors = self.errors[-100:]
        if failures:
            raise RuntimeError('\n'.join(failures))

    def _validated_settings(self, values: Mapping[str, Any]) -> tuple[str, str, int]:
        profiles = {p['id']: p for p in self.profiles()}
        card = values.get('idol_card_id')
        mode = values.get('console_mode_var')
        target = values.get('home_cycle_target_var')
        if not isinstance(card, str) or card not in profiles:
            raise ValueError('偶像卡不在本機 profile 清單內。')
        if mode not in MODES:
            raise ValueError('此後端尚未接入該培育模式；不會退回 Pro。')
        if type(target) is not int or not 1 <= target <= 999:
            raise ValueError('場數必須為 1–999 的整數。')
        self._validated_policy_variant(values.get('policy_variant_id', self.var('exam_policy_variant_var', 'baseline')))
        return profiles[card]['label'], MODES[mode], target

    def _validated_policy_variant(self, value):
        from ..gui_exam_models import get_gui_exam_policy, current_policy_selection_state
        descriptor = get_gui_exam_policy(value)
        if not descriptor['available']:
            raise ValueError(descriptor.get('reason') or '選擇的模型尚未就緒。')
        selection = current_policy_selection_state(value)
        if selection['locked'] and (selection['active_variant_id'] is None or value != selection['active_variant_id']):
            raise RuntimeError(selection['reason'])
        return value

    def _apply_settings(self, values: Mapping[str, Any]) -> None:
        # Validate the whole selection before the first mutation.
        label, mode, target = self._validated_settings(values)
        app = self.app
        if label != self.var('profile_selection_var'):
            app.profile_selection_var.set(label)
            app._on_console_profile_changed()
        if mode != self.var('console_mode_choice_var'):
            app.console_mode_choice_var.set(mode)
            app._on_console_mode_changed()
        app.home_cycle_target_var.set(str(target))
        variant = values.get('policy_variant_id', self.var('exam_policy_variant_var', 'baseline'))
        if variant != self.var('exam_policy_variant_var', 'baseline'):
            from ..gui_exam_models import current_policy_selection_state
            selection = current_policy_selection_state(variant)
            if not selection['active_run_id']:
                app._set_exam_policy_variant(variant)

    def dispatch(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if threading.get_ident() != self._owner_thread:
            raise RuntimeError("Tk callback 必須在建立 adapter 的執行緒執行。")
        with self.capture_notifications():
            result = self._dispatch(payload)
        if payload.get('callback') in ('_start_console_autopilot', '_start_console_batch') and not self.is_busy():
            raise RuntimeError(self.var('status_var', '控制器尚未開始；請查看狀態。'))
        return result

    def _dispatch(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if threading.get_ident() != self._owner_thread:
            raise RuntimeError("Tk callback 必須在建立 adapter 的執行緒執行。")
        name = payload.get('callback')
        developer_actions = frozenset() if self.public_build else getattr(self.setup_service, 'developer_actions', frozenset())
        developer_reads = getattr(self.setup_service, 'developer_read_actions', frozenset())
        if name not in (PUBLIC_COMMANDS if self.public_build else COMMANDS | developer_actions):
            raise ValueError('接口不在允許清單內。')
        if (name in CONTROL_CALLBACKS or name in developer_actions - developer_reads) and not self.enable_control:
            raise PermissionError('接線為唯讀模式；需明確以 --enable-control 啟動。')
        values = payload.get('values', {})
        if not isinstance(values, Mapping):
            raise ValueError('values 必須為 object。')
        stop = name in ('_stop_console_autopilot', '_stop_console_batch')
        if self.close_requested and name not in ('show_legacy', 'shutdown') and not stop:
            raise RuntimeError('接線正在安全結束，拒絕新增工作。')
        parallel_developer = developer_reads | (developer_actions & {'offline.start', 'offline.stop'})
        if self.is_busy() and not stop and name not in READ_CALLBACKS | parallel_developer and name not in ('show_legacy', 'shutdown'):
            raise RuntimeError('執行／停止處理中或手動工作忙碌；拒絕平行操作。')
        loadout_service = getattr(self.setup_service, 'loadout', None)
        loadout_reconciliation = (name == 'loadout.read' and loadout_service is not None
                                  and loadout_service.can_reconcile_pending())
        if self.has_pending_native_transaction() and not stop and not loadout_reconciliation and name not in READ_CALLBACKS | parallel_developer and name not in ('show_legacy','shutdown'):
            raise RuntimeError('尚有未完成的原生交易；保留原模型及操作身份，暫停新增工作。')
        app = self.app
        if name in SETUP_CALLBACKS | developer_actions:
            if self.setup_service is None:
                raise RuntimeError('整合啟動器尚未連線。')
            return self.setup_service.command(name, dict(values))
        elif name in ('_start_console_autopilot', '_start_console_batch'):
            self._validated_settings(values)
            from ..gui_exam_models import verify_gui_exam_model_artifacts
            variant = values.get('policy_variant_id', self.var('exam_policy_variant_var', 'baseline'))
            selected = verify_gui_exam_model_artifacts(variant)
            blocked = self.model_start_blocker(selected,values.get('idol_card_id'),values.get('console_mode_var'),
                target_cycles=values.get('home_cycle_target_var'))
            if blocked:
                raise RuntimeError(blocked)
            self._apply_settings(values)
            getattr(app, name)()
        elif stop:
            getattr(app, name)()
        elif name == '_on_console_profile_changed':
            profiles = {p['id']: p['label'] for p in self.profiles()}
            card = values.get('idol_card_id')
            if card not in profiles:
                raise ValueError('偶像卡不在本機 profile 清單內。')
            app.profile_selection_var.set(profiles[card])
            app._on_console_profile_changed()
        elif name == '_on_console_mode_changed':
            mode = values.get('console_mode_var')
            if mode not in MODES:
                raise ValueError('此模式尚未接線；不會默認成 Pro。')
            app.console_mode_choice_var.set(MODES[mode])
            app._on_console_mode_changed()
        elif name == 'set_target_cycles':
            target = values.get('home_cycle_target_var')
            if type(target) is not int or not 1 <= target <= 999:
                raise ValueError('場數必須為 1–999 的整數。')
            app.home_cycle_target_var.set(str(target))
            app._save_console_preferences()
        elif name == 'set_policy_variant':
            variant = self._validated_policy_variant(values.get('policy_variant_id'))
            from ..gui_exam_models import current_policy_selection_state
            selection = current_policy_selection_state(variant)
            if selection['locked']:
                raise RuntimeError(selection['reason'])
            app._set_exam_policy_variant(variant)
        elif name in ('_refresh_model_evaluation_page', '_activate_console_bundle'):
            bundle = values.get('console_bundle_var', '')
            if bundle:
                if bundle not in getattr(app, '_console_bundle_views', {}):
                    raise ValueError('模型包不在本機已發現清單內。')
                app.console_bundle_var.set(bundle)
            elif name == '_activate_console_bundle':
                raise ValueError('尚未選擇模型包。')
            getattr(app, name)()
        elif name == '_refresh_cultivation_overview':
            app._refresh_cultivation_overview()
        elif name in TOOLS:
            index = TOOLS.index(name)
            if type(payload.get('tool_index')) is not int or payload.get('tool_index') != index:
                raise ValueError('工具名稱與索引不一致。')
            # Current DLL status already has a purpose-built implementation.
            # Never dispatch the retired vision tools by widget position.
            from gkms_tool.gui import controller_status
            app._run_live_worker('status', controller_status)
        elif name == 'show_legacy':
            app.deiconify()
            app.lift()
        elif name == 'shutdown':
            self.close_requested = True
            if self.is_busy():
                app._stop_console_autopilot()
        return {'ok': True, 'message': '已轉交既有 GUI 接口；實際結果請查看後續狀態。'}
