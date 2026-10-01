"""Project-only presentation observations; the original v12 journal stays intact."""
from collections.abc import Mapping
from copy import deepcopy
import re

from .session import SessionResults, _field, _phase, _text


_MEMORY_GRADES = (None, 'F', 'E', 'D', 'C', 'C+', 'B', 'B+', 'A', 'A+',
                  'S', 'S+', 'SS', 'SS+', 'SSS', 'SSS+', 'SSSS', 'SSSS+', 'SSSSS', 'SSSSS+')


def _memory_grade(value):
    # Game ResultGrade enum; never derive it from exam score or ranking place.
    if type(value) is int:
        return _MEMORY_GRADES[value] if 0 < value < len(_MEMORY_GRADES) else None
    if isinstance(value, str):
        label = value.removeprefix('ResultGrade_').replace('_PLUS', '+')
        return label if label in _MEMORY_GRADES[1:] else None
    return None


class ProjectSessionResults(SessionResults):
    def __init__(self, clock=None):
        super().__init__(clock=clock)
        self._terminal_display = None
        self._model_display = None

    def begin(self, **identity):
        with self._lock:
            fresh = self._active is None
            result = super().begin(**identity)
            if fresh:
                self._terminal_display = None
                self._model_display = None
                self._active['memory_grade'] = None
            return result

    def bind_run(self, run_id):
        with self._lock:
            super().bind_run(run_id)
            if self._active is not None and self._active['score_conflict']:
                self._model_display = None
                self._active['memory_grade'] = None

    def observe_native_outer(self, snapshot, *, run_id):
        """Record this result's created UserMemory.Grade from an existing read."""
        raw = _field(snapshot, 'raw', snapshot)
        if not isinstance(raw, Mapping) or raw.get('surface') != 'produce_result':
            return
        ui = raw.get('ui_state')
        if not isinstance(ui, Mapping) or ui.get('data_ready') is not True:
            return
        memory = ui.get('created_memory_result')
        if (not isinstance(memory, Mapping) or memory.get('source') != 'UserMemory.Grade'
                or not isinstance(memory.get('memory_id'), str) or not memory['memory_id']
                or memory['memory_id'] != ui.get('created_memory_id')):
            return
        grade = _memory_grade(memory.get('grade'))
        if grade is None:
            return
        with self._lock:
            a = self._active
            if (a is None or a['score_conflict'] or not a['run_id'] or run_id != a['run_id']
                    or not a['idol_id'] or memory.get('idol_card_id') != a['idol_id']
                    or not a['produce_id'] or memory.get('produce_id') != a['produce_id']):
                return
            a['memory_grade'] = grade

    def snapshot(self):
        with self._lock:
            result = super().snapshot()
            grades = {row['id']: row.get('memory_grade') for row in self._rows}
            for row in result['rows']:
                row['memory_grade'] = grades.get(row['id'])
            return result

    def remember_model_progress(self, progress):
        """Remember worker provenance before GUI polling coalesces its messages.

        This proves model observation only, not action settlement or policy
        quality. No selected model, saved report or game state is inferred.
        """
        if not isinstance(progress, Mapping):
            return
        monitor = progress.get('monitor_snapshot')
        if not isinstance(monitor, Mapping):
            return
        with self._lock:
            active = self._active
            run_id = progress.get('source_run_id')
            if (active is None or active['score_conflict'] or not active['run_id']
                    or run_id != active['run_id']
                    or monitor.get('source_run_id', run_id) != run_id):
                self._model_display = None
                return
            state = monitor.get('state')
            if isinstance(state, Mapping) and state.get('in_progress') is False:
                self._model_display = None
                return
            if 'exam_policy' not in monitor:
                return  # Schedule/event progress does not revoke same-run proof.
            policy = monitor['exam_policy']
            if (monitor.get('source') != 'dll' or not isinstance(policy, Mapping)
                    or policy.get('loaded') is not True or policy.get('run_id') != run_id
                    or policy.get('variant_id') not in ('baseline', 'integrated', 'rl_shared_iql')
                    or not isinstance(policy.get('label'), str) or not policy['label'].strip()
                    or len(policy['label']) > 256 or not isinstance(policy.get('model_sha256'), str)
                    or not re.fullmatch(r'[a-fA-F0-9]{64}', policy['model_sha256'])):
                self._model_display = None
                return
            self._model_display = {
                'policy': {key: deepcopy(policy[key]) for key in
                           ('loaded', 'run_id', 'variant_id', 'label', 'model_sha256')},
                'display_source': 'latest-observed-in-this-run',
                'observed_at': self._clock(), 'is_live': False,
            }

    def model_display(self, run_id):
        with self._lock:
            value = self._model_display
            if not run_id or value is None or value['policy']['run_id'] != run_id:
                return None
            if self._active is not None and (self._active['run_id'] != run_id or self._active['score_conflict']):
                return None
            return deepcopy(value)

    def forget_model_display(self):
        with self._lock:
            self._model_display = None

    def remember_terminal_display(self, view, *, source_run_id=None):
        """Preserve the actual stopped view before legacy Tk resets its request.

        This cache is presentation only. It does not change controller state,
        mark a game win/loss or make its last observation current again.
        """
        phase = _phase(_field(view, 'phase'))
        if phase not in {'completed', 'cancelled', 'failed', 'blocked'}:
            return
        with self._lock:
            active = self._active
            if active is None:
                return
            run_id = _text(source_run_id) or active['run_id']
            if active['run_id'] and run_id != active['run_id']:
                self.observation_errors += 1
                return
            raw_monitor = _field(view, 'monitor_snapshot')
            monitor = deepcopy(dict(raw_monitor)) if isinstance(raw_monitor, Mapping) else {}
            if monitor.get('source_run_id') and run_id and monitor['source_run_id'] != run_id:
                self.observation_errors += 1
                return
            self._terminal_display = {'phase': phase, 'run_id': run_id or None,
                'idol_card_id': active['idol_id'] or None, 'produce_id': active['produce_id'] or None,
                'idol_name': active['character'] or None, 'monitor_snapshot': monitor,
                'stop_reason': _text(_field(view, 'stop_reason_text'), 4096),
                'last_action': _text(_field(view, 'recent_action_text'), 4096),
                'blockers': deepcopy(list(_field(view, 'blockers', ()) or ())),
                'observation_source': 'original-controller-terminal-view', 'is_live': False}

    def terminal_display(self):
        with self._lock:
            return deepcopy(self._terminal_display)
