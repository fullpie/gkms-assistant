"""Explicit operational-only recovery after all three native audition wins.

This scope has no deck/ancestry/training qualification and cannot enter another
cultivation decision, exam, shop or AP-consuming preparation.
"""
from __future__ import annotations
from collections.abc import Mapping
from copy import deepcopy

SCHEMA = 'gkms.final-presentation-checkpoint.v1'
SCOPE = 'final-presentation-only'
STAGES = {'ProduceStepType_AuditionMid1', 'ProduceStepType_AuditionMid2', 'ProduceStepType_AuditionFinal'}
SCALARS = ('in_progress', 'produce_id', 'week', 'step_type', 'progress_status', 'stamina', 'max_stamina',
    'produce_points', 'vocal', 'dance', 'visual', 'vote_count', 'star', 'star_permil',
    'user_selection_memory_id', 'result_selection_memory')


def _fail(reason):
    from .runtime_session_continuity import _fail as stop
    stop(reason)


def signature(raw, run):
    progress, state = raw.get('progress'), raw.get('state')
    if not isinstance(progress, Mapping) or not isinstance(state, Mapping):
        _fail('final presentation has no actual persisted progress/scalars')
    if (state.get('in_progress') is not True or state.get('produce_id') != run.produce_id
            or any(progress.get(k) != v for k,v in {'produceId':run.produce_id,'idolCardId':run.idol_card_id,'characterId':run.character_id}.items())
            or progress.get('stepType') != 'ProduceStepType_AuditionFinal'
            or progress.get('status') != 'ProduceProgressStatus_CharacterEventEnding'
            or progress.get('isFailedProduce') is True or type(progress.get('stepNumber')) is not int
            or type(progress.get('stepSelectNumber')) is not int
            or any(key not in state for key in SCALARS)):
        _fail('final-presentation recovery requires the original ended audition/story boundary')
    auditions = progress.get('auditions')
    if (not isinstance(auditions, list) or len(auditions) != 3
            or {row.get('auditionStepType') for row in auditions if isinstance(row, Mapping)} != STAGES
            or any(not isinstance(row, Mapping) or type(row.get('rank')) is not int or row['rank'] != 1
                or type(row.get('score')) is not int or row['score'] <= 0 for row in auditions)):
        _fail('final-presentation recovery requires all three recorded native first places')
    for key, expected in (('idol_card_id',run.idol_card_id),('character_id',run.character_id)):
        if key in state and state[key] != expected:
            _fail('final presentation scalar identity differs')
    return {'schema':SCHEMA, 'values':deepcopy(dict(progress)), 'scalar_state':{k:deepcopy(state[k]) for k in SCALARS},
        'cards_continuity_verified':False, 'launch_lineage_verified':False, 'training_admitted':False,
        'scope':SCOPE}


def validate_preflight(raw, run):
    ui=raw.get('ui_state') or {}
    if (raw.get('screen_type') != 'LiveScenePresenter' or raw.get('surface') != 'presentation'
            or raw.get('busy') is not False or ui.get('family') != 'live_presentation'
            or ui.get('identity_bound') is not True or ui.get('live_from_type') != 1):
        _fail('final-presentation recovery is restricted to the original identified Live preflight')
    return signature(raw,run)


def enforce(raw, run, checkpoint):
    """Called on every observed frame before the existing runner can select input."""
    from .runtime_login_touch import LOGIN_SCREENS
    screen=raw.get('screen_type');state=raw.get('state') or {};progress=raw.get('progress') or {}
    if screen in {'TitlePresenter','HomeTopScreenPresenter','HomeProduceProgressSheetPresenter'} | LOGIN_SCREENS:
        # The existing recovery chooser owns login/resume; a completed Home is
        # handled by the normal completion-evidence gate before another action.
        return 'terminal-home' if screen=='HomeTopScreenPresenter' and state.get('in_progress') is False else 'login-resume'
    identity={'produceId':run.produce_id,'idolCardId':run.idol_card_id,'characterId':run.character_id}
    if any(progress.get(k)!=v for k,v in identity.items()) or progress.get('auditions')!=checkpoint['values']['auditions']:
        _fail('final-presentation-only recovery lost its original identity or three native results')
    # Three recorded wins plus the final step bind the completed auditions.
    # Dialogue and result creation legitimately change both screen and status.
    # The admitted action families below keep recovery within the final flow.
    if (progress.get('stepType') != 'ProduceStepType_AuditionFinal'
            or progress.get('isFailedProduce') is True):
        _fail('final-presentation-only recovery refuses nonterminal cultivation progress')
    nav={'produce.evaluation_continue','produce.result_finish','reward.continue'}
    for action in raw.get('legal_actions',[]):
        action_id=action.get('action_id','');target=action.get('target') or {}
        if action_id == 'live.loading_continue':
            from .runtime_live_loading import loading_target
            if loading_target(raw) == target:
                continue
            _fail('final-presentation-only recovery received an unqualified loading control')
        if (action_id.startswith('result.') or action_id in {'story.skip','story.advance','story.confirm_skip',
                'effect.advance','effect.confirm_card_change','notice.dearness_advance','notice.dismiss_startup_image'}
                or action_id=='ui.navigation' and target.get('button_id') in nav):
            continue
        _fail('final-presentation-only recovery refuses an action outside final story/result completion')
    return 'final-presentation'
