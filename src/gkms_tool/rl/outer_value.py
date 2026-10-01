"""One shared exam Value boundary for explicit hypothetical outer outcomes.

OUTER data never enters the trained exam encoder. Only source-bound, genuinely
materialized native decision States returned by the preview provider can do so.
This module does not send game input, build an engine, or price PT heuristically.
"""
from collections import OrderedDict
from copy import deepcopy
from fractions import Fraction
import math

from .contracts import ContractError, DecisionKind, State, digest, sha256
from .features import encode_batch

SCHEMA = 'gkms.rl.outer-outcome-value.v1'
PREVIEW_KIND = 'owned-native-first-decision'
_PLAYER_FIELDS = frozenset({'vocal','dance','visual','stamina','maxStamina',
    'vocalBonusPermil','danceBonusPermil','visualBonusPermil','produceCards','produceItems','supportCards','produceDrinkIds'})
_OUTCOME_FIELDS = frozenset({'request_id','candidate_id','input_sha256','branch_id','native_target',
    'player_overrides','after_outer_resources','quoted_costs','effect_ledger','before_audition_recovery',
    'assumptions','unsupported'})


def _finite(value, label):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ContractError(label + ' must be finite')
    return float(value)


def _seed(value):
    # Original protobuf JSON encodes this UInt32 as a decimal string; native
    # initialization reports the same value as an integer. Neither is a new RNG.
    if type(value)is str and value.isascii() and value.isdigit() and str(int(value))==value:
        value=int(value)
    if type(value)is not int or not 0<=value<=2**32-1:
        raise ContractError('exact UInt32 scenario seed required')
    return value


def _scenario_binding(request):
    """Candidate deltas may differ; the counterfactual world/seed may not."""
    fixed = {key:value for key,value in request.items() if key not in _OUTCOME_FIELDS}
    source = request.get('original_source_ref',{})
    sha256(source.get('sha256'),'scenario original source')
    sha256(request.get('execution_master_hash'),'scenario execution Master')
    sha256(request.get('observed_snapshot_sha256'),'scenario outer snapshot')
    scope=request.get('scope',{})
    required={'produce_id','idol_card_id','flow_id','mode_id','stage_id','difficulty_number'}
    if set(scope)!=required or type(scope['difficulty_number'])is not int or scope['difficulty_number']<1:
        raise ContractError('complete original exam scope/difficulty required')
    seed=_seed(request.get('source_seed'));key=request.get('scenario_key')
    if key not in (f'seed:{seed}:recovery-lower',f'seed:{seed}:recovery-upper'):
        raise ContractError('scenario key must bind actual source seed and recovery boundary')
    fixed['source_seed']=seed
    overrides=request.get('player_overrides')
    if not isinstance(overrides,dict) or set(overrides)-_PLAYER_FIELDS:
        raise ContractError('outer branches may change only explicit player results')
    recovery=request.get('before_audition_recovery',{})
    if recovery.get('basis')!='max_stamina' or type(recovery.get('recovery_permille'))is not int:
        raise ContractError('explicit common before-audition recovery scenario required')
    fixed['recovery_scenario']={name:recovery.get(name)for name in ('basis','recovery_permille','application_phase','already_applied')}
    return digest(fixed)


def _closed_preview(closure):
    if (closure.get('native_preview_complete') is not True or closure.get('teacher_action_used') is not False
            or closure.get('game_io') is not False):
        raise ContractError('native preview did not establish its observation boundary')
    kind=closure.get('owner_boundary_kind')
    if kind=='fully-cleaned':
        if any(closure.get(key)is not True for key in ('owned_task_closed','hooks_restored','provider_cleanup_observed')):
            raise ContractError('fully cleaned preview needs actual task/hooks/provider evidence')
    elif kind=='single-use-process-discard':
        if (closure.get('process_exit_verified')is not True or closure.get('dedicated_child')is not True
                or not closure.get('process_outcome_ref') or closure.get('hooks_restored')is not False
                or closure.get('provider_cleanup_observed')is not False):
            raise ContractError('discarded preview needs a confirmed dedicated child exit; no cleanup claim')
    else:raise ContractError('explicit native preview owner boundary required')


def _check_model(model, schema, checkpoint, validator):
    identity=validator(model)
    if (not isinstance(identity,dict) or identity.get('checkpoint_sha256')!=checkpoint
            or identity.get('feature_schema_sha256')!=schema.identity or not identity.get('source_bindings')):
        raise ContractError('actual loaded model/version qualification differs')
    metadata=model.checkpoint_metadata()
    if (metadata.get('objective_id')!='remaining_return_iql_v1' or metadata.get('feature_schema_sha256')!=schema.identity
            or metadata.get('gamma')!=1 or metadata.get('score_scale')!=10000):
        raise ContractError('outer comparison needs the qualified shared exam objective/schema')
    devices={str(value.device)for value in model.parameters()}
    if len(devices)!=1:raise ContractError('shared outer evaluator requires one observed model device')
    return {**identity,'inference_device':next(iter(devices))}


def _chance_weights(outcome, branch_ids):
    proof=outcome.get('uncertainty')or{};ids=proof.get('branch_ids',())
    values=proof.get('probabilities',())
    if (proof.get('kind')!='single-marginal-chance' or proof.get('distribution_known')is not True
            or proof.get('joint_independence_claimed')is not False or proof.get('chance_projection_complete')is not True
            or not proof.get('coverage_evidence') or not isinstance(ids,(list,tuple))or len(ids)!=2
            or any(not isinstance(value,str)or not value for value in ids)or len(set(ids))!=2
            or set(ids)!=set(branch_ids) or not isinstance(values,list)or len(values)!=2):
        raise ContractError('One source-qualified complete marginal chance split required')
    weights={}
    for key,value in zip(ids,values):
        if (not isinstance(value,dict)or set(value)!={'numerator','denominator'}
                or type(value['numerator'])is not int or type(value['denominator'])is not int
                or not 0<value['numerator']<value['denominator']):
            raise ContractError('Exact positive proper rational marginal probabilities required')
        weights[key]=Fraction(value['numerator'],value['denominator'])
    if sum(weights.values())!=1:raise ContractError('Complete marginal probabilities must sum to one exactly')
    return weights


def _world_choice_groups(outcome, branch_ids):
    proof=outcome.get('uncertainty')or{}
    if (proof.get('kind')!='complete-possible-world-superset' or proof.get('superset_complete')is not True
            or proof.get('actual_native_pool_claimed')is not False or proof.get('distribution_known')is not False
            or proof.get('probabilities')is not None or not proof.get('coverage_evidence')):
        raise ContractError('Future worlds require a source-qualified superset without invented probabilities')
    worlds=proof.get('worlds');result={};seen=set()
    if not isinstance(worlds,list)or not worlds:
        raise ContractError('Complete future worlds and own-choice groups required')
    for world in worlds:
        key=world.get('world_id');ids=world.get('branch_ids')
        if (not isinstance(key,str)or not key or key in result or world.get('choice_set_complete')is not True
                or not isinstance(world.get('source'),dict)or not world['source']
                or not isinstance(ids,list)or not ids or any(not isinstance(i,str)or not i for i in ids)
                or len(ids)!=len(set(ids))or seen.intersection(ids)):
            raise ContractError('Each future world needs complete distinct player choices and source evidence')
        result[key]=ids;seen.update(ids)
    if seen!=set(branch_ids):raise ContractError('Future worlds must partition every possible choice branch')
    return result


def _branch_rule(outcome, requests):
    groups={}
    for request in requests:groups.setdefault(request['scenario_key'],[]).append(request.get('branch_id','identity'))
    if any(len(values)!=len(set(values))for values in groups.values()):
        raise ContractError('duplicate native outcome branch')
    mode=outcome.get('branch_aggregation')
    if mode is None and all(len(values)==1 for values in groups.values()):return 'single-outcome'
    if mode=='single-marginal-expectation':
        expected=set(next(iter(groups.values())))
        if any(set(values)!=expected for values in groups.values()):
            raise ContractError('Every recovery scenario must cover both marginal outcomes')
        _chance_weights(outcome,expected)
        return mode
    if mode=='best-complete-legal-choice' and outcome.get('choice_set_complete')is True:
        expected=set(next(iter(groups.values())))
        if any(set(values)!=expected for values in groups.values()):
            raise ContractError('Player choices must exist under every unresolved recovery scenario')
        return mode
    if mode=='best-bounded-player-choice':
        item=outcome.get('future_items')
        if item is not None:
            expected=set(next(iter(groups.values())));scenario=item.get('scenario')or{}
            if (item.get('schema')!='gkms.future-produce-item-consequences.v1'
                    or item.get('status')!='conditional-ready' or item.get('model_ready')is not True
                    or item.get('global_optimum_claimed')is not False or item.get('forced_upgrade_skip_invented')is not False
                    or type(item.get('search_complete'))is not bool or not item.get('conditions')or not item.get('source_ref')
                    or scenario.get('partition')!='train' or scenario.get('current_run_future_state_used')is not False
                    or not scenario.get('source_group') or not scenario.get('whole_trajectory_id')
                    or scenario.get('source_ref')!=item.get('source_case')
                    or set(item.get('branch_ids',()))!=expected
                    or any(set(values)!=expected for values in groups.values())
                    or any(r.get('original_source_ref',item['source_case'])!=item['source_case']for r in requests)
                    or any('after_outer_resources'in r and r['after_outer_resources'].get('vote_count')!=
                        item.get('current_resources',{}).get('vote_count')for r in requests)):
                raise ContractError('Bounded item choices need the same TRAIN source and explicit conditional effect proof')
            return mode
        proof=outcome.get('future_visit')or{};expected=set(next(iter(groups.values())))
        if (proof.get('schema')!='gkms.outer-future-customization-expansion.v1'
                or proof.get('status')!='ready'or proof.get('policy_forecast_allowed')is not True
                or proof.get('global_optimum_claimed')is not False or proof.get('no_spend_preserved')is not True
                or type(proof.get('search_complete'))is not bool or not proof.get('conditions')
                or not proof.get('source_ref')or set(proof.get('branch_ids',()))!=expected
                or any(set(values)!=expected for values in groups.values())):
            raise ContractError('Bounded future player choices need source evidence, no-spend and matched scenarios')
        return mode
    if mode=='world-min-choice-max':
        expected=set(next(iter(groups.values())))
        if any(set(values)!=expected for values in groups.values()):
            raise ContractError('Each recovery scenario needs the same complete future worlds')
        _world_choice_groups(outcome,expected)
        return mode
    if mode=='covered-result-envelope':
        proof=outcome.get('uncertainty',{})
        expected=set(next(iter(groups.values())))
        if (proof.get('kind')!='complete-possible-result-superset' or proof.get('superset_complete')is not True
                or proof.get('actual_native_pool_claimed')is not False or proof.get('distribution_known')is not False
                or proof.get('probabilities')is not None or not proof.get('coverage_evidence')
                or set(proof.get('branch_ids',()))!=expected or any(set(values)!=expected for values in groups.values())):
            raise ContractError('uncertain future results require one explicitly covered superset, not an invented distribution')
        return mode
    raise ContractError('multiple branches require complete legal choice or evidenced result-envelope semantics')


def validate_native_preview(request, preview, *, checkpoint_sha256, schema, model_identity):
    """Validate one real preview independently of complete strategic effects."""
    state=preview.get('state');closure=preview.get('completion',{})
    _closed_preview(closure)
    if (preview.get('request_id')!=request['request_id'] or preview.get('input_sha256')!=request['input_sha256']
            or preview.get('checkpoint_sha256')!=checkpoint_sha256 or preview.get('status')!='ready'
            or preview.get('source_kind')!=PREVIEW_KIND or not isinstance(state,State)
            or state.kind not in (DecisionKind.MAIN,DecisionKind.SECONDARY)
            or not preview.get('native_observation_ref') or not preview.get('projection_id')):
        raise ContractError('real closed native exam decision preview required; OUTER is not an exam input')
    scope=request['scope']
    if any(getattr(state.scope,key)!=scope[key]for key in ('flow_id','mode_id','stage_id')):
        raise ContractError('native preview flow/mode/stage differs')
    trained=preview.get('trained_source_binding',{});trained_id=digest(trained)
    owner=preview.get('engine_identity',{})
    from ..observed_empty_collections import PC_SOURCE
    for field in ('inputs_reference','initialization_reference'):
        sha256(owner.get(field,{}).get('sha256'),'owned preview '+field)
    sha256(preview['native_observation_ref'].get('sha256'),'native observation reference')
    if (model_identity['source_bindings'].get(trained_id)!=trained
            or trained.get('source_kind')!=PC_SOURCE or not trained.get('engine_id')
            or trained.get('source_master_hash')!=request['execution_master_hash']
            or trained.get('projection_id')!=preview.get('training_projection_id')
            or trained.get('feature_schema_sha256')!=schema.identity
            or owner.get('source_reference',{}).get('sha256')!=request['original_source_ref']['sha256']
            or any(preview.get(key)!=scope[key]for key in ('produce_id','idol_card_id','difficulty_number'))
            or _seed(preview.get('source_seed'))!=_seed(request['source_seed'])
            or state.scope.run_id!=owner.get('run_id') or state.scope.exam_id!=request['original_source_ref']['sha256']
            or type(owner.get('worker_pid'))is not int or owner['worker_pid']<1
            or state.session_generation!='owned-PC:'+str(owner.get('worker_pid'))
            or state.revision!=preview.get('observation_sha256')
            or state.binding_id!=digest({'checkpoint':checkpoint_sha256,'trained_source':trained_id,'owner':owner})):
        raise ContractError('native state binding/source/projection differs from fixed request and actual model')
    return state


def evaluate_outer_outcomes(outcomes, *, checkpoint_sha256, model, schema,
                            preview_provider=None, cache=None, max_batch=32, validate_model=None):
    """Rank complete paired exam previews; incomplete candidates block ranking.

Each outcome has candidate_id/native_target/requests/unsupported/assumptions.
The provider consumes all uncached requests together. Unknown future outcome
probabilities are never fabricated. Explicit scenario endpoints are compared
using their conservative lower model value, with upper values retained too.
    """
    sha256(checkpoint_sha256, 'fixed outer evaluator checkpoint')
    if not isinstance(outcomes, (tuple, list)) or not 1 <= len(outcomes) <= 128:
        raise ContractError('bounded nonempty outer candidate set required')
    if type(max_batch) is not int or not 1 <= max_batch <= 256:
        raise ContractError('bounded shared inference batch required')
    ids = [row.get('candidate_id') for row in outcomes]
    if any(not isinstance(value, str) or not value for value in ids) or len(ids) != len(set(ids)):
        raise ContractError('unique outer candidate IDs required')
    failures = [{'candidate_id': row['candidate_id'], 'reasons': list(row.get('unsupported', ()))}
        for row in outcomes if row.get('unsupported')]
    requests, paired, branch_rules = [], None, {}
    for outcome in outcomes:
        rows = outcome.get('requests')
        if not isinstance(rows, (tuple, list)) or not rows:
            failures.append({'candidate_id': outcome['candidate_id'], 'reasons': ['exam-input-request-missing']})
            continue
        scenarios = {}
        for row in rows:
            if row.get('checkpoint_sha256') != checkpoint_sha256 or row.get('candidate_id') != outcome['candidate_id']:
                raise ContractError('outer input changed candidate or fixed model')
            payload = {key: value for key, value in row.items() if key != 'input_sha256'}
            if row.get('input_sha256') != digest(payload): raise ContractError('outer input digest differs')
            binding=_scenario_binding(row)
            if row['scenario_key']in scenarios and scenarios[row['scenario_key']]!=binding:
                raise ContractError('choice branches changed a fixed paired exam scenario')
            scenarios[row['scenario_key']]=binding
        if paired is None: paired = scenarios
        elif paired != scenarios: raise ContractError('outer candidates use different exam scenarios')
        branch_rules[outcome['candidate_id']]=_branch_rule(outcome,rows)
        requests.extend(rows)
    if len(requests) > 512: raise ContractError('outer preview request budget exceeded')
    if len({row['request_id'] for row in requests}) != len(requests): raise ContractError('duplicate outer preview request')
    result = {'schema': SCHEMA, 'checkpoint_sha256': checkpoint_sha256, 'status': 'not-ready',
        'model_used': False, 'ranking': [], 'unavailable': failures, 'legacy_rule_fallback': False,
        'units': 'uncalibrated-exam-total-return-proxy', 'calibrated': False,
        'ranking_basis':'maximin-explicit-model-proxy-scenarios','future_distribution_assumed':False,
        'game_score_bounds_claimed':False,
        'game_io': False, 'automatic_model_activation': False, 'reward_changed': False,
        'assumptions': sorted({value for row in outcomes for value in row.get('assumptions', ())})}
    if failures: return result
    if preview_provider is None:
        result['unavailable'] = [{'reason': 'native-first-decision-preview-unavailable'}]
        return result
    if model is None or schema is None:
        result['unavailable'] = [{'reason': 'qualified-shared-exam-model-unavailable'}]
        return result
    if not callable(validate_model):
        result['unavailable']=[{'reason':'actual-model-identity-validator-required'}]
        return result
    if not isinstance(getattr(preview_provider, 'identity', None), str) or not preview_provider.identity:
        raise ContractError('source-bound native preview provider identity required')
    validate_preview = getattr(preview_provider, 'validate_unchanged', None)
    if not callable(validate_preview):raise ContractError('source-bound preview validation callback required')
    validate_preview()
    model_identity=_check_model(model,schema,checkpoint_sha256,validate_model)
    cache = OrderedDict() if cache is None else cache
    keys = {row['request_id']: digest({'checkpoint': checkpoint_sha256, 'input': row['input_sha256'],
        'schema': schema.identity, 'model_identity':model_identity,
        'preview_provider': preview_provider.identity}) for row in requests}
    missing = [row for row in requests if keys[row['request_id']] not in cache]
    previews = preview_provider(tuple(missing)) if missing else ()
    if not isinstance(previews, (tuple, list)) or len(previews) != len(missing):
        raise ContractError('preview provider omitted a candidate request')
    pending, pending_keys = [], []
    for request, preview in zip(missing, previews):
        if (preview.get('request_id') != request['request_id'] or preview.get('input_sha256') != request['input_sha256']
                or preview.get('checkpoint_sha256') != checkpoint_sha256):
            raise ContractError('native preview belongs to another input/model')
        if preview.get('status') != 'ready':
            result['unavailable'].append({'candidate_id': request['candidate_id'], 'request_id': request['request_id'],
                'reason': preview.get('reason', 'native-preview-incomplete')})
            continue
        validate_native_preview(request,preview,checkpoint_sha256=checkpoint_sha256,schema=schema,model_identity=model_identity)
        pending.append(preview); pending_keys.append(keys[request['request_id']])
    if result['unavailable']: return result
    import torch
    staged_cache={}
    with torch.inference_mode():
        for start in range(0, len(pending), max_batch):
            batch = pending[start:start + max_batch]
            encoded=encode_batch([row['state']for row in batch],schema).to(torch.device(model_identity['inference_device']))
            values = model.state_value(encoded).tolist()
            if len(values) != len(batch): raise ContractError('shared value output count differs')
            for offset, (preview, value) in enumerate(zip(batch, values)):
                normalized = _finite(value, 'normalized remaining value')
                observed = _finite(preview['observed_score'], 'native preview score')
                score = _finite(observed + 10000 * normalized, 'exam value proxy')
                staged_cache[pending_keys[start + offset]] = {'value': score, 'normalized_remaining_value': normalized,
                    'observed_score': observed, 'native_observation_ref': preview['native_observation_ref'],
                    'projection_id': preview['projection_id'], 'preview_completion': dict(preview['completion']),
                    'executed_request_id':preview.get('executed_request_id',preview['request_id']),
                    'executed_input_sha256':preview.get('executed_input_sha256',preview['input_sha256']),
                    'current_action_binding':preview.get('current_action_binding'),
                    'native_owner':dict(preview['engine_identity'])}
    if _check_model(model,schema,checkpoint_sha256,validate_model)!=model_identity:
        raise ContractError('actual model identity changed during outer inference')
    validate_preview()
    # A changed model/source invalidates only this uncommitted inference batch;
    # previous qualified cache entries remain untouched.
    resolved={**cache,**staged_cache}
    ranking = []
    for outcome in outcomes:
        scenarios = {}
        for request in outcome['requests']:
            evaluation = resolved[keys[request['request_id']]]
            scenarios.setdefault(request['scenario_key'], []).append({**evaluation,
                'request_id': request['request_id'], 'branch_id': request.get('branch_id', 'identity')})
        mode=branch_rules[outcome['candidate_id']];chosen={};lower_values=[];upper_values=[]
        fixed_choices={}
        if mode in ('world-min-choice-max','best-complete-legal-choice','best-bounded-player-choice'):
            branch_ids=[row['branch_id']for row in next(iter(scenarios.values()))]
            groups=(_world_choice_groups(outcome,branch_ids)if mode=='world-min-choice-max'
                else {'complete-player-choice':branch_ids})
            by_scenario={key:{row['branch_id']:row for row in rows}for key,rows in scenarios.items()}
            # The reward is observed before choosing what to keep. Future
            # audition recovery is not: choose once per reward world, robustly
            # across its still-unresolved recovery endpoints, not separately
            # after seeing each endpoint's future model value.
            fixed_choices={world:max(ids,key=lambda branch:(
                min(rows[branch]['value']for rows in by_scenario.values()),branch))
                for world,ids in groups.items()}
        for key,rows in scenarios.items():
            if mode=='single-marginal-expectation':
                weights=_chance_weights(outcome,[row['branch_id']for row in rows])
                lower=upper=sum(float(weights[row['branch_id']])*row['value']for row in rows)
                chosen[key]={'value':lower,'branches':rows,'probabilities_assumed':False,
                    'aggregation':'source-qualified-single-marginal-expectation',
                    'expected_model_proxy_not_actual_score':True}
            elif mode in ('best-complete-legal-choice','best-bounded-player-choice'):
                best=next(row for row in rows if row['branch_id']==fixed_choices['complete-player-choice'])
                lower=upper=best['value'];chosen[key]=best
            elif mode=='world-min-choice-max':
                by_id={row['branch_id']:row for row in rows}
                best={world:by_id[branch]for world,branch in fixed_choices.items()}
                lower=min(row['value']for row in best.values());upper=max(row['value']for row in best.values())
                chosen[key]={'lower':lower,'upper':upper,'best_player_choice_by_world':best,'branches':rows,
                    'aggregation':'best-complete-player-choice-then-unknown-world-envelope',
                    'player_choice_fixed_before_unresolved_recovery':True,
                    'actual_future_draw_pool_claimed':False,'distribution_assumed':False}
            else:
                lower=min(row['value']for row in rows);upper=max(row['value']for row in rows)
                chosen[key]=rows[0]if mode=='single-outcome'else {'lower':lower,'upper':upper,'branches':rows,
                    'actual_future_legal_pool_claimed':False,'distribution_assumed':False}
            lower_values.append(lower);upper_values.append(upper)
        lower=min(lower_values);upper=max(upper_values)
        ranking.append({'candidate_id': outcome['candidate_id'], 'native_target': outcome['native_target'],
            'transaction_proposal':deepcopy(outcome.get('transaction_proposal')),
            'strategic_plan':deepcopy(outcome.get('strategic_plan')),
            'future_visit':deepcopy(outcome.get('future_visit')),
            'value': lower,'value_interval':{'lower':lower,'upper':upper,'game_score_bound':False},
            'aggregation':{'branch':mode,'scenario':'conservative-lower-envelope','probabilities_assumed':False},
            'uncertainty':deepcopy(outcome.get('uncertainty')),
            'ranking_basis':'maximin-explicit-model-proxy-scenarios','scenarios': chosen})
    cache.update(staged_cache)
    while len(cache) > 512: cache.pop(next(iter(cache)))
    result.update(status='ready', model_used=True, ranking=sorted(ranking, key=lambda row: (-row['value'], row['candidate_id'])),
        inference_requests=len(missing), cache_hits=len(requests)-len(missing))
    return result
