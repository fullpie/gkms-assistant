"""Advisory score units for the fixed actor's uncalibrated remaining-return V.

These values never select an action. The trained network and source hashes are
unchanged; the existing encoder output is observed once for an extra V head.
"""
from copy import deepcopy
import math
import threading
import time

SCHEMA = 'gkms.rl-score-prediction.v1'


def greedy_actions_with_value(model, batch):
    """Keep the original actor call and reuse its encoder output, without patches."""
    encoded = []
    thread = threading.get_ident()
    def capture(_module, _inputs, output):
        if threading.get_ident() == thread:
            encoded.append(output[:, 0])
    encoder = getattr(model, 'encoder', None)
    if not callable(getattr(encoder, 'register_forward_hook', None)):
        return model.greedy_actions(batch), None, 'value-encoder-unavailable'
    hook = encoder.register_forward_hook(capture)
    try:
        actions = model.greedy_actions(batch)
    finally:
        hook.remove()
    try:
        if len(encoded) != 1:
            raise ValueError('Expected one actor encoder observation')
        value = model.expectile_v(encoded[0]).squeeze(-1)
        if tuple(value.shape) != (1,) or not math.isfinite(float(value.item())):
            raise ValueError('Single finite remaining-return value required')
        return actions, float(value.item()), None
    except (AttributeError, TypeError, ValueError, RuntimeError) as error:
        # A display-head failure does not replace or alter the legal actor action.
        return actions, None, 'value-estimate-unavailable:' + type(error).__name__


def from_remaining_value(value, *, score, metadata, model_sha256, run_id,
                         observation, now=None, unavailable_reason=None):
    if (metadata.get('objective_id') != 'remaining_return_iql_v1'
            or metadata.get('gamma') != 1 or metadata.get('score_scale') != 10000
            or metadata.get('value_semantics') != 'expectile-remaining-return-not-total-score-expectation'):
        raise ValueError('Score display requires the verified remaining-return objective and scale')
    if type(score) not in (int, float) or not math.isfinite(score) or score < 0:
        raise ValueError('Current observed native score is invalid')
    available = type(value) in (int, float) and math.isfinite(value)
    total = score + metadata['score_scale'] * value if available else None
    if total is not None and not math.isfinite(total):
        available, total = False, None
    return {'schema': SCHEMA, 'available': available, 'current': available, 'score': total,
        'last_confirmed': False, 'last_confirmed_score': None,
        'observed_score': score, 'normalized_remaining_value': value if available else None,
        'score_scale': metadata['score_scale'], 'objective_id': metadata['objective_id'],
        'source': 'RL-Value-remaining-return', 'calibrated': False, 'estimated': True,
        'run_id': run_id, 'model_sha256': model_sha256,
        'sequence_id': observation['sequence_id'], 'session_generation': observation['session_generation'],
        'source_revision': observation['revision'], 'source_state_sha256': observation['state_sha256'],
        'source_dto_sha256': observation['dto_sha256'], 'stage': observation['stage'],
        'boundary': 'observed-main', 'after_settled_request_id': None,
        'updated_monotonic': time.monotonic() if now is None else now,
        'reason': None if available else (unavailable_reason or 'value-estimate-unavailable')}


def unavailable(prediction, reason):
    result = deepcopy(prediction) if isinstance(prediction, dict) else {'schema': SCHEMA}
    result.update(available=False, current=False, score=None, reason=reason,
                  last_confirmed=False, last_confirmed_score=None)
    return result


def pending_update(prediction):
    result = unavailable(prediction, 'pending-action')
    if (isinstance(prediction, dict) and prediction.get('available') is True
            and prediction.get('boundary') == 'settled-main'
            and isinstance(prediction.get('after_settled_request_id'), str)
            and prediction['after_settled_request_id']
            and type(prediction.get('score')) in (int, float) and math.isfinite(prediction['score'])):
        result.update(last_confirmed=True, last_confirmed_score=prediction['score'])
    return result


def terminal_prediction(prediction, *, score, request_id, state_sha256, now=None):
    if (not isinstance(prediction, dict) or type(score) not in (int, float)
            or not math.isfinite(score) or score < 0 or not request_id or not state_sha256):
        return unavailable(prediction, 'terminal-score-unavailable')
    return {**deepcopy(prediction), 'available': True, 'current': True, 'score': score, 'observed_score': score,
        'last_confirmed': False, 'last_confirmed_score': None,
        'normalized_remaining_value': None, 'estimated': False, 'source': 'native-terminal-score',
        'boundary': 'settled-terminal', 'after_settled_request_id': request_id,
        'source_state_sha256': state_sha256, 'source_dto_sha256': None, 'source_revision': None,
        'updated_monotonic': time.monotonic() if now is None else now, 'reason': None}


def for_display(prediction, *, actual_policy, run_id, state, running, pending,
                retained=False, observation_identity=None, now=None, max_age=15.):
    """Do not carry a prior run/model/step estimate across the GUI boundary."""
    if not isinstance(prediction, dict):
        return unavailable(None, 'not-observed')
    if retained or not running:
        return unavailable(prediction, 'inactive-observation')
    latched = (prediction.get('last_confirmed') is True and prediction.get('reason') == 'pending-action'
               and prediction.get('current') is False and prediction.get('boundary') == 'settled-main'
               and isinstance(prediction.get('after_settled_request_id'), str)
               and bool(prediction['after_settled_request_id']))
    if prediction.get('available') is not True and not latched:
        return unavailable(prediction, prediction.get('reason') or 'value-estimate-unavailable')
    model = actual_policy if isinstance(actual_policy, dict) else {}
    current = state if isinstance(state, dict) else {}
    identity = observation_identity if isinstance(observation_identity, dict) else {}
    def sequence(value):
        try:
            text = str(value).removeprefix('native-sequence:')
            return int(text, 16 if text.lower().startswith('0x') else 10)
        except (TypeError, ValueError):
            return value
    stamp = prediction.get('updated_monotonic')
    age = (time.monotonic() if now is None else now) - stamp if type(stamp) in (int, float) else math.inf
    if (prediction.get('schema') != SCHEMA
            or not run_id or prediction.get('run_id') != run_id or model.get('run_id') != run_id
            or model.get('variant_id') != 'rl_shared_iql' or model.get('loaded') is not True
            or prediction.get('model_sha256') != model.get('model_sha256')
            or prediction.get('observed_score') != current.get('score')
            or prediction.get('stage') != {16:'Mid1',17:'Mid2',18:'Final'}.get(current.get('step_type_value'))
            or sequence(prediction.get('sequence_id')) != sequence(identity.get('session_transition_id'))
            or prediction.get('session_generation') != identity.get('session_generation')
            or prediction.get('source_revision') != identity.get('source_revision')
            or not 0 <= age <= max_age):
        return unavailable(prediction, 'stale-or-unbound-observation')
    value = prediction.get('last_confirmed_score') if latched else prediction.get('score')
    if type(value) not in (int, float) or not math.isfinite(value):
        return unavailable(prediction, 'nonfinite-score')
    if pending and not latched:
        return pending_update(prediction)
    return deepcopy(prediction)
