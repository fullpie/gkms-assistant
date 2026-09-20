"""Project-only presentation observations; the original v12 journal stays intact."""
from collections.abc import Mapping
from copy import deepcopy
import re

from .session import SessionResults, _field, _phase, _text


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
            return result

    def bind_run(self, run_id):
        with self._lock:
            super().bind_run(run_id)
            if self._active is not None and self._active['score_conflict']:
                self._model_display = None

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
                    or policy.get('variant_id') not in ('baseline', 'integrated')
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
