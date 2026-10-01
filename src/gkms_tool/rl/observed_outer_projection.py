"""Causal observed OUTER inputs using the existing semantic entity codec."""
from dataclasses import replace
from copy import deepcopy

from .contracts import ContractError,DecisionKind,FrozenJSON,Scope,State,digest
from .features import FeatureSchema,EntityBatch
from .semantic_entity_encoding import ENTITY_FEATURE_NAMES,encode_semantic_tokens
from ..outer_behavior_candidate import candidate_semantics

VERSION='gkms.rl.observed-outer-projection.v1'
OUTER_TYPES=('outer_context','outer_history','outer_offer')
OUTER_ZONES=('outer_current','outer_history','outer_offered')
ACTION_TYPES=('padding','play','drink','end_turn','select','schedule','event','take_card','upgrade',
    'customize','remove','duplicate','take_drink','replace_drink','buy','decline','confirm')
ACTION_IDS={name:index for index,name in enumerate(ACTION_TYPES)}
OUTER_OBJECTIVE='recorded-produce-rating-v1'
SUBTYPES=('outer-weekly-option',)
RESOURCE_MAP={'stamina':'stamina','max_stamina':'maxStamina','produce_points':'producePoint',
    'vocal':'parameterVocal','dance':'parameterDance','visual':'parameterVisual'}


def extend_schema(original):
    if not isinstance(original,FeatureSchema)or original.entity_names!=ENTITY_FEATURE_NAMES:
        raise ContractError('Observed OUTER requires the original reviewed 512-column entity codec')
    if set(OUTER_TYPES)&set(original.entity_types)or set(OUTER_ZONES)&set(original.zones):
        raise ContractError('Initialize observed OUTER from the unexpanded original exam schema')
    return replace(original,entity_types=(*original.entity_types,*OUTER_TYPES),zones=(*original.zones,*OUTER_ZONES))


def lift_exam_states(batch,original,expanded):
    """The categorical IDs/dimensions and all tensor storage stay unchanged."""
    if batch is None:return None
    if not isinstance(batch,EntityBatch):raise ContractError('Typed existing entity batch required')
    if batch.schema_id==expanded.identity:return batch
    if batch.schema_id!=original.identity or extend_schema(original)!=expanded:
        raise ContractError('Exam input is not the explicit append-only schema parent')
    return replace(batch,schema_id=expanded.identity)


def _scalar(tokens,path,value):
    if value is None:tokens.append('missing:'+path)
    elif type(value)is bool:tokens.append(path+':bool:'+str(int(value)))
    elif type(value)is int:tokens.append(path+':int:'+str(value))
    elif isinstance(value,str):tokens.append(path+':str:'+value)
    else:raise ContractError('Unexpected observed scalar at '+path)


def _candidate_tokens(candidate,prefix):
    expected=candidate_semantics(candidate['step_type'])
    if any(candidate.get(k)!=v for k,v in expected.items()):
        raise ContractError('Observed candidate differs from the shared exact Normal/SP mapping')
    if candidate.get('observed_offered')is not True or candidate.get('resource_feasibility')not in (None,True,False):
        raise ContractError('Observed-offer provenance and unknown/observed resource feasibility required')
    tokens=[]
    for key in (*expected,'step_sub_parameter_type','observed_offered','resource_feasibility'):
        _scalar(tokens,prefix+'.'+key,candidate.get(key))
    return tokens


def schedule_choice_tokens(step_type,*,step_sub_parameter_type=None,resource_feasibility=None):
    """One exact action meaning for recorded schedules and native choices.

    A recorded offer does not prove affordability. Native legal controls may
    supply True, keeping that extra observation distinct without renaming the
    learned axis/SP/rest semantics.
    """
    candidate={**candidate_semantics(step_type),'step_sub_parameter_type':step_sub_parameter_type,
        'observed_offered':True,'resource_feasibility':resource_feasibility}
    return _candidate_tokens(candidate,'outer.candidate')


def observed_context_tokens(context,week,phase,total_weeks,resources):
    """Shared current facts; missing replay resources remain explicitly absent."""
    tokens=[]
    for key,value in context.items():_scalar(tokens,'outer.context.'+key,value)
    for key,value in (('week',week),('phase',phase),('total_weeks',total_weeks)):
        _scalar(tokens,'outer.'+key,value)
    for key,value in resources.items():_scalar(tokens,'outer.resource.'+key,value)
    return tokens


def project_weekly_observation(row,schema,*,next_observation=False):
    """Read a strict input whitelist. Outcome/rating/current teacher labels are never features."""
    if row.get('decision_kind')not in SUBTYPES or row.get('objective_id')!=OUTER_OBJECTIVE:
        raise ContractError('Explicit observed weekly subtype and separate Produce-rating objective required')
    if not set(OUTER_TYPES)<=set(schema.entity_types)or not set(OUTER_ZONES)<=set(schema.zones):
        raise ContractError('Append-only observed OUTER schema required')
    observed=row.get('next_observation')if next_observation else row.get('observation')
    if not isinstance(observed,dict):raise ContractError('Actual nonterminal observed prefix required')
    allowed={'context','week','phase','total_weeks','past_observations','physical_state','missing','candidates','candidate_semantics'}
    if set(observed)-{'initial_loadout'}!=allowed:raise ContractError('Unreviewed observed OUTER input field')
    context=observed['context'];week=observed['week'];phase=observed['phase'];total=observed['total_weeks']
    if (set(context)!={'produce_id','plan_type','exam_effect_type','idol_card_id'}or
            type(week)is not int or not 1<=week<=total or type(phase)is not int or phase not in (1,2,3)):
        raise ContractError('Complete typed current outer scope/week required')
    history=observed['past_observations']
    if not isinstance(history,list)or [x.get('number')for x in history]!=list(range(1,week)):
        raise ContractError('Only the complete strictly earlier recorded weekly prefix is allowed')
    resources=observed['physical_state'];missing=observed['missing']
    if not isinstance(resources,dict)or not isinstance(missing,dict)or any(type(x)is not bool for x in missing.values()):
        raise ContractError('Explicit observed resource missing masks required')
    globals_={};tokens=['outer.decision_subtype:str:'+row['decision_kind']]
    tokens+=observed_context_tokens(context,week,phase,total,resources)
    for key,value in resources.items():
        if key not in missing or missing[key]!=(value is None):raise ContractError('Missing mask contradicts observed resource '+key)
        if key in RESOURCE_MAP:globals_[RESOURCE_MAP[key]]=value
    for key,value in missing.items():_scalar(tokens,'outer.missing.'+key,value)
    _scalar(tokens,'outer.candidate_semantics',observed['candidate_semantics'])
    entities=[]
    def add(kind,zone,definition,semantic):
        entities.append({'type':kind,'zone':zone,'definition_id':definition,'count':1,'values':encode_semantic_tokens(semantic)})
    add('outer_context','outer_current','outer-context',tokens)
    if 'initial_loadout'in observed:
        from .observed_initial_loadout import prepared_initial_entities
        entities.extend(prepared_initial_entities(observed['initial_loadout']))
    for position,entry in enumerate(history):
        if set(entry)-{'number','stepTypes','selectedStepType','stepSubParameterTypes','observed_rest_recovery','observed_completed_audition'}:
            raise ContractError('Unreviewed earlier-history field')
        if entry.get('selectedStepType')not in entry.get('stepTypes',()):raise ContractError('Past selected step was not offered')
        values=[]
        _scalar(values,'outer.history.position',position)
        values.extend(settled_schedule_history_tokens(entry['selectedStepType'],entry['number'],week))
        for index,offered in enumerate(entry['stepTypes']):_scalar(values,f'outer.history.offer[{index}]',offered)
        for index,subtype in enumerate(entry.get('stepSubParameterTypes',())):_scalar(values,f'outer.history.offer_subtype[{index}]',subtype)
        _scalar(values,'outer.history.rest_recovery',entry.get('observed_rest_recovery'))
        result=entry.get('observed_completed_audition',{})
        if set(result)-{'score','rank','stepSelectNumber','start_state'}:raise ContractError('Unreviewed previous audition result')
        values.extend(completed_audition_result_tokens(score=result.get('score'),tier=result.get('stepSelectNumber'),rank=result.get('rank')))
        if 'start_state'in result:
            if result['start_state']is not None and result['start_state'].get('step_type')!=entry['selectedStepType']:
                raise ContractError('Prior audition checkpoint belongs to a different recorded stage')
            values.extend(audition_milestone_tokens(result['start_state']))
        add('outer_history','outer_history','observed-week',values)
    candidates=[]
    for index,candidate in enumerate(observed['candidates']):
        values=encode_semantic_tokens(_candidate_tokens(candidate,'outer.candidate'))
        candidates.append({'type':ACTION_IDS['schedule'],'values':values,
            'target':{'action_id':'schedule.choose','step_type':candidate['native_step_type']},
            'observed_offered':True,'resource_feasibility':candidate['resource_feasibility']})
        add('outer_offer','outer_offered','schedule-option',_candidate_tokens(candidate,'outer.candidate'))
    if not candidates or len({digest(x['target'])for x in candidates})!=len(candidates):
        raise ContractError('Complete unique recorded weekly offered set required')
    if len(entities)>schema.max_entities:raise ContractError('Observed history/inventory exceeds explicit model limit; never truncate')
    identity=row['identity'];scope=Scope(identity['trajectory_id'],None,
        {'produce-004':'nia-pro','produce-005':'nia-master'}[context['produce_id']],
        context['plan_type']+'|'+context['exam_effect_type'],('Mid1','Mid2','Final')[phase-1])
    information={'schema':schema.identity,'global':globals_,'entities':entities}
    state=State(scope,'recorded-weekly-history:'+row['provenance']['raw']['sha256'],digest(observed),DecisionKind.OUTER,
        FrozenJSON.of(information),digest({'projection':VERSION,'schema':schema.identity,'objective':OUTER_OBJECTIVE,
            'source_group':identity['source_group'],'master':row['provenance']['master_manifest']['sha256']}))
    return {'state':state,'candidates':candidates,'decision_subtype':row['decision_kind'],
        'selection_semantics':'recorded-offers; resource-legality-unknown-when-null','objective_id':OUTER_OBJECTIVE}


def normalized_audition_start(value, *, semantic_source=None):
    """Same past-start facts for recommended history and native entry receipts.

    An observed milestone is not the current deck or a predicted after-state.
    Existing deck parsing preserves duplicates, permanent upgrade counts and
    exact customization ID/count pairs while discarding local slot/GUID data.
    """
    from ..deck_plan import _cards
    from ..nia_static_adapter import _STEP_TYPES
    from collections.abc import Mapping
    if value is None:return None
    required={'timing','step_type','vocal','dance','visual','cards'}
    if not isinstance(value,dict)or set(value)!=required or value['timing']!='audition-start':
        raise ContractError('Explicit observed audition-start milestone required')
    step=_STEP_TYPES.get(value['step_type'])if type(value['step_type'])is int else value['step_type']
    if step not in('ProduceStepType_AuditionMid1','ProduceStepType_AuditionMid2','ProduceStepType_AuditionFinal'):
        raise ContractError('Observed audition milestone stage is invalid')
    if any(type(value[key])is not int or value[key]<0 for key in('vocal','dance','visual')):
        raise ContractError('Observed audition milestone needs actual Vo/Da/Vi')
    if not isinstance(value['cards'],list)or not value['cards']:
        raise ContractError('Observed audition milestone needs its complete actual deck')
    cards=[]
    for card in value['cards']:
        if not isinstance(card,Mapping):raise ContractError('Observed audition card must be an object')
        current=deepcopy(card)
        if 'customizeCountList'in current:
            counts=current['customizeCountList']
            if not isinstance(counts,list)or any(type(x)is not int or x<0 for x in counts):
                raise ContractError('Native past customization counts are invalid')
            if any(counts):
                identity=current.get('produceCardId',current.get('card_id',current.get('id')))
                upgrade=current.get('upgradeCount',current.get('upgrade_count',current.get('upgrade',0)))
                static=(getattr(semantic_source,'payload',{})or{}).get('cards',{}).get(f'{identity}@{upgrade}')
                ids=static.get('customize_ids',[])if isinstance(static,Mapping)else[]
                if len(counts)>len(ids):raise ContractError('Past native customization lacks its exact source slot mapping')
                current['customizes']=[{'id':ids[i],'customizeCount':count}for i,count in enumerate(counts)if count]
            else:current['customizes']=[]
        cards.append(current)
    normalized=[card.to_dict()for card in _cards(cards)]
    if not normalized:raise ContractError('Observed audition milestone has no active cards')
    return {'timing':'audition-start','step_type':step,**{key:value[key]for key in('vocal','dance','visual')},'cards':normalized}


def audition_milestone_tokens(value, *, semantic_source=None, occurrence=0):
    """Shared tokens kept inside the existing history entity, with no new rows."""
    from ..native_structure_features import _emit
    from .native_information_view import public_semantic_tokens
    normalized=normalized_audition_start(value,semantic_source=semantic_source)
    tokens=[];gaps=[]
    if type(occurrence)is not int or occurrence<0:raise ContractError('Audition history occurrence must be nonnegative')
    _emit(f'outer.observed_audition_start[{occurrence}]',normalized,tokens,gaps)
    if gaps:raise ContractError('Observed audition milestone cannot be encoded: '+str(gaps[:3]))
    return list(public_semantic_tokens(tokens))


def settled_schedule_history_tokens(step_type,decision_week,current_week):
    """Known calendar meaning only; native callback ordinals are never weeks."""
    from ..nia_static_adapter import _STEP_TYPES
    step=_STEP_TYPES.get(step_type)if type(step_type)is int else step_type
    if step not in('ProduceStepType_AuditionMid1','ProduceStepType_AuditionMid2','ProduceStepType_AuditionFinal'):
        step=candidate_semantics(step)['step_type']
    if type(decision_week)is not int or type(current_week)is not int or not 1<=decision_week<=current_week:
        raise ContractError('Observed schedule history needs real nonfuture week numbers')
    tokens=[]
    for key,value in (('week',decision_week),('age_weeks',current_week-decision_week),('selected_step',step)):
        _scalar(tokens,'outer.history.'+key,value)
    return tokens


def completed_audition_result_tokens(*,score=None,tier=None,rank=None):
    """Shared observed score/tier; a native win flag does not invent a rank."""
    tokens=[]
    for key,value in (('score',score),('rank',rank),('stepSelectNumber',tier)):
        if value is not None and(type(value)is not int or value<0):raise ContractError('Observed audition result must be a nonnegative integer or unknown')
        _scalar(tokens,'outer.history.completed_audition.'+key,value)
    return tokens
