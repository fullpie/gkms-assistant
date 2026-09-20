"""Process-local UI results. No file reads/writes, tokens, paths or replay imports.

A run is recorded from the live controller's terminal callback, not from the
browser's polling frequency. A fresh SessionResults represents a new app launch.
"""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime, timezone
import math
import threading
import uuid
from collections.abc import Mapping


def _phase(value):
    return str(getattr(value, 'value', value) or '')


def _text(value, limit=256):
    return value[:limit] if isinstance(value, str) else ''


def _field(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, Mapping) else getattr(obj, key, default)


class SessionResults:
    """Observational, thread-safe, in-memory ledger; never owns game actions."""
    def __init__(self, clock=None):
        self.session_id = uuid.uuid4().hex
        self._clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
        self._lock = threading.RLock()
        self._rows = []
        self._active = None
        self._batch_id = 0
        self._target = 1
        self._started = 0
        self._completed = 0
        self._interrupted = 0
        self.observation_errors = 0

    def begin_batch(self, target):
        if type(target) is not int or not 1 <= target <= 999:
            raise ValueError('invalid batch target')
        with self._lock:
            self._batch_id += 1
            self._target = target
            self._started = self._completed = self._interrupted = 0

    def begin(self, *, character, mode, idol_id='', produce_id='', run_id=None):
        with self._lock:
            if self._active is not None:
                return self._active['id']
            self._started += 1
            self._active = {'id': uuid.uuid4().hex, 'batch_id': self._batch_id,
                'started_at': self._clock(), 'ended_at': None,
                'character': _text(character), 'mode': _text(mode),
                'idol_id': _text(idol_id), 'produce_id': _text(produce_id),
                'run_id': _text(run_id), 'status': 'running',
                'score': None, 'score_kind': 'final-exam', 'score_conflict': False}
            return self._active['id']

    def bind_run(self, run_id):
        if not isinstance(run_id, str) or not run_id or len(run_id) > 256:
            return
        with self._lock:
            if self._active is None:
                return
            current = self._active['run_id']
            if current and current != run_id:
                # Never move a stale score onto a new or unrelated run.
                self._active['score_conflict'] = True
                self._active['score'] = None
                return
            self._active['run_id'] = run_id

    def observe_native_outcome(self, outcome, *, require_settled=False):
        """Accept final audition score only from the native gateway's evidence.

        The gateway already validates its state and ownership. Do not read the
        GUI's generic 'score'/'P points', a policy value or a last-known estimate.
        Unknown state shapes simply have no displayable score.
        """
        if require_settled and _field(outcome, 'status') != 'settled':
            return
        evidence = _field(outcome, 'evidence')
        if evidence is None:
            return
        run_id = _field(evidence, 'run_id')
        game_state = _field(evidence, 'state')
        stage = _field(game_state, 'step_type_value')
        stage = getattr(stage, 'value', stage)
        if _field(game_state, 'exam_type') != 1 or stage not in (18, 'ProduceStepType_AuditionFinal'):
            return
        runtime = _field(game_state, 'root_runtime')
        if _field(runtime, 'is_exam_end_complete') is not True:
            return
        # Audited LocalSaveExamState.score (not produce_points or a policy value).
        score = _field(game_state, 'score')
        if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 9007199254740991:
            return
        with self._lock:
            a = self._active
            if a is None or not a['run_id'] or run_id != a['run_id'] or a['score_conflict']:
                return
            # Last verified terminal score wins (e.g. a legitimate same-run retry).
            a['score'] = score

    def finish(self, phase):
        phase = _phase(phase)
        if phase not in {'completed', 'cancelled', 'failed', 'blocked'}:
            return
        with self._lock:
            if self._active is None:
                return
            row = self._active
            row['ended_at'] = self._clock()
            row['status'] = 'completed' if phase == 'completed' else 'interrupted'
            row['terminal_phase'] = phase
            if row['status'] == 'completed': self._completed += 1
            else: self._interrupted += 1
            self._rows.append(row)
            self._active = None

    def snapshot(self):
        with self._lock:
            def public(row):
                return {k: deepcopy(row[k]) for k in ('id', 'started_at', 'ended_at',
                    'character', 'mode', 'produce_id', 'status', 'score', 'score_kind')}
            return {'schema': 'gkms.session-results.v1', 'session_id': self.session_id,
                'persistence': 'memory-only', 'score_kind': 'final-exam',
                'rows': [public(r) for r in reversed(self._rows)],
                'batch': {'target': self._target, 'current': self._started,
                    'completed': self._completed, 'interrupted': self._interrupted,
                    'running': self._active is not None},
                'recording_ok': self.observation_errors == 0}
