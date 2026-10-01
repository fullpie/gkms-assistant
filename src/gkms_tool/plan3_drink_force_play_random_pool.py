"""Detached Plan3 drink RandomPool selection and the existing UsePool owner.

Only selection reuses the shared CardCreateSearch primitive: its placement is
discarded. The real selected Master card is then a cost-free, non-manual
UsePool command, not a fabricated wrapper card or a normal player card.
"""
from __future__ import annotations
from dataclasses import dataclass,replace
from pathlib import Path

from .master_db import DEFAULT_DATABASE
from .plan3_card_create_search import apply_card_create_search,CardCreateSearchChanceInput
from .plan3_engine import PLAN3,load_plan3_effect
from .plan3_use_pool import Plan3UsePoolCommand


def load_plan3_random_pool_force_program(effect_id: str, database: Path=DEFAULT_DATABASE):
    # This loader already cross-checks every original effect/search/pool field,
    # authoritative ProduceCardPool precedence and recursive-force exclusion.
    # No Plan2 execution or Plan2-only candidate selection is used here.
    from .plan2_drink_force_play_random_pool import load_plan2_drink_force_play_random_pool_program
    program=load_plan2_drink_force_play_random_pool_program(Path(database))
    effect=load_plan3_effect(effect_id,Path(database))
    if (program.effect_id!=effect.id or effect.effect_type!="ProduceExamEffectType_ExamForcePlayCardSearch"
            or any((effect.value1,effect.value2,effect.effect_count,effect.effect_turn,effect.status_enchant_id,
                    effect.status_enchant,effect.chain_effect_id,effect.chain_effect_ids,effect.chain_effect,
                    effect.trigger,effect.once,effect.card_move_rule))):
        raise ValueError("Plan3 drink RandomPool complete Master contract mismatch")
    if not any(c.plan_type==PLAN3 for c in program.selector.pool.authoritative_candidates):
        raise ValueError("Plan3 drink RandomPool has no current authoritative Plan3 candidates")
    return program


@dataclass(frozen=True)
class Plan3DrinkForcedPlay:
    scalar_after: object
    native_after: object
    selected_card: object
    selection_trace: object
    command: Plan3UsePoolCommand
    card_step: object


def execute_plan3_random_pool_force_drink(state,native,effect_id,*,guid_token,
        plan_ignore_card_ids,settings,support_upgrades,support_card_searches,
        selected_card_guids=None,database=DEFAULT_DATABASE):
    from .plan3_native_search import execute_plan3_guid_use_pool_transaction
    if not isinstance(guid_token,str) or not guid_token:
        raise ValueError("Plan3 force drink requires a caller-owned generated GUID")
    if not isinstance(plan_ignore_card_ids,tuple):
        raise ValueError("Plan3 force drink requires exact plan-ignore IDs")
    program=load_plan3_random_pool_force_program(effect_id,Path(database))
    selected=apply_card_create_search(native,program.selector,execution_input=CardCreateSearchChanceInput(
        plan_type=PLAN3,plan_ignore_card_ids=plan_ignore_card_ids,hand_limit=settings.hand_limit,guid_tokens=(guid_token,)))
    if not selected.resolved or len(selected.trace.selected_card_ids)!=1:
        raise ValueError("Plan3 force drink RandomPool selection unresolved")
    card=selected.after.card_by_guid(guid_token)
    # Preserve only the selection's exam RNG suffix. The temporary placement
    # does not create a HandAdd event or alter the actual zone/card universe.
    random_native=replace(native,random_state=selected.trace.random_state_after)
    command=Plan3UsePoolCommand(card.guid,is_consume_cost=False,detached_card=card)
    result=execute_plan3_guid_use_pool_transaction(state,random_native,command,
        beam_width=4096,settings=settings,database=Path(database),support_upgrades=support_upgrades,
        support_card_searches=support_card_searches,battle_ranking_resolved=True,
        plan_type=PLAN3,plan_ignore_card_ids=plan_ignore_card_ids)
    matches=[]
    for path in result.candidates:
        steps=[s for s in path.steps if s.card_transition is not None and s.action is not None and s.action.guid==card.guid]
        if len(steps)!=1 or len(path.actions)!=1 or steps[0].queued_use_pool_commands:
            continue
        step=steps[0]
        if not step.card_transition.supported or not step.card_transition.legal:
            continue
        if selected_card_guids is not None and step.action.selected_card_guids!=tuple(selected_card_guids):
            continue
        matches.append((path,step))
    if len(matches)!=1:
        gaps=sorted({gap for diagnostic in result.diagnostics for gap in diagnostic.semantic_gaps})
        raise ValueError("Plan3 force drink needs one exact selected-GUID branch: "+str(len(matches))+":"+",".join(gaps))
    path,step=matches[0]
    if any(c.guid==card.guid for c in path.native_state.all_cards):
        raise ValueError("detached force drink card leaked into ordinary zones")
    if step.card_transition.stamina_paid or step.card_transition.force_stamina_paid:
        raise ValueError("cost-free force drink paid a card cost")
    return Plan3DrinkForcedPlay(path.state,path.native_state,card,selected.trace,command,step)
