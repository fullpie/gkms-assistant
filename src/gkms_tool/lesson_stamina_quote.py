"""Master lesson HP costs, distinct from a native card/payment quote.

Only the modified base cost is payable on entry. EndLesson item effects are
later resource changes; they cannot turn an observed legal lesson into an
unaffordable purchase. Their existing unknown-effect diagnostics stay intact.
"""
from copy import deepcopy
from functools import lru_cache
import hashlib
from pathlib import Path

from .qualification_verification import source_stamp
from .rl.contracts import ContractError,digest
from .runtime_outer_policy import _LESSONS,_master_row,_lesson_stamina,_native_state

KIND='master-lesson-stamina'
TABLES=('ProduceStepSelfLesson.yaml','ProduceItem.yaml','ProduceTrigger.yaml',
    'ProduceItemEffect.yaml','ProduceEffect.yaml')


@lru_cache(maxsize=64)
def _verified_bytes(path,before):
    path=Path(path)
    with path.open('rb')as stream:sha=hashlib.file_digest(stream,'sha256').hexdigest()
    if source_stamp(path)!=before:raise ContractError('Lesson quote source changed while reading')
    return sha,before[2]


def _reference(path):
    path=Path(path).resolve();before=source_stamp(path)
    sha,size=_verified_bytes(str(path),before)
    if source_stamp(path)!=before:raise ContractError('Lesson quote source changed while reading')
    return {'path':str(path),'sha256':sha,'bytes':size}


def build_lesson_stamina_quote(raw,target,master_dir):
    if (raw.get('surface')!='schedule' or raw.get('busy')is not False or raw.get('actions_complete')is not True
            or sum(action.get('target')==target for action in raw.get('legal_actions',()))!=1
            or target.get('action_id')!='schedule.choose' or target.get('step_type')not in _LESSONS):
        raise ContractError('Lesson quote requires one current settled legal lesson')
    state=_native_state(raw,raw['state']['produce_id'],raw['progress']['idolCardId'])
    rows=[row for row in raw['collections']['schedule']if row.get('stepNumber')==state['week']+1]
    if len(rows)!=1:raise ContractError('Lesson quote requires the observed next schedule')
    axis,sp=_LESSONS[target['step_type']];field=axis+('Sp'if sp else '')+'ProduceStepSelfLessonId'
    directory=Path(master_dir);refs={name:_reference(directory/name)for name in TABLES}
    lesson=_master_row(directory,'ProduceStepSelfLesson.yaml',rows[0][field])
    context={'self_lesson_stamina_modifiers':tuple(raw['progress'].get('selfLessonTypeStaminaPermils',())),
        'produce_items':tuple(raw['progress'].get('produceItems',()))}
    total,recovery,detail=_lesson_stamina(lesson,target['step_type'],context,directory)
    if refs!={name:_reference(directory/name)for name in TABLES}:
        raise ContractError('Lesson quote source changed during calculation')
    return {'kind':KIND,'snapshot_sha256':digest(raw),'native_target':deepcopy(target),
        'produce_id':state['produce_id'],'idol_card_id':state['idol_card_id'],'week':state['week'],
        'schedule_step':state['week']+1,'schedule_field':field,'lesson_id':lesson['id'],
        'entry_stamina_cost':detail['affected_base_stamina_cost'],
        'end_lesson_stamina_cost':detail['end_lesson_stamina_cost'],
        'end_lesson_stamina_recovery':recovery,'total_stamina_delta_cost':total,
        'stamina_detail':detail,'master_sources':refs,'native_payment_quote':False}


def validate_lesson_stamina_quote(raw,outcome,master_dir):
    expected=build_lesson_stamina_quote(raw,outcome['native_target'],master_dir)
    actual=outcome.get('quote_evidence')
    if digest(actual)!=digest(expected):raise ContractError('Lesson quote differs from current schedule, item counters or Master')
    costs=outcome.get('quoted_costs',{})
    if set(costs)!={'stamina'} or type(costs['stamina'])is not int or costs['stamina']!=expected['total_stamina_delta_cost']:
        raise ContractError('Lesson total HP cost differs from its source calculation')
    return True


def apply_lesson_stamina(before,maximum,cost,recovery,quote):
    """Return bounded HP and any unresolved effect-order diagnostic."""
    if (quote.get('kind')!=KIND or any(type(value)is not int or value<0 for value in (before,maximum,cost,recovery))
            or cost!=quote['entry_stamina_cost']+quote['end_lesson_stamina_cost']):
        raise ContractError('Lesson resource delta differs from its entry/end decomposition')
    entry=quote['entry_stamina_cost'];end=quote['end_lesson_stamina_cost']
    if before<entry:raise ContractError('Outer candidate cannot afford its lesson entry cost')
    remaining=before-entry
    gaps=[]
    # Exact relative order of multiple triggered EndLesson reductions/recoveries
    # is not established by a summed delta. Do not call a clipped estimate ready.
    if end and recovery and (remaining<end or remaining+recovery>maximum):
        gaps.append('lesson-end-stamina-order-at-boundary-unqualified')
    return max(0,min(maximum,remaining-end+recovery)),gaps
