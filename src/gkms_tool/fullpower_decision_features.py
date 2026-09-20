"""Source-neutral FullPower BC features, separate from native-v4/89-field models.

Both adapters call this encoder. The current typed game state supplies values;
an explicitly version-bound Master supplies effective card/drink semantics.
Source authority and runtime identifiers never enter the learned token bags.
"""
from __future__ import annotations
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, fields, is_dataclass, replace
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import get_type_hints, get_origin, get_args
import json
import math
import types
import typing

from .fullpower_decision_observation import (OBSERVATION_SCHEMA, FullPowerDecisionObservation,
    FullPowerObservationUnavailable, observation_from_mapping, observation_from_native,
    require_bound_master_paths as _bound_paths)

FEATURE_SCHEMA="gkms.fullpower-current-decision-features.v1"
_IGNORED={"authority","evidence_authority","source_origin","source","source_id","schema","schema_version",
    "seed","random","random_state","isReplay","is_replay","origin","trajectory_id","episode_id","split","source_index",
    "uid","native_uid","status_uid","next_status_uid","status_uid_cursor_exact","rid","native_rid","source_sha256",
    "native_source_sha256","instance_id","source_instance_id","source_enchant_instance_id","created_uid","merged_uid",
    "removed_uid","source_path","path","database","master_dir","fingerprint","master_hash","master_version",
    "name","asset_id","assetId","description","description_text","description_reference_name"}
_GUID_FIELDS={"guid","card_guid","source_guid","captured_guid","captured_card_guid","target_guid","sim_card_instance_id"}


@dataclass(frozen=True)
class EncodedFullPowerDecision:
    context_tokens: tuple[str,...]
    candidate_fields: tuple[tuple[str,...],...]
    candidate_keys: tuple[str,...]
    chosen_index: int|None
    source_origin: Mapping
    gaps: tuple[str,...]=()
    feature_schema: str=FEATURE_SCHEMA
    observation_schema: str=OBSERVATION_SCHEMA
    missing_fields: tuple[str,...]=()

    @property
    def complete(self):return not self.gaps


def _walk(prefix,value,guids,*,depth=0):
    if depth>20:raise FullPowerObservationUnavailable(("feature-depth-unavailable:"+prefix,))
    if value is None:return ["missing:"+prefix]
    if isinstance(value,Enum):value=value.value
    if type(value)is bool:return [f"b:{prefix}:{int(value)}"]
    if type(value)in (int,float):
        if not math.isfinite(value):raise FullPowerObservationUnavailable(("nonfinite-feature:"+prefix,))
        exact=str(value) if type(value)is int else format(value,'.17g')
        return [f"n:{prefix}:{exact}",f"coarse:{prefix}:{math.floor(value/8)}"]
    if isinstance(value,str):return [f"s:{prefix}:{guids.get(value,value)}"] if value else ["empty:"+prefix]
    if isinstance(value,Mapping):
        out=[]
        for key,child in sorted(value.items()):
            normalized=key.lstrip('_')
            if key in _IGNORED or normalized in _IGNORED or key.startswith('source_') and key.endswith(('hash','path')):continue
            if key in _GUID_FIELDS or normalized in _GUID_FIELDS:
                if child:out.append(f"relation:{prefix}.{key}:{guids.get(child,'outside-current-zones')}")
                continue
            # UID lists are allocation metadata; applying status order and
            # remaining counts are retained in their actual typed objects.
            if normalized.endswith(('uid','uids','Uid','Uids','UID','UIDs')):continue
            out.extend(_walk(prefix+'.'+key,child,guids,depth=depth+1))
        return out or ["empty-object:"+prefix]
    if isinstance(value,(list,tuple)):
        return [f"count:{prefix}:{len(value)}",*[token for i,child in enumerate(value) for token in _walk(f"{prefix}[{i}]",child,guids,depth=depth+1)]]
    raise FullPowerObservationUnavailable(("unrecognized-feature-value:"+prefix+':'+type(value).__name__,))


@lru_cache(maxsize=128)
def _hints(cls):return get_type_hints(cls)


def _restore(annotation,value):
    if value is None:return None
    origin=get_origin(annotation);args=get_args(annotation)
    if origin in (types.UnionType,typing.Union):
        for typ in args:
            if typ is type(None):continue
            if is_dataclass(typ) and isinstance(value,Mapping):return _restore(typ,value)
        return value
    if origin in (tuple,list,Sequence):
        typ=args[0] if args else object
        converted=[_restore(typ,v) for v in value]
        return tuple(converted) if origin is tuple else converted
    if is_dataclass(annotation):
        if isinstance(value,annotation):return value
        if not isinstance(value,Mapping):raise FullPowerObservationUnavailable(("typed-card-payload-mismatch:"+annotation.__name__,))
        allowed={field.name for field in fields(annotation)}
        if set(value)-allowed:raise FullPowerObservationUnavailable(("unknown-typed-card-field:"+annotation.__name__,))
        hints=_hints(annotation)
        return annotation(**{key:_restore(hints.get(key,object),child) for key,child in value.items()})
    if isinstance(annotation,type) and issubclass(annotation,Enum):return annotation(value)
    return value


def _guids(observation):
    identities={}
    for zone,cards in _card_zones(observation):
        for index,card in enumerate(cards):identities.setdefault(card['guid'],f'{zone}[{index}]')
    return identities


def _card_zones(observation):
    p=observation.payload
    yield from ((zone,p['cards'][zone]) for zone in ('hand','deck','grave','lost','hold'))
    if p['gimmick_runtime'] is not None:
        for family in ('future_decks','past_decks'):
            for index,group in enumerate(p['gimmick_runtime'][family]):
                yield f'gimmick.{family}[{index}]',group


def _card_semantics(card,state,settings,database,master_dir,search_runtime):
    from .plan3_native_state import Plan3NativeCard
    from .plan3_native_search import apply_plan3_native_runtime_growth
    from .plan3_engine import load_plan3_card,_plan3_effective_stamina_costs,_concentration_direct_stamina,COST_STAMINA
    from .plan3_search_stamina_change import resolve_plan3_search_stamina_payment
    native=_restore(Plan3NativeCard,card)
    base=load_plan3_card(native.card_id,native.effective_upgrade,Path(database))
    materialized=apply_plan3_native_runtime_growth(base,native)
    cost_source='card-getter'
    if materialized.cost_type==COST_STAMINA:
        payment=resolve_plan3_search_stamina_payment(search_runtime,native,materialized.stamina_cost,simulate=True)
        cost_source=payment.cost_source
        if cost_source=='search-override':
            materialized=replace(materialized,stamina_cost=payment.selected_stamina_cost)
    ordinary,force=_plan3_effective_stamina_costs(materialized,state,settings)
    hp_payment=max(0,ordinary-state.block)+force
    direct_requested=_concentration_direct_stamina(state,settings) if materialized.cost_type==COST_STAMINA else 0
    return {"effective_card":asdict(materialized),"current_cost":{"ordinary_stamina":ordinary,"unavoidable_stamina":force,
        "block_payment":min(state.block,ordinary),"hp_payment":hp_payment,
        "stance_direct_hp_request":direct_requested,
        "stance_direct_hp_payment":min(direct_requested,max(0,state.stamina-hp_payment)),
        "resource_type":materialized.cost_type,"resource_value":materialized.cost_value,"cost_source":cost_source},
        "runtime":card}


def _typed_context(observation):
    from .plan3_engine import Plan3State,Plan3ExamSettings
    state=Plan3State.from_json(json.dumps({key:value for key,value in observation.payload['scalar'].items()
        if key not in _IGNORED or key in {'next_status_uid','status_uid_cursor_exact'}}))
    settings=_restore(Plan3ExamSettings,observation.payload['settings'])
    from .plan3_search_stamina_change import Plan3SearchStaminaRuntime
    search_runtime=_restore(Plan3SearchStaminaRuntime,observation.payload['cards']['search_stamina_runtime'])
    return state,settings,search_runtime


def _context(observation,database,master_dir):
    from .version_bound_master import bind_master_database
    if not isinstance(observation,FullPowerDecisionObservation):raise TypeError("expected typed FullPower decision observation")
    state,settings,search_runtime=_typed_context(observation);identities=_guids(observation);p=observation.payload
    tokens=["feature-schema:"+FEATURE_SCHEMA];gaps=[]
    # Scalar runtime is the common canonical carrier. Duplicate native status
    # mirrors are validation inputs rather than double-weighted feature bags.
    scalar={k:v for k,v in p['scalar'].items() if k not in {'hand','draw_pile','discard_pile','lost_pile','hold_pile'}}
    # The shared current predicate uses Last/SkipLast only. The full recorder
    # lineage and its source indexes are not current decision state.
    if scalar['play_history'] is not None:
        scalar['play_history']={key:scalar['play_history'][key] for key in ('playing_card_present','semantic_tail')}
    for name,value in [('scope',p['decision_scope']),('current',scalar),('rules',p['settings']),
                       ('schedule',p['turn_parameter_schedule_remaining']),('gimmick',p['gimmick_runtime']),
                       ('native_runtime',{key:value for key,value in p['cards'].items() if key not in
                           {'hand','deck','grave','lost','hold','random_state','enthusiastic_runtime','anti_debuff_runtime'}})]:
        tokens.extend(_walk(name,value,identities))
    with bind_master_database(database):
        for zone,values in _card_zones(observation):
            tokens.append(f'zone-count:{zone}:{len(values)}')
            for index,card in enumerate(values):
                try:semantics=_card_semantics(card,state,settings,database,master_dir,search_runtime)
                except (ValueError,TypeError,KeyError,OSError) as error:
                    gap=f'card-semantics:{zone}[{index}]:{type(error).__name__}:{error}';gaps.append(gap)
                    semantics={'runtime':card,'effective_semantics_unavailable':True}
                tokens.extend(_walk(f'zone.{zone}[{index}]',semantics,identities))
        from .plan3_drink import load_plan3_drink_inventory
        inventory=load_plan3_drink_inventory(tuple(p['inventory']['drink_ids']),database=database,master_dir=master_dir)
        tokens.extend(_walk('drinks',[asdict(drink) for drink in inventory.drinks],identities))
    return tuple(tokens),tuple(gaps)


def _candidate_target(observation,candidate):
    kind=candidate.get('kind',candidate.get('action_type'))
    kind={'play':'use-hand','card':'use-hand','drink':'use-drink','end_turn':'turn-end'}.get(kind,kind)
    slot=candidate.get('slot_index',candidate.get('slot',candidate.get('hand_slot')))
    if kind=='turn-end':return kind,None,None,'END_TURN'
    if type(slot)is not int or slot<0:raise FullPowerObservationUnavailable(('candidate-current-slot-missing',))
    if kind=='use-hand':
        hand=observation.payload['cards']['hand']
        if slot>=len(hand):raise FullPowerObservationUnavailable(('candidate-slot-outside-hand',))
        card=hand[slot];guid=candidate.get('sim_card_instance_id',candidate.get('card_guid',candidate.get('guid')))
        if guid!=card['guid'] or candidate.get('card_id',card['card_id'])!=card['card_id'] or candidate.get(
            'effective_upgrade',candidate.get('upgrade',card['effective_upgrade']))!=card['effective_upgrade']:
            raise FullPowerObservationUnavailable(('candidate-current-card-identity-mismatch',))
        return kind,slot,card,'PLAY:'+guid
    if kind=='use-drink':
        drinks=observation.payload['inventory']['drink_ids']
        if slot>=len(drinks) or candidate.get('drink_id')!=drinks[slot]:raise FullPowerObservationUnavailable(('candidate-current-drink-identity-mismatch',))
        return kind,slot,drinks[slot],f'DRINK:{slot}:{drinks[slot]}'
    raise FullPowerObservationUnavailable(('candidate-family-unavailable:'+str(kind),))


def _candidates(observation,candidates,database,master_dir):
    from .version_bound_master import bind_master_database
    from .plan3_drink import load_plan3_drink_inventory
    state,settings,search_runtime=_typed_context(observation);identities=_guids(observation);rows=[];keys=[];gaps=[]
    with bind_master_database(database):
        for position,candidate in enumerate(candidates):
            kind,slot,target,key=_candidate_target(observation,candidate);keys.append(key)
            values={'kind':kind,'slot':slot,'plays_remaining':state.plays_remaining,'turns_remaining':state.turns_remaining}
            try:
                if kind=='use-hand':values.update(_card_semantics(target,state,settings,database,master_dir,search_runtime))
                elif kind=='use-drink':values['drink']=asdict(load_plan3_drink_inventory((target,),database=database,master_dir=master_dir).drinks[0])
            except (ValueError,TypeError,KeyError,OSError) as error:
                gaps.append(f'candidate-semantics:{position}:{type(error).__name__}:{error}')
                values['effective_semantics_unavailable']=True
            rows.append(tuple(_walk('candidate',values,identities)))
    if len(keys)!=len(set(keys)):raise FullPowerObservationUnavailable(('candidate-identities-duplicated',))
    return tuple(rows),tuple(keys),tuple(gaps)


def build_context_tokens(observation,*,database,master_dir):
    database,master_dir=_bound_paths(database,master_dir)
    tokens,gaps=_context(observation,database,master_dir)
    if gaps:raise FullPowerObservationUnavailable(gaps)
    return tokens


def candidate_fields(observation,candidates,*,database,master_dir):
    database,master_dir=_bound_paths(database,master_dir)
    tokens,keys,gaps=_candidates(observation,candidates,database,master_dir)
    if gaps:raise FullPowerObservationUnavailable(gaps)
    return tokens


def encode_decision_observation(observation,candidates,*,chosen_action=None,database,master_dir):
    database,master_dir=_bound_paths(database,master_dir)
    context,cg=_context(observation,database,master_dir);candidate_rows,keys,ag=_candidates(observation,candidates,database,master_dir)
    chosen_index=None
    if chosen_action is not None:
        key=_candidate_target(observation,chosen_action)[3]
        if key not in keys:raise FullPowerObservationUnavailable(('chosen-action-not-in-supplied-pool',))
        chosen_index=keys.index(key)
    missing=tuple('current.'+key for key in ('play_history','max_stamina') if observation.payload['scalar'][key] is None)
    return EncodedFullPowerDecision(context,candidate_rows,keys,chosen_index,observation.source_origin,
        tuple(dict.fromkeys((*cg,*ag))),missing_fields=missing)


def encode_teacher_transition(row,*,database,master_dir):
    if row.get('schema')!='gkms.simulator-teacher-transition.v1':raise FullPowerObservationUnavailable(('teacher-transition-schema-mismatch',))
    pool=row.get('legal_candidates',{})
    if pool.get('complete')is not True or pool.get('unresolved'):raise FullPowerObservationUnavailable(('teacher-candidate-pool-incomplete',))
    observation=observation_from_mapping(row['sim_state_before'],decision_scope=row['decision_scope'],
        source_origin={'kind':'simulator-teacher','source':row['source'],'evidence_authority':row.get('evidence_authority')})
    return encode_decision_observation(observation,pool['rows'],chosen_action=row['chosen'],database=database,master_dir=master_dir)


def encode_native_decision(raw_native,*,decision_scope,legal_candidates,database,master_dir):
    from .fullpower_native_observation import validate_native_candidate_pool
    database,master_dir=_bound_paths(database,master_dir)
    observation=observation_from_native(raw_native,decision_scope=decision_scope,database=database,master_dir=master_dir)
    validate_native_candidate_pool(observation,legal_candidates)
    return encode_decision_observation(observation,legal_candidates,database=database,master_dir=master_dir)
